#!/usr/bin/env python3
"""
Polymarket wallet counter-trade scanner.

The idea is deliberately split into two lanes:
  - Offline: rank wallets by resolved trade win rate and paper-test fixed-size
    fades.
  - Hot path: consume fresh fill rows from a fast/free listener, then emit
    counter-order intents with strict price and exposure guards.

This script does not require PolymarketData. It accepts CSV, JSON, or JSONL
exports and can also read live JSONL fills from stdin.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable


STATE_PATH = Path(__file__).with_suffix(".state.json")
DEFAULT_FIXED_DOLLARS = Decimal("25")
DEFAULT_MIN_PRICE = Decimal("0.30")
DEFAULT_MAX_PRICE = Decimal("0.70")
DEFAULT_MAX_SLIPPAGE = Decimal("0.01")
DEFAULT_MAX_EXPOSURE = Decimal("75")
DEFAULT_TAKE_PROFIT_PCT = Decimal("0.20")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")

WALLET_KEYS = (
    "wallet",
    "user",
    "proxyWallet",
    "profileProxyWallet",
    "trader",
    "account",
    "owner",
    "address",
)
MARKET_KEYS = ("market_id", "marketId", "conditionId", "condition_id", "market", "slug", "marketSlug")
OUTCOME_KEYS = ("outcome", "outcomeName", "assetOutcome", "tokenOutcome", "position", "answer")
ACTION_KEYS = ("action", "tradeSide", "orderSide", "side", "type")
PRICE_KEYS = ("price", "avgPrice", "averagePrice", "matchedPrice", "fillPrice")
SIZE_KEYS = ("size", "shares", "quantity", "amount", "filledAmount")
NOTIONAL_KEYS = ("notional", "value", "usdcSize", "cashAmount", "dollarAmount")
TX_KEYS = ("tx", "txHash", "transactionHash", "hash", "tradeId", "id", "orderHash")
TIMESTAMP_KEYS = ("timestamp", "createdAt", "created_at", "time", "datetime", "date")


@dataclass(frozen=True)
class Fill:
    raw: dict[str, Any]
    wallet: str
    market_id: str
    outcome: str
    action: str | None
    price: Decimal | None
    size: Decimal | None
    notional: Decimal | None
    timestamp: datetime | None
    tx_id: str | None
    win: bool | None

    @property
    def binary_outcome(self) -> str | None:
        value = self.outcome.strip().upper()
        if value in {"YES", "Y", "TRUE", "LONG"}:
            return "YES"
        if value in {"NO", "N", "FALSE", "SHORT"}:
            return "NO"
        return None

    @property
    def is_buy(self) -> bool:
        if self.action is None:
            return False
        return self.action.upper() in {"BUY", "BOUGHT", "BID", "TAKER_BUY"}


@dataclass(frozen=True)
class WalletStats:
    wallet: str
    decisions: int
    wins: int
    losses: int
    win_rate: Decimal
    fade_win_rate: Decimal
    volume: Decimal
    median_price: Decimal | None
    mid_price_share: Decimal


@dataclass(frozen=True)
class CounterOrder:
    source_wallet: str
    source_tx: str | None
    market_id: str
    source_outcome: str
    counter_outcome: str
    source_price: Decimal
    entry_price: Decimal
    quantity: Decimal
    notional: Decimal
    existing_exposure: Decimal
    exposure_after: Decimal
    take_profit_pct: Decimal
    needs_quote_check: bool
    reason: str


class CountertradeError(RuntimeError):
    pass


def main() -> int:
    load_env_file(Path.cwd() / ".env")
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.command == "rank":
        return command_rank(args)
    if args.command == "signals":
        return command_signals(args)
    if args.command == "stream":
        return command_stream(args)
    if args.command == "paper":
        return command_paper(args)
    raise CountertradeError(f"Unknown command: {args.command}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find and fade persistently bad Polymarket wallets")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("COUNTERTRADE_LOG_LEVEL", "INFO").upper(),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rank = subparsers.add_parser("rank", help="rank wallets from resolved historical fills")
    add_input_args(rank, historical=True)
    rank.add_argument("--min-decisions", type=int, default=20)
    rank.add_argument("--min-volume", type=Decimal, default=Decimal("500"))
    rank.add_argument("--max-win-rate", type=Decimal, default=Decimal("0.30"))
    rank.add_argument("--unit", choices=["trade", "market"], default="market")
    rank.add_argument("--top", type=int, default=25)
    rank.add_argument("--json", action="store_true", help="print JSON instead of a table")
    rank.add_argument("--write-wallets", help="write selected wallets as a JSON target file")

    signals = subparsers.add_parser("signals", help="emit counter-order intents from recent fills")
    add_input_args(signals, historical=False)
    add_signal_args(signals)

    stream = subparsers.add_parser("stream", help="read live fill events as JSONL from stdin")
    add_signal_args(stream)

    paper = subparsers.add_parser("paper", help="paper-test fixed-size fades on resolved fills")
    add_input_args(paper, historical=True)
    paper.add_argument("--wallets-file", required=True)
    paper.add_argument("--fixed-dollars", type=Decimal, default=DEFAULT_FIXED_DOLLARS)
    paper.add_argument("--min-source-price", type=Decimal, default=DEFAULT_MIN_PRICE)
    paper.add_argument("--max-source-price", type=Decimal, default=DEFAULT_MAX_PRICE)
    paper.add_argument("--unit", choices=["trade", "market"], default="market")
    paper.add_argument("--json", action="store_true")

    return parser.parse_args()


def add_input_args(parser: argparse.ArgumentParser, historical: bool) -> None:
    parser.add_argument(
        "--fills-file",
        action="append",
        default=[],
        help="CSV, JSON, or JSONL fill export. Can be passed more than once.",
    )
    if not historical:
        parser.add_argument(
            "--since-minutes",
            type=int,
            default=60,
            help="ignore rows older than this many minutes when timestamps are present",
        )


def add_signal_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wallets-file", required=True, help="JSON wallet list from rank --write-wallets")
    parser.add_argument("--state", default=str(STATE_PATH), help="dedupe/exposure state file")
    parser.add_argument("--fixed-dollars", type=Decimal, default=DEFAULT_FIXED_DOLLARS)
    parser.add_argument("--min-source-price", type=Decimal, default=DEFAULT_MIN_PRICE)
    parser.add_argument("--max-source-price", type=Decimal, default=DEFAULT_MAX_PRICE)
    parser.add_argument("--max-slippage", type=Decimal, default=DEFAULT_MAX_SLIPPAGE)
    parser.add_argument("--max-exposure-per-market", type=Decimal, default=DEFAULT_MAX_EXPOSURE)
    parser.add_argument("--take-profit-pct", type=Decimal, default=DEFAULT_TAKE_PROFIT_PCT)
    parser.add_argument(
        "--assume-buy",
        action="store_true",
        help="treat rows without an explicit BUY/SELL field as buys",
    )
    parser.add_argument("--jsonl", action="store_true", help="print one JSON order per line")
    parser.add_argument("--write-orders", help="write emitted order intents as JSONL")


def command_rank(args: argparse.Namespace) -> int:
    fills = load_fills(args.fills_file)
    stats = rank_wallets(
        fills,
        min_decisions=args.min_decisions,
        min_volume=args.min_volume,
        max_win_rate=args.max_win_rate,
        unit=args.unit,
    )
    selected = stats[: args.top]
    if args.json:
        print(json.dumps([wallet_stats_to_json(row) for row in selected], indent=2))
    else:
        print_wallet_stats(selected)
    if args.write_wallets:
        write_wallet_targets(Path(args.write_wallets), selected, args)
        logging.info("Wrote %s target wallets to %s", len(selected), args.write_wallets)
    return 0


def command_signals(args: argparse.Namespace) -> int:
    targets = load_wallet_targets(Path(args.wallets_file))
    state_path = Path(args.state)
    state = load_state(state_path)
    rows = [
        fill
        for fill in load_fills(args.fills_file)
        if fill_is_recent(fill, args.since_minutes)
    ]
    orders = build_counter_orders(rows, targets, state, args)
    emit_orders(orders, args)
    save_state(state_path, state)
    return 0


def command_stream(args: argparse.Namespace) -> int:
    targets = load_wallet_targets(Path(args.wallets_file))
    state_path = Path(args.state)
    state = load_state(state_path)
    output_handle = open(args.write_orders, "a", encoding="utf-8") if args.write_orders else None
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                fill = normalize_fill(raw)
            except Exception as exc:  # noqa: BLE001 - live input can be messy.
                logging.warning("Skipping malformed stream row: %s", exc)
                continue
            orders = build_counter_orders([fill], targets, state, args)
            for order in orders:
                text = json.dumps(counter_order_to_json(order), sort_keys=True)
                if args.jsonl:
                    print(text, flush=True)
                else:
                    print_order(order)
                if output_handle:
                    output_handle.write(text + "\n")
                    output_handle.flush()
            if orders:
                save_state(state_path, state)
    finally:
        if output_handle:
            output_handle.close()
    return 0


def command_paper(args: argparse.Namespace) -> int:
    targets = load_wallet_targets(Path(args.wallets_file))
    fills = load_fills(args.fills_file)
    trades = historical_units(fills, unit=args.unit)
    rows = []
    for fill in trades:
        if fill.wallet.casefold() not in targets:
            continue
        if fill.win is None or fill.price is None:
            continue
        binary_outcome = fill.binary_outcome
        if binary_outcome is None:
            continue
        if fill.price < args.min_source_price or fill.price > args.max_source_price:
            continue
        entry_price = quantize_price(Decimal("1") - fill.price)
        quantity = quantize_quantity(args.fixed_dollars / entry_price)
        fade_won = not fill.win
        pnl = (Decimal("1") - entry_price) * quantity if fade_won else -entry_price * quantity
        rows.append(
            {
                "wallet": fill.wallet,
                "market_id": fill.market_id,
                "source_outcome": binary_outcome,
                "counter_outcome": opposite_outcome(binary_outcome),
                "source_price": format_decimal(fill.price),
                "entry_price": format_decimal(entry_price),
                "quantity": format_decimal(quantity),
                "source_won": fill.win,
                "fade_won": fade_won,
                "pnl": format_decimal(quantize_money(pnl)),
            }
        )
    summary = summarize_paper(rows)
    if args.json:
        print(json.dumps({"summary": summary, "samples": rows[-25:]}, indent=2))
    else:
        print_paper_summary(summary)
    return 0


def load_fills(paths: list[str]) -> list[Fill]:
    fills: list[Fill] = []
    for path_text in paths:
        for row in load_rows(Path(path_text)):
            try:
                fills.append(normalize_fill(row))
            except CountertradeError as exc:
                logging.debug("Skipping row from %s: %s", path_text, exc)
    return fills


def load_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        return [dict(row) for row in csv.DictReader(text.splitlines())]
    stripped = text.lstrip()
    if stripped.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise CountertradeError(f"{path} JSON root must be a list")
        return [row for row in payload if isinstance(row, dict)]
    rows = []
    for line in text.splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def normalize_fill(row: dict[str, Any]) -> Fill:
    wallet = first_text(row, WALLET_KEYS)
    market_id = first_text(row, MARKET_KEYS)
    outcome = infer_outcome(row)
    if not wallet:
        raise CountertradeError("missing wallet")
    if not market_id:
        raise CountertradeError("missing market id")
    if not outcome:
        raise CountertradeError("missing outcome")
    price = first_decimal(row, PRICE_KEYS)
    size = first_decimal(row, SIZE_KEYS)
    notional = first_decimal(row, NOTIONAL_KEYS)
    if notional is None and price is not None and size is not None:
        notional = price * size
    return Fill(
        raw=row,
        wallet=normalize_wallet(wallet),
        market_id=str(market_id),
        outcome=outcome,
        action=infer_action(row),
        price=price,
        size=size,
        notional=notional,
        timestamp=infer_timestamp(row),
        tx_id=first_text(row, TX_KEYS),
        win=infer_win(row, outcome),
    )


def infer_outcome(row: dict[str, Any]) -> str | None:
    value = first_text(row, OUTCOME_KEYS)
    if value:
        return normalize_outcome(value)
    side = row.get("side")
    if isinstance(side, str) and side.strip().upper() in {"YES", "NO"}:
        return normalize_outcome(side)
    return None


def infer_action(row: dict[str, Any]) -> str | None:
    for key in ACTION_KEYS:
        value = row.get(key)
        if not isinstance(value, str):
            continue
        cleaned = value.strip().upper()
        if cleaned in {"BUY", "BOUGHT", "BID", "TAKER_BUY"}:
            return "BUY"
        if cleaned in {"SELL", "SOLD", "ASK", "TAKER_SELL"}:
            return "SELL"
    return None


def infer_timestamp(row: dict[str, Any]) -> datetime | None:
    value = first_value(row, TIMESTAMP_KEYS)
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.fromtimestamp(ts, timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return infer_timestamp({"timestamp": int(raw)})
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def infer_win(row: dict[str, Any], outcome: str) -> bool | None:
    for key in ("win", "won", "resolvedWin", "outcomeWon", "isWin"):
        value = row.get(key)
        parsed = parse_bool(value)
        if parsed is not None:
            return parsed

    for key in ("pnl", "profit", "realizedPnl", "realizedPNL"):
        value = to_decimal(row.get(key))
        if value is not None:
            return value > 0

    settlement = first_decimal(row, ("settlementPrice", "settledPrice", "redeemPrice", "payout"))
    if settlement is not None:
        return settlement >= Decimal("0.99")

    winner = first_text(row, ("winningOutcome", "winner", "resolution", "resolvedOutcome"))
    if winner:
        return normalize_outcome(winner).casefold() == normalize_outcome(outcome).casefold()
    return None


def rank_wallets(
    fills: list[Fill],
    min_decisions: int,
    min_volume: Decimal,
    max_win_rate: Decimal,
    unit: str,
) -> list[WalletStats]:
    by_wallet: dict[str, list[Fill]] = defaultdict(list)
    for fill in historical_units(fills, unit=unit):
        if fill.win is None:
            continue
        by_wallet[fill.wallet].append(fill)

    stats = []
    for wallet, wallet_fills in by_wallet.items():
        decisions = len(wallet_fills)
        wins = sum(1 for fill in wallet_fills if fill.win is True)
        losses = decisions - wins
        volume = sum((fill.notional or Decimal("0")) for fill in wallet_fills)
        if decisions == 0:
            continue
        win_rate = Decimal(wins) / Decimal(decisions)
        mid_count = sum(
            1
            for fill in wallet_fills
            if fill.price is not None and DEFAULT_MIN_PRICE <= fill.price <= DEFAULT_MAX_PRICE
        )
        prices = sorted(fill.price for fill in wallet_fills if fill.price is not None)
        row = WalletStats(
            wallet=wallet,
            decisions=decisions,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            fade_win_rate=Decimal("1") - win_rate,
            volume=volume,
            median_price=median_decimal(prices),
            mid_price_share=Decimal(mid_count) / Decimal(decisions),
        )
        if row.decisions >= min_decisions and row.volume >= min_volume and row.win_rate <= max_win_rate:
            stats.append(row)
    return sorted(
        stats,
        key=lambda row: (row.fade_win_rate, row.decisions, row.volume),
        reverse=True,
    )


def historical_units(fills: list[Fill], unit: str) -> list[Fill]:
    if unit == "trade":
        return list(fills)
    by_key: dict[tuple[str, str, str], Fill] = {}
    volume_by_key: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for fill in fills:
        key = (fill.wallet, fill.market_id, fill.outcome.casefold())
        if key not in by_key:
            by_key[key] = fill
        if fill.notional is not None:
            volume_by_key[key] += fill.notional
    aggregated = []
    for key, fill in by_key.items():
        if volume_by_key[key] and fill.notional != volume_by_key[key]:
            raw = dict(fill.raw)
            raw["notional"] = format_decimal(volume_by_key[key])
            aggregated.append(
                Fill(
                    raw=raw,
                    wallet=fill.wallet,
                    market_id=fill.market_id,
                    outcome=fill.outcome,
                    action=fill.action,
                    price=fill.price,
                    size=fill.size,
                    notional=volume_by_key[key],
                    timestamp=fill.timestamp,
                    tx_id=fill.tx_id,
                    win=fill.win,
                )
            )
        else:
            aggregated.append(fill)
    return aggregated


def build_counter_orders(
    fills: Iterable[Fill],
    targets: set[str],
    state: dict[str, Any],
    args: argparse.Namespace,
) -> list[CounterOrder]:
    orders = []
    for fill in fills:
        dedupe_key = fill_dedupe_key(fill)
        order = build_counter_order(fill, targets, state, args, dedupe_key)
        if order is None:
            continue
        orders.append(order)
        mark_state_for_order(state, order, dedupe_key)
    return orders


def build_counter_order(
    fill: Fill,
    targets: set[str],
    state: dict[str, Any],
    args: argparse.Namespace,
    dedupe_key: str | None = None,
) -> CounterOrder | None:
    if fill.wallet.casefold() not in targets:
        return None
    if not fill.is_buy and not args.assume_buy:
        logging.debug("Skip %s: not an explicit buy", fill.tx_id or fill.market_id)
        return None
    source_outcome = fill.binary_outcome
    if source_outcome is None:
        logging.debug("Skip %s: non-binary outcome %s", fill.tx_id or fill.market_id, fill.outcome)
        return None
    if fill.price is None:
        return None
    if fill.price < args.min_source_price or fill.price > args.max_source_price:
        return None

    dedupe_key = dedupe_key or fill_dedupe_key(fill)
    if dedupe_key in set(state.get("seen_fills") or []):
        return None

    fair_counter_price = quantize_price(Decimal("1") - fill.price)
    quote_price = counter_quote_price(fill.raw, opposite_outcome(source_outcome))
    needs_quote_check = quote_price is None
    entry_price = quote_price or fair_counter_price
    if entry_price > fair_counter_price + args.max_slippage:
        logging.info(
            "Skip %s: counter quote %s exceeds fair %s + slippage %s",
            dedupe_key,
            format_decimal(entry_price),
            format_decimal(fair_counter_price),
            format_decimal(args.max_slippage),
        )
        return None

    notional = quantize_money(args.fixed_dollars)
    quantity = quantize_quantity(notional / entry_price)
    exposure = market_exposure(state, fill.market_id)
    exposure_after = exposure + notional
    if exposure_after > args.max_exposure_per_market:
        logging.info(
            "Skip %s: exposure %s would exceed cap %s",
            fill.market_id,
            format_decimal(exposure_after),
            format_decimal(args.max_exposure_per_market),
        )
        state.setdefault("seen_fills", []).append(dedupe_key)
        return None

    return CounterOrder(
        source_wallet=fill.wallet,
        source_tx=fill.tx_id,
        market_id=fill.market_id,
        source_outcome=source_outcome,
        counter_outcome=opposite_outcome(source_outcome),
        source_price=quantize_price(fill.price),
        entry_price=quantize_price(entry_price),
        quantity=quantity,
        notional=notional,
        existing_exposure=exposure,
        exposure_after=exposure_after,
        take_profit_pct=args.take_profit_pct,
        needs_quote_check=needs_quote_check,
        reason="fade_low_win_wallet",
    )


def counter_quote_price(row: dict[str, Any], counter_outcome: str) -> Decimal | None:
    prefix = "yes" if counter_outcome == "YES" else "no"
    keys = (
        f"{prefix}Ask",
        f"{prefix}AskPrice",
        f"{prefix}_ask",
        f"{prefix}_ask_price",
        "counterAsk",
        "counterAskPrice",
        "oppositeAsk",
        "oppositeAskPrice",
    )
    return first_decimal(row, keys)


def fill_is_recent(fill: Fill, since_minutes: int) -> bool:
    if since_minutes <= 0 or fill.timestamp is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    return fill.timestamp >= cutoff


def fill_dedupe_key(fill: Fill) -> str:
    if fill.tx_id:
        return f"tx:{fill.tx_id}:{fill.wallet}:{fill.market_id}:{fill.outcome}"
    price = format_decimal(fill.price or Decimal("0"))
    size = format_decimal(fill.size or Decimal("0"))
    ts = fill.timestamp.isoformat() if fill.timestamp else ""
    return f"fill:{fill.wallet}:{fill.market_id}:{fill.outcome}:{price}:{size}:{ts}"


def market_exposure(state: dict[str, Any], market_id: str) -> Decimal:
    exposure = state.setdefault("market_exposure", {})
    return to_decimal(exposure.get(market_id)) or Decimal("0")


def mark_state_for_order(state: dict[str, Any], order: CounterOrder, dedupe_key: str) -> None:
    seen = state.setdefault("seen_fills", [])
    seen.append(dedupe_key)
    exposure = state.setdefault("market_exposure", {})
    exposure[order.market_id] = format_decimal(order.exposure_after)
    emitted = state.setdefault("orders", [])
    emitted.append({**counter_order_to_json(order), "created_at": datetime.now(timezone.utc).isoformat()})


def load_wallet_targets(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    wallets = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                wallets.append(item)
            elif isinstance(item, dict) and item.get("wallet"):
                wallets.append(str(item["wallet"]))
    elif isinstance(payload, dict):
        values = payload.get("wallets") or payload.get("targets") or []
        for item in values:
            if isinstance(item, str):
                wallets.append(item)
            elif isinstance(item, dict) and item.get("wallet"):
                wallets.append(str(item["wallet"]))
    return {normalize_wallet(wallet).casefold() for wallet in wallets if wallet}


def write_wallet_targets(path: Path, stats: list[WalletStats], args: argparse.Namespace) -> None:
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection": {
            "min_decisions": args.min_decisions,
            "min_volume": str(args.min_volume),
            "max_win_rate": str(args.max_win_rate),
            "unit": args.unit,
        },
        "wallets": [wallet_stats_to_json(row) for row in stats],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def emit_orders(orders: list[CounterOrder], args: argparse.Namespace) -> None:
    output_handle = open(args.write_orders, "w", encoding="utf-8") if args.write_orders else None
    try:
        if args.jsonl:
            for order in orders:
                text = json.dumps(counter_order_to_json(order), sort_keys=True)
                print(text)
                if output_handle:
                    output_handle.write(text + "\n")
        else:
            print(f"Counter orders: {len(orders)}")
            for order in orders:
                print_order(order)
                if output_handle:
                    output_handle.write(json.dumps(counter_order_to_json(order), sort_keys=True) + "\n")
    finally:
        if output_handle:
            output_handle.close()


def print_wallet_stats(rows: list[WalletStats]) -> None:
    print(
        f"{'wallet':<44} {'dec':>5} {'wins':>5} {'loss':>5} "
        f"{'win%':>7} {'fade%':>7} {'volume':>12} {'med_px':>7} {'30-70%':>7}"
    )
    for row in rows:
        print(
            f"{short_wallet(row.wallet):<44} "
            f"{row.decisions:>5} {row.wins:>5} {row.losses:>5} "
            f"{fmt_pct(row.win_rate):>7} {fmt_pct(row.fade_win_rate):>7} "
            f"{fmt_usd(row.volume):>12} "
            f"{format_decimal(row.median_price) if row.median_price is not None else 'N/A':>7} "
            f"{fmt_pct(row.mid_price_share):>7}"
        )


def print_order(order: CounterOrder) -> None:
    quote_flag = " quote-check" if order.needs_quote_check else ""
    print(
        f"{short_wallet(order.source_wallet)} fade {order.source_outcome}->{order.counter_outcome} "
        f"{order.market_id} @ {format_decimal(order.entry_price)} "
        f"qty={format_decimal(order.quantity)} notional=${format_decimal(order.notional)} "
        f"exposure=${format_decimal(order.exposure_after)} tp={fmt_pct(order.take_profit_pct)}{quote_flag}"
    )


def print_paper_summary(summary: dict[str, Any]) -> None:
    print("Paper fade summary")
    print(f"trades:    {summary['trades']}")
    print(f"wins:      {summary['wins']}")
    print(f"losses:    {summary['losses']}")
    print(f"win rate:  {summary['win_rate']}")
    print(f"pnl:       {summary['pnl']}")
    print(f"roi:       {summary['roi']}")


def wallet_stats_to_json(row: WalletStats) -> dict[str, Any]:
    return {
        "wallet": row.wallet,
        "decisions": row.decisions,
        "wins": row.wins,
        "losses": row.losses,
        "win_rate": format_decimal(row.win_rate),
        "fade_win_rate": format_decimal(row.fade_win_rate),
        "volume": format_decimal(quantize_money(row.volume)),
        "median_price": format_decimal(row.median_price) if row.median_price is not None else None,
        "mid_price_share": format_decimal(row.mid_price_share),
    }


def counter_order_to_json(order: CounterOrder) -> dict[str, Any]:
    return {
        "mode": "counter",
        "source_wallet": order.source_wallet,
        "source_tx": order.source_tx,
        "market_id": order.market_id,
        "source_outcome": order.source_outcome,
        "outcome": order.counter_outcome,
        "source_price": format_decimal(order.source_price),
        "limit_price": format_decimal(order.entry_price),
        "quantity": format_decimal(order.quantity),
        "notional": format_decimal(order.notional),
        "existing_exposure": format_decimal(order.existing_exposure),
        "exposure_after": format_decimal(order.exposure_after),
        "take_profit_pct": format_decimal(order.take_profit_pct),
        "needs_quote_check": order.needs_quote_check,
        "reason": order.reason,
    }


def summarize_paper(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = sum((to_decimal(row.get("pnl")) or Decimal("0")) for row in rows)
    wins = sum(1 for row in rows if row.get("fade_won") is True)
    losses = len(rows) - wins
    cost = sum(
        (to_decimal(row.get("entry_price")) or Decimal("0"))
        * (to_decimal(row.get("quantity")) or Decimal("0"))
        for row in rows
    )
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": fmt_pct(Decimal(wins) / Decimal(len(rows))) if rows else "N/A",
        "pnl": fmt_signed_usd(pnl),
        "roi": fmt_pct(pnl / cost) if cost else "N/A",
    }


def first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    lower_map = {str(key).casefold(): value for key, value in row.items()}
    for key in keys:
        value = lower_map.get(key.casefold())
        if value not in (None, ""):
            return value
    return None


def first_text(row: dict[str, Any], keys: Iterable[str]) -> str | None:
    value = first_value(row, keys)
    if value in (None, ""):
        return None
    return str(value).strip()


def first_decimal(row: dict[str, Any], keys: Iterable[str]) -> Decimal | None:
    return to_decimal(first_value(row, keys))


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().casefold()
        if cleaned in {"true", "yes", "y", "1", "won", "win"}:
            return True
        if cleaned in {"false", "no", "n", "0", "lost", "loss"}:
            return False
    return None


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except Exception:  # noqa: BLE001 - Decimal raises several parse exceptions.
        return None
    if parsed.is_nan():
        return None
    return parsed


def median_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / Decimal("2")


def normalize_wallet(wallet: str) -> str:
    return wallet.strip()


def normalize_outcome(outcome: str) -> str:
    cleaned = outcome.strip()
    upper = cleaned.upper()
    if upper in {"Y", "TRUE", "LONG"}:
        return "YES"
    if upper in {"N", "FALSE", "SHORT"}:
        return "NO"
    return cleaned


def opposite_outcome(outcome: str) -> str:
    upper = outcome.upper()
    if upper == "YES":
        return "NO"
    if upper == "NO":
        return "YES"
    raise CountertradeError(f"Cannot invert non-binary outcome {outcome!r}")


def quantize_price(price: Decimal) -> Decimal:
    return min(max(price, MIN_PRICE), MAX_PRICE).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def quantize_quantity(quantity: Decimal) -> Decimal:
    return max(quantity, Decimal("0")).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return format(value.normalize(), "f")


def fmt_pct(value: Decimal) -> str:
    return f"{format_decimal((value * Decimal('100')).quantize(Decimal('0.01')))}%"


def fmt_usd(value: Decimal) -> str:
    value = quantize_money(value)
    return f"${format_decimal(value)}"


def fmt_signed_usd(value: Decimal) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${format_decimal(quantize_money(abs(value)))}"


def short_wallet(wallet: str) -> str:
    if len(wallet) <= 18:
        return wallet
    return f"{wallet[:10]}...{wallet[-8:]}"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_fills": [], "market_exposure": {}, "orders": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen_fills": [], "market_exposure": {}, "orders": []}
    if not isinstance(state, dict):
        return {"seen_fills": [], "market_exposure": {}, "orders": []}
    state.setdefault("seen_fills", [])
    state.setdefault("market_exposure", {})
    state.setdefault("orders", [])
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
        return value[1:-1]
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CountertradeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
