#!/usr/bin/env python3
"""
Sportsbook-to-Polymarket esports arbitrage bot.

The strategy:
  1. Read decimal sportsbook odds for two-outcome matches.
  2. Remove vig with proportional normalization to get fair probabilities.
  3. Quote Polymarket US market sides only below fair value by a configured edge.
  4. When one leg fills, chase the opposite side only at a price that keeps
     the complete set under $1 by the configured locked edge.

Default mode is dry-run and emits order intents. Pass --live only after testing
with tiny size and a private fill stream/state reconciliation process.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any


POLYMARKET_US_GATEWAY_URL = "https://gateway.polymarket.us"
POLYMARKET_US_MARKETS_URL = f"{POLYMARKET_US_GATEWAY_URL}/v1/markets"
STATE_PATH = Path(__file__).with_suffix(".state.json")

DEFAULT_MIN_EDGE = Decimal("0.07")
DEFAULT_MIN_LOCKED_EDGE = Decimal("0.07")
DEFAULT_MIN_SPREAD = Decimal("0.05")
DEFAULT_QUOTE_DOLLARS = Decimal("25")
DEFAULT_MAX_SHARES = Decimal("100")
DEFAULT_TICK_SIZE = Decimal("0.01")
DEFAULT_REFRESH_SECONDS = 2
DEFAULT_MAX_ODDS_AGE_SECONDS = 15 * 60
DEFAULT_MAX_ORDER_AGE_SECONDS = Decimal("5")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


@dataclass(frozen=True)
class SportsbookOdds:
    event_id: str
    bookmaker: str
    sport: str
    league: str
    team_a: str
    team_b: str
    odds_a: Decimal
    odds_b: Decimal
    starts_at: str | None
    observed_at: datetime | None
    market_slug: str | None


@dataclass(frozen=True)
class FairMarket:
    event_id: str
    team_a: str
    team_b: str
    probabilities: dict[str, Decimal]
    books: int
    overround: Decimal
    market_slug: str | None


@dataclass(frozen=True)
class MarketOutcome:
    name: str
    intent: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketMapping:
    event_id: str
    market_slug: str
    outcomes: tuple[MarketOutcome, MarketOutcome]


@dataclass(frozen=True)
class OrderBook:
    intent: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class Fill:
    event_id: str
    market_slug: str | None
    outcome: str
    intent: str | None
    price: Decimal
    quantity: Decimal
    fill_id: str | None
    filled_at: datetime | None
    hedged_quantity: Decimal = Decimal("0")


@dataclass(frozen=True)
class OrderIntent:
    kind: str
    event_id: str
    market_slug: str
    outcome: str
    intent: str
    price: Decimal
    quantity: Decimal
    fair_price: Decimal | None
    edge: Decimal | None
    reason: str
    source_fill_id: str | None = None


class ArbitrageBotError(RuntimeError):
    pass


class PolymarketUSExecutor:
    def __init__(self, live: bool) -> None:
        self.live = live
        self.client = None
        if live:
            self._init_client()

    def _init_client(self) -> None:
        try:
            from polymarket_us import PolymarketUS  # type: ignore
        except ImportError as exc:
            raise ArbitrageBotError(
                "Live mode requires the Polymarket US SDK: "
                "`python3 -m pip install polymarket-us`."
            ) from exc

        key_id = env_first("POLYMARKET_KEY_ID", "POLYMARKET_ACCESS_KEY_ID")
        secret_key = env_first("POLYMARKET_SECRET_KEY", "POLYMARKET_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise ArbitrageBotError("Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY.")

        self.client = PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=float(os.getenv("POLYMARKET_TIMEOUT", "30")),
        )

    def create_limit_buy(self, intent: OrderIntent) -> str | None:
        payload = order_intent_to_json(intent)
        if not self.live:
            logging.info("DRY RUN create order: %s", json.dumps(payload, sort_keys=True))
            return None

        assert self.client is not None
        order = {
            "marketSlug": intent.market_slug,
            "intent": intent.intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format_decimal(intent.price), "currency": "USD"},
            "quantity": float(intent.quantity),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        response = self.client.orders.create(order)
        order_id = extract_order_id(response)
        logging.info("Placed %s order %s: %s", intent.kind, order_id or "<unknown>", response)
        return order_id

    def cancel_order(self, order_id: str, market_slug: str) -> bool:
        if not order_id:
            return False
        if not self.live:
            logging.info("DRY RUN cancel order %s in %s", order_id, market_slug)
            return True

        assert self.client is not None
        try:
            self.client.orders.cancel(order_id, {"marketSlug": market_slug})
            logging.info("Cancelled order %s in %s", order_id, market_slug)
            return True
        except Exception as exc:  # noqa: BLE001 - SDK exception types may vary.
            logging.warning("Could not cancel order %s in %s: %s", order_id, market_slug, exc)
            return False

    def list_open_orders(self, market_slugs: list[str]) -> list[dict[str, Any]]:
        if not self.live or not market_slugs:
            return []
        assert self.client is not None
        response = self.client.orders.list({"slugs": market_slugs})
        return normalize_response_list(response, "orders")

    def get_position(self, market_slug: str) -> dict[str, Any] | None:
        if not self.live or not market_slug:
            return None
        assert self.client is not None
        response = self.client.portfolio.positions({"market": market_slug, "limit": 50})
        positions = normalize_response_mapping(response, "positions")
        if market_slug in positions and isinstance(positions[market_slug], dict):
            return positions[market_slug]
        for position in positions.values():
            if not isinstance(position, dict):
                continue
            metadata = position.get("marketMetadata") or {}
            if isinstance(metadata, dict) and metadata.get("slug") == market_slug:
                return position
        return None

    def close(self) -> None:
        if self.client is not None and hasattr(self.client, "close"):
            self.client.close()


def main() -> int:
    load_env_file(Path.cwd() / ".env")
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    executor = PolymarketUSExecutor(args.live)
    state_path = Path(args.state)
    state = load_state(state_path)
    try:
        while True:
            if args.live and args.reconcile_live:
                reconcile_live_state(args, executor, state, state_path)
            cancel_stale_orders(args, executor, state, state_path)
            intents = run_cycle(args, state)
            execute_intents(args, executor, state, state_path, intents)
            if args.once:
                return 0
            time.sleep(args.refresh_seconds)
    finally:
        executor.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sportsbook-to-Polymarket esports arbitrage bot")
    parser.add_argument("--odds-file", required=True, help="CSV, JSON, or JSONL decimal sportsbook odds")
    parser.add_argument("--markets-file", help="optional JSON market/outcome mapping file")
    parser.add_argument("--fills-file", help="optional CSV, JSON, or JSONL fill export for hedge generation")
    parser.add_argument("--write-orders", help="write order intents as JSONL")
    parser.add_argument("--state", default=os.getenv("ARBITRAGE_STATE", str(STATE_PATH)))
    parser.add_argument("--live", action="store_true", help="place real Polymarket US orders")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--json", action="store_true", help="print JSON order intents")
    parser.add_argument("--refresh-seconds", type=float, default=DEFAULT_REFRESH_SECONDS)
    parser.add_argument("--min-edge", type=Decimal, default=DEFAULT_MIN_EDGE)
    parser.add_argument("--min-locked-edge", type=Decimal, default=DEFAULT_MIN_LOCKED_EDGE)
    parser.add_argument("--min-spread", type=Decimal, default=DEFAULT_MIN_SPREAD)
    parser.add_argument("--quote-dollars", type=Decimal, default=DEFAULT_QUOTE_DOLLARS)
    parser.add_argument("--max-shares", type=Decimal, default=DEFAULT_MAX_SHARES)
    parser.add_argument("--tick-size", type=Decimal, default=DEFAULT_TICK_SIZE)
    parser.add_argument("--max-odds-age-seconds", type=int, default=DEFAULT_MAX_ODDS_AGE_SECONDS)
    parser.add_argument("--max-order-age-seconds", type=Decimal, default=DEFAULT_MAX_ORDER_AGE_SECONDS)
    parser.add_argument("--max-orders-per-cycle", type=int, default=10)
    parser.add_argument(
        "--reconcile-live",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="before each live cycle, reconcile tracked orders against open orders and positions",
    )
    parser.add_argument(
        "--hedge-cross",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="allow hedge limit to cross the current ask when it still locks profit",
    )
    parser.add_argument(
        "--fetch-books",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="fetch live Polymarket US books; disable for offline tests",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("ARBITRAGE_LOG_LEVEL", "INFO").upper(),
    )
    return parser.parse_args()


def run_cycle(args: argparse.Namespace, state: dict[str, Any]) -> list[OrderIntent]:
    odds_rows = load_odds(Path(args.odds_file))
    fair_markets = build_fair_markets(odds_rows, max_age_seconds=args.max_odds_age_seconds)
    mappings = load_market_mappings(Path(args.markets_file) if args.markets_file else None, fair_markets)
    fills = load_fills(Path(args.fills_file)) if args.fills_file else []
    fills.extend(load_state_fills(state))

    books = fetch_market_books(mappings) if args.fetch_books else load_books_from_mappings(mappings)

    intents: list[OrderIntent] = []
    for fair in fair_markets.values():
        mapping = mappings.get(fair.event_id)
        if mapping is None:
            logging.info("Skip %s: no Polymarket mapping", fair.event_id)
            continue
        intents.extend(build_entry_intents(fair, mapping, books, args, state))
        intents.extend(build_hedge_intents(fair, mapping, books, fills, args, state))

    intents.sort(key=lambda intent: (intent.kind != "hedge", -(intent.edge or Decimal("0"))))
    selected = intents[: max(args.max_orders_per_cycle, 0)]
    emit_intents(selected, args)
    logging.info(
        "Loaded %s odds rows, %s fair markets, %s mappings, emitted %s intents",
        len(odds_rows),
        len(fair_markets),
        len(mappings),
        len(selected),
    )
    return selected


def build_fair_markets(
    odds_rows: list[SportsbookOdds],
    max_age_seconds: int | None = None,
) -> dict[str, FairMarket]:
    by_event: dict[str, list[SportsbookOdds]] = {}
    now = datetime.now(timezone.utc)
    for row in odds_rows:
        if max_age_seconds and row.observed_at is not None:
            age = (now - row.observed_at).total_seconds()
            if age > max_age_seconds:
                logging.debug("Skip stale odds for %s from %s: %.0fs old", row.event_id, row.bookmaker, age)
                continue
        by_event.setdefault(row.event_id, []).append(row)

    fair_markets: dict[str, FairMarket] = {}
    for event_id, rows in by_event.items():
        per_team: dict[str, list[Decimal]] = {}
        overrounds: list[Decimal] = []
        market_slugs = [row.market_slug for row in rows if row.market_slug]
        for row in rows:
            fair_a, fair_b, overround = devig_two_way(row.odds_a, row.odds_b)
            per_team.setdefault(row.team_a, []).append(fair_a)
            per_team.setdefault(row.team_b, []).append(fair_b)
            overrounds.append(overround)

        if len(per_team) != 2:
            logging.warning("Skip %s: expected 2 teams after odds aggregation, got %s", event_id, len(per_team))
            continue
        probabilities = {team: median_decimal(values) for team, values in per_team.items()}
        total = sum(probabilities.values(), Decimal("0"))
        if total <= 0:
            continue
        probabilities = {team: value / total for team, value in probabilities.items()}
        teams = list(probabilities)
        fair_markets[event_id] = FairMarket(
            event_id=event_id,
            team_a=teams[0],
            team_b=teams[1],
            probabilities=probabilities,
            books=len(rows),
            overround=median_decimal(overrounds),
            market_slug=most_common(market_slugs),
        )
    return fair_markets


def devig_two_way(odds_a: Decimal, odds_b: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    if odds_a <= 1 or odds_b <= 1:
        raise ArbitrageBotError(f"Decimal odds must be greater than 1, got {odds_a}, {odds_b}")
    implied_a = Decimal("1") / odds_a
    implied_b = Decimal("1") / odds_b
    total = implied_a + implied_b
    if total <= 0:
        raise ArbitrageBotError("Implied probability total must be positive")
    return implied_a / total, implied_b / total, total - Decimal("1")


def build_entry_intents(
    fair: FairMarket,
    mapping: MarketMapping,
    books: dict[str, OrderBook],
    args: argparse.Namespace,
    state: dict[str, Any],
) -> list[OrderIntent]:
    intents = []
    pending_sides = pending_order_sides(state)
    for team, fair_price in fair.probabilities.items():
        outcome = match_outcome(team, mapping)
        if outcome is None:
            logging.warning("Skip %s %s: could not match team to outcome", fair.event_id, team)
            continue
        if order_side_key(mapping.market_slug, outcome.intent) in pending_sides:
            continue
        book = books.get(order_side_key(mapping.market_slug, outcome.intent))
        if book is None:
            logging.info("Skip %s %s: no book", fair.event_id, outcome.name)
            continue
        if book.spread is not None and book.spread < args.min_spread:
            continue

        max_price = quantize_price(fair_price - args.min_edge, args.tick_size)
        if max_price < MIN_PRICE:
            continue
        target = maker_price(book, max_price, args.tick_size)
        if target is None:
            continue
        quantity = quote_quantity(args.quote_dollars, target, args.max_shares)
        if quantity <= 0:
            continue
        intents.append(
            OrderIntent(
                kind="entry",
                event_id=fair.event_id,
                market_slug=mapping.market_slug,
                outcome=outcome.name,
                intent=outcome.intent,
                price=target,
                quantity=quantity,
                fair_price=fair_price,
                edge=fair_price - target,
                reason=f"maker quote at >= {format_decimal(args.min_edge)} fair-value edge",
            )
        )
    return intents


def build_hedge_intents(
    fair: FairMarket,
    mapping: MarketMapping,
    books: dict[str, OrderBook],
    fills: list[Fill],
    args: argparse.Namespace,
    state: dict[str, Any],
) -> list[OrderIntent]:
    intents = []
    pending_sides = pending_order_sides(state, kind="hedge")
    event_fills = [fill for fill in fills if fill.event_id == fair.event_id]
    for fill in event_fills:
        source = match_fill_outcome(fill, mapping)
        if source is None:
            logging.warning("Skip hedge %s: cannot match fill outcome %s", fill.event_id, fill.outcome)
            continue
        opposite = opposite_outcome(source, mapping)
        if opposite is None or order_side_key(mapping.market_slug, opposite.intent) in pending_sides:
            continue
        remaining_qty = quantize_quantity(fill.quantity - fill.hedged_quantity)
        if remaining_qty <= 0:
            continue

        cap = quantize_price(Decimal("1") - args.min_locked_edge - fill.price, args.tick_size)
        if cap < MIN_PRICE:
            logging.warning(
                "Cannot hedge %s %s: fill=%s leaves cap=%s",
                fill.event_id,
                fill.outcome,
                format_decimal(fill.price),
                format_decimal(cap),
            )
            continue
        book = books.get(order_side_key(mapping.market_slug, opposite.intent))
        if book is None:
            continue
        target = hedge_price(book, cap, args.tick_size, cross=args.hedge_cross)
        if target is None:
            continue
        fair_price = matched_fair_price(opposite, fair, mapping)
        intents.append(
            OrderIntent(
                kind="hedge",
                event_id=fair.event_id,
                market_slug=mapping.market_slug,
                outcome=opposite.name,
                intent=opposite.intent,
                price=target,
                quantity=remaining_qty,
                fair_price=fair_price,
                edge=Decimal("1") - fill.price - target,
                reason=(
                    f"hedge fill {fill.outcome} @ {format_decimal(fill.price)}; "
                    f"complete-set edge {format_decimal(Decimal('1') - fill.price - target)}"
                ),
                source_fill_id=fill.fill_id,
            )
        )
    return intents


def maker_price(book: OrderBook, max_price: Decimal, tick: Decimal) -> Decimal | None:
    if book.best_bid is None:
        return max_price
    target = quantize_price(book.best_bid + tick, tick)
    if book.best_ask is not None and target >= book.best_ask:
        target = quantize_price(book.best_ask - tick, tick)
    target = min(target, max_price)
    if target < MIN_PRICE:
        return None
    return target


def hedge_price(book: OrderBook, cap: Decimal, tick: Decimal, cross: bool) -> Decimal | None:
    if cross and book.best_ask is not None and book.best_ask <= cap:
        return quantize_price(book.best_ask, tick)
    return maker_price(book, cap, tick)


def quote_quantity(dollars: Decimal, price: Decimal, max_shares: Decimal) -> Decimal:
    if price <= 0:
        return Decimal("0")
    return quantize_quantity(min(max_shares, dollars / price))


def load_odds(path: Path) -> list[SportsbookOdds]:
    rows = load_records(path)
    odds = []
    for raw in rows:
        normalized = normalize_odds_row(raw)
        if normalized is not None:
            odds.append(normalized)
    return odds


def normalize_odds_row(row: dict[str, Any]) -> SportsbookOdds | None:
    event_id = text_first(row, "event_id", "eventId", "match_id", "matchId", "game_id", "id")
    team_a = text_first(row, "team_a", "teamA", "home", "home_team", "participant_a", "p1")
    team_b = text_first(row, "team_b", "teamB", "away", "away_team", "participant_b", "p2")
    odds_a = decimal_first(row, "odds_a", "oddsA", "home_odds", "price_a", "decimal_a")
    odds_b = decimal_first(row, "odds_b", "oddsB", "away_odds", "price_b", "decimal_b")
    if not event_id or not team_a or not team_b or odds_a is None or odds_b is None:
        logging.debug("Skip odds row with missing required fields: %s", row)
        return None
    return SportsbookOdds(
        event_id=event_id,
        bookmaker=text_first(row, "bookmaker", "book", "source") or "unknown",
        sport=text_first(row, "sport", "game") or "",
        league=text_first(row, "league", "competition", "tournament") or "",
        team_a=team_a,
        team_b=team_b,
        odds_a=odds_a,
        odds_b=odds_b,
        starts_at=text_first(row, "starts_at", "start_time", "commence_time"),
        observed_at=parse_datetime(text_first(row, "observed_at", "timestamp", "updated_at")),
        market_slug=text_first(row, "market_slug", "slug", "polymarket_slug"),
    )


def load_market_mappings(
    path: Path | None,
    fair_markets: dict[str, FairMarket],
) -> dict[str, MarketMapping]:
    mappings: dict[str, MarketMapping] = {}
    if path is not None:
        for record in load_records(path):
            mapping = normalize_market_mapping(record)
            if mapping is not None:
                mappings[mapping.event_id] = mapping

    missing_slug_events = [
        fair for fair in fair_markets.values() if fair.event_id not in mappings and fair.market_slug
    ]
    for fair in missing_slug_events:
        try:
            mapping = fetch_mapping_from_gateway(fair)
        except Exception as exc:  # noqa: BLE001 - public API failures vary.
            logging.warning("Could not fetch US gateway mapping for %s/%s: %s", fair.event_id, fair.market_slug, exc)
            continue
        if mapping is not None:
            mappings[mapping.event_id] = mapping
    return mappings


def normalize_market_mapping(row: dict[str, Any]) -> MarketMapping | None:
    event_id = text_first(row, "event_id", "eventId", "match_id", "matchId", "game_id", "id")
    slug = text_first(row, "market_slug", "slug", "polymarket_slug")
    outcomes_raw = row.get("outcomes")
    if not event_id or not slug:
        return None

    outcomes = parse_outcomes(outcomes_raw)
    if len(outcomes) != 2:
        logging.warning("Skip mapping %s: expected 2 outcomes with Polymarket US intents", event_id)
        return None
    return MarketMapping(event_id=event_id, market_slug=slug, outcomes=(outcomes[0], outcomes[1]))


def parse_outcomes(outcomes_raw: Any) -> list[MarketOutcome]:
    if isinstance(outcomes_raw, str):
        parsed = parse_json_maybe(outcomes_raw)
        outcomes_raw = parsed if parsed is not None else [part.strip() for part in outcomes_raw.split(",")]

    if not isinstance(outcomes_raw, list):
        return []
    outcomes: list[MarketOutcome] = []
    for index, item in enumerate(outcomes_raw):
        aliases: tuple[str, ...] = ()
        default_intent = "ORDER_INTENT_BUY_LONG" if index == 0 else "ORDER_INTENT_BUY_SHORT"
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("outcome") or item.get("title") or "")
            intent = side_intent(item, default_intent)
            raw_aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            aliases = tuple(str(alias) for alias in raw_aliases)
        else:
            name = str(item)
            intent = default_intent
        if name and intent:
            outcomes.append(MarketOutcome(name=name, intent=intent, aliases=aliases))
    return outcomes


def side_intent(item: dict[str, Any], default: str) -> str:
    explicit = item.get("intent")
    if explicit:
        return str(explicit)
    side = str(item.get("side") or "").strip().casefold()
    if side in {"long", "yes"}:
        return "ORDER_INTENT_BUY_LONG"
    if side in {"short", "no"}:
        return "ORDER_INTENT_BUY_SHORT"
    if item.get("long") is True:
        return "ORDER_INTENT_BUY_LONG"
    if item.get("long") is False:
        return "ORDER_INTENT_BUY_SHORT"
    return default


def fetch_mapping_from_gateway(fair: FairMarket) -> MarketMapping | None:
    if not fair.market_slug:
        return None
    market = fetch_market_by_slug(fair.market_slug)
    if market is None:
        return None
    sides = [side for side in market.get("marketSides") or [] if isinstance(side, dict)]
    outcomes: list[dict[str, Any]] = []
    for side in sides:
        name = market_side_name(side)
        if not name:
            continue
        outcomes.append(
            {
                "name": name,
                "intent": "ORDER_INTENT_BUY_LONG"
                if side.get("long", True)
                else "ORDER_INTENT_BUY_SHORT",
                "aliases": market_side_aliases(side),
            }
        )
    if len(outcomes) != 2:
        return None
    return normalize_market_mapping(
        {
            "event_id": fair.event_id,
            "market_slug": market.get("slug") or fair.market_slug,
            "outcomes": outcomes,
        }
    )


def fetch_market_by_slug(slug: str) -> dict[str, Any] | None:
    try:
        payload = http_get_json(f"{POLYMARKET_US_MARKETS_URL}/{slug}")
    except ArbitrageBotError:
        payload = None
    if isinstance(payload, dict):
        market = payload.get("market")
        if isinstance(market, dict):
            return market
        if payload.get("slug") == slug or payload.get("marketSides"):
            return payload

    search_payload = http_get_json(POLYMARKET_US_MARKETS_URL, {"slug": slug, "limit": 1})
    markets = unpack_markets(search_payload)
    return markets[0] if markets else None


def unpack_markets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        markets = payload.get("markets") or payload.get("data") or []
        return [item for item in markets if isinstance(item, dict)] if isinstance(markets, list) else []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def market_side_name(side: dict[str, Any]) -> str | None:
    team = side.get("team") if isinstance(side.get("team"), dict) else {}
    value = (
        side.get("description")
        or side.get("identifier")
        or team.get("safeName")
        or team.get("name")
        or team.get("alias")
        or team.get("abbreviation")
    )
    return str(value).strip() if value else None


def market_side_aliases(side: dict[str, Any]) -> list[str]:
    team = side.get("team") if isinstance(side.get("team"), dict) else {}
    aliases = [
        side.get("description"),
        side.get("identifier"),
        team.get("safeName"),
        team.get("name"),
        team.get("alias"),
        team.get("abbreviation"),
        team.get("displayAbbreviation"),
    ]
    return [str(alias) for alias in aliases if alias]


def load_fills(path: Path) -> list[Fill]:
    fills = []
    for row in load_records(path):
        fill = normalize_fill(row)
        if fill is not None:
            fills.append(fill)
    return fills


def normalize_fill(row: dict[str, Any]) -> Fill | None:
    event_id = text_first(row, "event_id", "eventId", "match_id", "matchId", "game_id")
    outcome = text_first(row, "outcome", "team", "assetOutcome", "position")
    price = decimal_first(row, "price", "fill_price", "avgPrice", "averagePrice")
    quantity = decimal_first(row, "quantity", "size", "shares", "filled_size")
    if not event_id or not outcome or price is None or quantity is None:
        return None
    return Fill(
        event_id=event_id,
        market_slug=text_first(row, "market_slug", "slug", "polymarket_slug"),
        outcome=outcome,
        intent=text_first(row, "intent", "order_intent", "orderIntent"),
        price=price,
        quantity=quantity,
        fill_id=text_first(row, "fill_id", "trade_id", "tx", "txHash", "id"),
        filled_at=parse_datetime(text_first(row, "filled_at", "timestamp", "created_at")),
        hedged_quantity=decimal_first(row, "hedged_quantity", "hedgedQuantity") or Decimal("0"),
    )


def load_state_fills(state: dict[str, Any]) -> list[Fill]:
    fills = []
    for row in state.get("fills", []):
        if isinstance(row, dict):
            fill = normalize_fill(row)
            if fill is not None:
                fills.append(fill)
    return fills


def fetch_market_books(mappings: dict[str, MarketMapping]) -> dict[str, OrderBook]:
    books = {}
    for mapping in mappings.values():
        try:
            books.update(fetch_market_book(mapping.market_slug))
        except Exception as exc:  # noqa: BLE001 - public API failures vary.
            logging.warning("Could not fetch book for market %s: %s", mapping.market_slug, exc)
    return books


def fetch_market_book(market_slug: str) -> dict[str, OrderBook]:
    payload = http_get_json(f"{POLYMARKET_US_MARKETS_URL}/{market_slug}/book")
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    bids = data.get("bids") if isinstance(data.get("bids"), list) else []
    asks = data.get("offers") if isinstance(data.get("offers"), list) else []
    bid_prices = [
        price
        for item in bids
        if isinstance(item, dict)
        for price in [amount_value(item.get("px")) or decimal_first(item, "price")]
        if price is not None
    ]
    ask_prices = [
        price
        for item in asks
        if isinstance(item, dict)
        for price in [amount_value(item.get("px")) or decimal_first(item, "price")]
        if price is not None
    ]
    long_bid = max(bid_prices, default=None)
    long_ask = min(ask_prices, default=None)
    long_book = OrderBook(
        intent="ORDER_INTENT_BUY_LONG",
        best_bid=long_bid,
        best_ask=long_ask,
        bid_size=size_at_price(bids, long_bid),
        ask_size=size_at_price(asks, long_ask),
    )
    short_book = OrderBook(
        intent="ORDER_INTENT_BUY_SHORT",
        best_bid=(Decimal("1") - long_ask) if long_ask is not None else None,
        best_ask=(Decimal("1") - long_bid) if long_bid is not None else None,
        bid_size=size_at_price(asks, long_ask),
        ask_size=size_at_price(bids, long_bid),
    )
    return {
        order_side_key(market_slug, long_book.intent): long_book,
        order_side_key(market_slug, short_book.intent): short_book,
    }


def load_books_from_mappings(mappings: dict[str, MarketMapping]) -> dict[str, OrderBook]:
    books = {}
    for mapping in mappings.values():
        for outcome in mapping.outcomes:
            books[order_side_key(mapping.market_slug, outcome.intent)] = OrderBook(outcome.intent, None, None)
    return books


def size_at_price(rows: list[Any], price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_price = amount_value(row.get("px")) or decimal_first(row, "price")
        if row_price == price:
            return decimal_first(row, "size", "qty", "quantity")
    return None


def match_outcome(team: str, mapping: MarketMapping) -> MarketOutcome | None:
    team_key = normalize_name(team)
    exact = [
        outcome
        for outcome in mapping.outcomes
        if team_key in {normalize_name(outcome.name), *(normalize_name(alias) for alias in outcome.aliases)}
    ]
    if len(exact) == 1:
        return exact[0]

    scored = []
    for outcome in mapping.outcomes:
        keys = [normalize_name(outcome.name), *(normalize_name(alias) for alias in outcome.aliases)]
        score = max(name_similarity(team_key, key) for key in keys)
        scored.append((score, outcome))
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) >= 2 and scored[0][0] >= Decimal("0.72") and scored[0][0] > scored[1][0]:
        return scored[0][1]
    return None


def match_fill_outcome(fill: Fill, mapping: MarketMapping) -> MarketOutcome | None:
    if fill.intent:
        for outcome in mapping.outcomes:
            if outcome.intent == fill.intent:
                return outcome
    return match_outcome(fill.outcome, mapping)


def opposite_outcome(source: MarketOutcome, mapping: MarketMapping) -> MarketOutcome | None:
    for outcome in mapping.outcomes:
        if outcome.intent != source.intent:
            return outcome
    return None


def matched_fair_price(outcome: MarketOutcome, fair: FairMarket, mapping: MarketMapping) -> Decimal | None:
    for team, probability in fair.probabilities.items():
        matched = match_outcome(team, mapping)
        if matched and matched.intent == outcome.intent:
            return probability
    return None


def pending_order_sides(state: dict[str, Any], kind: str | None = None) -> set[str]:
    sides = set()
    for order in state.get("orders", []):
        if not isinstance(order, dict):
            continue
        if kind is not None and order.get("kind") != kind:
            continue
        status = str(order.get("status") or "pending").casefold()
        if status in {"cancelled", "filled", "rejected", "dry_run"}:
            continue
        market_slug = order.get("market_slug")
        intent = order.get("intent")
        if market_slug and intent:
            sides.add(order_side_key(str(market_slug), str(intent)))
    return sides


def reconcile_live_state(
    args: argparse.Namespace,
    executor: PolymarketUSExecutor,
    state: dict[str, Any],
    state_path: Path,
) -> None:
    pending = [
        order
        for order in state.get("orders", [])
        if isinstance(order, dict) and str(order.get("status") or "").casefold() == "pending"
    ]
    if not pending:
        return

    market_slugs = sorted({str(order.get("market_slug") or "") for order in pending if order.get("market_slug")})
    if not market_slugs:
        return

    open_order_ids: set[str] = set()
    open_orders_ok = True
    try:
        for order in executor.list_open_orders(market_slugs):
            order_id = extract_order_id(order)
            if order_id:
                open_order_ids.add(order_id)
    except Exception as exc:  # noqa: BLE001 - SDK errors vary by transport/version.
        open_orders_ok = False
        logging.warning("Could not list open live orders for reconciliation: %s", exc)

    positions: dict[str, dict[str, Any]] = {}
    position_errors: set[str] = set()
    for market_slug in market_slugs:
        try:
            position = executor.get_position(market_slug)
        except Exception as exc:  # noqa: BLE001 - SDK errors vary by transport/version.
            position_errors.add(market_slug)
            logging.warning("Could not fetch live position for %s: %s", market_slug, exc)
            continue
        if isinstance(position, dict) and position_quantity(position) > 0:
            positions[market_slug] = position

    now = datetime.now(timezone.utc).isoformat()
    changed = False
    fills_added = 0
    for order in pending:
        market_slug = str(order.get("market_slug") or "")
        order_id = str(order.get("order_id") or "")
        if open_orders_ok and order_id and order_id in open_order_ids:
            changed = update_order_fields(order, {"last_reconciled_at": now}) or changed
            continue

        if market_slug in positions:
            position = positions[market_slug]
            quantity = reconciled_fill_quantity(order, position)
            updates = {
                "status": "filled",
                "filled_at": order.get("filled_at") or now,
                "filled_quantity": format_decimal(quantity),
                "last_reconciled_at": now,
            }
            changed = update_order_fields(order, updates) or changed
            if str(order.get("kind") or "") == "entry" and add_state_fill_from_order(state, order, quantity, now):
                fills_added += 1
                changed = True
            continue

        if not open_orders_ok:
            updates = {"last_reconciled_at": now, "reconcile_error": "open_order_list_failed"}
        elif market_slug in position_errors:
            updates = {"status": "needs_review", "last_reconciled_at": now}
        else:
            updates = {"status": "unfilled", "last_reconciled_at": now}
        changed = update_order_fields(order, updates) or changed

    if changed:
        logging.info(
            "Reconciled live state: pending=%s open=%s positions=%s fills_added=%s",
            len(pending),
            len(open_order_ids),
            len(positions),
            fills_added,
        )
        save_state(state_path, state)


def reconciled_fill_quantity(order: dict[str, Any], position: dict[str, Any]) -> Decimal:
    order_quantity = to_decimal(order.get("quantity")) or Decimal("0")
    position_qty = position_quantity(position)
    if order_quantity <= 0:
        return quantize_quantity(position_qty)
    if position_qty <= 0:
        return quantize_quantity(order_quantity)
    return quantize_quantity(min(order_quantity, position_qty))


def add_state_fill_from_order(
    state: dict[str, Any],
    order: dict[str, Any],
    quantity: Decimal,
    filled_at: str,
) -> bool:
    fill_id = str(order.get("order_id") or order.get("source_fill_id") or "")
    if fill_id and any(
        isinstance(fill, dict) and str(fill.get("fill_id") or "") == fill_id
        for fill in state.get("fills", [])
    ):
        return False
    fill = {
        "event_id": order.get("event_id"),
        "market_slug": order.get("market_slug"),
        "outcome": order.get("outcome"),
        "intent": order.get("intent"),
        "price": order.get("price"),
        "quantity": format_decimal(quantity),
        "fill_id": fill_id or f"reconciled-{len(state.get('fills', [])) + 1}",
        "filled_at": filled_at,
        "hedged_quantity": "0",
    }
    state.setdefault("fills", []).append(fill)
    return True


def update_order_fields(order: dict[str, Any], updates: dict[str, Any]) -> bool:
    changed = False
    for key, value in updates.items():
        if order.get(key) != value:
            order[key] = value
            changed = True
    return changed


def cancel_stale_orders(
    args: argparse.Namespace,
    executor: PolymarketUSExecutor,
    state: dict[str, Any],
    state_path: Path,
) -> None:
    orders = state.get("orders", [])
    if not orders:
        return

    now = datetime.now(timezone.utc)
    kept = []
    canceled = 0
    changed = False
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "pending").casefold()
        if status != "pending":
            kept.append(order)
            continue
        created_at = parse_datetime(order.get("created_at"))
        age = Decimal(str((now - created_at).total_seconds())) if created_at else args.max_order_age_seconds
        if age < args.max_order_age_seconds:
            kept.append(order)
            continue

        if executor.cancel_order(str(order.get("order_id") or ""), str(order.get("market_slug") or "")):
            order = {**order, "status": "cancelled", "cancelled_at": now.isoformat()}
            canceled += 1
            changed = True
        else:
            order = {**order, "status": "cancel_unknown", "cancel_checked_at": now.isoformat()}
            changed = True
        kept.append(order)

    if canceled:
        logging.info("Cancelled %s tracked orders older than %ss", canceled, args.max_order_age_seconds)
    state["orders"] = kept
    if changed:
        save_state(state_path, state)


def execute_intents(
    args: argparse.Namespace,
    executor: PolymarketUSExecutor,
    state: dict[str, Any],
    state_path: Path,
    intents: list[OrderIntent],
) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    output_handle = open(args.write_orders, "a", encoding="utf-8") if args.write_orders else None
    try:
        for intent in intents:
            order_id = executor.create_limit_buy(intent)
            record = {
                **order_intent_to_json(intent),
                "order_id": order_id,
                "status": "pending" if order_id else "dry_run",
                "created_at": created_at,
            }
            state.setdefault("orders", []).append(record)
            if output_handle:
                output_handle.write(json.dumps(record, sort_keys=True) + "\n")
    finally:
        if output_handle:
            output_handle.close()
    trim_state(state)
    save_state(state_path, state)


def emit_intents(intents: list[OrderIntent], args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps([order_intent_to_json(intent) for intent in intents], indent=2, sort_keys=True))
        return
    if not intents:
        print("No order intents.")
        return
    print(f"Order intents: {len(intents)}")
    for intent in intents:
        edge = "" if intent.edge is None else f" edge={format_decimal(intent.edge)}"
        fair = "" if intent.fair_price is None else f" fair={format_decimal(intent.fair_price)}"
        print(
            f"{intent.kind.upper()} {intent.event_id} {intent.outcome} "
            f"@ {format_decimal(intent.price)} qty={format_decimal(intent.quantity)}"
            f"{fair}{edge} slug={intent.market_slug}"
        )


def order_intent_to_json(intent: OrderIntent) -> dict[str, Any]:
    return {
        "kind": intent.kind,
        "event_id": intent.event_id,
        "market_slug": intent.market_slug,
        "outcome": intent.outcome,
        "intent": intent.intent,
        "price": format_decimal(intent.price),
        "quantity": format_decimal(intent.quantity),
        "fair_price": format_decimal(intent.fair_price) if intent.fair_price is not None else None,
        "edge": format_decimal(intent.edge) if intent.edge is not None else None,
        "reason": intent.reason,
        "source_fill_id": intent.source_fill_id,
    }


def load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    text = path.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "odds", "markets", "fills"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-esports-arb/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ArbitrageBotError(f"GET {url} failed with HTTP {exc.code}: {body[:300]}") from exc


def text_first(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def decimal_first(row: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        parsed = to_decimal(row.get(key))
        if parsed is not None:
            return parsed
    return None


def amount_value(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        return to_decimal(value.get("value"))
    return to_decimal(value)


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001 - Decimal raises several parse exceptions.
        return None
    if parsed.is_nan():
        return None
    return parsed


def parse_json_maybe(value: str) -> Any | None:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    bounded = min(max(price, MIN_PRICE), MAX_PRICE)
    if tick <= 0:
        tick = DEFAULT_TICK_SIZE
    units = (bounded / tick).to_integral_value(rounding=ROUND_DOWN)
    return (units * tick).quantize(tick, rounding=ROUND_DOWN)


def quantize_quantity(quantity: Decimal) -> Decimal:
    return max(quantity, Decimal("0")).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def median_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return Decimal(str(statistics.median(values)))


def most_common(values: list[str | None]) -> str | None:
    filtered = [value for value in values if value]
    if not filtered:
        return None
    return max(set(filtered), key=filtered.count)


def normalize_name(value: str) -> str:
    value = value.casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"\b(esports|e-sports|gaming|team|club|clan)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def name_similarity(left: str, right: str) -> Decimal:
    if not left or not right:
        return Decimal("0")
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return Decimal("0")
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return Decimal(overlap) / Decimal(union)


def order_side_key(market_slug: str, intent: str) -> str:
    return f"{market_slug}:{intent}"


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def extract_order_id(response: Any) -> str | None:
    if isinstance(response, dict):
        return response.get("id") or response.get("orderId") or response.get("orderID")
    for attr in ("id", "orderId", "orderID"):
        order_id = getattr(response, attr, None)
        if order_id:
            return str(order_id)
    return None


def normalize_response_list(response: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        value = response.get(key) or response.get("data") or response.get("items") or []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)]
    value = getattr(response, key, None) or getattr(response, "data", None)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def normalize_response_mapping(response: Any, key: str) -> dict[str, Any]:
    if isinstance(response, dict):
        value = response.get(key) or response.get("data") or response
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {
                str(item.get("market") or item.get("marketSlug") or item.get("id") or index): item
                for index, item in enumerate(value)
                if isinstance(item, dict)
            }
    value = getattr(response, key, None) or getattr(response, "data", None)
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {
            str(item.get("market") or item.get("marketSlug") or item.get("id") or index): item
            for index, item in enumerate(value)
            if isinstance(item, dict)
        }
    return {}


def position_quantity(position: dict[str, Any]) -> Decimal:
    for key in ("quantity", "qty", "size", "shares", "balance"):
        parsed = to_decimal(position.get(key))
        if parsed is not None:
            return parsed
    bought = to_decimal(position.get("qtyBought")) or Decimal("0")
    sold = to_decimal(position.get("qtySold")) or Decimal("0")
    return max(bought - sold, Decimal("0"))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"orders": [], "fills": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"orders": [], "fills": []}
    if not isinstance(state, dict):
        return {"orders": [], "fills": []}
    state.setdefault("orders", [])
    state.setdefault("fills", [])
    return state


def trim_state(state: dict[str, Any], max_orders: int = 500) -> None:
    orders = state.get("orders")
    if isinstance(orders, list) and len(orders) > max_orders:
        state["orders"] = orders[-max_orders:]


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, clean_env_value(value))


def clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main())
