#!/usr/bin/env python3
"""
Polymarket weather market scanner.

Finds tomorrow's highest-temperature events, compares market bucket prices to
Open-Meteo forecasts, and ranks likely forecast/market disagreements.

This is intentionally a scanner, not an auto-trader. Weather resolution depends
on the exact source named in each market, so treat output as a research queue.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests


POLYMARKET_US_GATEWAY_URL = "https://gateway.polymarket.us"
POLYMARKET_US_EVENTS_URL = f"{POLYMARKET_US_GATEWAY_URL}/v1/events"
POLYMARKET_US_MARKETS_URL = f"{POLYMARKET_US_GATEWAY_URL}/v1/markets"
POLYMARKET_US_SEARCH_URL = f"{POLYMARKET_US_GATEWAY_URL}/v1/search"
POLYMARKETDATA_API_URL = "https://api.polymarketdata.co"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GFS_URL = "https://api.open-meteo.com/v1/gfs"
OPEN_METEO_ECMWF_URL = "https://api.open-meteo.com/v1/ecmwf"
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

DETERMINISTIC_MODEL_SPECS = [
    ("best_match", OPEN_METEO_FORECAST_URL, {}),
    ("noaa_gfs_seamless", OPEN_METEO_GFS_URL, {}),
    ("noaa_gfs_global", OPEN_METEO_GFS_URL, {"models": "gfs_global"}),
    ("noaa_gfs_025", OPEN_METEO_GFS_URL, {"models": "gfs025"}),
    ("noaa_gfs_013", OPEN_METEO_GFS_URL, {"models": "gfs013"}),
    ("ecmwf_ifs_hres", OPEN_METEO_ECMWF_URL, {}),
    ("ecmwf_ifs025", OPEN_METEO_ECMWF_URL, {"models": "ecmwf_ifs025"}),
    ("ecmwf_aifs025", OPEN_METEO_ECMWF_URL, {"models": "ecmwf_aifs025"}),
]

ENSEMBLE_MODEL_SPECS = [
    ("gfs_ensemble", {"models": "gfs_seamless"}),
    ("ecmwf_ensemble", {"models": "ecmwf_ifs025"}),
    ("icon_ensemble", {"models": "icon_seamless"}),
]

BACKTEST_MODEL_SPECS = [
    ("best_match", {}),
    ("gfs_global", {"models": "gfs_global"}),
    ("gfs_013", {"models": "gfs013"}),
    ("ecmwf_ifs025", {"models": "ecmwf_ifs025"}),
    ("ecmwf_aifs025", {"models": "ecmwf_aifs025"}),
]

DEFAULT_REFRESH_SECONDS = 30 * 60
DEFAULT_CACHE_SECONDS = 60 * 60
DEFAULT_TOP_DETAILED = 10
DEFAULT_MAX_EVENTS = 500
REQUEST_PAUSE_SECONDS = 0.2
STATE_DIR = Path(__file__).resolve().parent
WEATHER_CACHE_PATH = STATE_DIR / ".weather_cache.json"
WEATHER_STATE_PATH = STATE_DIR / ".weather_scanner.state.json"
WEATHER_SIM_STATE_PATH = STATE_DIR / ".weather_simulator.state.json"
WEATHER_DASHBOARD_PATH = STATE_DIR / "weather_dashboard.html"
WEATHER_BACKTEST_DASHBOARD_PATH = STATE_DIR / "weather_backtest_dashboard.html"
WEATHER_OPERATIONS_LOG_PATH = STATE_DIR / ".weather_operations.jsonl"
WEATHER_LOCATIONS_PATH = STATE_DIR / "weather_locations.json"
WEATHER_MARKET_HISTORY_PATH = STATE_DIR / "weather_market_history.jsonl"
DEFAULT_VALUE_THRESHOLD = Decimal("0.15")
DEFAULT_MAX_TRADE_SHARES = Decimal("2")
DEFAULT_MIN_TRADE_SHARES = Decimal("1")
DEFAULT_MAX_TRADES_PER_CYCLE = 3
DEFAULT_MAX_PENDING_TRADES_PER_CITY = 1
DEFAULT_ENSEMBLE_MODEL = "gfs_seamless"
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


CITY_COORDS = {
    "atlanta": (33.7490, -84.3880),
    "seoul": (37.5665, 126.9780),
    "shanghai": (31.2304, 121.4737),
    "wellington": (-41.2866, 174.7756),
    "london": (51.5074, -0.1278),
    "chicago": (41.8781, -87.6298),
    "nyc": (40.7128, -74.0060),
    "new-york-city": (40.7128, -74.0060),
    "tokyo": (35.6762, 139.6503),
    "buenos-aires": (-34.6037, -58.3816),
    "shenzhen": (22.5431, 114.0579),
    "singapore": (1.3521, 103.8198),
    "miami": (25.7617, -80.1918),
    "paris": (48.8566, 2.3522),
    "chongqing": (29.4316, 106.9123),
    "hong-kong": (22.3193, 114.1694),
    "ankara": (39.9334, 32.8597),
    "wuhan": (30.5928, 114.3055),
    "beijing": (39.9042, 116.4074),
    "warsaw": (52.2297, 21.0122),
    "seattle": (47.6062, -122.3321),
    "lucknow": (26.8467, 80.9462),
    "dallas": (32.7767, -96.7970),
    "madrid": (40.4168, -3.7038),
    "chengdu": (30.5728, 104.0668),
    "sao-paulo": (-23.5505, -46.6333),
    "toronto": (43.6532, -79.3832),
    "munich": (48.1351, 11.5820),
    "los-angeles": (34.0522, -118.2437),
    "tel-aviv": (32.0853, 34.7818),
    "milan": (45.4642, 9.1900),
    "taipei": (25.0330, 121.5654),
    "denver": (39.7392, -104.9903),
    "austin": (30.2672, -97.7431),
    "san-francisco": (37.7749, -122.4194),
    "houston": (29.7604, -95.3698),
    "las-vegas": (36.1699, -115.1398),
    "phoenix": (33.4484, -112.0740),
    "washington-dc": (38.9072, -77.0369),
    "philadelphia": (39.9526, -75.1652),
    "boston": (42.3601, -71.0589),
    "berlin": (52.5200, 13.4050),
    "rome": (41.9028, 12.4964),
    "amsterdam": (52.3676, 4.9041),
    "dubai": (25.2048, 55.2708),
    "istanbul": (41.0082, 28.9784),
    "mexico-city": (19.4326, -99.1332),
    "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631),
    "delhi": (28.6139, 77.2090),
    "mumbai": (19.0760, 72.8777),
    "bangkok": (13.7563, 100.5018),
    "guangzhou": (23.1291, 113.2644),
    "karachi": (24.8607, 67.0011),
    "busan": (35.1796, 129.0756),
    "kuala-lumpur": (3.1390, 101.6869),
    "cape-town": (-33.9249, 18.4241),
    "helsinki": (60.1699, 24.9384),
    "moscow": (55.7558, 37.6173),
    "jeddah": (21.4858, 39.1925),
    "manila": (14.5995, 120.9842),
    "qingdao": (36.0671, 120.3826),
    "panama-city": (8.9824, -79.5199),
    "auckland": (-36.8509, 174.7645),
}

POLYMARKET_US_WEATHER_CITY_CODES = {
    "nyc": "nyc",
    "sfo": "san-francisco",
    "mia": "miami",
    "mdw": "chicago",
    "lax": "los-angeles",
}

POLYMARKET_US_STATION_COORDS = {
    # Official Polymarket US weather settlements use NWS CLI station reports.
    "nyc": (40.7789, -73.9692),  # KNYC, Central Park
    "san-francisco": (37.6213, -122.3790),  # KSFO
    "miami": (25.7959, -80.2870),  # KMIA
    "chicago": (41.7868, -87.7522),  # KMDW
    "los-angeles": (33.9416, -118.4085),  # KLAX
}

POLYMARKET_US_STATION_INFO = {
    "nyc": {
        "station": "KNYC",
        "source": "CLINYC",
        "location": "Central Park",
        "city": "New York City",
    },
    "san-francisco": {
        "station": "KSFO",
        "source": "CLISFO",
        "location": "San Francisco International Airport",
        "city": "San Francisco",
    },
    "miami": {
        "station": "KMIA",
        "source": "CLIMIA",
        "location": "Miami International Airport",
        "city": "Miami",
    },
    "chicago": {
        "station": "KMDW",
        "source": "CLIMDW",
        "location": "Chicago Midway Airport",
        "city": "Chicago",
    },
    "los-angeles": {
        "station": "KLAX",
        "source": "CLILAX",
        "location": "Los Angeles International Airport",
        "city": "Los Angeles",
    },
}

POLYMARKETDATA_WEATHER_CITY_ALIASES = {
    "nyc": ["NYC", "New York City"],
    "san-francisco": ["San Francisco"],
    "miami": ["Miami"],
    "chicago": ["Chicago"],
    "los-angeles": ["Los Angeles"],
}

CITY_DISPLAY = {
    "nyc": "New York City",
    "new-york-city": "New York City",
    "buenos-aires": "Buenos Aires",
    "hong-kong": "Hong Kong",
    "sao-paulo": "Sao Paulo",
    "tel-aviv": "Tel Aviv",
    "san-francisco": "San Francisco",
    "los-angeles": "Los Angeles",
    "las-vegas": "Las Vegas",
    "washington-dc": "Washington DC",
    "mexico-city": "Mexico City",
    "kuala-lumpur": "Kuala Lumpur",
    "cape-town": "Cape Town",
    "panama-city": "Panama City",
}


@dataclass(frozen=True)
class Bucket:
    slug: str
    title: str
    yes_price: float
    no_price: float
    volume: float
    volume_24h: float
    best_bid: float | None
    best_ask: float | None
    current_price: float | None
    shares_traded: float
    open_interest: float
    bid_depth: int
    ask_depth: int
    token_ids: list[str]
    temp_c: float | None

    @property
    def display_price(self) -> float:
        return self.current_price if self.current_price is not None else self.yes_price

    @property
    def trade_price(self) -> float | None:
        return self.best_ask if self.best_ask is not None else self.yes_price

    @property
    def no_trade_price(self) -> float | None:
        if self.best_bid is not None:
            return max(0.0, 1.0 - self.best_bid)
        if self.no_price > 0:
            return self.no_price
        return None


@dataclass(frozen=True)
class MarketSnapshot:
    slug: str
    title: str
    total_volume: float
    volume_24h: float
    liquidity: float
    shares_traded: float
    open_interest: float
    buckets: list[Bucket]

    @property
    def favorite(self) -> Bucket | None:
        return max(self.buckets, key=lambda bucket: bucket.display_price, default=None)

    @property
    def hottest(self) -> Bucket | None:
        if not any(bucket.volume_24h > 0 or bucket.shares_traded > 0 for bucket in self.buckets):
            return None
        return max(
            self.buckets,
            key=lambda bucket: (bucket.volume_24h, bucket.shares_traded),
            default=None,
        )


@dataclass(frozen=True)
class Forecast:
    high_c: float
    low_c: float | None
    hourly_max_c: float | None
    current_temp_c: float | None
    timezone: str
    hourly: list[dict[str, Any]]


@dataclass(frozen=True)
class Ensemble:
    member_maxes_c: list[float]

    @property
    def mean_c(self) -> float:
        return statistics.fmean(self.member_maxes_c)

    @property
    def stdev_c(self) -> float:
        if len(self.member_maxes_c) < 2:
            return 0.0
        return statistics.pstdev(self.member_maxes_c)


@dataclass(frozen=True)
class ModelForecast:
    name: str
    kind: str
    high_c: float | None
    bucket_title: str | None
    member_count: int
    stdev_c: float | None
    error: str | None = None


@dataclass(frozen=True)
class Signal:
    city: str
    event: dict[str, Any]
    market: MarketSnapshot
    forecast: Forecast | None
    ensemble: Ensemble | None
    edge_c: float | None
    ensemble_edge_c: float | None
    forecast_bucket: Bucket | None
    ensemble_bucket: Bucket | None
    model_forecasts: list[ModelForecast]


@dataclass(frozen=True)
class TradeCandidate:
    city: str
    event_slug: str
    market_slug: str
    bucket_title: str
    side: str
    intent: str
    model_probability: Decimal
    ask_price: Decimal
    value_metric: Decimal
    quantity: Decimal


class WeatherScannerError(RuntimeError):
    pass


class WeatherTraderError(RuntimeError):
    pass


class WeatherTrader:
    def __init__(self, live: bool) -> None:
        self.live = live
        self.client = None
        if live:
            self._init_live_client()

    def _init_live_client(self) -> None:
        try:
            from polymarket_us import PolymarketUS  # type: ignore
        except ImportError as exc:
            raise WeatherTraderError(
                "Live mode requires the Polymarket US SDK. Install it with "
                "`python3 -m pip install polymarket-us`."
            ) from exc

        key_id = env_first("POLYMARKET_KEY_ID", "POLYMARKET_ACCESS_KEY_ID")
        secret_key = env_first("POLYMARKET_SECRET_KEY", "POLYMARKET_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise WeatherTraderError(
                "Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY before running --live."
            )

        self.client = PolymarketUS(
            key_id=key_id,
            secret_key=secret_key,
            timeout=float(os.getenv("POLYMARKET_TIMEOUT", "30")),
        )

    def place_limit_buy(self, candidate: TradeCandidate) -> str | None:
        order_params = {
            "marketSlug": candidate.market_slug,
            "intent": candidate.intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format_decimal(candidate.ask_price), "currency": "USD"},
            "quantity": float(candidate.quantity),
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

    def cancel_order(self, order_id: str, market_slug: str) -> bool:
        if not self.live:
            logging.info("DRY RUN cancel order %s in %s", order_id, market_slug)
            return True
        assert self.client is not None
        self.client.orders.cancel(order_id, {"marketSlug": market_slug})
        logging.info("Cancelled order %s in %s", order_id, market_slug)
        return True

    def close_position(self, market_slug: str, mark_price: Decimal, slippage_bips: int) -> str | None:
        params = {
            "marketSlug": market_slug,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "slippageTolerance": {
                "currentPrice": {"value": format_decimal(quantize_price(mark_price)), "currency": "USD"},
                "bips": max(slippage_bips, 0),
            },
        }
        if not self.live:
            logging.info("DRY RUN close position: %s", json.dumps(params, sort_keys=True))
            return None
        assert self.client is not None
        response = self.client.orders.close_position(params)
        order_id = extract_order_id(response)
        logging.info("Closed position for %s: %s", market_slug, response)
        return order_id

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


def normalize_response_list(response: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        values = response.get(key)
        if isinstance(values, list):
            return [value for value in values if isinstance(value, dict)]
        if isinstance(values, dict):
            return [value for value in values.values() if isinstance(value, dict)]
        return []
    values = getattr(response, key, None)
    if isinstance(values, list):
        return [value for value in values if isinstance(value, dict)]
    if isinstance(values, dict):
        return [value for value in values.values() if isinstance(value, dict)]
    return []


def normalize_response_mapping(response: Any, key: str) -> dict[str, Any]:
    if isinstance(response, dict):
        values = response.get(key)
        return values if isinstance(values, dict) else {}
    values = getattr(response, key, None)
    return values if isinstance(values, dict) else {}


class WeatherCache:
    def __init__(self, path: Path, max_age_seconds: int) -> None:
        self.path = path
        self.max_age_seconds = max_age_seconds
        self.data: dict[str, Any] = {}
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        if not self.path.exists():
            self.data = {}
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("Could not read weather cache at %s", self.path)
            self.data = {}
            return
        self.data = loaded if isinstance(loaded, dict) else {}

    def get(self, key: str) -> Any | None:
        self.load()
        entry = self.data.get(key)
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("_ts", 0)) > self.max_age_seconds:
            return None
        return entry.get("data")

    def set(self, key: str, value: Any) -> None:
        self.load()
        self.data[key] = {"_ts": time.time(), "data": value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)


def main() -> int:
    load_env_file()
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.self_test:
        run_self_test()
        return 0

    validation_error = validate_state_paths(args)
    if validation_error:
        logging.error("%s", validation_error)
        return 2

    try:
        load_extra_locations(Path(args.locations), required=args.locations_was_explicit)
    except WeatherScannerError as exc:
        logging.error("%s", exc)
        return 2

    if args.backtest:
        cache = WeatherCache(Path(args.cache), args.cache_seconds)
        session = requests.Session()
        session.headers.update({"User-Agent": "weather-market-scanner/1.0"})
        session.verify = not args.insecure
        if args.insecure:
            disable_insecure_request_warnings()
            logging.warning("TLS certificate verification is disabled for this run.")
        try:
            result = run_backtest(args, session, cache)
        except Exception:
            logging.exception("Backtest failed")
            return 1
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_backtest_report(result)
        if not args.no_dashboard:
            write_backtest_dashboard(args, result)
        return 0

    trader = WeatherTrader(live=args.live) if args.trade or args.live else None
    state_path = Path(args.state)
    cache = WeatherCache(Path(args.cache), args.cache_seconds)
    target_date = parse_target_date(args.date)
    session = requests.Session()
    session.headers.update({"User-Agent": "weather-market-scanner/1.0"})
    session.verify = not args.insecure
    if args.insecure:
        disable_insecure_request_warnings()
        logging.warning("TLS certificate verification is disabled for this run.")

    try:
        while True:
            try:
                signals = run_scan(args, session, cache, target_date)
                simulation_summary = None
                if args.json:
                    if args.simulate:
                        simulation_summary = run_simulation_cycle(
                            args, session, Path(args.simulation_state), signals, target_date, emit=False
                        )
                    payload: dict[str, Any] = {
                        "signals": [signal_to_json(signal) for signal in signals],
                    }
                    if args.trade or args.live:
                        payload["trade_candidates"] = [
                            trade_candidate_to_json(candidate)
                            for candidate in choose_trade_candidates(args, signals)
                        ]
                    if simulation_summary is not None:
                        payload["simulation"] = simulation_summary
                    print(json.dumps(payload, indent=2))
                else:
                    print_report(signals, target_date, args.top_detailed, args.min_edge)
                    if args.simulate:
                        run_simulation_cycle(
                            args, session, Path(args.simulation_state), signals, target_date, emit=True
                        )
                if args.record_market_history:
                    record_market_history(Path(args.market_history), signals, target_date)
                if trader is not None:
                    maybe_place_weather_trades(args, trader, session, state_path, signals, target_date)
                if not args.no_dashboard:
                    write_dashboard(args, session, signals, target_date)
            except Exception:
                logging.exception("Cycle failed")
                if args.once:
                    return 1

            if args.once:
                return 0
            time.sleep(args.refresh_seconds)
    finally:
        if trader is not None:
            trader.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan Polymarket weather markets for forecast edges.")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--backtest", action="store_true", help="backtest Open-Meteo previous-run models")
    parser.add_argument(
        "--backtest-start",
        help="backtest start date YYYY-MM-DD; defaults to 30 completed days ago",
    )
    parser.add_argument(
        "--backtest-end",
        help="backtest end date YYYY-MM-DD; defaults to yesterday",
    )
    parser.add_argument(
        "--backtest-days",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_BACKTEST_DAYS", "30")),
        help="completed days to backtest when start/end are omitted",
    )
    parser.add_argument(
        "--backtest-lead-days",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_BACKTEST_LEAD_DAYS", "1")),
        choices=[1, 2, 3, 4, 5, 6, 7],
        help="previous-run forecast lead day to score",
    )
    parser.add_argument(
        "--backtest-cities",
        default=os.getenv("WEATHER_SCANNER_BACKTEST_CITIES", ""),
        help="comma-separated city slugs; defaults to official Polymarket US weather cities",
    )
    parser.add_argument(
        "--backtest-bin-f",
        type=float,
        default=float(os.getenv("WEATHER_SCANNER_BACKTEST_BIN_F", "2")),
        help="Fahrenheit bucket width for model/bin hit-rate scoring",
    )
    parser.add_argument(
        "--backtest-dashboard",
        default=os.getenv("WEATHER_SCANNER_BACKTEST_DASHBOARD", str(WEATHER_BACKTEST_DASHBOARD_PATH)),
        help="HTML dashboard output path for backtest results",
    )
    parser.add_argument(
        "--backtest-trading",
        action="store_true",
        help="simulate trading P/L from a recorded Polymarket weather market-history JSONL file",
    )
    parser.add_argument(
        "--market-history-source",
        choices=["auto", "local", "polymarketdata"],
        default=os.getenv("WEATHER_SCANNER_MARKET_HISTORY_SOURCE", "auto"),
        help="historical market source for --backtest-trading",
    )
    parser.add_argument(
        "--market-history",
        default=os.getenv("WEATHER_SCANNER_MARKET_HISTORY", str(WEATHER_MARKET_HISTORY_PATH)),
        help="JSONL file of recorded Polymarket weather market snapshots",
    )
    parser.add_argument(
        "--polymarketdata-resolution",
        default=os.getenv("WEATHER_SCANNER_POLYMARKETDATA_RESOLUTION", "10m"),
        help="PolymarketData price-history resolution, limited by your plan",
    )
    parser.add_argument(
        "--polymarketdata-entry-hour-utc",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_POLYMARKETDATA_ENTRY_HOUR_UTC", "12")),
        choices=range(24),
        metavar="{0..23}",
        help="UTC hour used as the simulated historical entry time",
    )
    parser.add_argument(
        "--polymarketdata-window-hours",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_POLYMARKETDATA_WINDOW_HOURS", "8")),
        help="hours after the entry time to search for a historical price sample",
    )
    parser.add_argument(
        "--polymarketdata-max-markets-per-day",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_POLYMARKETDATA_MAX_MARKETS_PER_DAY", "12")),
        help="maximum bucket markets to price per city/day when using PolymarketData",
    )
    parser.add_argument(
        "--polymarketdata-request-pause",
        type=float,
        default=float(os.getenv("WEATHER_SCANNER_POLYMARKETDATA_REQUEST_PAUSE", "6.1")),
        help="seconds to pause after uncached PolymarketData requests to respect free-plan limits",
    )
    parser.add_argument(
        "--trade",
        action="store_true",
        help="evaluate and log a dry-run trade candidate when value clears the threshold",
    )
    parser.add_argument("--live", action="store_true", help="place a real Polymarket US order")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="run a paper-trading ledger that bids candidates and marks positions each cycle",
    )
    parser.add_argument(
        "--record-market-history",
        action="store_true",
        default=os.getenv("WEATHER_SCANNER_RECORD_MARKET_HISTORY", "").lower() in {"1", "true", "yes"},
        help="append current weather bucket BBO snapshots for future backtesting",
    )
    parser.add_argument(
        "--simulate-check-only",
        action="store_true",
        help="only mark existing simulated positions; do not add a new paper bid",
    )
    parser.add_argument(
        "--date",
        help="target local market date as YYYY-MM-DD; defaults to tomorrow",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)),
    )
    parser.add_argument(
        "--top-detailed",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_TOP_DETAILED", DEFAULT_TOP_DETAILED)),
        help="number of highest-volume markets to print in detail",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=float(os.getenv("WEATHER_SCANNER_MIN_EDGE_C", "2.0")),
        help="absolute C difference to label as a signal",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_MAX_EVENTS", DEFAULT_MAX_EVENTS)),
        help="maximum active Polymarket US events to inspect when using API discovery",
    )
    parser.add_argument(
        "--cache",
        default=os.getenv("WEATHER_SCANNER_CACHE", str(WEATHER_CACHE_PATH)),
        help="weather forecast cache path",
    )
    parser.add_argument(
        "--cache-seconds",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)),
    )
    parser.add_argument(
        "--no-ensemble",
        action="store_true",
        help="skip ensemble forecasts to reduce API calls",
    )
    parser.add_argument(
        "--ensemble-model",
        default=os.getenv("WEATHER_SCANNER_ENSEMBLE_MODEL", DEFAULT_ENSEMBLE_MODEL),
        help="Open-Meteo ensemble model for the primary ensemble signal; default gfs_seamless",
    )
    parser.add_argument(
        "--compare-models",
        action="store_true",
        default=os.getenv("WEATHER_SCANNER_COMPARE_MODELS", "").lower() in {"1", "true", "yes"},
        help="fetch additional Open-Meteo deterministic and ensemble models for comparison",
    )
    parser.add_argument(
        "--use-model-blend",
        action="store_true",
        default=os.getenv("WEATHER_SCANNER_USE_MODEL_BLEND", "").lower() in {"1", "true", "yes"},
        help="use multi-model bucket votes for trade probabilities instead of only ICON ensemble",
    )
    parser.add_argument(
        "--no-forecast-confirmation",
        dest="forecast_confirmation",
        action="store_false",
        default=os.getenv("WEATHER_SCANNER_FORECAST_CONFIRMATION", "1").lower() not in {"0", "false", "no"},
        help="allow trades whose side disagrees with the primary best_match forecast",
    )
    parser.add_argument(
        "--value-threshold",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_VALUE_THRESHOLD", str(DEFAULT_VALUE_THRESHOLD))),
        help="minimum model_probability - best_ask needed before trading",
    )
    parser.add_argument(
        "--max-shares",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_MAX_SHARES", str(DEFAULT_MAX_TRADE_SHARES))),
        help="maximum YES shares to buy per qualifying market",
    )
    parser.add_argument(
        "--min-shares",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_MIN_SHARES", str(DEFAULT_MIN_TRADE_SHARES))),
        help="minimum YES shares to buy when a market qualifies",
    )
    parser.add_argument(
        "--max-trades-per-cycle",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_MAX_TRADES_PER_CYCLE", DEFAULT_MAX_TRADES_PER_CYCLE)),
        help="maximum new qualifying markets to trade in one cycle",
    )
    parser.add_argument(
        "--max-pending-trades-per-city",
        type=int,
        default=int(
            os.getenv(
                "WEATHER_SCANNER_MAX_PENDING_TRADES_PER_CITY",
                DEFAULT_MAX_PENDING_TRADES_PER_CITY,
            )
        ),
        help="maximum locally tracked pending trades allowed per city",
    )
    parser.add_argument(
        "--allow-multiple-per-event",
        action="store_true",
        default=os.getenv("WEATHER_SCANNER_ALLOW_MULTIPLE_PER_EVENT", "").lower() in {"1", "true", "yes"},
        help="allow multiple bucket trades in the same weather event",
    )
    parser.add_argument(
        "--trade-min-price",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_TRADE_MIN_PRICE", str(MIN_PRICE))),
        help="skip buckets below this best ask",
    )
    parser.add_argument(
        "--trade-max-price",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_TRADE_MAX_PRICE", str(MAX_PRICE))),
        help="skip buckets above this best ask",
    )
    parser.add_argument(
        "--no-current-temp-filter",
        dest="current_temp_filter",
        action="store_false",
        default=os.getenv("WEATHER_SCANNER_CURRENT_TEMP_FILTER", "1").lower() not in {"0", "false", "no"},
        help="allow same-day trades in buckets already below the current temperature",
    )
    parser.add_argument(
        "--cancel-stale-orders",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("WEATHER_SCANNER_CANCEL_STALE_ORDERS", "1").lower() not in {"0", "false", "no"},
        help="cancel bot-tracked live orders older than --stale-order-seconds before placing new orders",
    )
    parser.add_argument(
        "--stale-order-seconds",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_STALE_ORDER_SECONDS", "900")),
        help="age after which locally tracked live orders are considered stale",
    )
    parser.add_argument(
        "--manage-exits",
        action="store_true",
        default=os.getenv("WEATHER_SCANNER_MANAGE_EXITS", "").lower() in {"1", "true", "yes"},
        help="close locally tracked live positions that hit stop-loss or take-profit thresholds",
    )
    parser.add_argument(
        "--stop-loss-pct",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_STOP_LOSS_PCT", "0.25")),
        help="mark-to-entry loss fraction that triggers --manage-exits",
    )
    parser.add_argument(
        "--take-profit-pct",
        type=Decimal,
        default=Decimal(os.getenv("WEATHER_SCANNER_TAKE_PROFIT_PCT", "0.35")),
        help="mark-to-entry gain fraction that triggers --manage-exits",
    )
    parser.add_argument(
        "--exit-slippage-bips",
        type=int,
        default=int(os.getenv("WEATHER_SCANNER_EXIT_SLIPPAGE_BIPS", "300")),
        help="slippage tolerance for SDK close_position calls",
    )
    parser.add_argument(
        "--state",
        default=os.getenv("WEATHER_SCANNER_STATE", str(WEATHER_STATE_PATH)),
        help="JSON state file for live order de-duplication",
    )
    parser.add_argument(
        "--simulation-state",
        default=os.getenv("WEATHER_SCANNER_SIM_STATE", str(WEATHER_SIM_STATE_PATH)),
        help="JSON state file for simulated paper-trading positions",
    )
    parser.add_argument(
        "--dashboard",
        default=os.getenv("WEATHER_SCANNER_DASHBOARD", str(WEATHER_DASHBOARD_PATH)),
        help="HTML dashboard output path",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="do not write the HTML dashboard after each cycle",
    )
    parser.add_argument(
        "--operations-log",
        default=os.getenv("WEATHER_SCANNER_OPERATIONS_LOG", str(WEATHER_OPERATIONS_LOG_PATH)),
        help="JSONL log for order/simulation/dashboard operations",
    )
    default_locations = os.getenv("WEATHER_SCANNER_LOCATIONS", str(WEATHER_LOCATIONS_PATH))
    parser.add_argument(
        "--locations",
        default=default_locations,
        help="optional JSON file with additional official Polymarket US weather locations",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification when local Python CA certs are broken",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("WEATHER_SCANNER_LOG_LEVEL", "WARNING").upper(),
    )
    parser.add_argument("--self-test", action="store_true", help="run parser smoke tests")
    args = parser.parse_args()
    args.locations_was_explicit = (
        "--locations" in sys.argv
        or "WEATHER_SCANNER_LOCATIONS" in os.environ
        or Path(default_locations).exists()
    )
    return args


def run_scan(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
    target_date: date,
) -> list[Signal]:
    events = discover_temperature_events(session, target_date, args.max_events)
    logging.debug("Discovered %s candidate temperature events", len(events))

    signals = []
    for event in events:
        city = extract_city_from_slug(str(event.get("slug") or ""))
        if not city:
            continue

        if city not in forecast_coords_by_city():
            logging.debug("Skipping %s: no coordinates for city slug %s", event.get("slug"), city)
            continue

        hydrated = ensure_event_markets(session, event)
        market = parse_market_snapshot(hydrated)
        if not market or not market.favorite or market.favorite.temp_c is None:
            continue
        market = enrich_market_snapshot(session, market)

        forecast = fetch_forecast(session, cache, city, target_date)
        ensemble = None if args.no_ensemble else fetch_ensemble(
            session,
            cache,
            city,
            target_date,
            args.ensemble_model,
        )
        model_forecasts = (
            fetch_model_forecasts(session, cache, city, target_date, market)
            if args.compare_models or args.use_model_blend
            else []
        )

        signal = build_signal(city, hydrated, market, forecast, ensemble, model_forecasts)
        signals.append(signal)
        time.sleep(REQUEST_PAUSE_SECONDS)

    signals.sort(
        key=lambda signal: (
            abs(signal.edge_c or 0.0),
            signal.market.shares_traded,
            signal.market.volume_24h,
            signal.market.liquidity,
        ),
        reverse=True,
    )
    return signals


def discover_temperature_events(
    session: requests.Session,
    target_date: date,
    max_events: int,
) -> list[dict[str, Any]]:
    events_by_slug: dict[str, dict[str, Any]] = {}

    try:
        for event in fetch_weather_events(session, target_date, max_events):
            slug = str(event.get("slug") or "")
            if is_temperature_event_slug(slug, target_date):
                events_by_slug[slug] = event
    except requests.RequestException as exc:
        logging.warning("Polymarket US event discovery failed: %s", exc)

    return sorted(events_by_slug.values(), key=lambda event: str(event.get("slug") or ""))


def fetch_weather_events(
    session: requests.Session,
    target_date: date,
    max_events: int,
) -> list[dict[str, Any]]:
    events_by_slug: dict[str, dict[str, Any]] = {}

    # The tag endpoint is the cleanest browse path for Polymarket US weather.
    for event in fetch_events_page(
        session,
        {
            "active": "true",
            "closed": "false",
            "tagSlug": "weather",
            "limit": min(100, max_events),
            "orderBy": "volume24hr",
            "orderDirection": "desc",
        },
    ):
        events_by_slug[str(event.get("slug") or "")] = event

    # Search catches newly listed daily markets even if tag browsing changes ordering.
    for query in ("temperature", "highest temperature"):
        for event in fetch_search_events(session, query, max_events):
            events_by_slug[str(event.get("slug") or "")] = event

    return [
        event
        for event in events_by_slug.values()
        if is_temperature_event_slug(str(event.get("slug") or ""), target_date)
    ][:max_events]


def fetch_events_page(session: requests.Session, params: dict[str, Any]) -> list[dict[str, Any]]:
    payload = http_get_json(session, POLYMARKET_US_EVENTS_URL, params=params)
    page = payload if isinstance(payload, list) else payload.get("events", [])
    return [item for item in page if isinstance(item, dict)]


def fetch_search_events(
    session: requests.Session,
    query: str,
    max_events: int,
) -> list[dict[str, Any]]:
    payload = http_get_json(
        session,
        POLYMARKET_US_SEARCH_URL,
        params={
            "query": query,
            "limit": min(100, max_events),
            "active": "true",
            "closed": "false",
        },
    )
    page = payload if isinstance(payload, list) else payload.get("events", [])
    return [item for item in page if isinstance(item, dict)]


def ensure_event_markets(session: requests.Session, event: dict[str, Any]) -> dict[str, Any]:
    if event.get("markets"):
        return event
    slug = str(event.get("slug") or "")
    if not slug:
        return event
    try:
        payload = http_get_json(session, POLYMARKET_US_EVENTS_URL, params={"slug": slug})
    except requests.RequestException as exc:
        logging.warning("Could not hydrate event %s: %s", slug, exc)
        return event
    if isinstance(payload, dict) and isinstance(payload.get("events"), list) and payload["events"]:
        first_event = payload["events"][0]
        return first_event if isinstance(first_event, dict) else event
    if isinstance(payload, list) and payload:
        first_event = payload[0]
        return first_event if isinstance(first_event, dict) else event
    return payload if isinstance(payload, dict) and payload.get("slug") else event


def parse_market_snapshot(event: dict[str, Any]) -> MarketSnapshot | None:
    markets = [market for market in event.get("markets") or [] if isinstance(market, dict)]
    if not markets:
        return None

    buckets = []
    total_volume = 0.0
    volume_24h = 0.0
    liquidity = to_float(event.get("liquidity"))

    for market in markets:
        if not is_tradeable_market(market):
            continue
        yes_price, no_price = market_yes_no_prices(market)
        vol = to_float(market.get("volume"))
        vol24 = to_float(
            market.get("volume24hr")
            or market.get("volume24h")
            or market.get("volume24Hr")
            or market.get("volumeNum24Hr")
        )
        market_liquidity = to_float(market.get("liquidity") or market.get("liquidityNum"))
        liquidity += market_liquidity
        total_volume += vol
        volume_24h += vol24

        title = str(
            market.get("groupItemTitle")
            or market.get("title")
            or market.get("titleShort")
            or market.get("question")
            or market.get("slug")
            or "Unknown"
        )
        bucket_unit = bucket_unit_from_market(market)
        bucket_text = f"{title} {bucket_unit}".strip()
        buckets.append(
            Bucket(
                slug=str(market.get("slug") or ""),
                title=title,
                yes_price=yes_price,
                no_price=no_price,
                volume=vol,
                volume_24h=vol24,
                best_bid=None,
                best_ask=None,
                current_price=None,
                shares_traded=0.0,
                open_interest=0.0,
                bid_depth=0,
                ask_depth=0,
                token_ids=parse_json_list(market.get("clobTokenIds")),
                temp_c=parse_bucket_temp_c(bucket_text),
            )
        )

    if not buckets:
        return None

    return MarketSnapshot(
        slug=str(event.get("slug") or ""),
        title=str(event.get("title") or event.get("question") or event.get("slug") or ""),
        total_volume=total_volume or to_float(event.get("volume")),
        volume_24h=volume_24h
        or to_float(event.get("volume24hr") or event.get("volume24h") or event.get("volumeNum24Hr")),
        liquidity=liquidity,
        shares_traded=0.0,
        open_interest=0.0,
        buckets=sorted(buckets, key=bucket_sort_key),
    )


def enrich_market_snapshot(session: requests.Session, snapshot: MarketSnapshot) -> MarketSnapshot:
    enriched = []
    shares_traded = 0.0
    open_interest = 0.0

    for bucket in snapshot.buckets:
        if not bucket.slug:
            enriched.append(bucket)
            continue
        try:
            market_data = fetch_market_bbo(session, bucket.slug)
        except requests.RequestException as exc:
            logging.debug("Could not fetch BBO for %s: %s", bucket.slug, exc)
            enriched.append(bucket)
            continue

        current_price = price_value(market_data.get("currentPx"))
        best_bid = price_value(market_data.get("bestBid"))
        best_ask = price_value(market_data.get("bestAsk"))
        last_sample = market_data.get("lastPriceSample") or {}
        if current_price is None and isinstance(last_sample, dict):
            current_price = price_value(last_sample.get("longPx"))

        updated = Bucket(
            slug=bucket.slug,
            title=bucket.title,
            yes_price=current_price if current_price is not None else bucket.yes_price,
            no_price=bucket.no_price,
            volume=bucket.volume,
            volume_24h=bucket.volume_24h,
            best_bid=best_bid,
            best_ask=best_ask,
            current_price=current_price,
            shares_traded=to_float(market_data.get("sharesTraded")),
            open_interest=to_float(market_data.get("openInterest")),
            bid_depth=int(to_float(market_data.get("bidDepth"))),
            ask_depth=int(to_float(market_data.get("askDepth"))),
            token_ids=bucket.token_ids,
            temp_c=bucket.temp_c,
        )
        shares_traded += updated.shares_traded
        open_interest += updated.open_interest
        enriched.append(updated)
        time.sleep(0.03)

    return MarketSnapshot(
        slug=snapshot.slug,
        title=snapshot.title,
        total_volume=snapshot.total_volume,
        volume_24h=snapshot.volume_24h,
        liquidity=snapshot.liquidity,
        shares_traded=shares_traded or snapshot.shares_traded,
        open_interest=open_interest or snapshot.open_interest,
        buckets=sorted(enriched, key=bucket_sort_key),
    )


def fetch_market_bbo(session: requests.Session, market_slug: str) -> dict[str, Any]:
    payload = http_get_json(session, f"{POLYMARKET_US_MARKETS_URL}/{market_slug}/bbo")
    market_data = payload.get("marketData") if isinstance(payload, dict) else None
    return market_data if isinstance(market_data, dict) else {}


def price_value(value: Any) -> float | None:
    if isinstance(value, dict):
        return to_optional_float(value.get("value"))
    return to_optional_float(value)


def is_tradeable_market(market: dict[str, Any]) -> bool:
    if market.get("closed") is True or market.get("active") is False:
        return False
    if market.get("archived") is True or market.get("hidden") is True:
        return False
    return True


def fetch_forecast(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
) -> Forecast | None:
    lat, lon = forecast_coords_by_city()[city]
    key = f"forecast:v4:{city}:{lat:.4f}:{lon:.4f}:{target_date.isoformat()}"
    cached = cache.get(key)
    if cached is not None:
        return parse_forecast(cached)

    payload = http_get_json(
        session,
        OPEN_METEO_FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m",
            "timezone": "auto",
            "forecast_days": 3,
        },
    )
    data = extract_forecast_payload(payload, target_date)
    if data:
        cache.set(key, data)
    return parse_forecast(data)


def fetch_ensemble(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
    ensemble_model: str = DEFAULT_ENSEMBLE_MODEL,
) -> Ensemble | None:
    lat, lon = forecast_coords_by_city()[city]
    key = f"ensemble:v3:{ensemble_model}:{city}:{lat:.4f}:{lon:.4f}:{target_date.isoformat()}"
    cached = cache.get(key)
    if cached is not None:
        return parse_ensemble(cached)

    payload = http_get_json(
        session,
        OPEN_METEO_ENSEMBLE_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "timezone": "auto",
            "forecast_days": 3,
            "models": ensemble_model,
        },
    )
    data = extract_ensemble_payload(payload, target_date)
    if data:
        cache.set(key, data)
    return parse_ensemble(data)


def fetch_model_forecasts(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
    market: MarketSnapshot,
) -> list[ModelForecast]:
    rows: list[ModelForecast] = []
    for name, url, extra_params in DETERMINISTIC_MODEL_SPECS:
        try:
            forecast = fetch_forecast_model(session, cache, city, target_date, name, url, extra_params)
        except requests.RequestException as exc:
            rows.append(ModelForecast(name=name, kind="deterministic", high_c=None, bucket_title=None, member_count=0, stdev_c=None, error=str(exc)))
            continue
        bucket = nearest_bucket(market.buckets, forecast.high_c) if forecast else None
        rows.append(
            ModelForecast(
                name=name,
                kind="deterministic",
                high_c=forecast.high_c if forecast else None,
                bucket_title=bucket.title if bucket else None,
                member_count=1 if forecast else 0,
                stdev_c=None,
                error=None if forecast else "unavailable",
            )
        )
        time.sleep(0.03)

    for name, extra_params in ENSEMBLE_MODEL_SPECS:
        try:
            ensemble = fetch_ensemble_model(session, cache, city, target_date, name, extra_params)
        except requests.RequestException as exc:
            rows.append(ModelForecast(name=name, kind="ensemble", high_c=None, bucket_title=None, member_count=0, stdev_c=None, error=str(exc)))
            continue
        bucket = nearest_bucket(market.buckets, ensemble.mean_c) if ensemble else None
        rows.append(
            ModelForecast(
                name=name,
                kind="ensemble",
                high_c=ensemble.mean_c if ensemble else None,
                bucket_title=bucket.title if bucket else None,
                member_count=len(ensemble.member_maxes_c) if ensemble else 0,
                stdev_c=ensemble.stdev_c if ensemble else None,
                error=None if ensemble else "unavailable",
            )
        )
        time.sleep(0.03)

    return rows


def fetch_forecast_model(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
    model_name: str,
    url: str,
    extra_params: dict[str, Any],
) -> Forecast | None:
    lat, lon = forecast_coords_by_city()[city]
    key = f"forecast-model:v1:{model_name}:{city}:{lat:.4f}:{lon:.4f}:{target_date.isoformat()}"
    cached = cache.get(key)
    if cached is not None:
        return parse_forecast(cached)
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "timezone": "auto",
        "forecast_days": 3,
        **extra_params,
    }
    payload = http_get_json(session, url, params=params)
    data = extract_forecast_payload(payload, target_date)
    if data:
        cache.set(key, data)
    return parse_forecast(data)


def fetch_ensemble_model(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
    model_name: str,
    extra_params: dict[str, Any],
) -> Ensemble | None:
    lat, lon = forecast_coords_by_city()[city]
    key = f"ensemble-model:v1:{model_name}:{city}:{lat:.4f}:{lon:.4f}:{target_date.isoformat()}"
    cached = cache.get(key)
    if cached is not None:
        return parse_ensemble(cached)
    payload = http_get_json(
        session,
        OPEN_METEO_ENSEMBLE_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "timezone": "auto",
            "forecast_days": 3,
            **extra_params,
        },
    )
    data = extract_ensemble_payload(payload, target_date)
    if data:
        cache.set(key, data)
    return parse_ensemble(data)


def run_backtest(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
) -> dict[str, Any]:
    start_date, end_date = backtest_date_range(args)
    cities = backtest_cities(args)
    city_results = []
    for city in cities:
        if city not in forecast_coords_by_city():
            logging.warning("Skipping backtest city %s: no coordinates", city)
            continue
        city_results.append(backtest_city(args, session, cache, city, start_date, end_date))
        time.sleep(0.1)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "lead_days": args.backtest_lead_days,
        "bin_f": args.backtest_bin_f,
        "market_history": str(args.market_history),
        "cities": city_results,
        "overall": aggregate_backtest_results(city_results),
        "trading_overall": aggregate_trading_results(city_results),
    }


def backtest_city(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    actual_by_day = fetch_actual_highs(session, cache, city, start_date, end_date)
    model_predictions: dict[str, dict[str, float]] = {}
    for model_name, extra_params in BACKTEST_MODEL_SPECS:
        try:
            model_predictions[model_name] = fetch_previous_run_highs(
                session,
                cache,
                city,
                start_date,
                end_date,
                args.backtest_lead_days,
                model_name,
                extra_params,
            )
        except requests.RequestException as exc:
            logging.warning("Backtest model %s failed for %s: %s", model_name, city, exc)
            model_predictions[model_name] = {}
        time.sleep(0.05)

    model_metrics = {
        name: score_predictions(actual_by_day, predictions, args.backtest_bin_f)
        for name, predictions in model_predictions.items()
    }
    blend_predictions = build_blend_predictions(actual_by_day, model_predictions, model_metrics, args.backtest_bin_f)
    blend_metrics = {
        name: score_predictions(actual_by_day, predictions, args.backtest_bin_f)
        for name, predictions in blend_predictions.items()
    }
    trading = (
        simulate_backtest_trading(
            args,
            session,
            cache,
            city,
            actual_by_day,
            model_predictions,
            blend_predictions,
            start_date,
            end_date,
        )
        if args.backtest_trading
        else {"available": False, "reason": "run with --backtest-trading to simulate market P/L"}
    )
    return {
        "city": city,
        "city_display": display_name(city),
        "station": station_code(city),
        "station_location": station_location(city),
        "days": len(actual_by_day),
        "models": model_metrics,
        "blends": blend_metrics,
        "trading": trading,
    }


def backtest_date_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.backtest_start or args.backtest_end:
        if not args.backtest_start or not args.backtest_end:
            raise WeatherScannerError("Provide both --backtest-start and --backtest-end.")
        start_date = datetime.strptime(args.backtest_start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.backtest_end, "%Y-%m-%d").date()
    else:
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=max(args.backtest_days, 1) - 1)
    if start_date > end_date:
        raise WeatherScannerError("Backtest start date must be on or before end date.")
    return start_date, end_date


def backtest_cities(args: argparse.Namespace) -> list[str]:
    if args.backtest_cities.strip():
        return [city.strip() for city in args.backtest_cities.split(",") if city.strip()]
    return sorted(POLYMARKET_US_STATION_INFO)


def fetch_actual_highs(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    start_date: date,
    end_date: date,
) -> dict[str, float]:
    lat, lon = forecast_coords_by_city()[city]
    key = f"backtest-actual:v1:{city}:{lat:.4f}:{lon:.4f}:{start_date}:{end_date}"
    cached = cache.get(key)
    if cached is not None:
        return {str(day): float(value) for day, value in cached.items()}
    payload = http_get_json(
        session,
        OPEN_METEO_ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "hourly": "temperature_2m",
            "timezone": "auto",
        },
    )
    highs = hourly_highs(payload, "temperature_2m")
    cache.set(key, highs)
    return highs


def fetch_previous_run_highs(
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    start_date: date,
    end_date: date,
    lead_days: int,
    model_name: str,
    extra_params: dict[str, Any],
) -> dict[str, float]:
    lat, lon = forecast_coords_by_city()[city]
    variable = f"temperature_2m_previous_day{lead_days}"
    key = (
        f"backtest-forecast:v1:{model_name}:{lead_days}:"
        f"{city}:{lat:.4f}:{lon:.4f}:{start_date}:{end_date}"
    )
    cached = cache.get(key)
    if cached is not None:
        return {str(day): float(value) for day, value in cached.items()}
    payload = http_get_json(
        session,
        OPEN_METEO_PREVIOUS_RUNS_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "hourly": variable,
            "timezone": "auto",
            **extra_params,
        },
    )
    highs = hourly_highs(payload, variable)
    cache.set(key, highs)
    return highs


def hourly_highs(payload: Any, variable: str) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get(variable) or []
    by_day: dict[str, list[float]] = {}
    for timestamp, value in zip(times, values):
        parsed = to_optional_float(value)
        if parsed is None:
            continue
        day = str(timestamp)[:10]
        by_day.setdefault(day, []).append(parsed)
    return {day: max(day_values) for day, day_values in by_day.items() if day_values}


def score_predictions(
    actual_by_day: dict[str, float],
    predicted_by_day: dict[str, float],
    bin_f: float,
) -> dict[str, Any]:
    errors = []
    bin_hits = 0
    samples = []
    for day, actual in sorted(actual_by_day.items()):
        predicted = predicted_by_day.get(day)
        if predicted is None:
            continue
        error = predicted - actual
        errors.append(error)
        actual_bin = temp_bin_f(actual, bin_f)
        predicted_bin = temp_bin_f(predicted, bin_f)
        if actual_bin == predicted_bin:
            bin_hits += 1
        samples.append(
            {
                "date": day,
                "actual_c": actual,
                "predicted_c": predicted,
                "error_c": error,
                "actual_bin_f": actual_bin,
                "predicted_bin_f": predicted_bin,
            }
        )
    n = len(errors)
    if not errors:
        return {"n": 0, "mae_c": None, "rmse_c": None, "bias_c": None, "bin_hit_rate": None, "samples": []}
    mae = statistics.fmean(abs(error) for error in errors)
    rmse = math.sqrt(statistics.fmean(error * error for error in errors))
    bias = statistics.fmean(errors)
    return {
        "n": n,
        "mae_c": mae,
        "rmse_c": rmse,
        "bias_c": bias,
        "bin_hit_rate": bin_hits / n,
        "samples": samples[-10:],
    }


def build_blend_predictions(
    actual_by_day: dict[str, float],
    model_predictions: dict[str, dict[str, float]],
    model_metrics: dict[str, dict[str, Any]],
    bin_f: float,
) -> dict[str, dict[str, float]]:
    days = sorted(actual_by_day)
    equal_average = {}
    weighted_average = {}
    most_frequent_bin = {}
    for day in days:
        values = [
            predictions[day]
            for predictions in model_predictions.values()
            if day in predictions
        ]
        if values:
            equal_average[day] = statistics.fmean(values)

        weighted_values = []
        for model_name, predictions in model_predictions.items():
            if day not in predictions:
                continue
            mae = model_metrics.get(model_name, {}).get("mae_c")
            if mae is None:
                continue
            weight = 1 / max(float(mae), 0.05)
            weighted_values.append((predictions[day], weight))
        if weighted_values:
            total_weight = sum(weight for _, weight in weighted_values)
            weighted_average[day] = sum(value * weight for value, weight in weighted_values) / total_weight

        bin_values = [temp_bin_f(value, bin_f) for value in values]
        if bin_values:
            mode_bin, _ = Counter(bin_values).most_common(1)[0]
            most_frequent_bin[day] = bin_center_c(mode_bin, bin_f)

    return {
        "equal_average": equal_average,
        "inverse_mae_weighted_average": weighted_average,
        "most_frequent_bin": most_frequent_bin,
    }


def record_market_history(path: Path, signals: list[Signal], target_date: date) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for signal in signals:
        for bucket in signal.market.buckets:
            rows.append(
                {
                    "ts": ts,
                    "target_date": target_date.isoformat(),
                    "city": signal.city,
                    "city_display": display_name(signal.city),
                    "station": station_code(signal.city),
                    "event_slug": signal.market.slug,
                    "market_slug": bucket.slug,
                    "bucket_title": bucket.title,
                    "yes_ask": bucket.trade_price,
                    "no_ask": bucket.no_trade_price,
                    "yes_bid": bucket.best_bid,
                    "current_price": bucket.current_price,
                    "shares_traded": bucket.shares_traded,
                    "open_interest": bucket.open_interest,
                    "temp_c": bucket.temp_c,
                }
            )
    if not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        raise WeatherScannerError(f"Could not write market history {path}: {exc}") from exc


def simulate_backtest_trading(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    actual_by_day: dict[str, float],
    model_predictions: dict[str, dict[str, float]],
    blend_predictions: dict[str, dict[str, float]],
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    reasons = []
    rows: dict[str, list[dict[str, Any]]] = {}
    history_path = Path(args.market_history)
    if args.market_history_source in {"auto", "local"}:
        if history_path.exists():
            rows = merge_market_history_rows(
                rows,
                load_market_history_rows(history_path, city, start_date, end_date),
            )
        elif args.market_history_source == "local":
            reasons.append(f"market history file not found: {history_path}")

    if args.market_history_source in {"auto", "polymarketdata"}:
        if polymarketdata_api_key():
            rows = merge_market_history_rows(
                rows,
                load_polymarketdata_history_rows(args, session, cache, city, start_date, end_date),
            )
        elif args.market_history_source == "polymarketdata":
            reasons.append("POLYMARKETDATA_API_KEY is not set")

    rows = {day: dedupe_market_history_day(day_rows) for day, day_rows in rows.items()}
    if not rows:
        return {
            "available": False,
            "reason": "; ".join(reasons) if reasons else f"no market history rows for {city} in range",
            "strategies": {},
        }

    strategies: dict[str, list[dict[str, Any]]] = {}
    prediction_sets = {"model_vote": {}}
    prediction_sets.update(model_predictions)
    prediction_sets.update(blend_predictions)

    for strategy_name in prediction_sets:
        trades = []
        for day, day_rows in sorted(rows.items()):
            actual_c = actual_by_day.get(day)
            if actual_c is None:
                continue
            candidates = []
            for row in day_rows:
                yes_probability = backtest_yes_probability(strategy_name, row, day, model_predictions, blend_predictions)
                if yes_probability is None:
                    continue
                for side, probability, price_key in (
                    ("YES", yes_probability, "yes_ask"),
                    ("NO", Decimal("1") - yes_probability, "no_ask"),
                ):
                    price = to_decimal(row.get(price_key))
                    if price is None or price < args.trade_min_price or price > args.trade_max_price:
                        continue
                    value = probability - price
                    if value < args.value_threshold:
                        continue
                    quantity = sized_trade_quantity(
                        value,
                        args.value_threshold,
                        quantize_quantity(args.min_shares),
                        quantize_quantity(args.max_shares),
                    )
                    candidates.append((value, side, price, quantity, row))
            candidates.sort(key=lambda item: item[0], reverse=True)
            selected = candidates[: max(args.max_trades_per_cycle, 0)]
            for value, side, price, quantity, row in selected:
                yes_win = market_slug_wins(str(row.get("market_slug") or ""), actual_c)
                win = yes_win if side == "YES" else not yes_win
                pnl = (Decimal("1") - price) * quantity if win else -price * quantity
                trades.append(
                    {
                        "date": day,
                        "city": city,
                        "market_slug": row.get("market_slug"),
                        "bucket_title": row.get("bucket_title"),
                        "side": side,
                        "price": format_decimal(quantize_price(price)),
                        "quantity": format_decimal(quantity),
                        "probability": format_decimal(probability),
                        "value_metric": format_decimal(value),
                        "win": win,
                        "pnl": format_decimal(quantize_money(pnl)),
                    }
                )
        strategies[strategy_name] = trades

    return {
        "available": True,
        "source": args.market_history_source,
        "rows": sum(len(day_rows) for day_rows in rows.values()),
        "strategies": {
            name: summarize_backtest_trades(trades)
            for name, trades in strategies.items()
        },
    }


def merge_market_history_rows(
    base: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    merged = {day: list(day_rows) for day, day_rows in base.items()}
    for day, day_rows in incoming.items():
        merged.setdefault(day, []).extend(day_rows)
    return merged


def dedupe_market_history_day(day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_market: dict[str, dict[str, Any]] = {}
    for row in day_rows:
        slug = str(row.get("market_slug") or "")
        if not slug:
            continue
        current = by_market.get(slug)
        if current is None or str(row.get("ts") or "") >= str(current.get("ts") or ""):
            by_market[slug] = row
    return list(by_market.values())


def load_polymarketdata_history_rows(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    start_date: date,
    end_date: date,
) -> dict[str, list[dict[str, Any]]]:
    usage = fetch_polymarketdata_usage(args, session, cache)
    max_history_days = int(to_float((usage.get("limits") or {}).get("max_history_days"), 0)) if usage else 0
    if max_history_days > 0:
        earliest = date.today() - timedelta(days=max_history_days)
        if start_date < earliest:
            logging.warning(
                "PolymarketData plan limits history to %s days; clamping %s to %s",
                max_history_days,
                start_date,
                earliest,
            )
            start_date = earliest
    if start_date > end_date:
        return {}

    rows: dict[str, list[dict[str, Any]]] = {}
    for day in date_range(start_date, end_date):
        markets = fetch_polymarketdata_weather_markets(args, session, cache, city, day)
        if args.polymarketdata_max_markets_per_day > 0:
            markets = markets[: args.polymarketdata_max_markets_per_day]
        day_rows = []
        entry_time = polymarketdata_entry_time(day, args.backtest_lead_days, args.polymarketdata_entry_hour_utc)
        for market in markets:
            row = polymarketdata_market_price_row(args, session, cache, city, day, entry_time, market)
            if row is not None:
                day_rows.append(row)
        if day_rows:
            rows[day.isoformat()] = day_rows
    return rows


def fetch_polymarketdata_usage(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
) -> dict[str, Any]:
    cached = cache.get("polymarketdata-usage:v1")
    if isinstance(cached, dict):
        return cached
    try:
        payload = polymarketdata_get_json(args, session, "/v1/usage")
    except requests.RequestException as exc:
        logging.warning("PolymarketData usage fetch failed: %s", exc)
        return {}
    if isinstance(payload, dict):
        cache.set("polymarketdata-usage:v1", payload)
        return payload
    return {}


def fetch_polymarketdata_weather_markets(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
) -> list[dict[str, Any]]:
    aliases = polymarketdata_city_aliases(city)
    all_markets: dict[str, dict[str, Any]] = {}
    for alias in aliases:
        query = f"highest temperature in {alias} on {target_date.strftime('%B')} {target_date.day} {target_date.year}"
        key = f"polymarketdata-markets:v2:{query}"
        cached = cache.get(key)
        if cached is None:
            try:
                payload = polymarketdata_get_json(
                    args,
                    session,
                    "/v1/markets",
                    params={"search": query, "limit": 100},
                )
            except requests.RequestException as exc:
                logging.warning("PolymarketData market search failed for %s: %s", query, exc)
                continue
            cached = payload.get("data") if isinstance(payload, dict) else []
            cache.set(key, cached)
        for market in cached if isinstance(cached, list) else []:
            if not isinstance(market, dict):
                continue
            slug = str(market.get("slug") or "")
            if is_polymarketdata_weather_slug(slug, city, target_date):
                all_markets[slug] = market
        if all_markets:
            break
    return sorted(all_markets.values(), key=lambda market: polymarketdata_bucket_sort_key(str(market.get("slug") or "")))


def polymarketdata_market_price_row(
    args: argparse.Namespace,
    session: requests.Session,
    cache: WeatherCache,
    city: str,
    target_date: date,
    entry_time: datetime,
    market: dict[str, Any],
) -> dict[str, Any] | None:
    slug = str(market.get("slug") or "")
    if not slug:
        return None
    start_ts = entry_time.isoformat().replace("+00:00", "Z")
    end_ts = (entry_time + timedelta(hours=max(args.polymarketdata_window_hours, 1))).isoformat().replace("+00:00", "Z")
    key = f"polymarketdata-prices:v2:{slug}:{start_ts}:{end_ts}:{args.polymarketdata_resolution}"
    cached = cache.get(key)
    if cached is None:
        try:
            payload = polymarketdata_get_json(
                args,
                session,
                f"/v1/markets/{slug}/prices",
                params={
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "resolution": args.polymarketdata_resolution,
                    "limit": 500,
                },
            )
        except requests.RequestException as exc:
            logging.warning("PolymarketData prices failed for %s: %s", slug, exc)
            return None
        cached = payload
        cache.set(key, cached)
    if not isinstance(cached, dict):
        return None
    data = cached.get("data") if isinstance(cached.get("data"), dict) else {}
    yes_price, yes_ts = select_polymarketdata_price(data.get("Yes"), entry_time)
    no_price, no_ts = select_polymarketdata_price(data.get("No"), entry_time)
    if yes_price is None and no_price is None:
        return None
    if yes_price is None and no_price is not None:
        yes_price = Decimal("1") - no_price
    if no_price is None and yes_price is not None:
        no_price = Decimal("1") - yes_price
    selected_ts = yes_ts or no_ts or entry_time.isoformat()
    return {
        "ts": selected_ts,
        "target_date": target_date.isoformat(),
        "city": city,
        "city_display": display_name(city),
        "station": station_code(city),
        "event_slug": polymarketdata_event_slug(city, target_date),
        "market_slug": slug,
        "bucket_title": str(market.get("question") or market.get("slug") or ""),
        "yes_ask": format_decimal(quantize_price(yes_price)),
        "no_ask": format_decimal(quantize_price(no_price)),
        "yes_bid": None,
        "current_price": format_decimal(quantize_price(yes_price)),
        "shares_traded": None,
        "open_interest": None,
        "temp_c": polymarketdata_slug_temp_c(slug),
        "source": "polymarketdata_prices",
        "entry_time": entry_time.isoformat(),
        "price_source": "historical token price proxy",
    }


def polymarketdata_get_json(
    args: argparse.Namespace,
    session: requests.Session,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    response = session.get(
        f"{POLYMARKETDATA_API_URL}{path}",
        headers={"X-API-Key": polymarketdata_api_key() or ""},
        params=params,
        timeout=30,
    )
    if response.status_code == 429:
        wait_seconds = max(args.polymarketdata_request_pause, 6.1)
        logging.warning("PolymarketData rate-limited; sleeping %.1fs", wait_seconds)
        time.sleep(wait_seconds)
        response = session.get(
            f"{POLYMARKETDATA_API_URL}{path}",
            headers={"X-API-Key": polymarketdata_api_key() or ""},
            params=params,
            timeout=30,
        )
    response.raise_for_status()
    payload = response.json()
    if args.polymarketdata_request_pause > 0:
        time.sleep(args.polymarketdata_request_pause)
    return payload


def date_range(start_date: date, end_date: date) -> Iterable[date]:
    cursor = start_date
    while cursor <= end_date:
        yield cursor
        cursor += timedelta(days=1)


def polymarketdata_api_key() -> str | None:
    return env_first("POLYMARKETDATA_API_KEY")


def polymarketdata_city_aliases(city: str) -> list[str]:
    return POLYMARKETDATA_WEATHER_CITY_ALIASES.get(city, [display_name(city)])


def polymarketdata_city_slug_aliases(city: str) -> list[str]:
    return [slugify_text(alias) for alias in polymarketdata_city_aliases(city)]


def polymarketdata_date_slug(value: date, include_year: bool = True) -> str:
    month = value.strftime("%B").lower()
    if include_year:
        return f"{month}-{value.day}-{value.year}"
    return f"{month}-{value.day}"


def polymarketdata_event_slug(city: str, target_date: date) -> str:
    slug_city = polymarketdata_city_slug_aliases(city)[0]
    return f"highest-temperature-in-{slug_city}-on-{polymarketdata_date_slug(target_date)}"


def is_polymarketdata_weather_slug(slug: str, city: str, target_date: date) -> bool:
    if not slug.startswith("highest-temperature-in-"):
        return False
    date_slugs = {
        polymarketdata_date_slug(target_date, include_year=True),
        polymarketdata_date_slug(target_date, include_year=False),
    }
    for city_slug in polymarketdata_city_slug_aliases(city):
        for date_slug in date_slugs:
            prefix = f"highest-temperature-in-{city_slug}-on-{date_slug}-"
            if slug.startswith(prefix):
                return True
    return False


def polymarketdata_bucket_sort_key(slug: str) -> tuple[int, float, str]:
    parsed = parse_polymarketdata_bucket_slug(slug)
    if not parsed:
        return (9, 0.0, slug)
    unit, low, high, kind = parsed
    if kind == "gte":
        return (2, low, slug)
    if kind == "lte":
        return (0, low, slug)
    return (1, low, slug)


def select_polymarketdata_price(series: Any, entry_time: datetime) -> tuple[Decimal | None, str | None]:
    if not isinstance(series, list):
        return None, None
    parsed = []
    for item in series:
        if not isinstance(item, dict):
            continue
        price = to_decimal(item.get("p"))
        timestamp = parse_api_datetime(item.get("t"))
        if price is None or timestamp is None:
            continue
        parsed.append((timestamp, price, str(item.get("t") or "")))
    if not parsed:
        return None, None
    before = [item for item in parsed if item[0] <= entry_time]
    chosen = max(before, key=lambda item: item[0]) if before else min(parsed, key=lambda item: item[0])
    return chosen[1], chosen[2]


def parse_api_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def polymarketdata_entry_time(target_date: date, lead_days: int, hour_utc: int) -> datetime:
    entry_day = target_date - timedelta(days=max(lead_days, 0))
    return datetime(
        entry_day.year,
        entry_day.month,
        entry_day.day,
        min(max(hour_utc, 0), 23),
        tzinfo=timezone.utc,
    )


def polymarketdata_slug_temp_c(slug: str) -> float | None:
    parsed = parse_polymarketdata_bucket_slug(slug)
    if not parsed:
        return None
    unit, low, high, _ = parsed
    midpoint = low if high is None else (low + high) / 2
    return fahrenheit_to_c(midpoint) if unit == "f" else midpoint


def polymarketdata_slug_bounds_c(slug: str) -> tuple[float | None, float | None] | None:
    parsed = parse_polymarketdata_bucket_slug(slug)
    if not parsed:
        return None
    unit, low, high, kind = parsed
    low_c = fahrenheit_to_c(low) if unit == "f" else low
    high_c = fahrenheit_to_c(high + 1) if unit == "f" and high is not None else high
    if unit != "f" and high is not None:
        high_c = high + 1
    if kind == "gte":
        return low_c, None
    if kind == "lte":
        return None, low_c
    return low_c, high_c


def parse_polymarketdata_bucket_slug(slug: str) -> tuple[str, float, float | None, str] | None:
    suffix = slug.rsplit("-", 1)[-1].casefold()
    range_match = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)([fc])", suffix)
    if range_match:
        return (
            range_match.group(3),
            float(range_match.group(1)),
            float(range_match.group(2)),
            "range",
        )
    gte_match = re.fullmatch(r"(\d+(?:\.\d+)?)([fc])?orhigher", suffix)
    if gte_match:
        return (gte_match.group(2) or "f", float(gte_match.group(1)), None, "gte")
    lte_match = re.fullmatch(r"(\d+(?:\.\d+)?)([fc])?orbelow", suffix)
    if lte_match:
        return (lte_match.group(2) or "f", float(lte_match.group(1)), None, "lte")
    exact_match = re.fullmatch(r"(\d+(?:\.\d+)?)([fc])", suffix)
    if exact_match:
        value = float(exact_match.group(1))
        return (exact_match.group(2), value, value, "exact")
    return None


def polymarketdata_slug_wins(market_slug: str, actual_c: float) -> bool | None:
    parsed = parse_polymarketdata_bucket_slug(market_slug)
    if not parsed:
        return None
    unit, low, high, kind = parsed
    actual = actual_c * 9 / 5 + 32 if unit == "f" else actual_c
    if kind == "gte":
        return actual >= low
    if kind == "lte":
        return actual <= low
    if high is None:
        return False
    upper = high + 1 if kind in {"range", "exact"} else high
    return low <= actual < upper


def slugify_text(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or value.casefold()


def load_market_history_rows(
    path: Path,
    city: str,
    start_date: date,
    end_date: date,
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("city") != city:
            continue
        target = str(row.get("target_date") or "")
        try:
            target_date = datetime.strptime(target, "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date <= target_date <= end_date:
            rows.setdefault(target, []).append(row)
    return rows


def backtest_yes_probability(
    strategy_name: str,
    row: dict[str, Any],
    day: str,
    model_predictions: dict[str, dict[str, float]],
    blend_predictions: dict[str, dict[str, float]],
) -> Decimal | None:
    market_slug = str(row.get("market_slug") or "")
    if strategy_name == "model_vote":
        votes = [
            predictions[day]
            for predictions in model_predictions.values()
            if day in predictions
        ]
        if not votes:
            return None
        wins = sum(1 for predicted_c in votes if market_slug_wins(market_slug, predicted_c))
        return Decimal(wins) / Decimal(len(votes))

    predictions = model_predictions.get(strategy_name) or blend_predictions.get(strategy_name)
    if not predictions or day not in predictions:
        return None
    return Decimal("1") if market_slug_wins(market_slug, predictions[day]) else Decimal("0")


def summarize_backtest_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total_pnl = sum((to_decimal(trade.get("pnl")) or Decimal("0")) for trade in trades)
    wins = sum(1 for trade in trades if trade.get("win") is True)
    cost = sum(
        (to_decimal(trade.get("price")) or Decimal("0")) * (to_decimal(trade.get("quantity")) or Decimal("0"))
        for trade in trades
    )
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": wins / len(trades) if trades else None,
        "cost": format_decimal(quantize_money(cost)),
        "pnl": format_decimal(quantize_money(total_pnl)),
        "roi": format_decimal(quantize_money(total_pnl / cost)) if cost else None,
        "samples": trades[-10:],
    }


def aggregate_trading_results(city_results: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for city in city_results:
        trading = city.get("trading") or {}
        for name, summary in (trading.get("strategies") or {}).items():
            buckets.setdefault(name, []).append(summary)
    output = {}
    for name, summaries in buckets.items():
        trades = sum(int(summary.get("trades") or 0) for summary in summaries)
        wins = sum(int(summary.get("wins") or 0) for summary in summaries)
        cost = sum((to_decimal(summary.get("cost")) or Decimal("0")) for summary in summaries)
        pnl = sum((to_decimal(summary.get("pnl")) or Decimal("0")) for summary in summaries)
        output[name] = {
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": wins / trades if trades else None,
            "cost": format_decimal(quantize_money(cost)),
            "pnl": format_decimal(quantize_money(pnl)),
            "roi": format_decimal(quantize_money(pnl / cost)) if cost else None,
        }
    return output


def extract_forecast_payload(payload: Any, target_date: date) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    target = target_date.isoformat()
    hourly = payload.get("hourly") or {}
    hourly_times = hourly.get("time") or []
    hourly_temps = hourly.get("temperature_2m") or []
    target_hourly = [
        {"time": timestamp, "temp_c": temp}
        for timestamp, temp in zip(hourly_times, hourly_temps)
        if str(timestamp).startswith(target) and temp is not None
    ]
    hourly_max = max((float(item["temp_c"]) for item in target_hourly), default=None)

    daily = payload.get("daily") or {}
    daily_times = daily.get("time") or []
    daily_maxes = daily.get("temperature_2m_max") or []
    daily_mins = daily.get("temperature_2m_min") or []
    daily_high = None
    daily_low = None
    for index, day in enumerate(daily_times):
        if day != target:
            continue
        if index < len(daily_maxes) and daily_maxes[index] is not None:
            daily_high = float(daily_maxes[index])
        if index < len(daily_mins) and daily_mins[index] is not None:
            daily_low = float(daily_mins[index])
        break

    high = daily_high if daily_high is not None else hourly_max
    if high is None:
        return None

    return {
        "high_c": high,
        "low_c": daily_low,
        "hourly_max_c": hourly_max,
        "current_temp_c": extract_current_temp_c(payload, target_date),
        "timezone": payload.get("timezone") or "unknown",
        "hourly": target_hourly,
    }


def extract_current_temp_c(payload: dict[str, Any], target_date: date) -> float | None:
    current = payload.get("current") or {}
    if not isinstance(current, dict):
        return None
    current_time = str(current.get("time") or "")
    if current_time[:10] != target_date.isoformat():
        return None
    return to_optional_float(current.get("temperature_2m"))


def extract_ensemble_payload(payload: Any, target_date: date) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    target = target_date.isoformat()
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    member_keys = [
        key
        for key in hourly
        if key == "temperature_2m" or re.fullmatch(r"temperature_2m_member\d+", key)
    ]
    member_maxes = []
    for key in member_keys:
        temps = hourly.get(key) or []
        target_temps = [
            float(temp)
            for timestamp, temp in zip(times, temps)
            if str(timestamp).startswith(target) and temp is not None
        ]
        if target_temps:
            member_maxes.append(max(target_temps))
    if not member_maxes:
        return None
    return {"member_maxes_c": sorted(member_maxes)}


def parse_forecast(data: Any) -> Forecast | None:
    if not isinstance(data, dict) or data.get("high_c") is None:
        return None
    return Forecast(
        high_c=float(data["high_c"]),
        low_c=to_optional_float(data.get("low_c")),
        hourly_max_c=to_optional_float(data.get("hourly_max_c")),
        current_temp_c=to_optional_float(data.get("current_temp_c")),
        timezone=str(data.get("timezone") or "unknown"),
        hourly=data.get("hourly") if isinstance(data.get("hourly"), list) else [],
    )


def parse_ensemble(data: Any) -> Ensemble | None:
    if not isinstance(data, dict):
        return None
    values = [float(value) for value in data.get("member_maxes_c") or [] if value is not None]
    if not values:
        return None
    return Ensemble(sorted(values))


def build_signal(
    city: str,
    event: dict[str, Any],
    market: MarketSnapshot,
    forecast: Forecast | None,
    ensemble: Ensemble | None,
    model_forecasts: list[ModelForecast] | None = None,
) -> Signal:
    favorite = market.favorite
    favorite_temp = favorite.temp_c if favorite else None
    edge = None
    ensemble_edge = None
    forecast_bucket = None
    ensemble_bucket = None

    if forecast and favorite_temp is not None:
        edge = forecast.high_c - favorite_temp
        forecast_bucket = nearest_bucket(market.buckets, forecast.high_c)
    if ensemble and favorite_temp is not None:
        ensemble_edge = ensemble.mean_c - favorite_temp
        ensemble_bucket = nearest_bucket(market.buckets, ensemble.mean_c)

    return Signal(
        city=city,
        event=event,
        market=market,
        forecast=forecast,
        ensemble=ensemble,
        edge_c=edge,
        ensemble_edge_c=ensemble_edge,
        forecast_bucket=forecast_bucket,
        ensemble_bucket=ensemble_bucket,
        model_forecasts=model_forecasts or [],
    )


def print_report(
    signals: list[Signal],
    target_date: date,
    top_detailed: int,
    min_edge: float,
) -> None:
    print()
    print(f"Polymarket Weather Scanner | target={target_date.isoformat()} | signals={len(signals)}")
    print("=" * 94)
    if not signals:
        print("No active temperature markets found with known coordinates and parseable buckets.")
        return

    print(
        f"{'City':18} {'Traded':>10} {'Fav':>12} {'Forecast':>12} {'Edge':>8} "
        f"{'Ens Mean':>10} {'Signal':>8}"
    )
    print("-" * 94)
    by_volume = sorted(
        signals,
        key=lambda signal: (signal.market.shares_traded, signal.market.volume_24h),
        reverse=True,
    )
    for signal in by_volume:
        favorite = signal.market.favorite
        edge = signal.edge_c
        ensemble = signal.ensemble
        signal_label = edge_label(edge, min_edge)
        print(
            f"{display_name(signal.city):18.18} "
            f"{fmt_market_traded(signal.market):>10} "
            f"{bucket_temp_label(favorite):>12} "
            f"{fmt_temp_c(signal.forecast.high_c if signal.forecast else None):>12} "
            f"{fmt_signed_temp(edge):>8} "
            f"{fmt_temp_c(ensemble.mean_c if ensemble else None):>10} "
            f"{signal_label:>8}"
        )

    details = by_volume[:top_detailed]
    if not details:
        return

    print()
    details_label = "Traded Shares" if any(signal.market.shares_traded > 0 for signal in signals) else "Edge"
    print(f"Top {len(details)} By {details_label}")
    print("=" * 94)
    for index, signal in enumerate(details, 1):
        print_detailed_signal(index, signal, min_edge)


def print_detailed_signal(index: int, signal: Signal, min_edge: float) -> None:
    market = signal.market
    favorite = market.favorite
    hottest = market.hottest
    print()
    print(f"{index}. {display_name(signal.city)} | {market.slug}")
    print(f"   URL: https://polymarket.us/event/{market.slug}")
    print(
        f"   Traded: {fmt_number(market.shares_traded)} shares | "
        f"Open interest: {fmt_number(market.open_interest)} | "
        f"24h vol: {fmt_usd(market.volume_24h)} | "
        f"Liquidity: {fmt_usd(market.liquidity)}"
    )
    print(
        f"   Favorite: {favorite.title if favorite else 'N/A'} @ "
        f"{fmt_prob(favorite.display_price if favorite else None)} | "
        f"Hottest bucket: {hottest.title if hottest else 'N/A'}"
    )
    if signal.forecast:
        print(
            f"   Forecast: high {fmt_temp_c(signal.forecast.high_c)}, "
            f"low {fmt_temp_c(signal.forecast.low_c)}, tz={signal.forecast.timezone}, "
            f"edge={fmt_signed_temp(signal.edge_c)} {edge_label(signal.edge_c, min_edge)}"
        )
    else:
        print("   Forecast: unavailable")
    if signal.ensemble:
        print(
            f"   Ensemble: mean {fmt_temp_c(signal.ensemble.mean_c)}, "
            f"range {fmt_temp_c(min(signal.ensemble.member_maxes_c))}-"
            f"{fmt_temp_c(max(signal.ensemble.member_maxes_c))}, "
            f"sd {signal.ensemble.stdev_c:.1f}C, n={len(signal.ensemble.member_maxes_c)}"
        )
        print(f"   Ensemble buckets: {ensemble_histogram(signal.ensemble)}")
    else:
        print("   Ensemble: unavailable")
    if signal.model_forecasts:
        print("   Model comparison:")
        for model in signal.model_forecasts:
            error = f" error={model.error}" if model.error else ""
            spread = f" sd={model.stdev_c:.1f}C n={model.member_count}" if model.stdev_c is not None else ""
            print(
                f"     {model.name:20.20} {model.kind:13.13} "
                f"high={fmt_temp_c(model.high_c):>12} bucket={model.bucket_title or 'N/A'}"
                f"{spread}{error}"
            )
    print("   Buckets:")
    for bucket in market.buckets:
        markers = []
        if favorite and bucket.title == favorite.title:
            markers.append("fav")
        if signal.forecast_bucket and bucket.title == signal.forecast_bucket.title:
            markers.append("forecast")
        if signal.ensemble_bucket and bucket.title == signal.ensemble_bucket.title:
            markers.append("ensemble")
        marker_text = f" [{', '.join(markers)}]" if markers else ""
        print(
            f"     {bucket.title:22.22} px={fmt_prob(bucket.display_price):>6} "
            f"ask={fmt_prob(bucket.best_ask):>6} bid={fmt_prob(bucket.best_bid):>6} "
            f"sh={fmt_number(bucket.shares_traded):>8} temp={fmt_temp_c(bucket.temp_c):>7}"
            f"{marker_text}"
        )


def signal_to_json(signal: Signal) -> dict[str, Any]:
    favorite = signal.market.favorite
    return {
        "city": signal.city,
        "city_display": display_name(signal.city),
        "station": station_code(signal.city),
        "station_location": station_location(signal.city),
        "slug": signal.market.slug,
        "url": f"https://polymarket.us/event/{signal.market.slug}",
        "volume_24h": signal.market.volume_24h,
        "total_volume": signal.market.total_volume,
        "liquidity": signal.market.liquidity,
        "shares_traded": signal.market.shares_traded,
        "open_interest": signal.market.open_interest,
        "favorite_bucket": favorite.title if favorite else None,
        "favorite_price": favorite.display_price if favorite else None,
        "favorite_temp_c": favorite.temp_c if favorite else None,
        "forecast_high_c": signal.forecast.high_c if signal.forecast else None,
        "edge_c": signal.edge_c,
        "ensemble_mean_c": signal.ensemble.mean_c if signal.ensemble else None,
        "ensemble_edge_c": signal.ensemble_edge_c,
        "model_forecasts": [
            {
                "name": model.name,
                "kind": model.kind,
                "high_c": model.high_c,
                "bucket_title": model.bucket_title,
                "member_count": model.member_count,
                "stdev_c": model.stdev_c,
                "error": model.error,
            }
            for model in signal.model_forecasts
        ],
        "buckets": [
            {
                "title": bucket.title,
                "slug": bucket.slug,
                "yes_price": bucket.yes_price,
                "no_price": bucket.no_price,
                "current_price": bucket.current_price,
                "best_bid": bucket.best_bid,
                "best_ask": bucket.best_ask,
                "shares_traded": bucket.shares_traded,
                "open_interest": bucket.open_interest,
                "volume_24h": bucket.volume_24h,
                "temp_c": bucket.temp_c,
            }
            for bucket in signal.market.buckets
        ],
    }


def run_simulation_cycle(
    args: argparse.Namespace,
    session: requests.Session,
    state_path: Path,
    signals: list[Signal],
    target_date: date,
    emit: bool,
) -> dict[str, Any]:
    state = load_simulation_state(state_path)
    positions = state.setdefault("positions", [])
    if not isinstance(positions, list):
        positions = []
        state["positions"] = positions

    marked_positions = mark_simulated_positions(session, positions)
    state["positions"] = marked_positions
    new_positions = []

    if not args.simulate_check_only:
        sim_state = simulation_positions_as_trade_state(marked_positions)
        candidates = choose_trade_candidates(args, signals, state=sim_state, target_date=target_date)
        for candidate in candidates:
            if simulated_position_exists(marked_positions, target_date, candidate):
                logging.debug("Simulation already has position for %s", candidate.market_slug)
                continue
            new_position = simulated_position_from_candidate(candidate, target_date)
            marked_positions.append(new_position)
            new_positions.append(new_position)
            logging.info("Added simulated weather position: %s", new_position["market_slug"])
            append_operation_log(Path(args.operations_log), "simulated_order", new_position)
        state["positions"] = marked_positions
        if not new_positions:
            logging.debug("Simulation found no new candidate above threshold")

    summary = simulation_summary(marked_positions, new_positions)
    save_state(state_path, state)
    if emit:
        print_simulation_report(summary)
    return summary


def load_simulation_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"positions": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read simulation state file %s; starting empty", path)
        return {"positions": []}
    if not isinstance(state, dict):
        return {"positions": []}
    if not isinstance(state.get("positions"), list):
        state["positions"] = []
    return state


def mark_simulated_positions(
    session: requests.Session,
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    marked = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        market_slug = str(position.get("market_slug") or "")
        side = str(position.get("side") or "YES").upper()
        quantity = to_decimal(position.get("quantity")) or Decimal("0")
        entry_price = to_decimal(position.get("entry_price")) or Decimal("0")
        mark_price = to_decimal(position.get("mark_price")) or Decimal("0")
        settlement_price = Decimal("0")
        side = str(position.get("side") or "YES").upper()

        if market_slug:
            try:
                market_data = fetch_market_bbo(session, market_slug)
                mark_price = market_mark_price_for_side(market_data, side)
                settlement_price = market_settlement_price_for_side(market_data, side) or Decimal("0")
            except requests.RequestException as exc:
                logging.warning("Could not mark simulated position %s: %s", market_slug, exc)

        cost = entry_price * quantity
        mark_value = mark_price * quantity
        updated = dict(position)
        updated.update(
            {
                "mark_price": format_decimal(quantize_price(mark_price)),
                "mark_value": format_decimal(quantize_money(mark_value)),
                "unrealized_pnl": format_decimal(quantize_money(mark_value - cost)),
                "settlement_price": format_decimal(quantize_price(settlement_price)),
                "last_marked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        marked.append(updated)
        time.sleep(0.03)
    return marked


def simulated_position_from_candidate(candidate: TradeCandidate, target_date: date) -> dict[str, Any]:
    cost = candidate.ask_price * candidate.quantity
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"{target_date.isoformat()}:{candidate.market_slug}",
        "target_date": target_date.isoformat(),
        "city": candidate.city,
        "city_display": display_name(candidate.city),
        "station": station_code(candidate.city),
        "station_location": station_location(candidate.city),
        "event_slug": candidate.event_slug,
        "market_slug": candidate.market_slug,
        "bucket_title": candidate.bucket_title,
        "side": candidate.side,
        "intent": candidate.intent,
        "quantity": format_decimal(candidate.quantity),
        "entry_price": format_decimal(candidate.ask_price),
        "cost": format_decimal(quantize_money(cost)),
        "model_probability": format_decimal(candidate.model_probability),
        "value_metric": format_decimal(candidate.value_metric),
        "opened_at": now,
        "last_marked_at": now,
        "mark_price": format_decimal(candidate.ask_price),
        "mark_value": format_decimal(quantize_money(cost)),
        "unrealized_pnl": "0",
        "settlement_price": "0",
    }


def simulated_position_exists(
    positions: list[dict[str, Any]],
    target_date: date,
    candidate: TradeCandidate,
) -> bool:
    position_id = f"{target_date.isoformat()}:{candidate.market_slug}"
    return any(position.get("id") == position_id for position in positions if isinstance(position, dict))


def simulation_positions_as_trade_state(positions: list[dict[str, Any]]) -> dict[str, Any]:
    traded_markets = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        position_id = str(position.get("id") or "")
        if position_id:
            traded_markets[position_id] = position
    return {"traded_markets": traded_markets}


def simulation_summary(
    positions: list[dict[str, Any]],
    new_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    total_cost = Decimal("0")
    total_value = Decimal("0")
    for position in positions:
        total_cost += to_decimal(position.get("cost")) or Decimal("0")
        total_value += to_decimal(position.get("mark_value")) or Decimal("0")
    return {
        "positions_count": len(positions),
        "total_cost": format_decimal(quantize_money(total_cost)),
        "total_mark_value": format_decimal(quantize_money(total_value)),
        "total_unrealized_pnl": format_decimal(quantize_money(total_value - total_cost)),
        "new_positions": new_positions,
        "new_position": new_positions[0] if new_positions else None,
        "positions": positions,
    }


def print_simulation_report(summary: dict[str, Any]) -> None:
    print()
    print("Simulated Runner")
    print("=" * 94)
    print(
        f"Positions: {summary['positions_count']} | "
        f"Cost: ${summary['total_cost']} | "
        f"Mark: ${summary['total_mark_value']} | "
        f"P/L: {fmt_signed_usd(summary['total_unrealized_pnl'])}"
    )
    new_positions = summary.get("new_positions") if isinstance(summary.get("new_positions"), list) else []
    if new_positions:
        print("New paper bids:")
        for new_position in new_positions:
            print(
                f"  {new_position['city_display']} {new_position['bucket_title']} "
                f"{new_position.get('side', 'YES')} {new_position['quantity']} @ {new_position['entry_price']} "
                f"value={new_position['value_metric']}"
            )
    else:
        print("New paper bids: none")

    positions = summary.get("positions") if isinstance(summary.get("positions"), list) else []
    if not positions:
        return
    print("Open paper positions:")
    for position in positions[-8:]:
        print(
            f"  {position.get('city_display', position.get('city', 'N/A')):16.16} "
            f"{str(position.get('side', 'YES')):3.3} {str(position.get('bucket_title', 'N/A')):14.14} "
            f"qty={position.get('quantity', '0'):>5} "
            f"entry={position.get('entry_price', '0'):>5} "
            f"mark={position.get('mark_price', '0'):>5} "
            f"p/l={fmt_signed_usd(position.get('unrealized_pnl')):>8} "
            f"{position.get('market_slug', '')}"
        )


def market_mark_price(market_data: dict[str, Any]) -> Decimal:
    return market_mark_price_for_side(market_data, "YES")


def market_mark_price_for_side(market_data: dict[str, Any], side: str) -> Decimal:
    if side.upper() == "NO":
        for value in (
            price_value(market_data.get("bestAsk")),
            price_value(market_data.get("currentPx")),
            price_value(market_data.get("lastTradePx")),
            price_value(market_data.get("settlementPx")),
        ):
            parsed = to_decimal(value)
            if parsed is not None:
                return quantize_price(Decimal("1") - parsed)
        return Decimal("0")
    for value in (
        price_value(market_data.get("bestBid")),
        price_value(market_data.get("currentPx")),
        price_value(market_data.get("lastTradePx")),
        price_value(market_data.get("settlementPx")),
    ):
        parsed = to_decimal(value)
        if parsed is not None:
            return quantize_price(parsed)
    return Decimal("0")


def market_settlement_price(market_data: dict[str, Any]) -> Decimal | None:
    return market_settlement_price_for_side(market_data, "YES")


def market_settlement_price_for_side(market_data: dict[str, Any], side: str) -> Decimal | None:
    parsed = to_decimal(price_value(market_data.get("settlementPx")))
    if parsed is None:
        return None
    if side.upper() == "NO":
        return quantize_price(Decimal("1") - parsed)
    return quantize_price(parsed)


def maybe_place_weather_trades(
    args: argparse.Namespace,
    trader: WeatherTrader,
    session: requests.Session,
    state_path: Path,
    signals: list[Signal],
    target_date: date,
) -> None:
    state = load_state(state_path)
    state_changed = False
    if trader.live:
        state_changed = reconcile_live_trade_state(args, trader, state, target_date) or state_changed
    if trader.live and args.cancel_stale_orders:
        state_changed = cancel_stale_live_orders(args, trader, state, target_date) or state_changed
    if trader.live and args.manage_exits:
        state_changed = manage_live_exits(args, trader, session, state, target_date) or state_changed
    if state_changed:
        save_state(state_path, state)


def reconcile_live_trade_state(
    args: argparse.Namespace,
    trader: WeatherTrader,
    state: dict[str, Any],
    target_date: date,
) -> bool:
    tracked = [
        position
        for position in (state.get("traded_markets") or {}).values()
        if isinstance(position, dict)
        and str(position.get("order_status") or "pending").casefold()
        not in {"cancelled", "closed", "close_requested"}
        and (parse_position_target_date(position) or target_date) == target_date
    ]
    market_slugs = sorted({str(position.get("market_slug") or "") for position in tracked if position.get("market_slug")})
    if not market_slugs:
        return False

    changed = False
    open_order_ids: set[str] = set()
    try:
        for order in trader.list_open_orders(market_slugs):
            order_id = extract_order_id(order)
            if order_id:
                open_order_ids.add(order_id)
    except Exception as exc:  # noqa: BLE001 - SDK errors vary by transport/version.
        logging.warning("Could not list open live orders for reconciliation: %s", exc)

    position_by_market: dict[str, dict[str, Any]] = {}
    for market_slug in market_slugs:
        try:
            position = trader.get_position(market_slug)
        except Exception as exc:  # noqa: BLE001 - SDK errors vary by transport/version.
            logging.warning("Could not fetch live position for %s: %s", market_slug, exc)
            continue
        if isinstance(position, dict) and position_quantity(position) > 0:
            position_by_market[market_slug] = position

    now = datetime.now(timezone.utc).isoformat()
    for position in tracked:
        market_slug = str(position.get("market_slug") or "")
        order_id = str(position.get("order_id") or "")
        portfolio_position = position_by_market.get(market_slug)
        if portfolio_position:
            quantity = position_quantity(portfolio_position)
            cost = amount_decimal((portfolio_position.get("cost") or {}).get("value"))
            cash_value = amount_decimal((portfolio_position.get("cashValue") or {}).get("value"))
            updates = {
                "order_status": "filled",
                "filled_at": position.get("filled_at") or now,
                "fill_quantity": format_decimal(quantize_quantity(quantity)),
                "quantity": format_decimal(quantize_quantity(quantity)),
                "cost": format_decimal(quantize_money(cost)) if cost is not None else position.get("cost"),
                "mark_value": format_decimal(quantize_money(cash_value)) if cash_value is not None else position.get("mark_value"),
                "last_reconciled_at": now,
            }
        elif order_id and order_id in open_order_ids:
            updates = {"order_status": "pending", "last_reconciled_at": now}
        else:
            current_status = str(position.get("order_status") or "pending").casefold()
            updates = (
                {"order_status": "unfilled", "last_reconciled_at": now}
                if current_status == "pending"
                else {"last_reconciled_at": now}
            )
        for key, value in updates.items():
            if position.get(key) != value:
                position[key] = value
                changed = True
    if changed:
        append_operation_log(
            Path(args.operations_log),
            "live_state_reconciled",
            {"markets": len(market_slugs), "open_orders": len(open_order_ids), "filled_positions": len(position_by_market)},
        )
    return changed

    candidates = choose_trade_candidates(args, signals, state=state, target_date=target_date)
    if not candidates:
        logging.debug("No weather trade clears value threshold %s", format_decimal(args.value_threshold))
        return

    for candidate in candidates:
        message = (
            f"Trade candidate: buy {format_decimal(candidate.quantity)} {candidate.side} "
            f"{display_name(candidate.city)} {candidate.bucket_title} "
            f"@ {format_decimal(candidate.ask_price)} | "
            f"model={format_decimal(candidate.model_probability)} "
            f"value={format_decimal(candidate.value_metric)} "
            f"slug={candidate.market_slug}"
        )
        if not args.json:
            print()
            print(("LIVE " if trader.live else "DRY RUN ") + message)
        logging.info("%s", message)
        append_operation_log(
            Path(args.operations_log),
            "live_order" if trader.live else "dry_run_order",
            trade_candidate_to_json(candidate) or {},
        )

        order_id = trader.place_limit_buy(candidate)
        if trader.live:
            trade_key = weather_trade_key(target_date, candidate)
            state.setdefault("traded_markets", {})[trade_key] = {
                "order_id": order_id,
                "city": candidate.city,
                "city_display": display_name(candidate.city),
                "station": station_code(candidate.city),
                "station_location": station_location(candidate.city),
                "event_slug": candidate.event_slug,
                "market_slug": candidate.market_slug,
                "bucket_title": candidate.bucket_title,
                "side": candidate.side,
                "intent": candidate.intent,
                "ask_price": format_decimal(candidate.ask_price),
                "model_probability": format_decimal(candidate.model_probability),
                "value_metric": format_decimal(candidate.value_metric),
                "quantity": format_decimal(candidate.quantity),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "order_status": "pending",
                "intended_cost": format_decimal(quantize_money(candidate.ask_price * candidate.quantity)),
                "cost": "0",
            }
        save_state(state_path, state)


def cancel_stale_live_orders(
    args: argparse.Namespace,
    trader: WeatherTrader,
    state: dict[str, Any],
    target_date: date,
) -> bool:
    changed = False
    now = datetime.now(timezone.utc)
    stale_after = max(args.stale_order_seconds, 0)
    for trade_key, position in list((state.get("traded_markets") or {}).items()):
        if not isinstance(position, dict):
            continue
        if not is_bot_order_pending(position, target_date):
            continue
        created_at = parse_api_datetime(position.get("created_at"))
        if created_at is None or (now - created_at).total_seconds() < stale_after:
            continue
        order_id = str(position.get("order_id") or "")
        market_slug = str(position.get("market_slug") or "")
        if not order_id or not market_slug:
            continue
        try:
            trader.cancel_order(order_id, market_slug)
        except Exception as exc:  # noqa: BLE001 - SDK errors vary by transport/version.
            logging.warning("Could not cancel stale order %s (%s): %s", order_id, market_slug, exc)
            continue
        position.update(
            {
                "order_status": "cancelled",
                "cancelled_at": now.isoformat(),
                "cancel_reason": "stale_order",
            }
        )
        append_operation_log(
            Path(args.operations_log),
            "cancel_stale_order",
            {"trade_key": trade_key, "order_id": order_id, "market_slug": market_slug},
        )
        changed = True
    return changed


def manage_live_exits(
    args: argparse.Namespace,
    trader: WeatherTrader,
    session: requests.Session,
    state: dict[str, Any],
    target_date: date,
) -> bool:
    changed = False
    now = datetime.now(timezone.utc)
    for trade_key, position in list((state.get("traded_markets") or {}).items()):
        if not isinstance(position, dict):
            continue
        if not is_position_pending(position, target_date):
            continue
        order_status = str(position.get("order_status") or "").casefold()
        if order_status in {"cancelled", "closed", "close_requested"}:
            continue
        if order_status == "pending":
            continue
        market_slug = str(position.get("market_slug") or "")
        if not market_slug:
            continue
        side = str(position.get("side") or "YES").upper()
        quantity = to_decimal(position.get("quantity")) or Decimal("0")
        entry_price = to_decimal(position.get("ask_price")) or to_decimal(position.get("entry_price"))
        if entry_price is None or entry_price <= 0 or quantity <= 0:
            continue
        try:
            market_data = fetch_market_bbo(session, market_slug)
        except requests.RequestException as exc:
            logging.warning("Could not mark live position %s for exit check: %s", market_slug, exc)
            continue

        mark_price = market_mark_price_for_side(market_data, side)
        settlement_price = market_settlement_price_for_side(market_data, side)
        cost = entry_price * quantity
        mark_value = mark_price * quantity
        pnl = mark_value - cost
        pnl_pct = pnl / cost if cost else Decimal("0")
        position.update(
            {
                "mark_price": format_decimal(quantize_price(mark_price)),
                "mark_value": format_decimal(quantize_money(mark_value)),
                "unrealized_pnl": format_decimal(quantize_money(pnl)),
                "unrealized_pnl_pct": format_decimal(quantize_money(pnl_pct)),
                "settlement_price": format_decimal(quantize_price(settlement_price)) if settlement_price is not None else None,
                "last_marked_at": now.isoformat(),
            }
        )
        changed = True
        if settlement_price is not None:
            continue
        exit_reason = None
        if pnl_pct <= -abs(args.stop_loss_pct):
            exit_reason = "stop_loss"
        elif pnl_pct >= abs(args.take_profit_pct):
            exit_reason = "take_profit"
        if exit_reason is None:
            continue
        try:
            close_order_id = trader.close_position(market_slug, mark_price, args.exit_slippage_bips)
        except Exception as exc:  # noqa: BLE001 - SDK errors vary by transport/version.
            logging.warning("Could not close position %s for %s: %s", market_slug, exit_reason, exc)
            continue
        position.update(
            {
                "order_status": "close_requested",
                "exit_reason": exit_reason,
                "exit_order_id": close_order_id,
                "exit_requested_at": now.isoformat(),
            }
        )
        append_operation_log(
            Path(args.operations_log),
            "close_position",
            {
                "trade_key": trade_key,
                "market_slug": market_slug,
                "reason": exit_reason,
                "mark_price": format_decimal(quantize_price(mark_price)),
                "pnl": format_decimal(quantize_money(pnl)),
                "pnl_pct": format_decimal(quantize_money(pnl_pct)),
                "exit_order_id": close_order_id,
            },
        )
    return changed


def choose_trade_candidate(args: argparse.Namespace, signals: list[Signal]) -> TradeCandidate | None:
    candidates = choose_trade_candidates(args, signals)
    return candidates[0] if candidates else None


def choose_trade_candidates(
    args: argparse.Namespace,
    signals: list[Signal],
    state: dict[str, Any] | None = None,
    target_date: date | None = None,
) -> list[TradeCandidate]:
    if args.no_ensemble and not args.use_model_blend:
        logging.debug("Skipping trade evaluation because --no-ensemble was set")
        return []
    min_quantity = quantize_quantity(args.min_shares)
    max_quantity = quantize_quantity(args.max_shares)
    if max_quantity <= 0:
        logging.debug("Skipping trade evaluation because max shares is %s", args.max_shares)
        return []
    if min_quantity <= 0:
        logging.debug("Skipping trade evaluation because min shares is %s", args.min_shares)
        return []
    if min_quantity > max_quantity:
        logging.debug(
            "Skipping trade evaluation because min shares %s exceeds max shares %s",
            args.min_shares,
            args.max_shares,
        )
        return []

    candidates = []
    for signal in signals:
        if not signal.ensemble and not signal.model_forecasts:
            continue
        current_temp_c = current_temp_for_signal(signal, target_date) if args.current_temp_filter else None
        for bucket in signal.market.buckets:
            if bucket.temp_c is None or bucket.trade_price is None:
                continue
            if current_temp_c is not None and bucket_is_below_current_temperature(bucket, current_temp_c):
                logging.debug(
                    "Skipping %s %s: current temp %.1fC has already breached bucket",
                    signal.city,
                    bucket.slug,
                    current_temp_c,
                )
                continue
            model_probability = trade_bucket_probability(args, signal, bucket)
            if model_probability is None:
                continue
            add_side_candidate(
                candidates,
                args,
                signal,
                bucket,
                side="YES",
                intent="ORDER_INTENT_BUY_LONG",
                model_probability=model_probability,
                raw_price=bucket.trade_price,
                min_quantity=min_quantity,
                max_quantity=max_quantity,
            )
            add_side_candidate(
                candidates,
                args,
                signal,
                bucket,
                side="NO",
                intent="ORDER_INTENT_BUY_SHORT",
                model_probability=Decimal("1") - model_probability,
                raw_price=bucket.no_trade_price,
                min_quantity=min_quantity,
                max_quantity=max_quantity,
            )

    candidates.sort(key=lambda candidate: candidate.value_metric, reverse=True)
    return select_trade_candidates(args, candidates, state, target_date)


def trade_bucket_probability(
    args: argparse.Namespace,
    signal: Signal,
    bucket: Bucket,
) -> Decimal | None:
    if args.use_model_blend:
        blended = blended_model_bucket_probability(signal, bucket)
        if blended is not None:
            return blended
        logging.debug("No model blend available for %s; falling back to ensemble", signal.city)
    return ensemble_bucket_probability(signal, bucket)


def current_temp_for_signal(signal: Signal, target_date: date | None) -> float | None:
    if target_date is None or signal.forecast is None:
        return None
    if signal.forecast.current_temp_c is None:
        return None
    try:
        forecast_now = datetime.now(ZoneInfo(signal.forecast.timezone))
    except Exception:  # noqa: BLE001 - timezone strings can vary by API response.
        forecast_now = datetime.now()
    if forecast_now.date() != target_date:
        return None
    return signal.forecast.current_temp_c


def bucket_is_below_current_temperature(bucket: Bucket, current_temp_c: float) -> bool:
    bounds = market_slug_temp_bounds_c(bucket.slug)
    if bounds is None:
        return False
    _low_c, high_c = bounds
    if high_c is None:
        return False
    return current_temp_c >= high_c


def market_slug_temp_bounds_c(market_slug: str) -> tuple[float | None, float | None] | None:
    polymarketdata_bounds = polymarketdata_slug_bounds_c(market_slug)
    if polymarketdata_bounds is not None:
        return polymarketdata_bounds

    lt_match = re.search(r"-lt(\d+(?:\.\d+)?)f$", market_slug)
    if lt_match:
        return None, fahrenheit_to_c(float(lt_match.group(1)))
    gte_match = re.search(r"-gte(\d+(?:\.\d+)?)(?:f)?$", market_slug)
    if gte_match:
        return fahrenheit_to_c(float(gte_match.group(1))), None
    range_match = re.search(r"-gte(\d+(?:\.\d+)?)lt(\d+(?:\.\d+)?)f$", market_slug)
    if range_match:
        return fahrenheit_to_c(float(range_match.group(1))), fahrenheit_to_c(float(range_match.group(2)))
    return None


def blended_model_bucket_probability(signal: Signal, bucket: Bucket) -> Decimal | None:
    usable = [
        model
        for model in signal.model_forecasts
        if model.bucket_title and not model.error
    ]
    if not usable:
        return None
    count = sum(1 for model in usable if model.bucket_title == bucket.title)
    return Decimal(count) / Decimal(len(usable))


def add_side_candidate(
    candidates: list[TradeCandidate],
    args: argparse.Namespace,
    signal: Signal,
    bucket: Bucket,
    side: str,
    intent: str,
    model_probability: Decimal,
    raw_price: float | None,
    min_quantity: Decimal,
    max_quantity: Decimal,
) -> None:
    ask_price = to_decimal(raw_price)
    if ask_price is None:
        return
    if ask_price < args.trade_min_price or ask_price > args.trade_max_price:
        return
    if args.forecast_confirmation and not side_confirmed_by_primary_forecast(signal, bucket, side):
        logging.debug(
            "Skipping %s %s: primary forecast does not confirm %s",
            side,
            bucket.slug,
            bucket.title,
        )
        return
    value_metric = model_probability - ask_price
    if value_metric < args.value_threshold:
        return
    quantity = sized_trade_quantity(value_metric, args.value_threshold, min_quantity, max_quantity)
    candidates.append(
        TradeCandidate(
            city=signal.city,
            event_slug=signal.market.slug,
            market_slug=bucket.slug,
            bucket_title=bucket.title,
            side=side,
            intent=intent,
            model_probability=model_probability,
            ask_price=quantize_price(ask_price),
            value_metric=value_metric,
            quantity=quantity,
        )
    )


def side_confirmed_by_primary_forecast(signal: Signal, bucket: Bucket, side: str) -> bool:
    if signal.forecast is None:
        return False
    yes_wins = market_slug_wins(bucket.slug, signal.forecast.high_c)
    return yes_wins if side == "YES" else not yes_wins


def select_trade_candidates(
    args: argparse.Namespace,
    candidates: list[TradeCandidate],
    state: dict[str, Any] | None,
    target_date: date | None,
) -> list[TradeCandidate]:
    selected = []
    used_events: set[str] = set()
    traded_markets = state.get("traded_markets", {}) if isinstance(state, dict) else {}
    pending_by_city = pending_city_counts(traded_markets.values(), target_date)
    city_cap = max(args.max_pending_trades_per_city, 0)
    for candidate in candidates:
        if target_date and weather_trade_key(target_date, candidate) in traded_markets:
            continue
        if not args.allow_multiple_per_event and candidate.event_slug in used_events:
            continue
        if city_cap and pending_by_city.get(candidate.city, 0) >= city_cap:
            logging.debug(
                "Skipping %s %s: city %s already has %s pending trades",
                candidate.side,
                candidate.market_slug,
                candidate.city,
                pending_by_city.get(candidate.city, 0),
            )
            continue
        selected.append(candidate)
        used_events.add(candidate.event_slug)
        pending_by_city[candidate.city] = pending_by_city.get(candidate.city, 0) + 1
        if len(selected) >= max(args.max_trades_per_cycle, 0):
            break
    return selected


def pending_city_counts(positions: Iterable[Any], target_date: date | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    for position in positions:
        if not isinstance(position, dict):
            continue
        row_target_date = parse_position_target_date(position)
        if target_date is not None and row_target_date is not None and row_target_date != target_date:
            continue
        if not is_position_pending(position, target_date):
            continue
        city = str(position.get("city") or "").strip()
        if city:
            counts[city] += 1
    return counts


def is_position_pending(position: dict[str, Any], target_date: date | None) -> bool:
    if str(position.get("order_status") or "").casefold() in {"cancelled", "closed", "close_requested", "unfilled"}:
        return False
    row_target_date = parse_position_target_date(position) or target_date
    if row_target_date is None:
        return True
    settlement_price = to_decimal(position.get("settlement_price"))
    return position_status(row_target_date, settlement_price) == "pending"


def is_bot_order_pending(position: dict[str, Any], target_date: date | None) -> bool:
    status = str(position.get("order_status") or "pending").casefold()
    if status != "pending":
        return False
    return is_position_pending(position, target_date)


def sized_trade_quantity(
    value_metric: Decimal,
    threshold: Decimal,
    min_quantity: Decimal,
    max_quantity: Decimal,
) -> Decimal:
    if threshold <= 0:
        return max_quantity
    scale = (value_metric / threshold).to_integral_value(rounding=ROUND_DOWN)
    scaled = max(min_quantity, Decimal(scale))
    return quantize_quantity(min(scaled, max_quantity))


def ensemble_bucket_probability(signal: Signal, bucket: Bucket) -> Decimal | None:
    if not signal.ensemble:
        return None
    total = len(signal.ensemble.member_maxes_c)
    if total == 0:
        return None

    count = 0
    for member_temp in signal.ensemble.member_maxes_c:
        assigned = nearest_bucket(signal.market.buckets, member_temp)
        if assigned and assigned.slug == bucket.slug:
            count += 1
    return Decimal(count) / Decimal(total)


def trade_candidate_to_json(candidate: TradeCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "city": candidate.city,
        "city_display": display_name(candidate.city),
        "event_slug": candidate.event_slug,
        "market_slug": candidate.market_slug,
        "bucket_title": candidate.bucket_title,
        "side": candidate.side,
        "intent": candidate.intent,
        "model_probability": str(candidate.model_probability),
        "ask_price": str(candidate.ask_price),
        "value_metric": str(candidate.value_metric),
        "quantity": str(candidate.quantity),
    }


def weather_trade_key(target_date: date, candidate: TradeCandidate) -> str:
    return f"{target_date.isoformat()}:{candidate.market_slug}"


def write_dashboard(
    args: argparse.Namespace,
    session: requests.Session,
    signals: list[Signal],
    target_date: date,
) -> None:
    dashboard_path = Path(args.dashboard)
    live_state = load_state(Path(args.state))
    sim_state = load_simulation_state(Path(args.simulation_state))
    live_rows = mark_position_rows(
        session,
        list((live_state.get("traded_markets") or {}).values()),
        target_date,
        source="live",
    )
    paper_rows = mark_position_rows(
        session,
        sim_state.get("positions") if isinstance(sim_state.get("positions"), list) else [],
        target_date,
        source="paper",
    )
    rows = live_rows + paper_rows
    signal_rows = [dashboard_signal_row(signal) for signal in signals]
    html_text = render_dashboard_html(target_date, signal_rows, rows)
    try:
        dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        dashboard_path.write_text(html_text, encoding="utf-8")
    except OSError as exc:
        raise WeatherScannerError(f"Could not write dashboard {dashboard_path}: {exc}") from exc
    append_operation_log(
        Path(args.operations_log),
        "dashboard_written",
        {"path": str(dashboard_path), "positions": len(rows), "signals": len(signal_rows)},
    )


def mark_position_rows(
    session: requests.Session,
    positions: list[dict[str, Any]],
    target_date: date,
    source: str,
) -> list[dict[str, Any]]:
    rows = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        market_slug = str(position.get("market_slug") or "")
        side = str(position.get("side") or "YES").upper()
        order_status = normalized_order_status(position, source)
        financially_active = order_status in {"filled", "close_requested", "closed"}
        quantity = to_decimal(position.get("quantity")) or Decimal("0")
        entry_price = (
            to_decimal(position.get("ask_price"))
            or to_decimal(position.get("entry_price"))
            or Decimal("0")
        )
        mark_price = to_decimal(position.get("mark_price")) or Decimal("0")
        settlement_price = to_decimal(position.get("settlement_price"))
        if financially_active and market_slug:
            try:
                market_data = fetch_market_bbo(session, market_slug)
                mark_price = market_mark_price_for_side(market_data, side)
                settlement_price = market_settlement_price_for_side(market_data, side)
            except requests.RequestException as exc:
                logging.debug("Could not mark dashboard position %s: %s", market_slug, exc)

        cost = entry_price * quantity if financially_active else Decimal("0")
        mark_value = mark_price * quantity if financially_active else Decimal("0")
        row_target_date = parse_position_target_date(position) or target_date
        status = display_position_status(order_status, row_target_date, settlement_price)
        row = {
            "source": source,
            "status": status,
            "city": str(position.get("city") or ""),
            "city_display": str(position.get("city_display") or display_name(str(position.get("city") or ""))),
            "station": str(position.get("station") or station_code(str(position.get("city") or ""))),
            "station_location": str(
                position.get("station_location") or station_location(str(position.get("city") or ""))
            ),
            "bucket_title": str(position.get("bucket_title") or ""),
            "market_slug": market_slug,
            "event_slug": str(position.get("event_slug") or ""),
            "order_id": str(position.get("order_id") or ""),
            "side": side,
            "intent": str(position.get("intent") or ""),
            "quantity": format_decimal(quantity),
            "entry_price": format_decimal(quantize_price(entry_price)),
            "mark_price": format_decimal(quantize_price(mark_price)) if financially_active else "0",
            "cost": format_decimal(quantize_money(cost)),
            "mark_value": format_decimal(quantize_money(mark_value)),
            "pnl": format_decimal(quantize_money(mark_value - cost)),
            "settlement_price": format_decimal(quantize_price(settlement_price or Decimal("0"))),
            "model_probability": str(position.get("model_probability") or ""),
            "value_metric": str(position.get("value_metric") or ""),
            "created_at": str(position.get("created_at") or position.get("opened_at") or ""),
        }
        rows.append(row)
        time.sleep(0.03)
    return rows


def dashboard_signal_row(signal: Signal) -> dict[str, Any]:
    favorite = signal.market.favorite
    return {
        "city": display_name(signal.city),
        "station": station_code(signal.city),
        "station_location": station_location(signal.city),
        "event_slug": signal.market.slug,
        "traded": fmt_number(signal.market.shares_traded),
        "favorite": favorite.title if favorite else "N/A",
        "favorite_price": fmt_prob(favorite.display_price if favorite else None),
        "forecast": fmt_temp_c(signal.forecast.high_c if signal.forecast else None),
        "forecast_bucket": signal.forecast_bucket.title if signal.forecast_bucket else "N/A",
        "ensemble_mean": fmt_temp_c(signal.ensemble.mean_c if signal.ensemble else None),
        "ensemble_bucket": signal.ensemble_bucket.title if signal.ensemble_bucket else "N/A",
        "edge": fmt_signed_temp(signal.edge_c),
        "ensemble_edge": fmt_signed_temp(signal.ensemble_edge_c),
        "models": model_forecast_summary(signal.model_forecasts),
    }


def model_forecast_summary(models: list[ModelForecast]) -> str:
    if not models:
        return "N/A"
    buckets = [model.bucket_title for model in models if model.bucket_title]
    if not buckets:
        return "N/A"
    counts = Counter(buckets)
    return " | ".join(f"{bucket}:{count}" for bucket, count in counts.most_common(4))


def aggregate_backtest_results(city_results: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for city in city_results:
        for section in ("models", "blends"):
            for name, metrics in (city.get(section) or {}).items():
                buckets.setdefault(name, []).append(metrics)
    output = {}
    for name, metrics_list in buckets.items():
        output[name] = aggregate_metric_list(metrics_list)
    return output


def aggregate_metric_list(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    total_n = sum(int(metrics.get("n") or 0) for metrics in metrics_list)
    if total_n == 0:
        return {"n": 0, "mae_c": None, "rmse_c": None, "bias_c": None, "bin_hit_rate": None}
    mae = weighted_metric(metrics_list, "mae_c")
    bias = weighted_metric(metrics_list, "bias_c")
    bin_hit = weighted_metric(metrics_list, "bin_hit_rate")
    rmse_terms = [
        (float(metrics["rmse_c"]) ** 2, int(metrics.get("n") or 0))
        for metrics in metrics_list
        if metrics.get("rmse_c") is not None and int(metrics.get("n") or 0) > 0
    ]
    rmse = math.sqrt(sum(value * n for value, n in rmse_terms) / sum(n for _, n in rmse_terms)) if rmse_terms else None
    return {"n": total_n, "mae_c": mae, "rmse_c": rmse, "bias_c": bias, "bin_hit_rate": bin_hit}


def weighted_metric(metrics_list: list[dict[str, Any]], key: str) -> float | None:
    terms = [
        (float(metrics[key]), int(metrics.get("n") or 0))
        for metrics in metrics_list
        if metrics.get(key) is not None and int(metrics.get("n") or 0) > 0
    ]
    if not terms:
        return None
    return sum(value * n for value, n in terms) / sum(n for _, n in terms)


def print_backtest_report(result: dict[str, Any]) -> None:
    print()
    print(
        "Weather Model Backtest | "
        f"{result['start_date']} to {result['end_date']} | "
        f"lead={result['lead_days']}d | bin={result['bin_f']}F"
    )
    print("=" * 110)
    for city in result.get("cities", []):
        print()
        print(
            f"{city['city_display']} ({city.get('station') or 'N/A'} "
            f"{city.get('station_location') or ''}) | days={city['days']}"
        )
        print("-" * 110)
        print_metric_table("Models", city.get("models") or {})
        print_metric_table("Blends", city.get("blends") or {})
        print_trading_table(city.get("trading") or {})
    print()
    print("Overall")
    print("-" * 110)
    print_metric_table("All", result.get("overall") or {})
    if result.get("trading_overall"):
        print_trading_table({"available": True, "strategies": result.get("trading_overall") or {}})


def print_metric_table(title: str, rows: dict[str, dict[str, Any]]) -> None:
    if not rows:
        print(f"{title}: no data")
        return
    print(f"{title}:")
    print(f"  {'Name':32} {'N':>4} {'MAE':>8} {'RMSE':>8} {'Bias':>8} {'BinHit':>8}")
    for name, metrics in sorted(
        rows.items(),
        key=lambda item: (
            math.inf if item[1].get("mae_c") is None else item[1].get("mae_c"),
            item[0],
        ),
    ):
        print(
            f"  {name:32.32} "
            f"{int(metrics.get('n') or 0):>4} "
            f"{fmt_metric_c(metrics.get('mae_c')):>8} "
            f"{fmt_metric_c(metrics.get('rmse_c')):>8} "
            f"{fmt_metric_c(metrics.get('bias_c'), signed=True):>8} "
            f"{fmt_pct(metrics.get('bin_hit_rate')):>8}"
        )


def print_trading_table(trading: dict[str, Any]) -> None:
    if not trading.get("available"):
        reason = trading.get("reason") or "market history unavailable"
        print(f"Trading P/L: unavailable ({reason})")
        return
    rows = trading.get("strategies") or {}
    if not rows:
        print("Trading P/L: no trades")
        return
    print("Trading P/L:")
    print(f"  {'Strategy':32} {'Trades':>6} {'Win%':>8} {'Cost':>8} {'P/L':>8} {'ROI':>8}")
    for name, summary in sorted(
        rows.items(),
        key=lambda item: (to_decimal(item[1].get("pnl")) or Decimal("0")),
        reverse=True,
    ):
        print(
            f"  {name:32.32} "
            f"{int(summary.get('trades') or 0):>6} "
            f"{fmt_pct(summary.get('win_rate')):>8} "
            f"{('$' + str(summary.get('cost'))) if summary.get('cost') is not None else 'N/A':>8} "
            f"{fmt_signed_usd(summary.get('pnl')):>8} "
            f"{fmt_pct(summary.get('roi')):>8}"
        )


def write_backtest_dashboard(args: argparse.Namespace, result: dict[str, Any]) -> None:
    path = Path(args.backtest_dashboard)
    html_text = render_backtest_dashboard_html(result)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_text, encoding="utf-8")
    except OSError as exc:
        raise WeatherScannerError(f"Could not write backtest dashboard {path}: {exc}") from exc
    append_operation_log(
        Path(args.operations_log),
        "backtest_dashboard_written",
        {"path": str(path), "cities": len(result.get("cities") or [])},
    )


def render_backtest_dashboard_html(result: dict[str, Any]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    overall = result.get("overall") or {}
    best_overall = best_metric_name(overall)
    cities = result.get("cities") if isinstance(result.get("cities"), list) else []
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Weather Model Backtest Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f5ef;
      --panel: #ffffff;
      --ink: #1d2528;
      --muted: #66757a;
      --line: #d9ded6;
      --good: #176b4d;
      --bad: #a13d32;
      --accent: #215a75;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    header {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
    h1 {{ margin: 0; font-size: 26px; letter-spacing: 0; }}
    h2 {{ margin: 28px 0 10px; font-size: 16px; letter-spacing: 0; }}
    h3 {{ margin: 18px 0 8px; font-size: 14px; letter-spacing: 0; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .metric div:first-child {{ color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; font-size: 22px; margin-top: 6px; overflow-wrap: anywhere; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 9px 10px; font-size: 13px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 650; background: #fbfaf6; }}
    tr:last-child td {{ border-bottom: none; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .spark {{ min-width: 180px; }}
    @media (max-width: 1000px) {{
      main {{ padding: 14px; }}
      header {{ display: block; }}
      .metrics, .grid {{ grid-template-columns: 1fr; }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Weather Model Backtest Dashboard</h1>
      <div class="muted">{h(result.get('start_date'))} to {h(result.get('end_date'))} | lead={h(result.get('lead_days'))}d | bin={h(result.get('bin_f'))}F</div>
    </div>
    <div class="muted">Generated {h(generated_at)}</div>
  </header>

  <section class="metrics">
    <div class="metric"><div>Cities</div><strong>{len(cities)}</strong></div>
    <div class="metric"><div>Best Overall</div><strong>{h(best_overall or 'N/A')}</strong></div>
    <div class="metric"><div>Best MAE</div><strong>{h(fmt_metric_c(metric_value(overall, best_overall, 'mae_c')))}</strong></div>
    <div class="metric"><div>Best Bin Hit</div><strong>{h(fmt_pct(metric_value(overall, best_overall, 'bin_hit_rate')))}</strong></div>
  </section>

  <section>
    <h2>Overall Ranking</h2>
    {render_backtest_metric_table(overall, include_spark=False)}
  </section>

  <section>
    <h2>Overall Trading P/L</h2>
    {render_backtest_trading_table(result.get('trading_overall') or {})}
  </section>

  <section>
    <h2>Regions</h2>
    <div class="grid">
      {''.join(render_backtest_city_panel(city) for city in cities)}
    </div>
  </section>
</main>
</body>
</html>
"""


def render_backtest_city_panel(city: dict[str, Any]) -> str:
    models = city.get("models") or {}
    blends = city.get("blends") or {}
    merged = {**models, **blends}
    best = best_metric_name(merged)
    return (
        '<section class="panel">'
        f"<h3>{h(city.get('city_display'))} <span class=\"muted\">{h(city.get('station'))} {h(city.get('station_location'))}</span></h3>"
        f"<div class=\"muted\">Days: {h(city.get('days'))} | Best: {h(best or 'N/A')} ({h(fmt_metric_c(metric_value(merged, best, 'mae_c')))})</div>"
        "<h3>Trading P/L</h3>"
        f"{render_backtest_trading_table((city.get('trading') or {}).get('strategies') or {}, (city.get('trading') or {}).get('reason'))}"
        "<h3>Model Ranking</h3>"
        f"{render_backtest_metric_table(models, include_spark=True)}"
        "<h3>Blend Ranking</h3>"
        f"{render_backtest_metric_table(blends, include_spark=True)}"
        "</section>"
    )


def render_backtest_metric_table(rows: dict[str, dict[str, Any]], include_spark: bool) -> str:
    if not rows:
        return '<table><tbody><tr><td class="muted">No data.</td></tr></tbody></table>'
    header = (
        "<table><thead><tr><th>Name</th><th class=\"num\">N</th><th class=\"num\">MAE</th>"
        "<th class=\"num\">RMSE</th><th class=\"num\">Bias</th><th class=\"num\">Bin Hit</th>"
    )
    if include_spark:
        header += "<th>Recent Error</th>"
    header += "</tr></thead><tbody>"
    body = []
    for name, metrics in sorted(
        rows.items(),
        key=lambda item: (
            math.inf if item[1].get("mae_c") is None else item[1].get("mae_c"),
            item[0],
        ),
    ):
        bias = to_optional_float(metrics.get("bias_c"))
        bias_class = "good" if bias is not None and bias < 0 else "bad" if bias is not None and bias > 0 else ""
        body.append(
            "<tr>"
            f"<td>{h(name)}</td>"
            f"<td class=\"num\">{int(metrics.get('n') or 0)}</td>"
            f"<td class=\"num\">{h(fmt_metric_c(metrics.get('mae_c')))}</td>"
            f"<td class=\"num\">{h(fmt_metric_c(metrics.get('rmse_c')))}</td>"
            f"<td class=\"num {bias_class}\">{h(fmt_metric_c(metrics.get('bias_c'), signed=True))}</td>"
            f"<td class=\"num\">{h(fmt_pct(metrics.get('bin_hit_rate')))}</td>"
            + (f"<td class=\"spark\">{render_error_sparkline(metrics.get('samples') or [])}</td>" if include_spark else "")
            + "</tr>"
        )
    return header + "".join(body) + "</tbody></table>"


def render_backtest_trading_table(rows: dict[str, dict[str, Any]], unavailable_reason: str | None = None) -> str:
    if not rows:
        text = unavailable_reason or "No trading simulation data."
        return f'<table><tbody><tr><td class="muted">{h(text)}</td></tr></tbody></table>'
    body = []
    for name, summary in sorted(
        rows.items(),
        key=lambda item: (to_decimal(item[1].get("pnl")) or Decimal("0")),
        reverse=True,
    ):
        pnl = to_decimal(summary.get("pnl")) or Decimal("0")
        body.append(
            "<tr>"
            f"<td>{h(name)}</td>"
            f"<td class=\"num\">{int(summary.get('trades') or 0)}</td>"
            f"<td class=\"num\">{h(fmt_pct(summary.get('win_rate')))}</td>"
            f"<td class=\"num\">${h(summary.get('cost'))}</td>"
            f"<td class=\"num {pnl_class(pnl)}\">{h(fmt_signed_usd(pnl))}</td>"
            f"<td class=\"num\">{h(fmt_pct(summary.get('roi')))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Strategy</th><th class=\"num\">Trades</th><th class=\"num\">Win%</th>"
        "<th class=\"num\">Cost</th><th class=\"num\">P/L</th><th class=\"num\">ROI</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def render_error_sparkline(samples: list[dict[str, Any]]) -> str:
    errors = [to_optional_float(sample.get("error_c")) for sample in samples if isinstance(sample, dict)]
    values = [value for value in errors if value is not None]
    if not values:
        return '<span class="muted">N/A</span>'
    width = 220
    height = 46
    pad = 6
    lo = min(values + [0.0])
    hi = max(values + [0.0])
    span = hi - lo or 1.0
    points = []
    for index, value in enumerate(values):
        x = pad if len(values) == 1 else pad + index * (width - pad * 2) / (len(values) - 1)
        y = height - pad - ((value - lo) / span) * (height - pad * 2)
        points.append(f"{x:.1f},{y:.1f}")
    zero_y = height - pad - ((0.0 - lo) / span) * (height - pad * 2)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="220" height="46" role="img">'
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width-pad}" y2="{zero_y:.1f}" stroke="#d9ded6"/>'
        f'<polyline fill="none" stroke="#215a75" stroke-width="2" points="{" ".join(points)}"/>'
        "</svg>"
    )


def best_metric_name(rows: dict[str, dict[str, Any]]) -> str | None:
    usable = [
        (name, metrics)
        for name, metrics in rows.items()
        if isinstance(metrics, dict) and metrics.get("mae_c") is not None
    ]
    if not usable:
        return None
    return min(usable, key=lambda item: (item[1].get("mae_c"), item[0]))[0]


def metric_value(rows: dict[str, dict[str, Any]], name: str | None, key: str) -> Any:
    if not name:
        return None
    metrics = rows.get(name) if isinstance(rows, dict) else None
    return metrics.get(key) if isinstance(metrics, dict) else None


def render_dashboard_html(
    target_date: date,
    signals: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> str:
    total_cost = sum((to_decimal(row.get("cost")) or Decimal("0")) for row in positions)
    total_value = sum((to_decimal(row.get("mark_value")) or Decimal("0")) for row in positions)
    total_pnl = total_value - total_cost
    status_counts = Counter(str(row.get("status") or "pending") for row in positions)
    chart_svg = render_pnl_chart(positions)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Polymarket Weather Bot Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f5ef;
      --panel: #ffffff;
      --ink: #1d2528;
      --muted: #66757a;
      --line: #d9ded6;
      --good: #176b4d;
      --bad: #a13d32;
      --warn: #9b6a1c;
      --accent: #215a75;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    header {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
    h1 {{ margin: 0; font-size: 26px; letter-spacing: 0; }}
    h2 {{ margin: 28px 0 10px; font-size: 16px; letter-spacing: 0; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .metric div:first-child {{ color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; font-size: 24px; margin-top: 6px; }}
    .grid {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 18px; align-items: start; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 9px 10px; font-size: 13px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 650; background: #fbfaf6; }}
    tr:last-child td {{ border-bottom: none; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .status {{ font-weight: 700; }}
    .pending {{ color: var(--warn); }}
    .filled {{ color: var(--accent); }}
    .won {{ color: var(--good); }}
    .lost {{ color: var(--bad); }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .chart {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-height: 220px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    @media (max-width: 1000px) {{
      main {{ padding: 14px; }}
      .metrics, .grid {{ grid-template-columns: 1fr; }}
      header {{ display: block; }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Polymarket Weather Bot Dashboard</h1>
      <div class="muted">Target {h(target_date.isoformat())} | Generated {h(generated_at)} | Local bot state only</div>
    </div>
    <div class="muted">Settlement stations: KNYC, KSFO, KMIA, KMDW, KLAX</div>
  </header>

  <section class="metrics">
    <div class="metric"><div>Positions</div><strong>{len(positions)}</strong></div>
    <div class="metric"><div>Cost</div><strong>${h(format_decimal(quantize_money(total_cost)))}</strong></div>
    <div class="metric"><div>Mark Value</div><strong>${h(format_decimal(quantize_money(total_value)))}</strong></div>
    <div class="metric"><div>P/L</div><strong class="{pnl_class(total_pnl)}">{h(fmt_signed_usd(total_pnl))}</strong></div>
  </section>

  <div class="grid">
    <section>
      <h2>Placed Orders And Positions</h2>
      {render_positions_table(positions)}
    </section>
    <section>
      <h2>P/L Graph</h2>
      <div class="chart">{chart_svg}</div>
      <div class="muted">Pending {status_counts.get('pending', 0)} | Filled {status_counts.get('filled', 0)} | Won {status_counts.get('won', 0)} | Lost {status_counts.get('lost', 0)}</div>
    </section>
  </div>

  <section>
    <h2>Weather Edge Location Table</h2>
    {render_signals_table(signals)}
  </section>
</main>
</body>
</html>
"""


def render_positions_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<table><tbody><tr><td class="muted">No local bot positions yet.</td></tr></tbody></table>'
    body = []
    for row in rows:
        status = str(row.get("status") or "pending")
        pnl = to_decimal(row.get("pnl")) or Decimal("0")
        body.append(
            "<tr>"
            f"<td class=\"status {h(status)}\">{h(status)}</td>"
            f"<td>{h(row.get('source'))}</td>"
            f"<td>{h(row.get('city_display'))}<br><span class=\"muted\">{h(row.get('station'))} {h(row.get('station_location'))}</span></td>"
            f"<td>{h(row.get('side'))} {h(row.get('bucket_title'))}<br><span class=\"muted\">{h(row.get('market_slug'))}</span></td>"
            f"<td class=\"num\">{h(row.get('quantity'))}</td>"
            f"<td class=\"num\">{h(row.get('entry_price'))}</td>"
            f"<td class=\"num\">{h(row.get('mark_price'))}</td>"
            f"<td class=\"num {pnl_class(pnl)}\">{h(fmt_signed_usd(pnl))}</td>"
            f"<td class=\"num\">{h(row.get('value_metric'))}</td>"
            f"<td>{h(row.get('order_id'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Status</th><th>Book</th><th>Location</th><th>Market</th>"
        "<th class=\"num\">Qty</th><th class=\"num\">Entry</th><th class=\"num\">Mark</th>"
        "<th class=\"num\">P/L</th><th class=\"num\">Edge</th><th>Order</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def render_signals_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<table><tbody><tr><td class="muted">No active weather signals.</td></tr></tbody></table>'
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{h(row.get('city'))}<br><span class=\"muted\">{h(row.get('station'))} {h(row.get('station_location'))}</span></td>"
            f"<td>{h(row.get('favorite'))}<br><span class=\"muted\">{h(row.get('favorite_price'))}</span></td>"
            f"<td>{h(row.get('forecast'))}<br><span class=\"muted\">{h(row.get('forecast_bucket'))}</span></td>"
            f"<td>{h(row.get('ensemble_mean'))}<br><span class=\"muted\">{h(row.get('ensemble_bucket'))}</span></td>"
            f"<td>{h(row.get('models'))}</td>"
            f"<td class=\"num\">{h(row.get('edge'))}</td>"
            f"<td class=\"num\">{h(row.get('ensemble_edge'))}</td>"
            f"<td class=\"num\">{h(row.get('traded'))}</td>"
            f"<td><a href=\"https://polymarket.us/event/{h(row.get('event_slug'))}\">{h(row.get('event_slug'))}</a></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Location</th><th>Favorite</th><th>Forecast</th>"
        "<th>Ensemble</th><th>Models</th><th class=\"num\">Edge</th><th class=\"num\">Ens Edge</th>"
        "<th class=\"num\">Traded</th><th>Event</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def render_pnl_chart(rows: list[dict[str, Any]]) -> str:
    values = []
    running = Decimal("0")
    for row in sorted(rows, key=lambda item: str(item.get("created_at") or "")):
        running += to_decimal(row.get("pnl")) or Decimal("0")
        values.append(float(running))
    if not values:
        return '<div class="muted">No P/L history yet.</div>'
    width = 560
    height = 180
    pad = 24
    lo = min(values + [0.0])
    hi = max(values + [0.0])
    span = hi - lo or 1.0
    points = []
    for index, value in enumerate(values):
        x = pad if len(values) == 1 else pad + index * (width - pad * 2) / (len(values) - 1)
        y = height - pad - ((value - lo) / span) * (height - pad * 2)
        points.append(f"{x:.1f},{y:.1f}")
    zero_y = height - pad - ((0.0 - lo) / span) * (height - pad * 2)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="210" role="img">'
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width-pad}" y2="{zero_y:.1f}" stroke="#d9ded6"/>'
        f'<polyline fill="none" stroke="#215a75" stroke-width="3" points="{" ".join(points)}"/>'
        f'<text x="{pad}" y="18" fill="#66757a" font-size="12">Cumulative marked P/L</text>'
        f'<text x="{pad}" y="{height-4}" fill="#66757a" font-size="12">Last {h(fmt_signed_usd(values[-1]))}</text>'
        "</svg>"
    )


def position_status(target_date: date, settlement_price: Decimal | None) -> str:
    if not settlement_window_has_passed(target_date):
        return "pending"
    if settlement_price is None:
        return "pending"
    if settlement_price >= Decimal("0.99"):
        return "won"
    if settlement_price <= Decimal("0.01"):
        return "lost"
    return "pending"


def normalized_order_status(position: dict[str, Any], source: str) -> str:
    status = str(position.get("order_status") or "").casefold()
    if status:
        return status
    return "filled" if source == "live" else "filled"


def display_position_status(
    order_status: str,
    target_date: date,
    settlement_price: Decimal | None,
) -> str:
    if order_status in {"pending", "unfilled", "cancelled", "close_requested", "closed"}:
        return order_status
    outcome = position_status(target_date, settlement_price)
    return "filled" if outcome == "pending" else outcome


def parse_position_target_date(position: dict[str, Any]) -> date | None:
    value = position.get("target_date")
    if value:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None
    event_slug = str(position.get("event_slug") or "")
    match = re.search(r"(\d{4}-\d{2}-\d{2})", event_slug)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def settlement_window_has_passed(target_date: date) -> bool:
    eastern = ZoneInfo("America/New_York")
    settlement_time = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        8,
        0,
        tzinfo=eastern,
    ) + timedelta(days=1)
    return datetime.now(eastern) >= settlement_time


def append_operation_log(path: Path, kind: str, payload: dict[str, Any]) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "payload": payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        logging.warning("Could not append operations log %s: %s", path, exc)


def parse_bucket_temp_c(title: str) -> float | None:
    text = title.replace("−", "-").replace("–", "-").replace("—", "-")
    numbers = [float(num) for num in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if not numbers:
        return None

    lower_text = text.casefold()
    is_fahrenheit = bool(re.search(r"(?:°\s*f|\bfahrenheit\b|(?<=\d)\s*f\b)", lower_text))
    is_celsius = bool(re.search(r"(?:°\s*c|\bcelsius\b|(?<=\d)\s*c\b)", lower_text))
    value = numbers[0]

    range_match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)", lower_text)
    if range_match:
        value = (float(range_match.group(1)) + float(range_match.group(2))) / 2
    if not is_celsius and not is_fahrenheit:
        # Polymarket weather buckets usually include the unit in the event or bucket
        # text, but high US-city values are almost certainly Fahrenheit.
        if value >= 55:
            is_fahrenheit = True

    if is_fahrenheit:
        value = (value - 32) * 5 / 9
    return value


def nearest_bucket(buckets: Iterable[Bucket], temp_c: float) -> Bucket | None:
    parseable = [bucket for bucket in buckets if bucket.temp_c is not None]
    if not parseable:
        return None
    return min(parseable, key=lambda bucket: abs(float(bucket.temp_c or 0.0) - temp_c))


def bucket_sort_key(bucket: Bucket) -> tuple[int, float, str]:
    if bucket.temp_c is None:
        return (1, math.inf, bucket.title)
    return (0, bucket.temp_c, bucket.title)


def ensemble_histogram(ensemble: Ensemble) -> str:
    counts = Counter(round(value) for value in ensemble.member_maxes_c)
    total = len(ensemble.member_maxes_c)
    parts = []
    for temp, count in counts.most_common(6):
        pct = count / total * 100
        parts.append(f"{temp}C:{pct:.0f}%")
    return " ".join(parts)


def is_temperature_event_slug(slug: str, target_date: date) -> bool:
    if extract_city_from_slug(slug) is None:
        return False
    if re.fullmatch(r"temp-[a-z]+high-\d{4}-\d{2}-\d{2}", slug):
        return slug.endswith(target_date.isoformat())
    return slug.endswith(date_to_slug(target_date))


def extract_city_from_slug(slug: str) -> str | None:
    us_match = re.fullmatch(r"temp-([a-z]+)high-\d{4}-\d{2}-\d{2}", slug)
    if us_match:
        return POLYMARKET_US_WEATHER_CITY_CODES.get(us_match.group(1))

    legacy_match = re.fullmatch(
        r"highest-temperature-in-([a-z0-9-]+)-on-[a-z]+-\d{1,2}-\d{4}",
        slug,
    )
    return legacy_match.group(1) if legacy_match else None


def parse_target_date(value: str | None) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return date.today() + timedelta(days=1)


def date_to_slug(value: date) -> str:
    return f"{value.strftime('%B').lower()}-{value.day}-{value.year}"


def forecast_coords_by_city() -> dict[str, tuple[float, float]]:
    return {**CITY_COORDS, **POLYMARKET_US_STATION_COORDS}


def http_get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def disable_insecure_request_warnings() -> None:
    try:
        import urllib3
    except ImportError:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def validate_state_paths(args: argparse.Namespace) -> str | None:
    paths = {
        "--state": str(args.state),
        "--simulation-state": str(args.simulation_state),
    }
    for arg_name, value in paths.items():
        if value == "/path/to/sim.json" or value.startswith("/path/to/"):
            return (
                f"{arg_name}={value!r} is an example placeholder. "
                "Use the default path, a real path under this project, or something like "
                "/private/tmp/weather_sim.json."
            )
    return None


def load_extra_locations(path: Path, required: bool) -> None:
    if not path.exists():
        if required:
            raise WeatherScannerError(f"Location file {path} does not exist.")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeatherScannerError(f"Could not read location file {path}: {exc}") from exc

    locations = payload.get("locations") if isinstance(payload, dict) else payload
    if not isinstance(locations, list):
        raise WeatherScannerError("Location file must be a list or an object with a 'locations' list.")

    for index, item in enumerate(locations, 1):
        if not isinstance(item, dict):
            raise WeatherScannerError(f"Location #{index} is not an object.")
        register_extra_location(parse_extra_location(item, index))


def parse_extra_location(item: dict[str, Any], index: int) -> dict[str, Any]:
    slug_code = required_slug(item, "slug_code", index)
    city_slug = required_slug(item, "city_slug", index)
    station = required_text(item, "station", index).upper()
    location = required_text(item, "location", index)
    city = required_text(item, "city", index)
    source = str(item.get("source") or "")
    latitude = required_float(item, "latitude", index)
    longitude = required_float(item, "longitude", index)

    if not re.fullmatch(r"K[A-Z0-9]{3}", station):
        raise WeatherScannerError(
            f"Location #{index} station {station!r} should be a 4-character NWS station like KBOS."
        )
    return {
        "slug_code": slug_code,
        "city_slug": city_slug,
        "station": station,
        "source": source,
        "location": location,
        "city": city,
        "latitude": latitude,
        "longitude": longitude,
    }


def register_extra_location(location: dict[str, Any]) -> None:
    slug_code = str(location["slug_code"])
    city_slug = str(location["city_slug"])
    POLYMARKET_US_WEATHER_CITY_CODES[slug_code] = city_slug
    POLYMARKET_US_STATION_COORDS[city_slug] = (
        float(location["latitude"]),
        float(location["longitude"]),
    )
    POLYMARKET_US_STATION_INFO[city_slug] = {
        "station": str(location["station"]),
        "source": str(location.get("source") or ""),
        "location": str(location["location"]),
        "city": str(location["city"]),
    }
    CITY_DISPLAY[city_slug] = str(location["city"])


def required_slug(item: dict[str, Any], key: str, index: int) -> str:
    value = required_text(item, key, index).casefold()
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise WeatherScannerError(f"Location #{index} field {key!r} must be a slug.")
    return value


def required_text(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WeatherScannerError(f"Location #{index} missing required text field {key!r}.")
    return value.strip()


def required_float(item: dict[str, Any], key: str, index: int) -> float:
    value = to_optional_float(item.get(key))
    if value is None:
        raise WeatherScannerError(f"Location #{index} missing numeric field {key!r}.")
    return value


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"traded_markets": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read state file %s; starting with empty state", path)
        return {"traded_markets": {}}
    if not isinstance(state, dict):
        return {"traded_markets": {}}
    traded_markets = state.get("traded_markets")
    if not isinstance(traded_markets, dict):
        state["traded_markets"] = {}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        raise WeatherScannerError(f"Could not write state file {path}: {exc}") from exc


def quantize_price(price: Decimal) -> Decimal:
    bounded = min(max(price, MIN_PRICE), MAX_PRICE)
    return bounded.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def quantize_quantity(quantity: Decimal) -> Decimal:
    bounded = max(quantity, Decimal("0"))
    return bounded.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


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


def parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def market_yes_no_prices(market: dict[str, Any]) -> tuple[float, float]:
    yes_price = None
    no_price = None
    for side in market.get("marketSides") or []:
        if not isinstance(side, dict):
            continue
        price = (
            to_optional_float((side.get("quote") or {}).get("value"))
            or to_optional_float(side.get("price"))
        )
        description = str(side.get("description") or "").strip().casefold()
        if description == "yes" or side.get("long") is True:
            yes_price = price if price is not None else yes_price
        elif description == "no" or side.get("long") is False:
            no_price = price if price is not None else no_price

    if yes_price is not None:
        return yes_price, no_price or 0.0

    outcomes = [str(outcome).strip().casefold() for outcome in parse_json_list(market.get("outcomes"))]
    prices = [to_optional_float(price) for price in parse_json_list(market.get("outcomePrices"))]
    for outcome, price in zip(outcomes, prices):
        if price is None:
            continue
        if outcome == "yes":
            yes_price = price
        elif outcome == "no":
            no_price = price
    return yes_price or 0.0, no_price or 0.0


def bucket_unit_from_market(market: dict[str, Any]) -> str:
    text = " ".join(
        str(value)
        for value in (market.get("title"), market.get("titleShort"), market.get("description"))
        if value
    ).casefold()
    if re.search(r"(?:°\s*f|\bfahrenheit\b|(?<=\d)\s*f\b)", text):
        return "F"
    if re.search(r"(?:°\s*c|\bcelsius\b|(?<=\d)\s*c\b)", text):
        return "C"
    return ""


def to_float(value: Any, default: float = 0.0) -> float:
    parsed = to_optional_float(value)
    return default if parsed is None else parsed


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


def amount_decimal(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("value")
    return to_decimal(value)


def position_quantity(position: dict[str, Any]) -> Decimal:
    for key in ("qtyAvailable", "netPosition"):
        parsed = to_decimal(position.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    bought = to_decimal(position.get("qtyBought")) or Decimal("0")
    sold = to_decimal(position.get("qtySold")) or Decimal("0")
    return max(bought - sold, Decimal("0"))


def to_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def display_name(city: str) -> str:
    return CITY_DISPLAY.get(city, city.replace("-", " ").title())


def station_info(city: str) -> dict[str, str]:
    return POLYMARKET_US_STATION_INFO.get(city, {})


def station_code(city: str) -> str:
    return station_info(city).get("station", "")


def station_location(city: str) -> str:
    return station_info(city).get("location", "")


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def pnl_class(value: Any) -> str:
    parsed = to_decimal(value)
    if parsed is None or parsed == 0:
        return ""
    return "good" if parsed > 0 else "bad"


def fmt_number(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def fmt_signed_usd(value: Any) -> str:
    parsed = to_decimal(value)
    if parsed is None:
        return "N/A"
    sign = "+" if parsed >= 0 else "-"
    return f"{sign}${format_decimal(abs(parsed))}"


def fmt_metric_c(value: Any, signed: bool = False) -> str:
    parsed = to_optional_float(value)
    if parsed is None:
        return "N/A"
    prefix = "+" if signed and parsed >= 0 else ""
    return f"{prefix}{parsed:.2f}C"


def fmt_pct(value: Any) -> str:
    parsed = to_optional_float(value)
    if parsed is None:
        return "N/A"
    return f"{parsed * 100:.0f}%"


def fmt_market_traded(market: MarketSnapshot) -> str:
    if market.shares_traded > 0:
        return f"{fmt_number(market.shares_traded)} sh"
    return fmt_usd(market.volume_24h)


def fmt_temp_c(value: float | None) -> str:
    if value is None:
        return "N/A"
    temp_f = value * 9 / 5 + 32
    return f"{value:.1f}C/{temp_f:.0f}F"


def fmt_signed_temp(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.1f}C"


def fmt_prob(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.0f}%"


def temp_bin_f(temp_c: float, bin_f: float) -> int:
    temp_f = temp_c * 9 / 5 + 32
    width = max(bin_f, 0.1)
    return int(math.floor(temp_f / width) * width)


def fahrenheit_to_c(temp_f: float) -> float:
    return (temp_f - 32) * 5 / 9


def bin_center_c(bin_start_f: int, bin_f: float) -> float:
    center_f = bin_start_f + max(bin_f, 0.1) / 2
    return fahrenheit_to_c(center_f)


def market_slug_wins(market_slug: str, actual_c: float) -> bool:
    polymarketdata_result = polymarketdata_slug_wins(market_slug, actual_c)
    if polymarketdata_result is not None:
        return polymarketdata_result

    actual_f = actual_c * 9 / 5 + 32
    lt_match = re.search(r"-lt(\d+(?:\.\d+)?)f$", market_slug)
    if lt_match:
        return actual_f < float(lt_match.group(1))
    gte_match = re.search(r"-gte(\d+(?:\.\d+)?)(?:f)?$", market_slug)
    if gte_match:
        return actual_f >= float(gte_match.group(1))
    range_match = re.search(r"-gte(\d+(?:\.\d+)?)lt(\d+(?:\.\d+)?)f$", market_slug)
    if range_match:
        return float(range_match.group(1)) <= actual_f < float(range_match.group(2))
    return False


def bucket_temp_label(bucket: Bucket | None) -> str:
    if bucket is None:
        return "N/A"
    return fmt_temp_c(bucket.temp_c)


def edge_label(edge: float | None, min_edge: float) -> str:
    if edge is None:
        return "?"
    if abs(edge) >= min_edge:
        return "EDGE"
    if abs(edge) >= min_edge / 2:
        return "watch"
    return "-"


def run_self_test() -> None:
    cases = {
        "17C": 17.0,
        "17°C": 17.0,
        "88-89F": (88.5 - 32) * 5 / 9,
        "88 to 89 F": (88.5 - 32) * 5 / 9,
        "75 to 76 F": (75.5 - 32) * 5 / 9,
        "72": (72 - 32) * 5 / 9,
        "-2C or lower": -2.0,
    }
    for title, expected in cases.items():
        actual = parse_bucket_temp_c(title)
        if actual is None or abs(actual - expected) > 0.01:
            raise WeatherScannerError(f"parse_bucket_temp_c({title!r})={actual}, expected {expected}")

    slug = "highest-temperature-in-london-on-june-10-2026"
    if extract_city_from_slug(slug) != "london":
        raise WeatherScannerError("city extraction failed")
    if not is_temperature_event_slug(slug, date(2026, 6, 10)):
        raise WeatherScannerError("temperature slug matching failed")

    us_slug = "temp-sfohigh-2026-06-10"
    if extract_city_from_slug(us_slug) != "san-francisco":
        raise WeatherScannerError("US city extraction failed")
    if not is_temperature_event_slug(us_slug, date(2026, 6, 10)):
        raise WeatherScannerError("US temperature slug matching failed")

    yes_price, no_price = market_yes_no_prices(
        {
            "outcomes": '["No","Yes"]',
            "outcomePrices": '["0.1600","0.2900"]',
            "marketSides": [
                {"description": "Yes", "long": True, "quote": {"value": "0.2900"}},
                {"description": "No", "long": False, "quote": {"value": "0.84"}},
            ],
        }
    )
    if yes_price != 0.29 or no_price != 0.84:
        raise WeatherScannerError("US side price parsing failed")

    no_fallback_bucket = Bucket(
        slug="test",
        title="79 or above",
        yes_price=0.22,
        no_price=0.0,
        volume=0.0,
        volume_24h=0.0,
        best_bid=0.22,
        best_ask=0.25,
        current_price=0.23,
        shares_traded=0.0,
        open_interest=0.0,
        bid_depth=1,
        ask_depth=1,
        token_ids=[],
        temp_c=26.1,
    )
    if no_fallback_bucket.no_trade_price is None or abs(no_fallback_bucket.no_trade_price - 0.78) > 0.001:
        raise WeatherScannerError("NO side price fallback failed")

    split_bucket = Bucket(
        slug="split",
        title="75 to 76",
        yes_price=0.5,
        no_price=0.5,
        volume=0.0,
        volume_24h=0.0,
        best_bid=0.49,
        best_ask=0.51,
        current_price=0.5,
        shares_traded=0.0,
        open_interest=0.0,
        bid_depth=1,
        ask_depth=1,
        token_ids=[],
        temp_c=24.2,
    )
    split_signal = Signal(
        city="test",
        event={},
        market=MarketSnapshot(
            slug="test",
            title="test",
            total_volume=0.0,
            volume_24h=0.0,
            liquidity=0.0,
            shares_traded=0.0,
            open_interest=0.0,
            buckets=[split_bucket, no_fallback_bucket],
        ),
        forecast=None,
        ensemble=None,
        edge_c=None,
        ensemble_edge_c=None,
        forecast_bucket=None,
        ensemble_bucket=None,
        model_forecasts=[
            ModelForecast("a", "deterministic", 24.2, "75 to 76", 1, None),
            ModelForecast("b", "deterministic", 26.1, "79 or above", 1, None),
        ],
    )
    if blended_model_bucket_probability(split_signal, split_bucket) != Decimal("0.5"):
        raise WeatherScannerError("model blend split probability failed")

    if sized_trade_quantity(Decimal("0.16"), Decimal("0.15"), Decimal("1"), Decimal("3")) != Decimal("1.0000"):
        raise WeatherScannerError("minimum trade sizing failed")
    if sized_trade_quantity(Decimal("0.31"), Decimal("0.15"), Decimal("1"), Decimal("3")) != Decimal("2.0000"):
        raise WeatherScannerError("scaled trade sizing failed")
    if sized_trade_quantity(Decimal("0.70"), Decimal("0.15"), Decimal("1"), Decimal("3")) != Decimal("3.0000"):
        raise WeatherScannerError("maximum trade sizing failed")

    parsed_location = parse_extra_location(
        {
            "slug_code": "bos",
            "city_slug": "boston",
            "city": "Boston",
            "station": "KBOS",
            "source": "CLIBOS",
            "location": "Boston Logan International Airport",
            "latitude": 42.3656,
            "longitude": -71.0096,
        },
        1,
    )
    if parsed_location["station"] != "KBOS" or parsed_location["slug_code"] != "bos":
        raise WeatherScannerError("extra location parsing failed")

    cap_args = argparse.Namespace(
        allow_multiple_per_event=True,
        max_trades_per_cycle=3,
        max_pending_trades_per_city=2,
    )
    cap_state = {
        "traded_markets": {
            "2026-06-10:sfo-a": {"city": "san-francisco", "event_slug": "a"},
            "2026-06-10:sfo-b": {"city": "san-francisco", "event_slug": "b"},
            "2026-06-09:sfo-old": {
                "city": "san-francisco",
                "event_slug": "old",
                "target_date": "2026-06-09",
            },
        }
    }
    cap_candidates = [
        TradeCandidate(
            city="san-francisco",
            event_slug="temp-sfohigh-2026-06-10",
            market_slug="sfo-c",
            bucket_title="70 or below",
            side="YES",
            intent="ORDER_INTENT_BUY_LONG",
            model_probability=Decimal("0.8"),
            ask_price=Decimal("0.1"),
            value_metric=Decimal("0.7"),
            quantity=Decimal("1"),
        ),
        TradeCandidate(
            city="nyc",
            event_slug="temp-nychigh-2026-06-10",
            market_slug="nyc-a",
            bucket_title="83 or above",
            side="NO",
            intent="ORDER_INTENT_BUY_SHORT",
            model_probability=Decimal("0.8"),
            ask_price=Decimal("0.2"),
            value_metric=Decimal("0.6"),
            quantity=Decimal("1"),
        ),
    ]
    selected = select_trade_candidates(cap_args, cap_candidates, cap_state, date(2026, 6, 10))
    if [candidate.market_slug for candidate in selected] != ["nyc-a"]:
        raise WeatherScannerError("pending city cap failed")
    cap_state_different_day = {
        "traded_markets": {
            "2026-06-09:sfo-old": {
                "city": "san-francisco",
                "event_slug": "old",
                "target_date": "2026-06-09",
            },
        }
    }
    selected = select_trade_candidates(cap_args, cap_candidates, cap_state_different_day, date(2026, 6, 10))
    if [candidate.market_slug for candidate in selected] != ["sfo-c", "nyc-a"]:
        raise WeatherScannerError("pending city cap should be target-date scoped")

    actual = {"2026-06-01": 30.0, "2026-06-02": 31.0}
    predictions = {"2026-06-01": 31.0, "2026-06-02": 30.0}
    metrics = score_predictions(actual, predictions, 2.0)
    if metrics["n"] != 2 or abs(float(metrics["mae_c"]) - 1.0) > 0.001:
        raise WeatherScannerError("backtest scoring failed")
    if not market_slug_wins("tc-temp-test-gte94lt95f", (94.2 - 32) * 5 / 9):
        raise WeatherScannerError("range market settlement matching failed")
    if not market_slug_wins("tc-temp-test-lt71f", (70.0 - 32) * 5 / 9):
        raise WeatherScannerError("lower-tail market settlement matching failed")
    if market_slug_wins("tc-temp-test-gte83f", (82.0 - 32) * 5 / 9):
        raise WeatherScannerError("upper-tail market settlement matching failed")
    blends = build_blend_predictions(
        actual,
        {"a": {"2026-06-01": 30.0}, "b": {"2026-06-01": 32.0}},
        {"a": {"n": 1, "mae_c": 1.0}, "b": {"n": 1, "mae_c": 3.0}},
        2.0,
    )
    if "equal_average" not in blends or "inverse_mae_weighted_average" not in blends:
        raise WeatherScannerError("backtest blend generation failed")
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
