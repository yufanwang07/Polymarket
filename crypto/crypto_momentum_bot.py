#!/usr/bin/env python3
"""
Polymarket US crypto momentum bot.

This bot is intentionally a compact Python adaptation of two ideas:
  - The HFT engine pattern: separate signal, risk, execution, and state.
  - The 5-minute crypto lag strategy: if Binance moves hard over a short
    window, look for the matching Polymarket crypto up/down market before
    market odds fully reprice.

Default mode is dry-run. Pass --live to submit real Polymarket US orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable

import requests
import websockets


POLYMARKET_US_GATEWAY_URL = "https://gateway.polymarket.us"
POLYMARKET_US_MARKETS_URL = f"{POLYMARKET_US_GATEWAY_URL}/v1/markets"
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
BINANCE_US_WS_URL = "wss://stream.binance.us:9443/stream"
BINANCE_REST_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_US_REST_KLINES_URL = "https://api.binance.us/api/v3/klines"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{product}/spot"
STATE_DIR = Path(__file__).resolve().parent
STATE_PATH = STATE_DIR / ".crypto_momentum.state.json"

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_THRESHOLD_PCT = Decimal("0.003")
DEFAULT_BID_DOLLARS = Decimal("5")
DEFAULT_REFRESH_SECONDS = 30
DEFAULT_MAX_ENTRY_PRICE = Decimal("0.75")
DEFAULT_TICK_SIZE = Decimal("0.005")
DEFAULT_DEPEG_THRESHOLD = Decimal("0.0015")
DEFAULT_COOLDOWN_SECONDS = 180
DEFAULT_MIN_SECONDS_REMAINING = 20
DEFAULT_MAX_ORDER_AGE_SECONDS = 90
DEFAULT_MAX_OPEN_ORDERS = 3
DEFAULT_SIM_MIN_SHARES = Decimal("1")
DEFAULT_SIM_MAX_SHARES = Decimal("10")
DEFAULT_SIM_SYNTHETIC_PRICE = Decimal("0.50")
DEFAULT_SIM_SYNTHETIC_DURATION_SECONDS = 5 * 60
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: Decimal
    ts: float


@dataclass(frozen=True)
class MomentumSignal:
    symbol: str
    direction: str
    pct_move: Decimal
    price: Decimal
    window_seconds: int
    ts: float


@dataclass(frozen=True)
class MarketChoice:
    market_slug: str
    market_id: str
    question: str
    symbol: str
    direction: str
    intent: str
    current_price: Decimal
    bid_price: Decimal
    quantity: Decimal
    seconds_remaining: int | None


class CryptoBotError(RuntimeError):
    pass


class Simulator:
    def __init__(self, min_shares: Decimal, max_shares: Decimal) -> None:
        self.min_shares = min_shares
        self.max_shares = max_shares
        self.positions: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.latest_prices: dict[str, Decimal] = {}
        self.last_report_ts = time.time()

    def update_price(self, tick: PriceTick) -> None:
        self.latest_prices[tick.symbol] = tick.price
        self.settle_expired(tick.ts)

    def open_position(self, choice: MarketChoice, signal: MomentumSignal) -> None:
        quantity = min(max(choice.quantity, self.min_shares), self.max_shares)
        if quantity < self.min_shares:
            logging.info(
                "SIM skip %s %s: quantity %s below minimum %s",
                choice.symbol,
                choice.direction,
                format_decimal(quantity),
                format_decimal(self.min_shares),
            )
            return

        expires_at = None
        if choice.seconds_remaining is not None:
            expires_at = datetime.fromtimestamp(signal.ts + choice.seconds_remaining, timezone.utc)

        position = {
            "id": f"sim-{int(signal.ts * 1000)}-{len(self.positions) + len(self.closed)}",
            "symbol": choice.symbol,
            "direction": choice.direction,
            "market_slug": choice.market_slug,
            "entry_price": format_decimal(choice.bid_price),
            "quantity": format_decimal(quantity),
            "entry_underlying": format_decimal(signal.price),
            "opened_at": datetime.fromtimestamp(signal.ts, timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        self.positions.append(position)
        self.latest_prices[signal.symbol] = signal.price
        logging.info(
            "SIM fill %s %s qty=%s entry=%s underlying=%s slug=%s",
            choice.symbol,
            choice.direction,
            format_decimal(quantity),
            format_decimal(choice.bid_price),
            format_decimal(signal.price),
            choice.market_slug,
        )

    def settle_expired(self, now_ts: float) -> None:
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        kept = []
        for position in self.positions:
            expires_at = parse_datetime(position.get("expires_at"))
            if expires_at is None or expires_at > now:
                kept.append(position)
                continue
            self.closed.append(self.close_position(position, "expired"))
        self.positions = kept

    def report_due(self, now_ts: float, interval_seconds: int) -> bool:
        if now_ts - self.last_report_ts < interval_seconds:
            return False
        self.last_report_ts = now_ts
        return True

    def report(self, label: str = "SIM performance") -> None:
        open_marks = [self.mark_position(position) for position in self.positions]
        realized = sum(
            ((to_decimal(trade["pnl"]) or Decimal("0")) for trade in self.closed),
            Decimal("0"),
        )
        unrealized = sum((mark["pnl"] for mark in open_marks), Decimal("0"))
        invested = sum((mark["cost"] for mark in open_marks), Decimal("0")) + sum(
            ((to_decimal(trade["cost"]) or Decimal("0")) for trade in self.closed),
            Decimal("0"),
        )
        wins = sum(1 for trade in self.closed if (to_decimal(trade["pnl"]) or Decimal("0")) > 0)
        losses = sum(1 for trade in self.closed if (to_decimal(trade["pnl"]) or Decimal("0")) <= 0)

        logging.info(
            "%s: open=%s closed=%s wins=%s losses=%s invested=$%s realized=$%s unrealized=$%s total=$%s",
            label,
            len(self.positions),
            len(self.closed),
            wins,
            losses,
            format_decimal(invested),
            format_decimal(realized),
            format_decimal(unrealized),
            format_decimal(realized + unrealized),
        )
        for mark in open_marks[:5]:
            logging.info(
                "SIM open %s %s qty=%s entry=%s now=%s pnl=$%s slug=%s",
                mark["symbol"],
                mark["direction"],
                format_decimal(mark["quantity"]),
                format_decimal(mark["entry_price"]),
                format_decimal(mark["current_underlying"]),
                format_decimal(mark["pnl"]),
                mark["market_slug"],
            )

    def close_position(self, position: dict[str, Any], reason: str) -> dict[str, str]:
        mark = self.mark_position(position)
        payout = mark["quantity"] if mark["in_the_money"] else Decimal("0")
        closed = {
            **position,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "close_reason": reason,
            "payout": format_decimal(payout),
            "cost": format_decimal(mark["cost"]),
            "pnl": format_decimal(payout - mark["cost"]),
        }
        logging.info(
            "SIM close %s %s payout=$%s pnl=$%s reason=%s",
            position["symbol"],
            position["direction"],
            closed["payout"],
            closed["pnl"],
            reason,
        )
        return closed

    def mark_position(self, position: dict[str, Any]) -> dict[str, Any]:
        symbol = str(position["symbol"])
        current = self.latest_prices.get(symbol) or to_decimal(position["entry_underlying"]) or Decimal("0")
        entry_underlying = to_decimal(position["entry_underlying"]) or current
        entry_price = to_decimal(position["entry_price"]) or Decimal("0")
        quantity = to_decimal(position["quantity"]) or Decimal("0")
        direction = str(position["direction"])
        in_the_money = current > entry_underlying if direction == "UP" else current < entry_underlying
        mark_value = quantity if in_the_money else Decimal("0")
        cost = entry_price * quantity
        return {
            "symbol": symbol,
            "direction": direction,
            "market_slug": position["market_slug"],
            "quantity": quantity,
            "entry_price": entry_price,
            "entry_underlying": entry_underlying,
            "current_underlying": current,
            "in_the_money": in_the_money,
            "cost": cost,
            "pnl": mark_value - cost,
        }


class MomentumDetector:
    def __init__(self, window_seconds: int, threshold_pct: Decimal) -> None:
        self.window_seconds = window_seconds
        self.threshold_pct = threshold_pct
        self.windows: dict[str, deque[PriceTick]] = defaultdict(deque)

    def ingest(self, tick: PriceTick) -> MomentumSignal | None:
        window = self.windows[tick.symbol]
        window.append(tick)
        cutoff = tick.ts - self.window_seconds
        while window and window[0].ts < cutoff:
            window.popleft()
        if len(window) < 2:
            return None

        oldest = window[0]
        pct_move = (tick.price - oldest.price) / oldest.price
        if abs(pct_move) < self.threshold_pct:
            return None
        return MomentumSignal(
            symbol=tick.symbol,
            direction="UP" if pct_move > 0 else "DOWN",
            pct_move=pct_move,
            price=tick.price,
            window_seconds=self.window_seconds,
            ts=tick.ts,
        )


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
            raise CryptoBotError(
                "Live mode requires `python3 -m pip install polymarket-us`."
            ) from exc

        key_id = env_first("POLYMARKET_KEY_ID", "POLYMARKET_ACCESS_KEY_ID")
        secret_key = env_first("POLYMARKET_SECRET_KEY", "POLYMARKET_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise CryptoBotError("Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY.")

        self.client = PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=float(os.getenv("POLYMARKET_TIMEOUT", "30")),
        )

    def list_crypto_markets(self, limit: int) -> list[dict[str, Any]]:
        if self.client is not None:
            payload = self.client.markets.list(
                {"limit": limit, "active": True, "closed": False, "categories": ["crypto"]}
            )
        else:
            payload = http_get_json(
                POLYMARKET_US_MARKETS_URL,
                {"limit": limit, "active": "true", "closed": "false", "categories": "crypto"},
            )
        markets = unpack_markets(payload)
        if markets:
            return markets

        logging.info("No category=crypto markets returned; falling back to broad active scan")
        if self.client is not None:
            payload = self.client.markets.list({"limit": limit, "active": True, "closed": False})
        else:
            payload = http_get_json(
                POLYMARKET_US_MARKETS_URL,
                {"limit": limit, "active": "true", "closed": "false"},
            )
        return [
            market
            for market in unpack_markets(payload)
            if market_symbol(market) in {"BTCUSDT", "ETHUSDT"}
        ]

    def bbo(self, market_slug: str) -> dict[str, Any] | None:
        try:
            if self.client is not None:
                return self.client.markets.bbo(market_slug)
            return http_get_json(f"{POLYMARKET_US_MARKETS_URL}/{market_slug}/bbo")
        except Exception as exc:  # noqa: BLE001 - public endpoint/SDK errors vary.
            logging.debug("Could not fetch BBO for %s: %s", market_slug, exc)
            return None

    def create_order(self, choice: MarketChoice) -> str | None:
        order = {
            "marketSlug": choice.market_slug,
            "intent": choice.intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format_decimal(choice.bid_price), "currency": "USD"},
            "quantity": float(choice.quantity),
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
        except Exception as exc:  # noqa: BLE001
            logging.warning("Could not cancel order %s in %s: %s", order_id, market_slug, exc)

    def close(self) -> None:
        if self.client is not None and hasattr(self.client, "close"):
            self.client.close()


class RiskManager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.last_trade_ts: dict[tuple[str, str], float] = {}

    def allow_signal(
        self,
        signal: MomentumSignal,
        state: dict[str, Any],
        simulator: Simulator | None = None,
    ) -> bool:
        open_count = len(simulator.positions) if simulator is not None else len(state.get("orders", []))
        if open_count >= self.args.max_open_orders:
            logging.info("Risk skip: %s open orders >= max %s", open_count, self.args.max_open_orders)
            return False

        key = (signal.symbol, signal.direction)
        elapsed = signal.ts - self.last_trade_ts.get(key, 0)
        if elapsed < self.args.cooldown_seconds:
            logging.info("Risk skip: %s %s cooldown %.1fs", signal.symbol, signal.direction, elapsed)
            return False

        if self.args.check_depeg and not coinbase_binance_in_line(
            signal.symbol,
            signal.price,
            self.args.depeg_threshold,
        ):
            logging.warning("Risk skip: Coinbase/Binance divergence exceeded threshold")
            return False

        return True

    def mark_trade(self, signal: MomentumSignal) -> None:
        self.last_trade_ts[(signal.symbol, signal.direction)] = signal.ts


async def main_async() -> int:
    load_env_file(Path.cwd() / ".env")
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    executor = PolymarketUSExecutor(live=args.live)
    risk = RiskManager(args)
    simulator = Simulator(args.sim_min_shares, args.sim_max_shares) if args.simulate else None
    state_path = Path(args.state)
    state = load_state(state_path)
    cancel_old_orders(executor, state, args.max_order_age_seconds)
    save_state(state_path, state)

    try:
        if args.scan_once:
            scan_once(executor, args)
            return 0
        if args.once:
            return run_once(executor, risk, simulator, state, state_path, args)
        await run_stream(executor, risk, simulator, state, state_path, args)
        return 0
    finally:
        if simulator is not None:
            simulator.report("SIM final performance")
        executor.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket US crypto momentum bot")
    parser.add_argument("--live", action="store_true", help="submit real Polymarket US orders")
    parser.add_argument("--simulate", action="store_true", help="paper trade during runtime and report PnL")
    parser.add_argument("--once", action="store_true", help="run one REST signal check and exit")
    parser.add_argument("--scan-once", action="store_true", help="list matching crypto markets and exit")
    parser.add_argument(
        "--symbols",
        default=os.getenv("CRYPTO_BOT_SYMBOLS", ",".join(DEFAULT_SYMBOLS)),
        help="comma-separated Binance symbols",
    )
    parser.add_argument(
        "--price-source",
        choices=["auto", "binance", "binance-us"],
        default=os.getenv("CRYPTO_BOT_PRICE_SOURCE", "auto"),
        help="price feed source; auto uses Binance.US for streaming and fallback for REST",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)),
    )
    parser.add_argument(
        "--threshold-pct",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_THRESHOLD_PCT", str(DEFAULT_THRESHOLD_PCT))),
    )
    parser.add_argument(
        "--bid-dollars",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_BID_DOLLARS", str(DEFAULT_BID_DOLLARS))),
    )
    parser.add_argument(
        "--sim-min-shares",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_SIM_MIN_SHARES", str(DEFAULT_SIM_MIN_SHARES))),
    )
    parser.add_argument(
        "--sim-max-shares",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_SIM_MAX_SHARES", str(DEFAULT_SIM_MAX_SHARES))),
    )
    parser.add_argument(
        "--sim-synthetic-markets",
        action=argparse.BooleanOptionalAction,
        default=env_bool("CRYPTO_BOT_SIM_SYNTHETIC_MARKETS", True),
        help="in --simulate mode, paper-trade synthetic 5m markets when no real market is eligible",
    )
    parser.add_argument(
        "--sim-synthetic-price",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_SIM_SYNTHETIC_PRICE", str(DEFAULT_SIM_SYNTHETIC_PRICE))),
    )
    parser.add_argument(
        "--sim-synthetic-duration-seconds",
        type=int,
        default=int(
            os.getenv(
                "CRYPTO_BOT_SIM_SYNTHETIC_DURATION_SECONDS",
                DEFAULT_SIM_SYNTHETIC_DURATION_SECONDS,
            )
        ),
    )
    parser.add_argument(
        "--max-entry-price",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_MAX_ENTRY_PRICE", str(DEFAULT_MAX_ENTRY_PRICE))),
    )
    parser.add_argument(
        "--entry-mode",
        choices=["maker", "taker"],
        default=os.getenv("CRYPTO_BOT_ENTRY_MODE", "maker"),
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)),
    )
    parser.add_argument(
        "--min-seconds-remaining",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_MIN_SECONDS_REMAINING", DEFAULT_MIN_SECONDS_REMAINING)),
    )
    parser.add_argument(
        "--max-order-age-seconds",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_MAX_ORDER_AGE_SECONDS", DEFAULT_MAX_ORDER_AGE_SECONDS)),
    )
    parser.add_argument(
        "--max-open-orders",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_MAX_OPEN_ORDERS", DEFAULT_MAX_OPEN_ORDERS)),
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS)),
    )
    parser.add_argument(
        "--market-limit",
        type=int,
        default=int(os.getenv("CRYPTO_BOT_MARKET_LIMIT", "200")),
    )
    parser.add_argument(
        "--check-depeg",
        action=argparse.BooleanOptionalAction,
        default=env_bool("CRYPTO_BOT_CHECK_DEPEG", True),
    )
    parser.add_argument(
        "--depeg-threshold",
        type=Decimal,
        default=Decimal(os.getenv("CRYPTO_BOT_DEPEG_THRESHOLD", str(DEFAULT_DEPEG_THRESHOLD))),
    )
    parser.add_argument(
        "--state",
        default=os.getenv("CRYPTO_BOT_STATE", str(STATE_PATH)),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("CRYPTO_BOT_LOG_LEVEL", "INFO").upper(),
    )
    args = parser.parse_args()
    if args.live and args.simulate:
        parser.error("--live and --simulate are mutually exclusive")
    if args.sim_min_shares <= 0 or args.sim_max_shares < args.sim_min_shares:
        parser.error("--sim-min-shares must be > 0 and <= --sim-max-shares")
    return args


async def run_stream(
    executor: PolymarketUSExecutor,
    risk: RiskManager,
    simulator: Simulator | None,
    state: dict[str, Any],
    state_path: Path,
    args: argparse.Namespace,
) -> None:
    detector = MomentumDetector(args.window_seconds, args.threshold_pct)
    symbols = parse_symbols(args.symbols)
    streams = "/".join(f"{symbol.lower()}@aggTrade" for symbol in symbols)
    url = f"{stream_base_url(args)}?streams={streams}"
    mode = "LIVE" if args.live else "SIM" if args.simulate else "DRY-RUN"
    logging.info("Starting stream for %s in %s mode", ",".join(symbols), mode)

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                logging.info("Connected to Binance stream")
                async for raw in ws:
                    payload = json.loads(raw)
                    data = payload.get("data") or payload
                    tick = PriceTick(
                        symbol=str(data.get("s") or "").upper(),
                        price=Decimal(str(data["p"])),
                        ts=float(data["T"]) / 1000,
                    )
                    if simulator is not None:
                        simulator.update_price(tick)
                        if simulator.report_due(tick.ts, args.window_seconds):
                            simulator.report("SIM periodic performance")
                    signal = detector.ingest(tick)
                    if signal is None:
                        continue
                    handle_signal(executor, risk, simulator, state, state_path, args, signal)
        except Exception:
            logging.exception("Binance stream disconnected; reconnecting after %ss", args.refresh_seconds)
            await asyncio.sleep(args.refresh_seconds)


def run_once(
    executor: PolymarketUSExecutor,
    risk: RiskManager,
    simulator: Simulator | None,
    state: dict[str, Any],
    state_path: Path,
    args: argparse.Namespace,
) -> int:
    symbols = parse_symbols(args.symbols)
    logging.info("Running one REST signal check for %s", ",".join(symbols))
    for symbol in symbols:
        signal = rest_momentum_signal(symbol, args)
        if signal is None:
            logging.info("%s no signal over %ss", symbol, args.window_seconds)
            continue
        if simulator is not None:
            simulator.latest_prices[signal.symbol] = signal.price
        handle_signal(executor, risk, simulator, state, state_path, args, signal)
    if simulator is not None:
        simulator.report("SIM once performance")
    return 0


def scan_once(executor: PolymarketUSExecutor, args: argparse.Namespace) -> None:
    markets = executor.list_crypto_markets(args.market_limit)
    symbols = parse_symbols(args.symbols)
    matches = [
        market
        for market in markets
        if market_symbol(market) in symbols and market_direction(market) in {"UP", "DOWN"}
    ]
    logging.info("Found %s candidate crypto markets", len(matches))
    for market in matches[:25]:
        logging.info(
            "%s direction=%s slug=%s price=%s end=%s",
            market_symbol(market),
            market_direction(market),
            market.get("slug"),
            side_price_for_direction(market, market_direction(market)),
            market.get("endDate") or market.get("endTime"),
        )


def handle_signal(
    executor: PolymarketUSExecutor,
    risk: RiskManager,
    simulator: Simulator | None,
    state: dict[str, Any],
    state_path: Path,
    args: argparse.Namespace,
    signal: MomentumSignal,
) -> None:
    logging.info(
        "Signal %s %s move=%s price=%s",
        signal.symbol,
        signal.direction,
        format_percent(signal.pct_move),
        format_decimal(signal.price),
    )
    cancel_old_orders(executor, state, args.max_order_age_seconds)
    if not risk.allow_signal(signal, state, simulator):
        save_state(state_path, state)
        return

    markets = executor.list_crypto_markets(args.market_limit)
    choice = choose_market_for_signal(executor, markets, signal, args)
    if choice is None:
        if simulator is None or not args.sim_synthetic_markets:
            logging.info("No eligible market for %s %s", signal.symbol, signal.direction)
            save_state(state_path, state)
            return
        choice = synthetic_market_choice(signal, args)
        logging.info(
            "SIM using synthetic market for %s %s because no live Polymarket market is eligible",
            signal.symbol,
            signal.direction,
        )

    risk.mark_trade(signal)
    if simulator is not None:
        simulator.open_position(choice, signal)
        save_state(state_path, state)
        return

    order_id = executor.create_order(choice)
    if order_id:
        state.setdefault("orders", []).append(
            {
                "order_id": order_id,
                "market_slug": choice.market_slug,
                "market_id": choice.market_id,
                "symbol": choice.symbol,
                "direction": choice.direction,
                "intent": choice.intent,
                "price": format_decimal(choice.bid_price),
                "quantity": format_decimal(choice.quantity),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    save_state(state_path, state)


def choose_market_for_signal(
    executor: PolymarketUSExecutor,
    markets: list[dict[str, Any]],
    signal: MomentumSignal,
    args: argparse.Namespace,
) -> MarketChoice | None:
    candidates = []
    now = datetime.now(timezone.utc)
    for market in markets:
        if market_symbol(market) != signal.symbol:
            continue
        if market_direction(market) != signal.direction:
            continue
        if not is_tradeable_market(market):
            continue

        seconds_remaining = seconds_until_close(market, now)
        if seconds_remaining is not None and seconds_remaining <= args.min_seconds_remaining:
            logging.info("Skip %s: only %ss remaining", market.get("slug"), seconds_remaining)
            continue

        current_price = side_price_for_direction(market, signal.direction)
        if current_price is None or current_price > args.max_entry_price:
            continue

        bid_price = entry_price(executor, market, signal.direction, current_price, args)
        if bid_price is None or bid_price > args.max_entry_price:
            continue

        quantity = quantize_quantity(args.bid_dollars / bid_price)
        if quantity <= 0:
            continue

        candidates.append(
            MarketChoice(
                market_slug=str(market.get("slug") or ""),
                market_id=str(market.get("id") or ""),
                question=str(market.get("question") or market.get("title") or ""),
                symbol=signal.symbol,
                direction=signal.direction,
                intent=intent_for_direction(market, signal.direction),
                current_price=current_price,
                bid_price=bid_price,
                quantity=quantity,
                seconds_remaining=seconds_remaining,
            )
        )

    if not candidates:
        return None
    return min(
        candidates,
        key=lambda choice: (
            choice.seconds_remaining if choice.seconds_remaining is not None else 999999,
            choice.current_price,
        ),
    )


def synthetic_market_choice(signal: MomentumSignal, args: argparse.Namespace) -> MarketChoice:
    price = quantize_price(args.sim_synthetic_price, DEFAULT_TICK_SIZE)
    quantity = quantize_quantity(args.bid_dollars / price)
    bucket = int(signal.ts // args.sim_synthetic_duration_seconds)
    return MarketChoice(
        market_slug=f"sim-{signal.symbol.lower()}-{signal.direction.lower()}-{bucket}",
        market_id=f"sim-{bucket}",
        question=f"SIM: {signal.symbol} {signal.direction} over next 5 minutes?",
        symbol=signal.symbol,
        direction=signal.direction,
        intent="ORDER_INTENT_BUY_LONG",
        current_price=price,
        bid_price=price,
        quantity=quantity,
        seconds_remaining=args.sim_synthetic_duration_seconds,
    )


def entry_price(
    executor: PolymarketUSExecutor,
    market: dict[str, Any],
    direction: str,
    current_price: Decimal,
    args: argparse.Namespace,
) -> Decimal | None:
    tick = market_tick_size(market)
    if args.entry_mode == "taker":
        return quantize_price(current_price, tick)

    bbo = executor.bbo(str(market.get("slug") or ""))
    best_bid = bbo_price(bbo, "bestBid")
    if best_bid is not None:
        return quantize_price(min(best_bid + tick, current_price), tick)
    return quantize_price(current_price * Decimal("0.99"), tick)


def rest_momentum_signal(symbol: str, args: argparse.Namespace) -> MomentumSignal | None:
    # Binance /api/v3/ticker/price only gives now; use recent klines for once-mode.
    interval = "1m"
    limit = max(2, min(10, (args.window_seconds // 60) + 2))
    payload = fetch_klines(symbol, interval, limit, args.price_source)
    if not payload:
        return None
    oldest = Decimal(str(payload[0][1]))
    latest = Decimal(str(payload[-1][4]))
    pct_move = (latest - oldest) / oldest
    if pct_move == 0 or abs(pct_move) < args.threshold_pct:
        return None
    return MomentumSignal(
        symbol=symbol,
        direction="UP" if pct_move > 0 else "DOWN",
        pct_move=pct_move,
        price=latest,
        window_seconds=args.window_seconds,
        ts=time.time(),
    )


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int,
    price_source: str,
) -> list[Any]:
    urls = []
    if price_source in {"auto", "binance"}:
        urls.append(BINANCE_REST_KLINES_URL)
    if price_source in {"auto", "binance-us"}:
        urls.append(BINANCE_US_REST_KLINES_URL)

    last_error = None
    for url in urls:
        try:
            return http_get_json(url, {"symbol": symbol, "interval": interval, "limit": limit})
        except requests.HTTPError as exc:
            last_error = exc
            logging.warning("Kline source failed %s for %s: %s", url, symbol, exc)
    if last_error:
        raise last_error
    return []


def stream_base_url(args: argparse.Namespace) -> str:
    if args.price_source == "binance":
        return BINANCE_WS_URL
    return BINANCE_US_WS_URL


def cancel_old_orders(
    executor: PolymarketUSExecutor,
    state: dict[str, Any],
    max_order_age_seconds: int,
) -> None:
    now = datetime.now(timezone.utc)
    kept = []
    for order in state.get("orders", []):
        created_at = parse_datetime(order.get("created_at"))
        age = (now - created_at).total_seconds() if created_at else max_order_age_seconds + 1
        if age > max_order_age_seconds:
            executor.cancel_order(str(order.get("order_id") or ""), str(order.get("market_slug") or ""))
        else:
            kept.append(order)
    state["orders"] = kept


def coinbase_binance_in_line(
    symbol: str,
    binance_price: Decimal,
    threshold: Decimal,
) -> bool:
    product = coinbase_product(symbol)
    if product is None:
        return True
    try:
        payload = http_get_json(COINBASE_SPOT_URL.format(product=product))
    except Exception as exc:  # noqa: BLE001
        logging.warning("Coinbase spot check failed for %s: %s", product, exc)
        return False

    coinbase_price = to_decimal((payload.get("data") or {}).get("amount"))
    if coinbase_price is None:
        return False
    divergence = abs(binance_price - coinbase_price) / binance_price
    logging.info("Coinbase/Binance divergence %s", format_percent(divergence))
    return divergence <= threshold


def coinbase_product(symbol: str) -> str | None:
    if symbol == "BTCUSDT":
        return "BTC-USD"
    if symbol == "ETHUSDT":
        return "ETH-USD"
    return None


def market_symbol(market: dict[str, Any]) -> str | None:
    text = searchable_text(market)
    if re.search(r"\b(BTC|Bitcoin)\b", text, flags=re.IGNORECASE):
        return "BTCUSDT"
    if re.search(r"\b(ETH|Ethereum)\b", text, flags=re.IGNORECASE):
        return "ETHUSDT"
    return None


def market_direction(market: dict[str, Any]) -> str | None:
    text = searchable_text(market)
    if re.search(r"\b(up|higher|above|rise|green)\b", text, flags=re.IGNORECASE):
        return "UP"
    if re.search(r"\b(down|lower|below|fall|red)\b", text, flags=re.IGNORECASE):
        return "DOWN"

    for side in market.get("marketSides") or []:
        side_text = searchable_text(side)
        if re.search(r"\b(up|higher|above|rise|green)\b", side_text, flags=re.IGNORECASE):
            return "UP"
        if re.search(r"\b(down|lower|below|fall|red)\b", side_text, flags=re.IGNORECASE):
            return "DOWN"
    return None


def side_price_for_direction(market: dict[str, Any], direction: str | None) -> Decimal | None:
    if direction is None:
        return None

    for side in market.get("marketSides") or []:
        if market_side_direction(side) == direction:
            return to_decimal(side.get("price"))

    for key in ("bestAsk", "lastTradePrice", "price"):
        price = amount_value(market.get(key))
        if price is not None:
            return price
    return None


def market_side_direction(side: dict[str, Any]) -> str | None:
    text = searchable_text(side)
    if re.search(r"\b(up|higher|above|rise|green)\b", text, flags=re.IGNORECASE):
        return "UP"
    if re.search(r"\b(down|lower|below|fall|red)\b", text, flags=re.IGNORECASE):
        return "DOWN"
    return None


def intent_for_direction(market: dict[str, Any], direction: str) -> str:
    for side in market.get("marketSides") or []:
        if market_side_direction(side) == direction:
            return "ORDER_INTENT_BUY_LONG" if side.get("long", True) else "ORDER_INTENT_BUY_SHORT"
    return "ORDER_INTENT_BUY_LONG"


def is_tradeable_market(market: dict[str, Any]) -> bool:
    if market.get("closed") is True or market.get("active") is False:
        return False
    if market.get("archived") is True or market.get("hidden") is True:
        return False
    if not market.get("slug"):
        return False
    text = searchable_text(market).lower()
    return "5" in text and ("minute" in text or "min" in text)


def seconds_until_close(market: dict[str, Any], now: datetime) -> int | None:
    for key in ("endDate", "endTime", "closeTime", "resolutionTime"):
        dt = parse_datetime(market.get(key))
        if dt is not None:
            return int((dt - now).total_seconds())
    return None


def market_tick_size(market: dict[str, Any]) -> Decimal:
    return (
        to_decimal(market.get("orderPriceMinTickSize"))
        or to_decimal(market.get("minimumTickSize"))
        or to_decimal(market.get("tickSize"))
        or DEFAULT_TICK_SIZE
    )


def bbo_price(bbo: dict[str, Any] | None, field: str) -> Decimal | None:
    if not bbo:
        return None
    data = bbo.get("marketData") if isinstance(bbo, dict) else None
    if not isinstance(data, dict):
        data = bbo
    return amount_value(data.get(field))


def amount_value(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        return to_decimal(value.get("value"))
    return to_decimal(value)


def unpack_markets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        markets = payload.get("markets") or payload.get("data") or []
        return markets if isinstance(markets, list) else []
    return payload if isinstance(payload, list) else []


def searchable_text(obj: dict[str, Any]) -> str:
    fields = [
        obj.get("title"),
        obj.get("question"),
        obj.get("slug"),
        obj.get("description"),
        obj.get("subtitle"),
        obj.get("outcome"),
        obj.get("label"),
        obj.get("name"),
        obj.get("identifier"),
    ]
    for key in ("tags", "marketSides", "outcomes"):
        value = obj.get(key)
        if isinstance(value, list):
            fields.extend(searchable_text(item) if isinstance(item, dict) else str(item) for item in value)
    return " ".join(str(field) for field in fields if field)


def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(
        url,
        params=params,
        timeout=15,
        headers={"User-Agent": "crypto-momentum-bot/1.0"},
    )
    response.raise_for_status()
    return response.json()


def parse_symbols(value: str) -> list[str]:
    return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]


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
        return {"orders": []}
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
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
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        decimal = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if decimal.is_nan():
        return None
    return decimal


def quantize_price(price: Decimal, tick_size: Decimal) -> Decimal:
    bounded = min(max(price, MIN_PRICE), MAX_PRICE)
    ticks = (bounded / tick_size).to_integral_value(rounding=ROUND_DOWN)
    return max(ticks * tick_size, MIN_PRICE).quantize(tick_size)


def quantize_quantity(quantity: Decimal) -> Decimal:
    return quantity.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def format_percent(value: Decimal) -> str:
    return f"{format_decimal(value * Decimal('100'))}%"


def extract_order_id(response: Any) -> str | None:
    if isinstance(response, dict):
        return response.get("id") or response.get("orderId") or response.get("orderID")
    for attr in ("id", "orderId", "orderID"):
        order_id = getattr(response, attr, None)
        if order_id:
            return str(order_id)
    return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
