#!/usr/bin/env python3
"""
Polymarket US binary hedge arbitrage scanner/trader.

The only true locked hedge is buying complementary outcomes in the same
binary market for less than the guaranteed payout after fees. This script
therefore defaults to executable order-book pricing:

    buy LONG at best offer + buy SHORT at (1 - best bid)

That is deliberately stricter than comparing displayed side prices. Displayed
prices can make a market look underpriced while the executable book is still
wide and non-arbitrageable.

Default mode is dry-run. Pass --live to place real orders.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any


POLYMARKET_US_GATEWAY_URL = "https://gateway.polymarket.us"
POLYMARKET_US_MARKETS_URL = f"{POLYMARKET_US_GATEWAY_URL}/v1/markets"
STATE_PATH = Path(__file__).with_suffix(".state.json")

DEFAULT_PAGE_SIZE = 250
DEFAULT_MARKET_LIMIT = 2000
DEFAULT_REFRESH_SECONDS = 1
DEFAULT_MAX_ORDER_AGE_SECONDS = Decimal("2")
DEFAULT_BOOK_WORKERS = 16
DEFAULT_TRADE_DOLLARS = Decimal("5")
DEFAULT_SUM_TARGET = Decimal("0.99")
DEFAULT_TAKER_THETA = Decimal("0.05")
DEFAULT_TICK_SIZE = Decimal("0.001")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")

SPECIAL_SETTLEMENT_PATTERNS = (
    "last fair market",
    "divided by the number",
    "rounded down to the nearest tick",
)


@dataclass(frozen=True)
class MarketSide:
    label: str
    intent: str
    displayed_price: Decimal | None
    team: str | None


@dataclass(frozen=True)
class BinaryMarket:
    market_id: str
    slug: str
    question: str
    category: str
    sports_market_type: str
    tick_size: Decimal
    minimum_trade_qty: Decimal
    fee_theta: Decimal
    long_side: MarketSide
    short_side: MarketSide
    special_settlement: bool


@dataclass(frozen=True)
class BookQuote:
    long_bid: Decimal | None
    long_ask: Decimal | None
    bid_qty: Decimal | None
    ask_qty: Decimal | None


@dataclass(frozen=True)
class Opportunity:
    market: BinaryMarket
    long_price: Decimal
    short_price: Decimal
    quantity: Decimal
    gross_sum: Decimal
    fee_sum: Decimal
    all_in_sum: Decimal
    edge: Decimal
    pricing_mode: str
    book: BookQuote | None


class BinaryArbError(RuntimeError):
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
            raise BinaryArbError(
                "Live mode requires the Polymarket US SDK: "
                "`python3 -m pip install polymarket-us`."
            ) from exc

        key_id = env_first("POLYMARKET_KEY_ID", "POLYMARKET_ACCESS_KEY_ID")
        secret_key = env_first("POLYMARKET_SECRET_KEY", "POLYMARKET_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise BinaryArbError("Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY.")

        self.client = PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=float(os.getenv("POLYMARKET_TIMEOUT", "30")),
        )

    def create_limit_buy(
        self,
        market_slug: str,
        intent: str,
        price: Decimal,
        quantity: Decimal,
    ) -> str | None:
        order = {
            "marketSlug": market_slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format_decimal(price), "currency": "USD"},
            "quantity": float(quantity),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        if not self.live:
            logging.info("DRY RUN create order: %s", json.dumps(order, sort_keys=True))
            return None

        assert self.client is not None
        response = self.client.orders.create(order)
        order_id = extract_order_id(response)
        logging.info("Placed order %s: %s", order_id or "<unknown>", response)
        return order_id

    def create_hedge_orders(self, opportunity: Opportunity) -> list[dict[str, str]]:
        slug = opportunity.market.slug
        quantity = opportunity.quantity
        legs = [
            ("long", opportunity.market.long_side.intent, opportunity.long_price),
            ("short", opportunity.market.short_side.intent, opportunity.short_price),
        ]
        if not self.live:
            for _, intent, price in legs:
                self.create_limit_buy(slug, intent, price, quantity)
            return []

        created: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.create_limit_buy, slug, intent, price, quantity)
                for _, intent, price in legs
            ]
            for (side, intent, price), future in zip(legs, futures):
                order_id = future.result()
                if order_id:
                    created.append(
                        {
                            "side": side,
                            "intent": intent,
                            "order_id": order_id,
                            "market_slug": slug,
                            "price": format_decimal(price),
                            "quantity": format_decimal(quantity),
                        }
                    )
        return created

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
    if args.live and args.pricing_mode != "executable":
        raise BinaryArbError("--live requires --pricing-mode executable")

    executor = PolymarketUSExecutor(live=args.live)
    state_path = Path(args.state)
    state = load_state(state_path)
    try:
        while True:
            cancel_stale_orders(args, executor, state, state_path)
            opportunities = run_scan(args)
            maybe_trade(args, executor, state, state_path, opportunities)
            if args.once:
                return 0
            time.sleep(args.refresh_seconds)
    finally:
        executor.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket US binary hedge arbitrage bot")
    parser.add_argument("--live", action="store_true", help="place real hedge orders")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument(
        "--pricing-mode",
        choices=["executable", "displayed"],
        default=os.getenv("BINARY_ARB_PRICING_MODE", "executable"),
        help="executable uses the book; displayed is research-only and can show false arbs",
    )
    parser.add_argument(
        "--sum-target",
        type=Decimal,
        default=Decimal(os.getenv("BINARY_ARB_SUM_TARGET", str(DEFAULT_SUM_TARGET))),
        help="maximum all-in price sum per complete set after fees",
    )
    parser.add_argument(
        "--trade-dollars",
        type=Decimal,
        default=Decimal(os.getenv("BINARY_ARB_TRADE_DOLLARS", str(DEFAULT_TRADE_DOLLARS))),
        help="notional dollars per hedge leg",
    )
    parser.add_argument(
        "--market-limit",
        type=int,
        default=int(os.getenv("BINARY_ARB_MARKET_LIMIT", DEFAULT_MARKET_LIMIT)),
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=int(os.getenv("BINARY_ARB_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
    )
    parser.add_argument(
        "--category",
        default=os.getenv("BINARY_ARB_CATEGORY", ""),
        help="optional category filter, e.g. sports or crypto",
    )
    parser.add_argument(
        "--include-special-settlement",
        action=argparse.BooleanOptionalAction,
        default=env_bool("BINARY_ARB_INCLUDE_SPECIAL_SETTLEMENT", False),
        help="include markets with fractional/last-fair-market settlement language",
    )
    parser.add_argument(
        "--max-trades-per-cycle",
        type=int,
        default=int(os.getenv("BINARY_ARB_MAX_TRADES_PER_CYCLE", "1")),
    )
    parser.add_argument(
        "--max-opportunities",
        type=int,
        default=int(os.getenv("BINARY_ARB_MAX_OPPORTUNITIES", "25")),
    )
    parser.add_argument(
        "--max-order-age-seconds",
        type=Decimal,
        default=Decimal(
            os.getenv("BINARY_ARB_MAX_ORDER_AGE_SECONDS", str(DEFAULT_MAX_ORDER_AGE_SECONDS))
        ),
        help="cancel tracked live orders older than this many seconds",
    )
    parser.add_argument(
        "--book-workers",
        type=int,
        default=int(os.getenv("BINARY_ARB_BOOK_WORKERS", DEFAULT_BOOK_WORKERS)),
        help="parallel workers for market book reads",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=float,
        default=float(os.getenv("BINARY_ARB_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)),
    )
    parser.add_argument(
        "--state",
        default=os.getenv("BINARY_ARB_STATE", str(STATE_PATH)),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("BINARY_ARB_LOG_LEVEL", "INFO").upper(),
    )
    return parser.parse_args()


def run_scan(args: argparse.Namespace) -> list[Opportunity]:
    raw_markets = fetch_active_markets(
        limit=args.market_limit,
        page_size=args.page_size,
        category=args.category or None,
    )
    binary_markets = []
    skipped_special = 0
    for raw in raw_markets:
        market = parse_binary_market(raw)
        if market is None:
            continue
        if market.special_settlement and not args.include_special_settlement:
            skipped_special += 1
            continue
        binary_markets.append(market)

    opportunities: list[Opportunity] = []
    workers = max(1, args.book_workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda market: evaluate_market_safely(market, args), binary_markets)
    for opportunity in results:
        if opportunity is not None:
            opportunities.append(opportunity)

    opportunities.sort(key=lambda item: item.edge, reverse=True)
    logging.info(
        "Scanned %s active markets, %s binary candidates, %s skipped special settlement, %s opportunities",
        len(raw_markets),
        len(binary_markets),
        skipped_special,
        len(opportunities),
    )
    for opportunity in opportunities[: args.max_opportunities]:
        log_opportunity(opportunity)
    if not opportunities:
        logging.info("No all-in complete-set cost below target %s", format_decimal(args.sum_target))
    return opportunities


def evaluate_market_safely(market: BinaryMarket, args: argparse.Namespace) -> Opportunity | None:
    try:
        return evaluate_market(market, args)
    except Exception as exc:  # noqa: BLE001 - public API failures vary.
        logging.debug("Could not evaluate %s: %s", market.slug, exc)
        return None


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
    for order in orders:
        if not isinstance(order, dict):
            continue
        created_at = parse_datetime(order.get("created_at"))
        age = Decimal(str((now - created_at).total_seconds())) if created_at else args.max_order_age_seconds
        if age < args.max_order_age_seconds:
            kept.append(order)
            continue
        if executor.cancel_order(
            str(order.get("order_id") or ""),
            str(order.get("market_slug") or ""),
        ):
            canceled += 1
        # If cancel fails because the order already filled or disappeared, stop
        # treating it as an open order. Position-stream handling should replace
        # this local TTL ledger before using larger size.
    if canceled:
        logging.info("Cancelled %s tracked orders older than %ss", canceled, args.max_order_age_seconds)
    state["orders"] = kept
    save_state(state_path, state)


def maybe_trade(
    args: argparse.Namespace,
    executor: PolymarketUSExecutor,
    state: dict[str, Any],
    state_path: Path,
    opportunities: list[Opportunity],
) -> None:
    if not opportunities:
        save_state(state_path, state)
        return

    traded_slugs = {
        str(item.get("market_slug"))
        for item in state.get("orders", [])
        if item.get("market_slug")
    }
    created_count = 0
    for opportunity in opportunities:
        if created_count >= args.max_trades_per_cycle:
            break
        if opportunity.market.slug in traded_slugs:
            logging.info("Skip %s: already has tracked orders", opportunity.market.slug)
            continue

        created = executor.create_hedge_orders(opportunity)
        created_count += 1
        now = datetime.now(timezone.utc).isoformat()
        if executor.live:
            if len(created) != 2:
                logging.warning(
                    "Expected 2 hedge orders for %s, got %s; cancelling created legs",
                    opportunity.market.slug,
                    len(created),
                )
                for order in created:
                    executor.cancel_order(order["order_id"], order["market_slug"])
                continue
            state.setdefault("orders", []).extend(
                {
                    **order,
                    "created_at": now,
                    "all_in_sum": format_decimal(opportunity.all_in_sum),
                    "edge": format_decimal(opportunity.edge),
                }
                for order in created
            )
        else:
            state.setdefault("dry_run_opportunities", []).append(
                {
                    "market_slug": opportunity.market.slug,
                    "question": opportunity.market.question,
                    "long_price": format_decimal(opportunity.long_price),
                    "short_price": format_decimal(opportunity.short_price),
                    "quantity": format_decimal(opportunity.quantity),
                    "all_in_sum": format_decimal(opportunity.all_in_sum),
                    "edge": format_decimal(opportunity.edge),
                    "created_at": now,
                }
            )
    save_state(state_path, state)


def evaluate_market(market: BinaryMarket, args: argparse.Namespace) -> Opportunity | None:
    book = None
    if args.pricing_mode == "executable":
        book = fetch_book(market.slug)
        if book.long_ask is None or book.long_bid is None:
            return None
        long_price = quantize_price(book.long_ask, market.tick_size)
        short_price = quantize_price(Decimal("1") - book.long_bid, market.tick_size)
    else:
        if market.long_side.displayed_price is None or market.short_side.displayed_price is None:
            return None
        long_price = quantize_price(market.long_side.displayed_price, market.tick_size)
        short_price = quantize_price(market.short_side.displayed_price, market.tick_size)

    if long_price <= 0 or short_price <= 0:
        return None
    gross_sum = long_price + short_price
    quantity = quantize_quantity(args.trade_dollars / max(long_price, short_price))
    if quantity < market.minimum_trade_qty:
        return None
    fee_sum = taker_fee_per_contract(long_price, market.fee_theta) + taker_fee_per_contract(
        short_price,
        market.fee_theta,
    )
    all_in_sum = gross_sum + fee_sum
    edge = Decimal("1") - all_in_sum
    if all_in_sum > args.sum_target:
        return None
    return Opportunity(
        market=market,
        long_price=long_price,
        short_price=short_price,
        quantity=quantity,
        gross_sum=gross_sum,
        fee_sum=fee_sum,
        all_in_sum=all_in_sum,
        edge=edge,
        pricing_mode=args.pricing_mode,
        book=book,
    )


def log_opportunity(opportunity: Opportunity) -> None:
    market = opportunity.market
    labels = f"{market.long_side.label}/{market.short_side.label}"
    team_bits = [team for team in (market.long_side.team, market.short_side.team) if team]
    team_text = f" teams={','.join(team_bits)}" if team_bits else ""
    book_text = ""
    if opportunity.book is not None:
        book_text = (
            f" bid={fmt_optional(opportunity.book.long_bid)}"
            f" ask={fmt_optional(opportunity.book.long_ask)}"
        )
    logging.info(
        "ARB %s all_in=%s edge=%s gross=%s fee=%s qty=%s sides=%s%s%s question=%s",
        market.slug,
        format_decimal(opportunity.all_in_sum),
        format_decimal(opportunity.edge),
        format_decimal(opportunity.gross_sum),
        format_decimal(opportunity.fee_sum),
        format_decimal(opportunity.quantity),
        labels,
        team_text,
        book_text,
        market.question,
    )


def fetch_active_markets(
    limit: int,
    page_size: int,
    category: str | None,
) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while len(markets) < limit:
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": min(page_size, limit - len(markets)),
            "offset": offset,
        }
        if category:
            params["categories"] = category
        payload = http_get_json(POLYMARKET_US_MARKETS_URL, params)
        page = unpack_markets(payload)
        if not page:
            break
        markets.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return markets


def fetch_book(slug: str) -> BookQuote:
    payload = http_get_json(f"{POLYMARKET_US_MARKETS_URL}/{slug}/book")
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    bids = data.get("bids") if isinstance(data.get("bids"), list) else []
    offers = data.get("offers") if isinstance(data.get("offers"), list) else []
    best_bid = max((amount_value(item.get("px")) for item in bids), default=None)
    best_ask = min((amount_value(item.get("px")) for item in offers), default=None)
    bid_qty = first_qty_at_price(bids, best_bid)
    ask_qty = first_qty_at_price(offers, best_ask)
    return BookQuote(best_bid, best_ask, bid_qty, ask_qty)


def parse_binary_market(market: dict[str, Any]) -> BinaryMarket | None:
    if not is_tradeable_market(market):
        return None
    sides = [side for side in market.get("marketSides") or [] if isinstance(side, dict)]
    if len(sides) != 2:
        return None
    long_raw = first(side for side in sides if side.get("long") is True)
    short_raw = first(side for side in sides if side.get("long") is False)
    if long_raw is None or short_raw is None:
        return None
    if long_raw.get("tradable") is False or short_raw.get("tradable") is False:
        return None

    slug = str(market.get("slug") or "")
    if not slug:
        return None

    text = searchable_text(market).lower()
    return BinaryMarket(
        market_id=str(market.get("id") or ""),
        slug=slug,
        question=str(market.get("question") or market.get("title") or slug),
        category=str(market.get("category") or ""),
        sports_market_type=str(market.get("sportsMarketType") or market.get("sportsMarketTypeV2") or ""),
        tick_size=market_tick_size(market),
        minimum_trade_qty=market_minimum_trade_qty(market),
        fee_theta=to_decimal(market.get("feeCoefficient")) or DEFAULT_TAKER_THETA,
        long_side=parse_side(long_raw, "ORDER_INTENT_BUY_LONG"),
        short_side=parse_side(short_raw, "ORDER_INTENT_BUY_SHORT"),
        special_settlement=any(pattern in text for pattern in SPECIAL_SETTLEMENT_PATTERNS),
    )


def parse_side(side: dict[str, Any], intent: str) -> MarketSide:
    team = side.get("team") if isinstance(side.get("team"), dict) else {}
    team_name = (
        team.get("safeName")
        or team.get("name")
        or team.get("alias")
        or team.get("abbreviation")
    )
    label = (
        side.get("description")
        or team_name
        or ("Long" if intent == "ORDER_INTENT_BUY_LONG" else "Short")
    )
    return MarketSide(
        label=str(label),
        intent=intent,
        displayed_price=amount_value(side.get("quote")) or to_decimal(side.get("price")),
        team=str(team_name) if team_name else None,
    )


def is_tradeable_market(market: dict[str, Any]) -> bool:
    if market.get("closed") is True or market.get("active") is False:
        return False
    if market.get("archived") is True or market.get("hidden") is True:
        return False
    return bool(market.get("slug"))


def taker_fee_per_contract(price: Decimal, theta: Decimal) -> Decimal:
    return theta * price * (Decimal("1") - price)


def market_tick_size(market: dict[str, Any]) -> Decimal:
    return (
        to_decimal(market.get("orderPriceMinTickSize"))
        or to_decimal(market.get("minimumTickSize"))
        or to_decimal(market.get("tickSize"))
        or DEFAULT_TICK_SIZE
    )


def market_minimum_trade_qty(market: dict[str, Any]) -> Decimal:
    return (
        to_decimal(market.get("minimumTradeQty"))
        or to_decimal(market.get("minimumOrderSize"))
        or Decimal("1")
    )


def first_qty_at_price(items: list[Any], price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if amount_value(item.get("px")) == price:
            return to_decimal(item.get("qty"))
    return None


def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "binary-arb-bot/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BinaryArbError(f"GET {url} failed with HTTP {exc.code}: {body[:300]}") from exc


def unpack_markets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        markets = payload.get("markets") or payload.get("data") or []
        return markets if isinstance(markets, list) else []
    return payload if isinstance(payload, list) else []


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


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    bounded = min(max(price, MIN_PRICE), MAX_PRICE)
    if tick <= 0:
        tick = DEFAULT_TICK_SIZE
    units = (bounded / tick).to_integral_value(rounding=ROUND_DOWN)
    return (units * tick).quantize(tick, rounding=ROUND_DOWN)


def quantize_quantity(quantity: Decimal) -> Decimal:
    bounded = max(quantity, Decimal("0"))
    return bounded.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def fmt_optional(value: Decimal | None) -> str:
    return "-" if value is None else format_decimal(value)


def extract_order_id(response: Any) -> str | None:
    if isinstance(response, dict):
        return response.get("id") or response.get("orderId") or response.get("orderID")
    for attr in ("id", "orderId", "orderID"):
        order_id = getattr(response, attr, None)
        if order_id:
            return str(order_id)
    return None


def searchable_text(obj: dict[str, Any]) -> str:
    fields = [
        obj.get("title"),
        obj.get("titleShort"),
        obj.get("question"),
        obj.get("slug"),
        obj.get("description"),
        obj.get("subtitle"),
        obj.get("category"),
        obj.get("marketType"),
        obj.get("sportsMarketType"),
        obj.get("sportsMarketTypeV2"),
    ]
    for side in obj.get("marketSides") or []:
        if not isinstance(side, dict):
            continue
        fields.append(side.get("description"))
        team = side.get("team") if isinstance(side.get("team"), dict) else {}
        fields.extend([team.get("name"), team.get("safeName"), team.get("alias")])
    return " ".join(str(field) for field in fields if field)


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


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"orders": [], "dry_run_opportunities": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"orders": [], "dry_run_opportunities": []}
    if not isinstance(state, dict):
        return {"orders": [], "dry_run_opportunities": []}
    state.setdefault("orders", [])
    state.setdefault("dry_run_opportunities", [])
    return state


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
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def first(items: Any) -> Any | None:
    return next((item for item in items if item), None)


if __name__ == "__main__":
    raise SystemExit(main())
