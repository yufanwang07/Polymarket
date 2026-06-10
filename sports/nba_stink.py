#!/usr/bin/env python3
"""
NBA Stink Bot for Polymarket US.

Strategy:
  - Check ESPN for live NBA quarter status.
  - During Q1/Q2 only, bid on the favorite in each live NBA moneyline market.
  - Bid 30% below the current side price, about $5 notional per bid.
  - Refresh every 15 minutes by cancelling this bot's outstanding orders and
    placing fresh bids.
  - Once games are in Q3+ or not in the first half, cancel this bot's open
    orders and hold any fills through settlement.

Default mode is dry-run. Pass --live to place/cancel real orders.

.env is supported, so live trading can be configured with:
  POLYMARKET_KEY_ID="..."
  POLYMARKET_SECRET_KEY="..."
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable

import requests


ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
POLYMARKET_US_BASE_URL = "https://gateway.polymarket.us"
POLYMARKET_NBA_EVENTS_URL = f"{POLYMARKET_US_BASE_URL}/v2/leagues/nba/events"
STATE_PATH = Path(__file__).with_suffix(".state.json")

DEFAULT_REFRESH_SECONDS = 15 * 60
DEFAULT_BID_DOLLARS = Decimal("5")
DEFAULT_DISCOUNT = Decimal("0.30")
DEFAULT_TICK_SIZE = Decimal("0.01")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


TEAM_ALIASES = {
    "Atlanta Hawks": ["Atlanta", "Hawks", "ATL"],
    "Boston Celtics": ["Boston", "Celtics", "BOS"],
    "Brooklyn Nets": ["Brooklyn", "Nets", "BKN"],
    "Charlotte Hornets": ["Charlotte", "Hornets", "CHA"],
    "Chicago Bulls": ["Chicago", "Bulls", "CHI"],
    "Cleveland Cavaliers": ["Cleveland", "Cavaliers", "Cavs", "CLE"],
    "Dallas Mavericks": ["Dallas", "Mavericks", "Mavs", "DAL"],
    "Denver Nuggets": ["Denver", "Nuggets", "DEN"],
    "Detroit Pistons": ["Detroit", "Pistons", "DET"],
    "Golden State Warriors": ["Golden State", "Warriors", "GSW"],
    "Houston Rockets": ["Houston", "Rockets", "HOU"],
    "Indiana Pacers": ["Indiana", "Pacers", "IND"],
    "LA Clippers": ["LA Clippers", "Los Angeles Clippers", "Clippers", "LAC"],
    "Los Angeles Clippers": ["LA Clippers", "Los Angeles Clippers", "Clippers", "LAC"],
    "Los Angeles Lakers": ["Los Angeles Lakers", "LA Lakers", "Lakers", "LAL"],
    "Memphis Grizzlies": ["Memphis", "Grizzlies", "MEM"],
    "Miami Heat": ["Miami", "Heat", "MIA"],
    "Milwaukee Bucks": ["Milwaukee", "Bucks", "MIL"],
    "Minnesota Timberwolves": ["Minnesota", "Timberwolves", "Wolves", "MIN"],
    "New Orleans Pelicans": ["New Orleans", "Pelicans", "NOP"],
    "New York Knicks": ["New York", "Knicks", "NYK"],
    "Oklahoma City Thunder": ["Oklahoma City", "OKC", "Thunder"],
    "Orlando Magic": ["Orlando", "Magic", "ORL"],
    "Philadelphia 76ers": ["Philadelphia", "76ers", "Sixers", "PHI"],
    "Phoenix Suns": ["Phoenix", "Suns", "PHX"],
    "Portland Trail Blazers": ["Portland", "Trail Blazers", "Blazers", "POR"],
    "Sacramento Kings": ["Sacramento", "Kings", "SAC"],
    "San Antonio Spurs": ["San Antonio", "Spurs", "SAS"],
    "Toronto Raptors": ["Toronto", "Raptors", "TOR"],
    "Utah Jazz": ["Utah", "Jazz", "UTA"],
    "Washington Wizards": ["Washington", "Wizards", "WAS"],
}


@dataclass(frozen=True)
class ESPNTeam:
    name: str
    abbreviation: str
    short_name: str

    @property
    def aliases(self) -> list[str]:
        names = [self.name, self.short_name, self.abbreviation]
        names.extend(TEAM_ALIASES.get(self.name, []))
        return sorted({name for name in names if name}, key=len, reverse=True)


@dataclass(frozen=True)
class ESPNGame:
    espn_id: str
    name: str
    status_name: str
    period: int
    start_time: datetime | None
    home: ESPNTeam
    away: ESPNTeam

    @property
    def is_first_half(self) -> bool:
        return self.status_name == "STATUS_IN_PROGRESS" and self.period in {1, 2}


@dataclass(frozen=True)
class OutcomeChoice:
    team: ESPNTeam
    market_slug: str
    market_id: str
    question: str
    price: Decimal
    intent: str
    tick_size: Decimal
    minimum_trade_qty: Decimal


@dataclass(frozen=True)
class TargetBid:
    game: ESPNGame
    market_slug: str
    market_id: str
    question: str
    team: ESPNTeam
    intent: str
    market_price: Decimal
    bid_price: Decimal
    quantity: Decimal


class StinkBotError(RuntimeError):
    pass


class PolymarketUSTrader:
    def __init__(self, live: bool) -> None:
        self.live = live
        self.client = None
        if live:
            self._init_live_client()

    def _init_live_client(self) -> None:
        try:
            from polymarket_us import PolymarketUS  # type: ignore
        except ImportError as exc:
            raise StinkBotError(
                "Live mode requires the Polymarket US SDK. Install it with "
                "`python3 -m pip install polymarket-us`."
            ) from exc

        key_id = env_first("POLYMARKET_KEY_ID", "POLYMARKET_ACCESS_KEY_ID")
        secret_key = env_first("POLYMARKET_SECRET_KEY", "POLYMARKET_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise StinkBotError(
                "Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY before running --live."
            )

        self.client = PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=float(os.getenv("POLYMARKET_TIMEOUT", "30")),
        )

    def place_limit_buy(self, target: TargetBid) -> str | None:
        order_params = {
            "marketSlug": target.market_slug,
            "intent": target.intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format_decimal(target.bid_price), "currency": "USD"},
            "quantity": float(target.quantity),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        if not self.live:
            logging.info("DRY RUN create order: %s", json.dumps(order_params, sort_keys=True))
            return None

        assert self.client is not None
        order = self.client.orders.create(order_params)
        order_id = extract_order_id(order)
        logging.info("Placed order %s: %s", order_id or "<unknown>", order)
        return order_id

    def cancel_order(self, order_id: str, market_slug: str) -> None:
        if not order_id:
            return
        if not self.live:
            logging.info("DRY RUN cancel order %s in %s", order_id, market_slug)
            return

        assert self.client is not None
        try:
            self.client.orders.cancel(order_id, {"marketSlug": market_slug})
            logging.info("Cancelled order %s in %s", order_id, market_slug)
        except Exception as exc:  # noqa: BLE001 - SDK exception types may vary.
            logging.warning("Could not cancel order %s in %s: %s", order_id, market_slug, exc)

    def cancel_all_account_orders(self) -> None:
        if not self.live:
            logging.info("DRY RUN cancel all account orders")
            return

        assert self.client is not None
        self.client.orders.cancel_all()
        logging.warning("Cancelled all open account orders.")

    def close(self) -> None:
        if self.client is not None and hasattr(self.client, "close"):
            self.client.close()


def main() -> int:
    load_env_file()
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    trader = PolymarketUSTrader(live=args.live)
    state_path = Path(args.state)

    logging.info(
        "Starting NBA stink bot in %s mode. Refresh=%ss, bid=$%s, discount=%s%%",
        "LIVE" if args.live else "DRY-RUN",
        args.refresh_seconds,
        format_decimal(args.bid_dollars),
        format_decimal(args.discount * Decimal("100")),
    )

    try:
        while True:
            try:
                run_cycle(args, trader, state_path)
            except Exception:
                logging.exception("Cycle failed")
                if args.once:
                    return 1

            if args.once:
                return 0
            time.sleep(args.refresh_seconds)
    finally:
        trader.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket US NBA first-half lowball bot")
    parser.add_argument("--live", action="store_true", help="place and cancel real orders")
    parser.add_argument("--once", action="store_true", help="run one refresh cycle and exit")
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=int(os.getenv("NBA_STINK_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)),
    )
    parser.add_argument(
        "--bid-dollars",
        type=Decimal,
        default=Decimal(os.getenv("NBA_STINK_BID_DOLLARS", str(DEFAULT_BID_DOLLARS))),
        help="USD notional per bid",
    )
    parser.add_argument(
        "--discount",
        type=Decimal,
        default=Decimal(os.getenv("NBA_STINK_DISCOUNT", str(DEFAULT_DISCOUNT))),
        help="fraction below market price, e.g. 0.30 for 30%% below",
    )
    parser.add_argument(
        "--min-market-price",
        type=Decimal,
        default=Decimal(os.getenv("NBA_STINK_MIN_MARKET_PRICE", "0.05")),
        help="skip favorites below this current market price",
    )
    parser.add_argument(
        "--state",
        default=os.getenv("NBA_STINK_STATE", str(STATE_PATH)),
        help="JSON state file for tracked order IDs",
    )
    parser.add_argument(
        "--cancel-all-account-orders",
        action="store_true",
        default=env_bool("NBA_STINK_CANCEL_ALL_ACCOUNT_ORDERS"),
        help="dangerous: cancel all account orders when no Q1/Q2 games are live",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("NBA_STINK_LOG_LEVEL", "INFO").upper(),
    )
    return parser.parse_args()


def run_cycle(args: argparse.Namespace, trader: PolymarketUSTrader, state_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    logging.info("Refresh cycle at %s", now)

    games = fetch_espn_games()
    first_half_games = [game for game in games if game.is_first_half]
    logging.info(
        "ESPN live NBA games: %s first-half, %s total",
        len(first_half_games),
        len(games),
    )

    state = load_state(state_path)
    if state.get("orders"):
        logging.info("Cancelling %s tracked stale orders before refresh", len(state["orders"]))
        cancel_tracked_orders(trader, state)
        save_state(state_path, state)

    if not first_half_games:
        logging.info("No Q1/Q2 NBA games are live. Nothing to bid.")
        if args.cancel_all_account_orders:
            trader.cancel_all_account_orders()
        return

    polymarket_events = fetch_polymarket_nba_events()
    placed_orders = []

    for game in first_half_games:
        target = build_target_bid(
            game=game,
            events=polymarket_events,
            bid_dollars=args.bid_dollars,
            discount=args.discount,
            min_market_price=args.min_market_price,
        )
        if target is None:
            logging.info("No usable Polymarket US moneyline market found for %s", game.name)
            continue

        logging.info(
            "%s favorite: %s market=%s bid=%s qty=%s slug=%s",
            game.name,
            target.team.name,
            format_decimal(target.market_price),
            format_decimal(target.bid_price),
            format_decimal(target.quantity),
            target.market_slug,
        )
        order_id = trader.place_limit_buy(target)
        if order_id:
            placed_orders.append(
                {
                    "order_id": order_id,
                    "market_slug": target.market_slug,
                    "market_id": target.market_id,
                    "team": target.team.name,
                    "game": game.name,
                    "created_at": now,
                }
            )

    state["orders"] = placed_orders
    save_state(state_path, state)
    logging.info("Cycle complete. Tracking %s live orders.", len(placed_orders))


def fetch_espn_games() -> list[ESPNGame]:
    payload = http_get_json(ESPN_SCOREBOARD_URL, params={"limit": 100})
    games = []
    for event in payload.get("events", []):
        competition = first(event.get("competitions", []))
        if not competition:
            continue

        status = competition.get("status") or event.get("status") or {}
        status_type = status.get("type") or {}
        period = int(status.get("period") or 0)
        competitors = competition.get("competitors") or []
        home = parse_espn_team(competitors, "home")
        away = parse_espn_team(competitors, "away")
        if not home or not away:
            continue

        games.append(
            ESPNGame(
                espn_id=str(event.get("id", "")),
                name=event.get("name") or f"{away.name} at {home.name}",
                status_name=status_type.get("name", ""),
                period=period,
                start_time=parse_datetime(event.get("date")),
                home=home,
                away=away,
            )
        )
    return games


def parse_espn_team(competitors: Iterable[dict[str, Any]], home_away: str) -> ESPNTeam | None:
    for competitor in competitors:
        if competitor.get("homeAway") != home_away:
            continue
        team = competitor.get("team") or {}
        return ESPNTeam(
            name=team.get("displayName") or team.get("name") or "",
            abbreviation=team.get("abbreviation") or "",
            short_name=team.get("shortDisplayName") or team.get("name") or "",
        )
    return None


def fetch_polymarket_nba_events() -> list[dict[str, Any]]:
    payload = http_get_json(POLYMARKET_NBA_EVENTS_URL)
    if isinstance(payload, dict):
        events = payload.get("events") or []
    elif isinstance(payload, list):
        events = payload
    else:
        events = []
    logging.info("Fetched %s Polymarket US NBA events", len(events))
    return events


def build_target_bid(
    game: ESPNGame,
    events: list[dict[str, Any]],
    bid_dollars: Decimal,
    discount: Decimal,
    min_market_price: Decimal,
) -> TargetBid | None:
    choices = []
    for market in find_game_moneyline_markets(game, events):
        choices.extend(market_outcome_choices(game, market))
    if not choices:
        return None

    favorite = max(choices, key=lambda choice: choice.price)
    if favorite.price < min_market_price:
        logging.info(
            "Skipping %s: favorite price %s below minimum %s",
            favorite.question,
            favorite.price,
            min_market_price,
        )
        return None

    bid_price = quantize_price(favorite.price * (Decimal("1") - discount), favorite.tick_size)
    quantity = quantize_quantity(bid_dollars / bid_price)
    if quantity < favorite.minimum_trade_qty:
        logging.info(
            "Skipping %s: $%s at %s is %s qty, below minimum %s",
            favorite.question,
            format_decimal(bid_dollars),
            format_decimal(bid_price),
            format_decimal(quantity),
            format_decimal(favorite.minimum_trade_qty),
        )
        return None

    return TargetBid(
        game=game,
        market_slug=favorite.market_slug,
        market_id=favorite.market_id,
        question=favorite.question,
        team=favorite.team,
        intent=favorite.intent,
        market_price=favorite.price,
        bid_price=bid_price,
        quantity=quantity,
    )


def find_game_moneyline_markets(
    game: ESPNGame,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matches = []
    for event in events:
        event_text = searchable_text(event)
        if not text_mentions_team(event_text, game.home) or not text_mentions_team(
            event_text, game.away
        ):
            continue

        for market in event.get("markets") or []:
            market_text = searchable_text(market)
            combined_text = f"{event_text} {market_text}"
            if not text_mentions_team(combined_text, game.home):
                continue
            if not text_mentions_team(combined_text, game.away):
                continue
            if not is_moneyline_market(market):
                continue
            if not is_tradeable_market(market):
                continue
            if not market_matches_game_time(game, event, market):
                continue
            matches.append(market)

    return dedupe_markets(matches)


def market_outcome_choices(game: ESPNGame, market: dict[str, Any]) -> list[OutcomeChoice]:
    choices = choices_from_market_sides(game, market)
    if choices:
        return choices
    return choices_from_legacy_outcomes(game, market)


def choices_from_market_sides(game: ESPNGame, market: dict[str, Any]) -> list[OutcomeChoice]:
    output = []
    for side in market.get("marketSides") or []:
        team = team_from_market_side(side, game)
        price = to_decimal(side.get("price"))
        if team is None or price is None:
            continue
        output.append(
            OutcomeChoice(
                team=team,
                market_slug=str(market.get("slug") or ""),
                market_id=str(market.get("id") or ""),
                question=str(market.get("question") or market.get("title") or ""),
                price=price,
                intent="ORDER_INTENT_BUY_LONG"
                if side.get("long", True)
                else "ORDER_INTENT_BUY_SHORT",
                tick_size=market_tick_size(market),
                minimum_trade_qty=market_minimum_trade_qty(market),
            )
        )
    return output


def choices_from_legacy_outcomes(game: ESPNGame, market: dict[str, Any]) -> list[OutcomeChoice]:
    outcomes = parse_json_list(market.get("outcomes"))
    prices = [to_decimal(price) for price in parse_json_list(market.get("outcomePrices"))]
    if len(outcomes) != len(prices):
        return []

    question = str(market.get("question") or market.get("title") or "")
    output = []
    for outcome, price in zip(outcomes, prices):
        team = team_for_outcome(outcome, question, game)
        if team is None or price is None:
            continue
        output.append(
            OutcomeChoice(
                team=team,
                market_slug=str(market.get("slug") or ""),
                market_id=str(market.get("id") or ""),
                question=question,
                price=price,
                intent="ORDER_INTENT_BUY_LONG",
                tick_size=market_tick_size(market),
                minimum_trade_qty=market_minimum_trade_qty(market),
            )
        )
    return output


def team_from_market_side(side: dict[str, Any], game: ESPNGame) -> ESPNTeam | None:
    team = side.get("team") or {}
    names = [
        side.get("description"),
        side.get("identifier"),
        team.get("name"),
        team.get("abbreviation"),
        team.get("alias"),
        team.get("safeName"),
        team.get("displayAbbreviation"),
    ]
    text = " ".join(str(name) for name in names if name)
    if text_mentions_team(text, game.home):
        return game.home
    if text_mentions_team(text, game.away):
        return game.away
    return None


def team_for_outcome(outcome: Any, question: str, game: ESPNGame) -> ESPNTeam | None:
    outcome_text = str(outcome)
    if text_mentions_team(outcome_text, game.home):
        return game.home
    if text_mentions_team(outcome_text, game.away):
        return game.away

    normalized = outcome_text.strip().lower()
    if normalized == "yes":
        if text_mentions_team(question, game.home) and not text_mentions_team(question, game.away):
            return game.home
        if text_mentions_team(question, game.away) and not text_mentions_team(question, game.home):
            return game.away
    if normalized == "no":
        if text_mentions_team(question, game.home) and not text_mentions_team(question, game.away):
            return game.away
        if text_mentions_team(question, game.away) and not text_mentions_team(question, game.home):
            return game.home
    return None


def is_tradeable_market(market: dict[str, Any]) -> bool:
    if market.get("closed") is True or market.get("active") is False:
        return False
    if market.get("archived") is True or market.get("hidden") is True:
        return False
    if not market.get("slug"):
        return False
    return bool(market.get("marketSides") or parse_json_list(market.get("outcomes")))


def is_moneyline_market(market: dict[str, Any]) -> bool:
    sports_type = str(market.get("sportsMarketType") or "").upper()
    if sports_type == "MONEYLINE":
        return True
    if sports_type and sports_type != "MONEYLINE":
        return False

    text = searchable_text(market).lower()
    reject_terms = ("spread", "total", "over", "under", "points", "margin", "quarter", "half")
    if any(term in text for term in reject_terms):
        return False
    return any(term in f" {text} " for term in (" win", " winner", " moneyline", "beat"))


def market_matches_game_time(
    game: ESPNGame,
    event: dict[str, Any],
    market: dict[str, Any],
) -> bool:
    if game.start_time is None:
        return True

    candidates = [
        market.get("gameStartTime"),
        market.get("startDate"),
        market.get("endDate"),
        event.get("startTime"),
        event.get("startDate"),
        event.get("eventDate"),
    ]
    pm_times = [parsed for value in candidates if (parsed := parse_datetime(value))]
    if not pm_times:
        return bool(event.get("live") or (event.get("eventState") or {}).get("live"))

    closest = min(pm_times, key=lambda value: abs(value - game.start_time))
    return abs(closest - game.start_time).total_seconds() <= 18 * 60 * 60


def market_tick_size(market: dict[str, Any]) -> Decimal:
    return (
        to_decimal(market.get("orderPriceMinTickSize"))
        or to_decimal(market.get("minimumTickSize"))
        or to_decimal(market.get("tickSize"))
        or DEFAULT_TICK_SIZE
    )


def market_minimum_trade_qty(market: dict[str, Any]) -> Decimal:
    return to_decimal(market.get("minimumTradeQty")) or Decimal("0")


def text_mentions_team(text: str, team: ESPNTeam) -> bool:
    normalized = normalize_text(text)
    for alias in team.aliases:
        alias_norm = normalize_text(alias)
        if not alias_norm:
            continue
        if re.search(rf"(^|\W){re.escape(alias_norm)}($|\W)", normalized):
            return True
    return False


def searchable_text(obj: dict[str, Any]) -> str:
    fields = [
        obj.get("title"),
        obj.get("subtitle"),
        obj.get("question"),
        obj.get("slug"),
        obj.get("description"),
        obj.get("ticker"),
        obj.get("period"),
    ]
    for key in ("tags", "markets", "teams", "participants"):
        value = obj.get(key)
        if isinstance(value, list):
            fields.extend(searchable_text(item) if isinstance(item, dict) else str(item) for item in value)
    return " ".join(str(field) for field in fields if field)


def dedupe_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for market in markets:
        key = market.get("id") or market.get("slug")
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(market)
    return output


def cancel_tracked_orders(trader: PolymarketUSTrader, state: dict[str, Any]) -> None:
    for order in list(state.get("orders", [])):
        trader.cancel_order(
            str(order.get("order_id") or ""),
            str(order.get("market_slug") or ""),
        )
    state["orders"] = []


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"orders": []}
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read state file %s; starting with empty state", path)
        return {"orders": []}
    if not isinstance(state, dict):
        return {"orders": []}
    state.setdefault("orders", [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(
        url,
        params=params,
        timeout=15,
        headers={"User-Agent": "nba-stink-bot/1.0"},
    )
    response.raise_for_status()
    return response.json()


def parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        decimal = Decimal(str(value))
    except Exception:  # noqa: BLE001 - Decimal raises several parse exceptions.
        return None
    if decimal.is_nan():
        return None
    return decimal


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


def quantize_price(price: Decimal, tick_size: Decimal) -> Decimal:
    bounded = min(max(price, MIN_PRICE), MAX_PRICE)
    ticks = (bounded / tick_size).to_integral_value(rounding=ROUND_DOWN)
    return max(ticks * tick_size, MIN_PRICE).quantize(tick_size)


def quantize_quantity(quantity: Decimal) -> Decimal:
    return quantity.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold().replace("-", " ")).strip()


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


def env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


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
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        os.environ.setdefault(key, clean_env_value(value))


def clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def first(items: Iterable[Any]) -> Any | None:
    return next(iter(items), None)


if __name__ == "__main__":
    raise SystemExit(main())
