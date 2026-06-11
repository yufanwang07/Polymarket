#!/usr/bin/env python3
"""
Collect sportsbook odds and write the normalized odds file consumed by
arbitrage_bot.py.

Primary supported feed:
  - The Odds API v4, using official HTTP endpoints.
  - Odds-API.io v3, using the free-tier-friendly events + odds/multi flow.

Utility sources:
  - generic-json/public-html for a provider page/export you are allowed to access.
  - ggbet-json/ggbet-html and spinbetter-json/spinbetter-html aliases around
    generic extraction, useful when you have a permitted page, endpoint, saved
    response, or feed export for those books.

This script writes JSONL by default so arbitrage_bot.py can reload fresh rows
each loop without needing provider-specific logic in the trading path.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.error import URLError
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


THE_ODDS_API_BASE_URL = "https://api.the-odds-api.com"
ODDS_API_IO_BASE_URL = "https://api.odds-api.io"
POLYMARKET_US_MARKETS_URL = "https://gateway.polymarket.us/v1/markets"
POLYMARKET_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_MARKETS_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
KALSHI_SERIES_URL = "https://external-api.kalshi.com/trade-api/v2/series"
DEFAULT_ESPORT_HINTS = ("esports", "counter", "cs2", "csgo", "dota", "league of legends", "lol", "valorant")
DEFAULT_KALSHI_SERIES_KEYWORDS = (
    "esports",
    "valorant",
    "cs2",
    "counter strike",
    "counter-strike",
    "dota",
    "league of legends",
    "lol",
    "game winner",
    "match winner",
)
DEFAULT_MARKETS = "h2h"
DEFAULT_REGIONS = "us,us2,uk,eu,au"
DEFAULT_BOOKMAKERS = (
    "ggbet",
    "pinnacle",
    "betonlineag",
    "bovada",
    "betfair",
    "bet365",
    "unibet",
    "williamhill",
    "ladbrokes",
    "betvictor",
    "mybookieag",
)
EGAMERSWORLD_GAME_URLS = {
    "all": "https://egamersworld.com/bets",
    "cs2": "https://egamersworld.com/bets",
    "dota2": "https://egamersworld.com/bets",
    "lol": "https://egamersworld.com/bets",
    "valorant": "https://egamersworld.com/bets",
}


@dataclass(frozen=True)
class OddsRow:
    event_id: str
    bookmaker: str
    sport: str
    league: str
    team_a: str
    team_b: str
    odds_a: Decimal
    odds_b: Decimal
    starts_at: str | None
    observed_at: str
    market_slug: str | None = None


class OddsCollectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrossVenueTarget:
    event_id: str
    market_slug: str
    title: str
    description: str
    sport: str
    league: str
    outcome_a: str
    outcome_b: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    starts_at: str | None


@dataclass(frozen=True)
class CrossVenueReference:
    source: str
    reference_id: str
    title: str
    description: str
    outcome_a: str
    outcome_b: str
    probability_a: Decimal
    spread: Decimal | None
    observed_at: str


def main() -> int:
    load_env_file(Path.cwd() / ".env")
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    while True:
        try:
            rows = collect(args)
        except OddsCollectorError as exc:
            logging.error("%s", exc)
            if args.once:
                return 1
            time.sleep(args.refresh_seconds)
            continue
        if not rows and not args.allow_empty_write:
            logging.warning(
                "Collected 0 odds rows; leaving %s unchanged. Pass --allow-empty-write to overwrite anyway.",
                args.output,
            )
            if args.once:
                return 1
            time.sleep(args.refresh_seconds)
            continue
        write_rows(
            rows,
            Path(args.output),
            append=args.append,
            fmt=args.format,
            merge_existing=args.merge_existing,
        )
        logging.info("Wrote %s odds rows to %s", len(rows), args.output)
        if args.once:
            return 0
        time.sleep(args.refresh_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect esports sportsbook odds for arbitrage_bot.py")
    parser.add_argument(
        "--provider",
        choices=[
            "the-odds-api",
            "odds-api-io",
            "generic-json",
            "public-html",
            "ggbet-json",
            "ggbet-html",
            "spinbetter-json",
            "spinbetter-html",
            "egamersworld-html",
            "egamersworld-auto",
            "cross-venue",
        ],
        default=os.getenv("ODDS_PROVIDER", "the-odds-api"),
    )
    parser.add_argument("--output", default="arbitrage/odds.jsonl")
    parser.add_argument("--format", choices=["jsonl", "json", "csv"], default="jsonl")
    parser.add_argument("--append", action="store_true", help="append instead of replacing output")
    parser.add_argument(
        "--merge-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="merge/upsert with existing output rows by event_id+bookmaker",
    )
    parser.add_argument(
        "--allow-empty-write",
        action="store_true",
        help="allow a failed/empty scrape to overwrite the output with zero rows",
    )
    parser.add_argument("--once", action="store_true", help="collect once and exit")
    parser.add_argument("--refresh-seconds", type=float, default=60)
    parser.add_argument("--market-slug-map", help="optional JSON file mapping event_id to Polymarket slug")
    parser.add_argument(
        "--bookmakers",
        default=os.getenv("ODDS_BOOKMAKERS", ""),
        help="comma-separated bookmaker keys for providers that support filtering",
    )
    parser.add_argument("--sport", action="append", default=[], help="provider sport key; can repeat")
    parser.add_argument("--markets", default=DEFAULT_MARKETS)
    parser.add_argument("--regions", default=DEFAULT_REGIONS)
    parser.add_argument("--max-events-per-sport", type=int, default=50)
    parser.add_argument(
        "--cross-venue-sources",
        default=os.getenv("CROSS_VENUE_SOURCES", "kalshi"),
        help="comma-separated reference venues for --provider cross-venue",
    )
    parser.add_argument("--target-limit", type=int, default=1000, help="max wide Polymarket US targets to compare per loop")
    parser.add_argument("--target-scan-limit", type=int, default=15000, help="max Polymarket US markets to page through")
    parser.add_argument("--reference-limit", type=int, default=300, help="max reference markets per source")
    parser.add_argument("--target-max-pages", type=int, default=100)
    parser.add_argument("--reference-max-pages", type=int, default=3)
    parser.add_argument("--target-min-spread", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--reference-max-spread", type=Decimal, default=Decimal("0.04"))
    parser.add_argument("--min-reference-spread-advantage", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--match-threshold", type=Decimal, default=Decimal("0.72"))
    parser.add_argument(
        "--target-keywords",
        default=os.getenv("CROSS_VENUE_TARGET_KEYWORDS", ""),
        help="comma-separated words that must appear in target text; useful for diagnostics",
    )
    parser.add_argument(
        "--target-market-types",
        default=os.getenv("CROSS_VENUE_TARGET_MARKET_TYPES", "moneyline,drawable_outcome"),
        help="comma-separated Polymarket US marketType/sportsMarketType values; empty allows all",
    )
    parser.add_argument("--kalshi-category", default=os.getenv("KALSHI_CATEGORY", ""))
    parser.add_argument(
        "--kalshi-series",
        default=os.getenv("KALSHI_SERIES", "auto"),
        help="comma-separated Kalshi series tickers, or auto/all for catalog discovery",
    )
    parser.add_argument(
        "--kalshi-series-scan-limit",
        type=int,
        default=int(os.getenv("KALSHI_SERIES_SCAN_LIMIT", "1000")),
        help="max Kalshi series to inspect when --kalshi-series is auto/all",
    )
    parser.add_argument(
        "--kalshi-market-scan-limit",
        type=int,
        default=int(os.getenv("KALSHI_MARKET_SCAN_LIMIT", "5000")),
        help="max open Kalshi markets to inspect when --kalshi-series is auto/all",
    )
    parser.add_argument(
        "--kalshi-series-keywords",
        default=os.getenv("KALSHI_SERIES_KEYWORDS", ",".join(DEFAULT_KALSHI_SERIES_KEYWORDS)),
        help="comma-separated keywords used to choose series in auto mode",
    )
    parser.add_argument(
        "--kalshi-market-url",
        action="append",
        default=[],
        help="Kalshi market URL to seed as a reference; can repeat",
    )
    parser.add_argument(
        "--kalshi-ticker",
        action="append",
        default=[],
        help="Kalshi market ticker to seed as a reference; can repeat",
    )
    parser.add_argument(
        "--cross-venue-fetch-books",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="fetch US Gateway books while scanning wide target markets",
    )
    parser.add_argument("--api-key", default=os.getenv("THE_ODDS_API_KEY") or os.getenv("ODDS_API_IO_KEY"))
    parser.add_argument("--the-odds-api-key", default=os.getenv("THE_ODDS_API_KEY"))
    parser.add_argument(
        "--the-odds-api-keys",
        default=os.getenv("THE_ODDS_API_KEYS"),
        help="comma-separated The Odds API keys; requests rotate through this pool",
    )
    parser.add_argument("--odds-api-io-key", default=os.getenv("ODDS_API_IO_KEY"))
    parser.add_argument(
        "--odds-api-io-keys",
        default=os.getenv("ODDS_API_IO_KEYS"),
        help="comma-separated Odds-API.io keys; requests rotate through this pool",
    )
    parser.add_argument("--source-url", help="generic JSON URL")
    parser.add_argument("--source-file", help="generic JSON/HTML file")
    parser.add_argument(
        "--egw-game",
        action="append",
        default=[],
        choices=sorted(EGAMERSWORLD_GAME_URLS),
        help="EGamersWorld game page to scrape; can repeat. Default: cs2,dota2,lol,valorant",
    )
    parser.add_argument(
        "--render-html",
        action="store_true",
        help="render --source-url with Playwright before extracting JSON from HTML",
    )
    parser.add_argument("--wait-ms", type=int, default=2500, help="extra wait after page load when rendering HTML")
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="when rendering HTML, let Chromium ignore invalid site TLS certificates",
    )
    parser.add_argument(
        "--insecure-ssl",
        action="store_true",
        help="last resort for public scraping when the local Python cert store is broken",
    )
    parser.add_argument(
        "--events-path",
        default="",
        help="dot path to a list of event objects in generic JSON; empty means root",
    )
    parser.add_argument("--event-id-path", default="id")
    parser.add_argument("--team-a-path", default="home_team")
    parser.add_argument("--team-b-path", default="away_team")
    parser.add_argument("--starts-at-path", default="commence_time")
    parser.add_argument("--sport-path", default="sport_key")
    parser.add_argument("--league-path", default="sport_title")
    parser.add_argument("--bookmakers-path", default="bookmakers")
    parser.add_argument("--bookmaker-key-path", default="key")
    parser.add_argument("--bookmaker-title-path", default="title")
    parser.add_argument("--markets-path", default="markets")
    parser.add_argument("--market-key-path", default="key")
    parser.add_argument("--outcomes-path", default="outcomes")
    parser.add_argument("--outcome-name-path", default="name")
    parser.add_argument("--outcome-price-path", default="price")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.getenv("ODDS_COLLECTOR_LOG_LEVEL", "INFO").upper(),
    )
    return parser.parse_args()


def collect(args: argparse.Namespace) -> list[OddsRow]:
    market_slug_map = load_market_slug_map(Path(args.market_slug_map)) if args.market_slug_map else {}
    if args.provider == "the-odds-api":
        return collect_the_odds_api(args, market_slug_map)
    if args.provider == "odds-api-io":
        return collect_odds_api_io(args, market_slug_map)
    if args.provider == "cross-venue":
        return collect_cross_venue(args)
    if args.provider == "egamersworld-auto":
        return collect_egamersworld_auto(args, market_slug_map)
    if args.provider in {
        "generic-json",
        "public-html",
        "ggbet-json",
        "ggbet-html",
        "spinbetter-json",
        "spinbetter-html",
        "egamersworld-html",
    }:
        return collect_generic_json(args, market_slug_map)
    raise OddsCollectorError(f"Unsupported provider: {args.provider}")


def collect_cross_venue(args: argparse.Namespace) -> list[OddsRow]:
    wide_targets = [
        target
        for target in fetch_polymarket_us_targets(args)
        if target.spread is not None and target.spread >= args.target_min_spread
    ]
    targets = sorted(wide_targets, key=lambda target: target.spread or Decimal("0"), reverse=True)[
        : max(args.target_limit, 0)
    ]
    references = fetch_cross_venue_references(args)
    if not references:
        logging.warning("No cross-venue reference markets found.")
        return []

    rows: list[OddsRow] = []
    unmatched: list[tuple[CrossVenueTarget, Decimal, CrossVenueReference | None]] = []
    for target in targets:
        matches: list[tuple[CrossVenueReference, Decimal, Decimal]] = []
        best_score = Decimal("0")
        best_reference = None
        for reference in references:
            score = cross_venue_match_score(target, reference)
            if score > best_score:
                best_score = score
                best_reference = reference
            if score < args.match_threshold:
                continue
            reference_spread = reference.spread if reference.spread is not None else Decimal("0")
            if reference_spread > args.reference_max_spread:
                continue
            if target.spread is not None and target.spread - reference_spread < args.min_reference_spread_advantage:
                continue
            probability = reference_probability_for_target(target, reference)
            if probability is None:
                continue
            matches.append((reference, probability, score))

        if not matches:
            unmatched.append((target, best_score, best_reference))
            continue
        probability_a = average_decimal([probability for _, probability, _ in matches])
        if probability_a <= Decimal("0.001") or probability_a >= Decimal("0.999"):
            continue
        sources = "+".join(sorted({reference.source for reference, _, _ in matches}))
        rows.append(
            OddsRow(
                event_id=target.event_id,
                bookmaker=f"cross-venue:{sources}",
                sport=target.sport,
                league=target.league,
                team_a=target.outcome_a,
                team_b=target.outcome_b,
                odds_a=Decimal("1") / probability_a,
                odds_b=Decimal("1") / (Decimal("1") - probability_a),
                starts_at=target.starts_at,
                observed_at=now_iso(),
                market_slug=target.market_slug,
            )
        )
        best_score = max(score for _, _, score in matches)
        logging.info(
            "Matched %s to %s reference(s), fair %s=%s, target spread=%s, best score=%s",
            target.market_slug,
            len(matches),
            target.outcome_a,
            format_decimal(probability_a),
            format_decimal(target.spread or Decimal("0")),
            format_decimal(best_score),
        )
    if unmatched:
        for target, score, reference in unmatched[:10]:
            logging.info(
                "Unmatched wide target %s spread=%s best_score=%s best_ref=%s:%s",
                target.market_slug,
                format_decimal(target.spread or Decimal("0")),
                format_decimal(score),
                reference.source if reference else "<none>",
                reference.title if reference else "<none>",
            )
    return rows


def fetch_polymarket_us_targets(args: argparse.Namespace) -> list[CrossVenueTarget]:
    markets: list[dict[str, Any]] = []
    seen: set[str] = set()
    target_scan_limit = max(getattr(args, "target_scan_limit", args.target_limit), args.target_limit)
    page_limit = min(max(target_scan_limit, 1), 100)
    pages_needed = (target_scan_limit + page_limit - 1) // page_limit
    page_count = max(max(args.target_max_pages, 1), pages_needed)
    for page in range(page_count):
        if len(markets) >= target_scan_limit:
            break
        payload = http_get_json(
            POLYMARKET_US_MARKETS_URL,
            {
                "active": "true",
                "closed": "false",
                "limit": page_limit,
                "offset": page * page_limit,
            },
        )
        page_markets = unpack_markets(payload)
        fresh = 0
        for market in page_markets:
            slug = value_to_str(market.get("slug"))
            if not slug or slug in seen:
                continue
            seen.add(slug)
            markets.append(market)
            fresh += 1
            if len(markets) >= target_scan_limit:
                break
        if fresh == 0 or len(page_markets) < page_limit:
            break

    targets = []
    keywords = {normalize_name(value) for value in normalize_csv(getattr(args, "target_keywords", ""))}
    market_types = {normalize_name(value) for value in normalize_csv(getattr(args, "target_market_types", ""))}
    for market in markets:
        if keywords and not keywords.issubset(set(normalize_name(market_text(market)).split())):
            continue
        if market_types and not market_matches_type(market, market_types):
            continue
        target = polymarket_us_target_from_market(market, fetch_book=args.cross_venue_fetch_books)
        if target is not None:
            targets.append(target)
    logging.info("Scanned %s Polymarket US markets, parsed %s targets", len(markets), len(targets))
    return targets


def market_text(market: dict[str, Any]) -> str:
    return " ".join(
        value_to_str(market.get(key)) or ""
        for key in ("slug", "question", "title", "description", "outcomes", "marketType", "sportsMarketType")
    )


def market_matches_type(market: dict[str, Any], allowed: set[str]) -> bool:
    values = {
        normalize_name(value)
        for value in [
            value_to_str(market.get("marketType")),
            value_to_str(market.get("sportsMarketType")),
            value_to_str(market.get("sportsMarketTypeV2")),
        ]
        if value
    }
    return bool(values & allowed)


def polymarket_us_target_from_market(market: dict[str, Any], fetch_book: bool) -> CrossVenueTarget | None:
    slug = value_to_str(market.get("slug"))
    if not slug or market.get("closed") is True or market.get("active") is False or market.get("hidden") is True:
        return None
    outcomes = parse_json_array(market.get("outcomes"))
    if len(outcomes) != 2:
        outcomes = market_outcomes_from_sides(market)
    if len(outcomes) != 2:
        return None

    best_bid = amount_value(market.get("bestBidQuote"))
    best_ask = amount_value(market.get("bestAskQuote"))
    if fetch_book:
        try:
            book_bid, book_ask = fetch_polymarket_us_book_quote(slug)
            best_bid = book_bid if book_bid is not None else best_bid
            best_ask = book_ask if book_ask is not None else best_ask
        except Exception as exc:  # noqa: BLE001 - public book failures vary.
            logging.debug("Could not fetch Polymarket US book for %s: %s", slug, exc)
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    if spread is None or spread <= 0:
        return None

    return CrossVenueTarget(
        event_id=slug,
        market_slug=slug,
        title=value_to_str(market.get("title") or market.get("question")) or slug,
        description=value_to_str(market.get("description")) or "",
        sport=value_to_str(market.get("category") or market.get("marketType")) or "",
        league=value_to_str(market.get("sportsMarketType") or market.get("marketType")) or "",
        outcome_a=str(outcomes[0]),
        outcome_b=str(outcomes[1]),
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        starts_at=value_to_str(market.get("gameStartTime") or market.get("endDate")),
    )


def fetch_polymarket_us_book_quote(slug: str) -> tuple[Decimal | None, Decimal | None]:
    payload = http_get_json(f"{POLYMARKET_US_MARKETS_URL}/{slug}/book")
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, None
    bids = data.get("bids") if isinstance(data.get("bids"), list) else []
    asks = data.get("offers") if isinstance(data.get("offers"), list) else []
    bid_prices = [
        price
        for item in bids
        if isinstance(item, dict)
        for price in [amount_value(item.get("px")) or to_decimal(item.get("price"))]
        if price is not None
    ]
    ask_prices = [
        price
        for item in asks
        if isinstance(item, dict)
        for price in [amount_value(item.get("px")) or to_decimal(item.get("price"))]
        if price is not None
    ]
    return max(bid_prices, default=None), min(ask_prices, default=None)


def market_outcomes_from_sides(market: dict[str, Any]) -> list[str]:
    sides = [side for side in market.get("marketSides") or [] if isinstance(side, dict)]
    sides.sort(key=lambda side: 0 if side.get("long", True) else 1)
    outcomes = []
    for side in sides[:2]:
        team = side.get("team") if isinstance(side.get("team"), dict) else {}
        value = (
            side.get("description")
            or team.get("safeName")
            or team.get("name")
            or team.get("alias")
            or team.get("abbreviation")
        )
        if value:
            outcomes.append(str(value))
    return outcomes


def fetch_cross_venue_references(args: argparse.Namespace) -> list[CrossVenueReference]:
    references: list[CrossVenueReference] = []
    sources = {source.casefold() for source in normalize_csv(args.cross_venue_sources)}
    if "the-odds-api" in sources or "odds" in sources:
        references.extend(fetch_the_odds_api_references(args))
    if "kalshi" in sources:
        references.extend(fetch_kalshi_references(args))
    if "polymarket-gamma" in sources or "gamma" in sources:
        references.extend(fetch_polymarket_gamma_references(args))
    logging.info("Loaded %s cross-venue references from %s", len(references), ",".join(sorted(sources)))
    return references


def fetch_the_odds_api_references(args: argparse.Namespace) -> list[CrossVenueReference]:
    api_keys = the_odds_api_key_pool(args)
    if not api_keys:
        logging.warning("Skipping The Odds API references: no THE_ODDS_API_KEY/THE_ODDS_API_KEYS configured.")
        return []
    reference_args = argparse.Namespace(**vars(args))
    reference_args.provider = "the-odds-api"
    reference_args.sport = args.sport or ["upcoming"]
    reference_args.max_events_per_sport = max(args.reference_limit, args.max_events_per_sport)
    try:
        odds_rows = collect_the_odds_api(reference_args, {})
    except OddsCollectorError as exc:
        logging.warning("Skipping The Odds API references: %s", exc)
        return []
    references = [reference_from_odds_row(row) for row in odds_rows]
    return [reference for reference in references if reference is not None][: max(args.reference_limit, 0)]


def reference_from_odds_row(row: OddsRow) -> CrossVenueReference | None:
    implied_a = Decimal("1") / row.odds_a if row.odds_a > 1 else None
    implied_b = Decimal("1") / row.odds_b if row.odds_b > 1 else None
    if implied_a is None or implied_b is None:
        return None
    total = implied_a + implied_b
    if total <= 0:
        return None
    probability_a = implied_a / total
    title = f"{row.team_a} vs {row.team_b}"
    return CrossVenueReference(
        source=f"the-odds-api:{row.bookmaker}",
        reference_id=row.event_id,
        title=title,
        description=" ".join(part for part in [row.sport, row.league, row.starts_at or ""] if part),
        outcome_a=row.team_a,
        outcome_b=row.team_b,
        probability_a=probability_a,
        spread=total - Decimal("1"),
        observed_at=row.observed_at,
    )


def fetch_kalshi_references(args: argparse.Namespace) -> list[CrossVenueReference]:
    references: list[CrossVenueReference] = []
    seen_tickers: set[str] = set()
    for ticker in kalshi_seed_tickers(args):
        market = fetch_kalshi_market_by_ticker(ticker)
        if not market:
            continue
        reference = kalshi_reference_from_market(market)
        if reference is not None:
            seen_tickers.add(reference.reference_id)
            references.append(reference)

    for series in kalshi_series_to_scan(args):
        if len(references) >= args.reference_limit:
            break
        references.extend(fetch_kalshi_series_references(args, series, seen_tickers))

    if not references:
        references.extend(fetch_kalshi_broad_references(args, seen_tickers))
    return references[: max(args.reference_limit, 0)]


def kalshi_seed_tickers(args: argparse.Namespace) -> list[str]:
    tickers: list[str] = []
    for raw_url in getattr(args, "kalshi_market_url", []) or []:
        tickers.extend(kalshi_tickers_from_url(raw_url))
    tickers.extend(getattr(args, "kalshi_ticker", []) or [])
    return unique_nonempty([ticker.upper() for ticker in tickers])


def kalshi_seed_series(args: argparse.Namespace) -> list[str]:
    series: list[str] = []
    for raw_url in getattr(args, "kalshi_market_url", []) or []:
        series.extend(kalshi_series_from_url(raw_url))
    for ticker in kalshi_seed_tickers(args):
        series.append(ticker.split("-", 1)[0])
    return unique_nonempty([item.upper() for item in series])


def kalshi_series_from_url(raw_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(raw_url)
    series = []
    for part in parsed.path.split("/"):
        part = part.strip()
        if part.upper().startswith("KX") and "-" not in part:
            series.append(part)
    return series


def kalshi_series_to_scan(args: argparse.Namespace) -> list[str]:
    requested = [item.upper() for item in normalize_csv(getattr(args, "kalshi_series", ""))]
    seeded = kalshi_seed_series(args)
    if not requested:
        requested = ["AUTO"]
    auto_requested = "AUTO" in requested
    all_requested = "ALL" in requested
    explicit = [series for series in requested if series not in {"AUTO", "ALL"}]
    discovered = []
    if auto_requested or all_requested:
        discovered = [
            *discover_kalshi_active_series(args, include_all=all_requested),
            *discover_kalshi_series(args, include_all=all_requested),
        ]
    return unique_nonempty([*seeded, *explicit, *discovered])


def kalshi_discovery_keywords(args: argparse.Namespace) -> list[str]:
    return unique_nonempty(
        [
            *[normalize_name(keyword) for keyword in normalize_csv(getattr(args, "kalshi_series_keywords", ""))],
            *[normalize_name(keyword) for keyword in normalize_csv(getattr(args, "target_keywords", ""))],
        ]
    )


def discover_kalshi_active_series(args: argparse.Namespace, include_all: bool = False) -> list[str]:
    keywords = kalshi_discovery_keywords(args)
    scored: list[tuple[int, str]] = []
    cursor = None
    scanned = 0
    scan_limit = max(getattr(args, "kalshi_market_scan_limit", 5000), 1)
    page_limit = min(scan_limit, 1000)
    for _ in range(max(getattr(args, "reference_max_pages", 1), 1)):
        if scanned >= scan_limit:
            break
        params: dict[str, Any] = {
            "status": "open",
            "limit": min(page_limit, scan_limit - scanned),
            "mve_filter": "exclude",
        }
        if cursor:
            params["cursor"] = cursor
        payload = http_get_json(KALSHI_MARKETS_URL, params)
        markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(markets, list):
            break
        markets = markets[: max(scan_limit - scanned, 0)]
        scanned += len(markets)
        for market in markets:
            if not isinstance(market, dict):
                continue
            series = kalshi_market_series(market)
            if not series:
                continue
            score = kalshi_market_score(market, keywords)
            if include_all or score > 0:
                scored.append((score, series))
        cursor = value_to_str(payload.get("cursor")) if isinstance(payload, dict) else None
        if not cursor or len(markets) < page_limit:
            break

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    series = unique_nonempty([ticker for _, ticker in scored])
    logging.info(
        "Discovered %s Kalshi series from %s open market rows%s",
        len(series),
        scanned,
        " (all mode)" if include_all else "",
    )
    return series


def kalshi_market_series(market: dict[str, Any]) -> str | None:
    series = value_to_str(market.get("series_ticker"))
    if series:
        return series.upper()
    event_ticker = value_to_str(market.get("event_ticker"))
    if event_ticker and "-" in event_ticker:
        return event_ticker.split("-", 1)[0].upper()
    ticker = value_to_str(market.get("ticker"))
    if ticker and "-" in ticker:
        return ticker.split("-", 1)[0].upper()
    return None


def kalshi_market_score(market: dict[str, Any], keywords: list[str]) -> int:
    text = normalize_name(
        " ".join(
            part
            for part in [
                value_to_str(market.get("ticker")),
                value_to_str(market.get("event_ticker")),
                value_to_str(market.get("series_ticker")),
                value_to_str(market.get("title")),
                value_to_str(market.get("sub_title")),
                value_to_str(market.get("yes_sub_title")),
                value_to_str(market.get("no_sub_title")),
                value_to_str(market.get("rules_primary")),
                value_to_str(market.get("category")),
            ]
            if part
        )
    )
    score = sum(1 for keyword in keywords if keyword and keyword in text)
    if "esports" in text or "valorant" in text or "cs2" in text or "dota" in text:
        score += 2
    if "game" in text and ("winner" in text or " win " in f" {text} "):
        score += 1
    return score


def discover_kalshi_series(args: argparse.Namespace, include_all: bool = False) -> list[str]:
    keywords = kalshi_discovery_keywords(args)
    scored: list[tuple[int, str]] = []
    cursor = None
    scanned = 0
    scan_limit = max(getattr(args, "kalshi_series_scan_limit", 1000), 1)
    page_limit = min(scan_limit, 1000)
    for _ in range(max(getattr(args, "reference_max_pages", 1), 1)):
        if scanned >= scan_limit:
            break
        params: dict[str, Any] = {"limit": min(page_limit, scan_limit - scanned)}
        if cursor:
            params["cursor"] = cursor
        payload = http_get_json(KALSHI_SERIES_URL, params)
        series_rows = payload.get("series") if isinstance(payload, dict) else None
        if not isinstance(series_rows, list):
            break
        series_rows = series_rows[: max(scan_limit - scanned, 0)]
        scanned += len(series_rows)
        for row in series_rows:
            if not isinstance(row, dict):
                continue
            ticker = value_to_str(row.get("ticker"))
            if not ticker:
                continue
            score = kalshi_series_score(row, keywords)
            if include_all or score > 0:
                scored.append((score, ticker.upper()))
        cursor = value_to_str(payload.get("cursor")) if isinstance(payload, dict) else None
        if not cursor or len(series_rows) < page_limit:
            break

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    series = unique_nonempty([ticker for _, ticker in scored])
    logging.info(
        "Discovered %s Kalshi series from %s scanned catalog rows%s",
        len(series),
        scanned,
        " (all mode)" if include_all else "",
    )
    return series


def kalshi_series_score(series: dict[str, Any], keywords: list[str]) -> int:
    text = normalize_name(
        " ".join(
            part
            for part in [
                value_to_str(series.get("ticker")),
                value_to_str(series.get("title")),
                value_to_str(series.get("category")),
                " ".join(str(tag) for tag in (series.get("tags") or []) if tag),
            ]
            if part
        )
    )
    score = sum(1 for keyword in keywords if keyword and keyword in text)
    if "sports" in text and ("game" in text or "match" in text or "winner" in text):
        score += 1
    if "kx" in text and "game" in text:
        score += 1
    return score


def kalshi_tickers_from_url(raw_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(raw_url)
    tickers = []
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("op_market_ticker", "market_ticker", "ticker"):
        tickers.extend(query.get(key, []))
    if tickers:
        return tickers
    for part in parsed.path.split("/"):
        part = part.strip()
        if part.upper().startswith("KX") and "-" in part:
            tickers.append(part)
    return tickers


def fetch_kalshi_market_by_ticker(ticker: str) -> dict[str, Any] | None:
    try:
        payload = http_get_json(f"{KALSHI_MARKETS_URL}/{urllib.parse.quote(ticker)}")
    except OddsCollectorError as exc:
        logging.warning("Could not fetch Kalshi ticker %s: %s", ticker, exc)
        return None
    if isinstance(payload, dict):
        market = payload.get("market")
        if isinstance(market, dict):
            return market
        if payload.get("ticker") == ticker:
            return payload
    return None


def fetch_kalshi_series_references(
    args: argparse.Namespace,
    series: str,
    seen_tickers: set[str],
) -> list[CrossVenueReference]:
    references: list[CrossVenueReference] = []
    cursor = None
    page_limit = min(max(args.reference_limit, 1), 1000)
    for _ in range(max(args.reference_max_pages, 1)):
        if len(references) >= args.reference_limit:
            break
        params: dict[str, Any] = {
            "status": "open",
            "limit": page_limit,
            "mve_filter": "exclude",
            "series_ticker": series.upper(),
        }
        if cursor:
            params["cursor"] = cursor
        payload = http_get_json(KALSHI_MARKETS_URL, params)
        markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(markets, list):
            break
        references.extend(kalshi_references_from_markets(markets, seen_tickers, args.reference_limit - len(references)))
        cursor = value_to_str(payload.get("cursor")) if isinstance(payload, dict) else None
        if not cursor:
            break
    logging.info("Loaded %s Kalshi references from series %s", len(references), series.upper())
    return references


def fetch_kalshi_broad_references(args: argparse.Namespace, seen_tickers: set[str]) -> list[CrossVenueReference]:
    references: list[CrossVenueReference] = []
    cursor = None
    page_limit = min(max(args.reference_limit, 1), 1000)
    for _ in range(max(args.reference_max_pages, 1)):
        if len(references) >= args.reference_limit:
            break
        params: dict[str, Any] = {"status": "open", "limit": page_limit, "mve_filter": "exclude"}
        if args.kalshi_category:
            params["category"] = args.kalshi_category
        if cursor:
            params["cursor"] = cursor
        payload = http_get_json(KALSHI_MARKETS_URL, params)
        markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(markets, list):
            break
        references.extend(kalshi_references_from_markets(markets, seen_tickers, args.reference_limit - len(references)))
        cursor = value_to_str(payload.get("cursor")) if isinstance(payload, dict) else None
        if not cursor:
            break
    return references


def kalshi_references_from_markets(
    markets: list[Any],
    seen_tickers: set[str],
    limit: int,
) -> list[CrossVenueReference]:
    references = []
    for market in markets:
        if len(references) >= limit:
            break
        if not isinstance(market, dict):
            continue
        reference = kalshi_reference_from_market(market)
        if reference is None or reference.reference_id in seen_tickers:
            continue
        seen_tickers.add(reference.reference_id)
        references.append(reference)
    return references


def kalshi_reference_from_market(market: dict[str, Any]) -> CrossVenueReference | None:
    yes_bid = to_decimal(market.get("yes_bid_dollars"))
    yes_ask = to_decimal(market.get("yes_ask_dollars"))
    if yes_bid is None or yes_ask is None or yes_ask <= 0 or yes_ask > 1 or yes_bid < 0 or yes_bid > yes_ask:
        return None
    title = value_to_str(market.get("title")) or value_to_str(market.get("ticker")) or ""
    if not title:
        return None
    yes_label = value_to_str(market.get("yes_sub_title")) or "Yes"
    no_label = value_to_str(market.get("no_sub_title")) or "No"
    yes_label, no_label = infer_kalshi_game_labels(title, yes_label, no_label)
    return CrossVenueReference(
        source="kalshi",
        reference_id=value_to_str(market.get("ticker")) or title,
        title=title,
        description=" ".join(
            part
            for part in [
                value_to_str(market.get("rules_primary")),
                value_to_str(market.get("rules_secondary")),
                yes_label,
                no_label,
            ]
            if part
        ),
        outcome_a=yes_label,
        outcome_b=no_label,
        probability_a=(yes_bid + yes_ask) / Decimal("2"),
        spread=yes_ask - yes_bid,
        observed_at=value_to_str(market.get("updated_time") or market.get("open_time")) or now_iso(),
    )


def infer_kalshi_game_labels(title: str, yes_label: str, no_label: str) -> tuple[str, str]:
    if normalize_name(no_label) != "no":
        return yes_label, no_label
    opponents = title_opponents(title)
    if len(opponents) != 2:
        return yes_label, no_label
    yes_key = normalize_name(yes_label)
    first_key = normalize_name(opponents[0])
    second_key = normalize_name(opponents[1])
    if name_score(yes_key, first_key) >= Decimal("0.72"):
        return yes_label, opponents[1]
    if name_score(yes_key, second_key) >= Decimal("0.72"):
        return yes_label, opponents[0]
    return yes_label, no_label


def title_opponents(title: str) -> list[str]:
    cleaned = re.sub(r"^[^:]{1,40}:\s*", "", title).strip()
    cleaned = re.sub(r"\b(?:game\s+)?winner\??.*$", "", cleaned, flags=re.IGNORECASE).strip()
    win_match = re.search(
        r"\bwin\s+the\s+(?P<event>.+?)\s+(?:valorant|cs2|counter[- ]strike|league of legends|lol|dota(?:\s*2)?|match)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if win_match:
        cleaned = win_match.group("event").strip()
    for separator in (" vs. ", " vs ", " v. ", " v ", " at "):
        if separator in cleaned.casefold():
            pattern = re.compile(re.escape(separator), flags=re.IGNORECASE)
            parts = [part.strip(" ?-") for part in pattern.split(cleaned, maxsplit=1)]
            if len(parts) == 2 and all(parts):
                return parts
    return []


def fetch_polymarket_gamma_references(args: argparse.Namespace) -> list[CrossVenueReference]:
    references: list[CrossVenueReference] = []
    page_limit = min(max(args.reference_limit, 1), 500)
    for page in range(max(args.reference_max_pages, 1)):
        if len(references) >= args.reference_limit:
            break
        payload = http_get_json(
            POLYMARKET_GAMMA_MARKETS_URL,
            {
                "active": "true",
                "closed": "false",
                "limit": page_limit,
                "offset": page * page_limit,
                "order": "volumeNum",
                "ascending": "false",
            },
        )
        markets = payload if isinstance(payload, list) else unpack_markets(payload)
        fresh = 0
        for market in markets:
            if not isinstance(market, dict):
                continue
            reference = polymarket_gamma_reference_from_market(market)
            if reference is None:
                continue
            references.append(reference)
            fresh += 1
            if len(references) >= args.reference_limit:
                break
        if fresh == 0 or len(markets) < page_limit:
            break
    return references


def polymarket_gamma_reference_from_market(market: dict[str, Any]) -> CrossVenueReference | None:
    outcomes = parse_json_array(market.get("outcomes"))
    prices = [to_decimal(price) for price in parse_json_array(market.get("outcomePrices"))]
    if len(outcomes) != 2 or len(prices) != 2 or prices[0] is None:
        return None
    probability = prices[0]
    if probability <= 0 or probability >= 1:
        return None
    title = value_to_str(market.get("question") or market.get("title") or market.get("slug")) or ""
    if not title:
        return None
    return CrossVenueReference(
        source="polymarket-gamma",
        reference_id=value_to_str(market.get("slug") or market.get("id")) or title,
        title=title,
        description=value_to_str(market.get("description")) or "",
        outcome_a=str(outcomes[0]),
        outcome_b=str(outcomes[1]),
        probability_a=probability,
        spread=Decimal("0"),
        observed_at=value_to_str(market.get("updatedAt") or market.get("createdAt")) or now_iso(),
    )


def cross_venue_match_score(target: CrossVenueTarget, reference: CrossVenueReference) -> Decimal:
    text_score = token_similarity(cross_venue_text(target), cross_venue_reference_text(reference))
    if binary_outcomes(target.outcome_a, target.outcome_b) or binary_outcomes(reference.outcome_a, reference.outcome_b):
        return text_score
    direct = (
        name_score(target.outcome_a, reference.outcome_a)
        + name_score(target.outcome_b, reference.outcome_b)
    ) / Decimal("2")
    reverse = (
        name_score(target.outcome_a, reference.outcome_b)
        + name_score(target.outcome_b, reference.outcome_a)
    ) / Decimal("2")
    outcome_score = max(direct, reverse)
    return max(text_score, (text_score * Decimal("0.65")) + (outcome_score * Decimal("0.35")))


def cross_venue_text(target: CrossVenueTarget) -> str:
    return " ".join([target.title, target.description, target.outcome_a, target.outcome_b])


def cross_venue_reference_text(reference: CrossVenueReference) -> str:
    return " ".join([reference.title, reference.description, reference.outcome_a, reference.outcome_b])


def reference_probability_for_target(
    target: CrossVenueTarget,
    reference: CrossVenueReference,
) -> Decimal | None:
    if binary_outcomes(target.outcome_a, target.outcome_b):
        if normalize_name(reference.outcome_a) == "no" and normalize_name(reference.outcome_b) == "yes":
            return Decimal("1") - reference.probability_a
        return reference.probability_a

    direct = name_score(target.outcome_a, reference.outcome_a)
    reverse = name_score(target.outcome_a, reference.outcome_b)
    if direct >= Decimal("0.72") and direct > reverse:
        return reference.probability_a
    if reverse >= Decimal("0.72") and reverse > direct:
        return Decimal("1") - reference.probability_a
    return None


def binary_outcomes(outcome_a: str, outcome_b: str) -> bool:
    return {normalize_name(outcome_a), normalize_name(outcome_b)} == {"yes", "no"}


def name_score(first: str, second: str) -> Decimal:
    if normalize_name(first) == normalize_name(second):
        return Decimal("1")
    return token_similarity(first, second)


def token_similarity(first: str, second: str) -> Decimal:
    left = meaningful_tokens(first)
    right = meaningful_tokens(second)
    if not left or not right:
        return Decimal("0")
    intersection = len(left & right)
    return Decimal(str((2 * intersection) / (len(left) + len(right))))


def meaningful_tokens(value: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "at",
        "be",
        "before",
        "by",
        "for",
        "game",
        "in",
        "is",
        "market",
        "no",
        "of",
        "on",
        "or",
        "the",
        "to",
        "vs",
        "will",
        "win",
        "winner",
        "yes",
    }
    return {token for token in normalize_name(value).split() if len(token) > 1 and token not in stopwords}


def average_decimal(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def unpack_markets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        markets = payload.get("markets") or payload.get("data") or []
        return [item for item in markets if isinstance(item, dict)] if isinstance(markets, list) else []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def collect_the_odds_api(args: argparse.Namespace, market_slug_map: dict[str, str]) -> list[OddsRow]:
    api_keys = the_odds_api_key_pool(args)
    if not api_keys:
        raise OddsCollectorError("Set THE_ODDS_API_KEY/THE_ODDS_API_KEYS or pass --the-odds-api-key.")

    sports = args.sport or discover_the_odds_api_esports(
        api_keys[0],
        verify_ssl=not getattr(args, "insecure_ssl", False),
    )
    if not sports:
        raise OddsCollectorError("No esports sport keys found. Pass --sport explicitly.")

    rows = []
    bookmaker_filter = normalize_csv(args.bookmakers)
    for index, sport in enumerate(sports):
        api_key = rotating_key(api_keys, index)
        params = {
            "apiKey": api_key,
            "regions": args.regions,
            "markets": args.markets,
            "oddsFormat": "decimal",
        }
        if bookmaker_filter:
            params["bookmakers"] = ",".join(bookmaker_filter)
        events = http_get_json(
            f"{THE_ODDS_API_BASE_URL}/v4/sports/{sport}/odds/",
            params,
            verify_ssl=not getattr(args, "insecure_ssl", False),
        )
        if not isinstance(events, list):
            logging.warning("Unexpected The Odds API response for %s", sport)
            continue
        for event in events[: max(args.max_events_per_sport, 0)]:
            if isinstance(event, dict):
                rows.extend(the_odds_api_event_rows(event, market_slug_map))
    return rows


def the_odds_api_key_pool(args: argparse.Namespace) -> list[str]:
    keys = normalize_csv(getattr(args, "the_odds_api_keys", None))
    single = getattr(args, "the_odds_api_key", None)
    if not single and getattr(args, "provider", None) == "the-odds-api":
        single = getattr(args, "api_key", None)
    if single:
        keys.append(str(single).strip())
    return unique_nonempty(keys)


def discover_the_odds_api_esports(api_key: str, verify_ssl: bool = True) -> list[str]:
    sports = http_get_json(
        f"{THE_ODDS_API_BASE_URL}/v4/sports",
        {"apiKey": api_key, "all": "true"},
        verify_ssl=verify_ssl,
    )
    keys = []
    if not isinstance(sports, list):
        return keys
    for sport in sports:
        if not isinstance(sport, dict):
            continue
        text = " ".join(
            str(sport.get(key) or "")
            for key in ("key", "group", "title", "description")
        ).casefold()
        if any(hint in text for hint in DEFAULT_ESPORT_HINTS):
            key = str(sport.get("key") or "")
            if key:
                keys.append(key)
    logging.info("Discovered esports sport keys: %s", ", ".join(keys) or "<none>")
    return keys


def the_odds_api_event_rows(event: dict[str, Any], market_slug_map: dict[str, str]) -> list[OddsRow]:
    event_id = str(event.get("id") or "")
    home = str(event.get("home_team") or "").strip()
    away = str(event.get("away_team") or "").strip()
    if not event_id or not home or not away:
        return []

    sport = str(event.get("sport_key") or "")
    league = str(event.get("sport_title") or "")
    starts_at = str(event.get("commence_time") or "") or None
    rows = []
    for bookmaker in event.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue
        book_name = str(bookmaker.get("key") or bookmaker.get("title") or "unknown")
        observed_at = str(bookmaker.get("last_update") or now_iso())
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict) or str(market.get("key") or "") != "h2h":
                continue
            odds = outcomes_to_two_way_odds(market.get("outcomes"), home, away)
            if odds is None:
                continue
            rows.append(
                OddsRow(
                    event_id=event_id,
                    bookmaker=book_name,
                    sport=sport,
                    league=league,
                    team_a=home,
                    team_b=away,
                    odds_a=odds[0],
                    odds_b=odds[1],
                    starts_at=starts_at,
                    observed_at=observed_at,
                    market_slug=market_slug_map.get(event_id),
                )
            )
    return rows


def collect_odds_api_io(args: argparse.Namespace, market_slug_map: dict[str, str]) -> list[OddsRow]:
    api_keys = odds_api_io_key_pool(args)
    if not api_keys:
        raise OddsCollectorError("Set ODDS_API_IO_KEY/ODDS_API_IO_KEYS or pass --odds-api-io-key.")

    sports = args.sport or discover_odds_api_io_esports(
        verify_ssl=not getattr(args, "insecure_ssl", False),
    )
    if not sports:
        raise OddsCollectorError("No Odds-API.io esports sport slugs found. Pass --sport explicitly.")

    rows = []
    bookmakers = normalize_csv(args.bookmakers)
    bookmaker_param = ",".join(bookmakers) if bookmakers else None
    request_index = 0
    for sport_index, sport in enumerate(sports):
        events_key = rotating_key(api_keys, sport_index)
        events = odds_api_io_events(
            events_key,
            sport,
            args.max_events_per_sport,
            verify_ssl=not getattr(args, "insecure_ssl", False),
        )
        pending = [event for event in events if odds_api_io_event_is_open(event)]
        for chunk in chunks(pending, 10):
            request_index += 1
            odds_key = rotating_key(api_keys, sport_index + request_index)
            odds_payloads = odds_api_io_multi_odds(
                odds_key,
                chunk,
                bookmaker_param,
                verify_ssl=not getattr(args, "insecure_ssl", False),
            )
            for payload in odds_payloads:
                if isinstance(payload, dict):
                    rows.extend(odds_api_io_odds_rows(payload, market_slug_map))
    return rows


def odds_api_io_key_pool(args: argparse.Namespace) -> list[str]:
    keys = normalize_csv(getattr(args, "odds_api_io_keys", None))
    single = getattr(args, "odds_api_io_key", None)
    if not single and getattr(args, "provider", None) == "odds-api-io":
        single = getattr(args, "api_key", None)
    if single:
        keys.append(str(single).strip())
    return unique_nonempty(keys)


def unique_nonempty(values: list[str]) -> list[str]:
    output = []
    seen = set()
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def rotating_key(keys: list[str], index: int) -> str:
    if not keys:
        raise OddsCollectorError("No Odds-API.io keys configured.")
    return keys[index % len(keys)]


def discover_odds_api_io_esports(verify_ssl: bool = True) -> list[str]:
    sports = http_get_json(f"{ODDS_API_IO_BASE_URL}/v3/sports", verify_ssl=verify_ssl)
    if not isinstance(sports, list):
        return []
    keys = []
    for sport in sports:
        if not isinstance(sport, dict):
            continue
        text = " ".join(str(sport.get(key) or "") for key in ("name", "slug")).casefold()
        if any(hint in text for hint in DEFAULT_ESPORT_HINTS):
            slug = str(sport.get("slug") or "")
            if slug:
                keys.append(slug)
    logging.info("Discovered Odds-API.io esports sport slugs: %s", ", ".join(keys) or "<none>")
    return keys


def odds_api_io_events(
    api_key: str,
    sport: str,
    limit: int,
    verify_ssl: bool = True,
) -> list[dict[str, Any]]:
    payload = http_get_json(
        f"{ODDS_API_IO_BASE_URL}/v3/events",
        {"apiKey": api_key, "sport": sport, "limit": max(limit, 1)},
        verify_ssl=verify_ssl,
    )
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        events = payload.get("events") or payload.get("data") or []
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
    return []


def odds_api_io_multi_odds(
    api_key: str,
    events: list[dict[str, Any]],
    bookmakers: str | None,
    verify_ssl: bool = True,
) -> list[Any]:
    event_ids = [str(event.get("id") or "") for event in events if event.get("id")]
    if not event_ids:
        return []
    params = {"apiKey": api_key, "eventIds": ",".join(event_ids)}
    if bookmakers:
        params["bookmakers"] = bookmakers
    payload = http_get_json(f"{ODDS_API_IO_BASE_URL}/v3/odds/multi", params, verify_ssl=verify_ssl)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("odds") or payload.get("data") or payload.get("events")
        if isinstance(value, list):
            return value
        return [payload]
    return []


def odds_api_io_event_is_open(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or "").casefold()
    if status in {"settled", "finished", "closed", "cancelled", "canceled"}:
        return False
    return bool(event.get("id") and (event.get("home") or event.get("home_team")) and (event.get("away") or event.get("away_team")))


def odds_api_io_odds_rows(payload: dict[str, Any], market_slug_map: dict[str, str]) -> list[OddsRow]:
    event_id = str(payload.get("id") or payload.get("eventId") or "")
    team_a = str(payload.get("home") or payload.get("home_team") or "").strip()
    team_b = str(payload.get("away") or payload.get("away_team") or "").strip()
    if not event_id or not team_a or not team_b:
        return []
    sport = nested_name(payload.get("sport")) or str(payload.get("sport") or "")
    league = nested_name(payload.get("league")) or str(payload.get("league") or "")
    starts_at = str(payload.get("date") or payload.get("commence_time") or payload.get("starts_at") or "") or None
    rows = []
    bookmakers = payload.get("bookmakers")
    if not isinstance(bookmakers, dict):
        return rows
    for bookmaker, markets in bookmakers.items():
        if not isinstance(markets, list):
            continue
        ml = next(
            (
                market
                for market in markets
                if isinstance(market, dict)
                and str(market.get("name") or market.get("key") or "").casefold()
                in {"ml", "moneyline", "match winner", "match_winner", "winner"}
            ),
            None,
        )
        if not isinstance(ml, dict):
            continue
        odds = first_market_odds(ml)
        if odds is None:
            continue
        odds_a = to_decimal(odds.get("home"))
        odds_b = to_decimal(odds.get("away"))
        if odds_a is None or odds_b is None or odds_a <= 1 or odds_b <= 1:
            continue
        rows.append(
            OddsRow(
                event_id=event_id,
                bookmaker=str(bookmaker),
                sport=sport,
                league=league,
                team_a=team_a,
                team_b=team_b,
                odds_a=odds_a,
                odds_b=odds_b,
                starts_at=starts_at,
                observed_at=str(ml.get("updatedAt") or ml.get("updated_at") or now_iso()),
                market_slug=market_slug_map.get(event_id),
            )
        )
    return rows


def first_market_odds(market: dict[str, Any]) -> dict[str, Any] | None:
    odds = market.get("odds")
    if isinstance(odds, list):
        return next((item for item in odds if isinstance(item, dict)), None)
    return odds if isinstance(odds, dict) else None


def nested_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value_to_str(value.get("name") or value.get("slug"))
    return None


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def collect_egamersworld_auto(args: argparse.Namespace, market_slug_map: dict[str, str]) -> list[OddsRow]:
    requested_games = args.egw_game or ["cs2", "dota2", "lol", "valorant"]
    urls = sorted({EGAMERSWORLD_GAME_URLS[game] for game in requested_games})
    rows = []
    sport_label = ",".join(requested_games)
    for url in urls:
        try:
            text = (
                render_page_text(url, args.wait_ms, ignore_https_errors=args.ignore_https_errors)
                if args.render_html
                else http_get_text(url, verify_ssl=not args.insecure_ssl)
            )
        except Exception as exc:  # noqa: BLE001 - network/page failures vary.
            logging.warning("Could not fetch EGamersWorld page %s: %s%s", url, exc, ssl_hint(exc))
            continue
        game_rows = parse_egamersworld_text(text, sport_label, market_slug_map)
        logging.info("EGamersWorld %s: parsed %s odds rows", url, len(game_rows))
        rows.extend(game_rows)
    return rows


def parse_egamersworld_text(
    page_text: str,
    sport: str,
    market_slug_map: dict[str, str] | None = None,
) -> list[OddsRow]:
    text = html_to_text(page_text)
    rows = []
    for match in find_egamersworld_matches(text):
        odds_pairs = [
            (to_decimal(first), to_decimal(second))
            for first, second in re.findall(r"\b(\d{1,2}\.\d{1,2})\s+(\d{1,2}\.\d{1,2})\s+Make a bet", match["odds_text"])
        ]
        for index, (odds_a, odds_b) in enumerate(odds_pairs, start=1):
            if odds_a is None or odds_b is None or odds_a <= 1 or odds_b <= 1:
                continue
            event_id = egamersworld_event_id(sport, match["team_a"], match["team_b"], match["starts_at"])
            rows.append(
                OddsRow(
                    event_id=event_id,
                    bookmaker=f"egamersworld-{index}",
                    sport=sport,
                    league=match["league"],
                    team_a=match["team_a"],
                    team_b=match["team_b"],
                    odds_a=odds_a,
                    odds_b=odds_b,
                    starts_at=match["starts_at"],
                    observed_at=now_iso(),
                    market_slug=(market_slug_map or {}).get(event_id),
                )
            )
    return rows


def find_egamersworld_matches(text: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"(?P<league>[^#]{3,120}?)\s+"
        r"(?:#\d+\s+)?(?P<team_a>.+?)\s+"
        r"(?P<date>\d{2}\.\d{2}\.\d{2})\s+"
        r"(?P<time>\d{2}:\d{2})\s+"
        r"(?:(?:Live|Upcoming|Finished|Postponed|Cancelled)\s+)?"
        r"(?P<format>Bo\d|BO\d|bo\d)?\s+"
        r"(?:#\d+\s+)?(?P<team_b>.+?)\s+"
        r"(?P<odds>(?:\d{1,2}\.\d{1,2}\s+\d{1,2}\.\d{1,2}\s+Make a bet\s*)+)",
        flags=re.IGNORECASE,
    )
    matches = []
    for found in pattern.finditer(text):
        league = clean_egw_name(found.group("league"))
        team_a = clean_egw_team(found.group("team_a"))
        team_b = clean_egw_team(found.group("team_b"))
        if not league or not team_a or not team_b:
            continue
        if len(team_a) > 80 or len(team_b) > 80:
            continue
        starts_at = parse_egw_datetime(found.group("date"), found.group("time"))
        matches.append(
            {
                "league": league,
                "team_a": team_a,
                "team_b": team_b,
                "starts_at": starts_at,
                "odds_text": found.group("odds"),
            }
        )
    return matches


def clean_egw_name(value: str) -> str:
    value = re.sub(r"\b(today|tomorrow'?s|upcoming matches|live)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -|")


def clean_egw_team(value: str) -> str:
    value = clean_egw_name(value)
    rank_matches = list(re.finditer(r"#\d+\s+", value))
    if rank_matches:
        value = value[rank_matches[-1].end() :]
    return clean_egw_name(value)


def parse_egw_datetime(date_text: str, time_text: str) -> str:
    try:
        parsed = datetime.strptime(f"{date_text} {time_text}", "%d.%m.%y %H:%M")
    except ValueError:
        return f"{date_text} {time_text}"
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def egamersworld_event_id(sport: str, team_a: str, team_b: str, starts_at: str) -> str:
    date_part = starts_at[:10] if starts_at else "unknown-date"
    return f"egw-{sport}-{slugify(team_a)}-vs-{slugify(team_b)}-{date_part}"


def html_to_text(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def collect_generic_json(args: argparse.Namespace, market_slug_map: dict[str, str]) -> list[OddsRow]:
    payload = load_generic_payload(args)
    events = get_path(payload, args.events_path) if args.events_path else payload
    if isinstance(events, dict):
        for key in ("events", "data", "rows", "matches"):
            value = events.get(key)
            if isinstance(value, list):
                events = value
                break
    if not isinstance(events, list):
        raise OddsCollectorError("Generic source did not resolve to a list of events.")

    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        rows.extend(generic_event_rows(event, args, market_slug_map))
    return rows


def load_generic_payload(args: argparse.Namespace) -> Any:
    if args.source_file:
        text = Path(args.source_file).read_text(encoding="utf-8")
    elif args.source_url and args.render_html:
        text = render_page_text(
            args.source_url,
            wait_ms=args.wait_ms,
            ignore_https_errors=args.ignore_https_errors,
        )
    elif args.source_url:
        text = http_get_text(args.source_url, verify_ssl=not args.insecure_ssl)
    else:
        text = sys.stdin.read()
    parsed = parse_json_from_text(text)
    if parsed is None:
        raise OddsCollectorError("Could not parse JSON from source.")
    return parsed


def generic_event_rows(
    event: dict[str, Any],
    args: argparse.Namespace,
    market_slug_map: dict[str, str],
) -> list[OddsRow]:
    event_id = str(get_path(event, args.event_id_path) or "")
    team_a = str(get_path(event, args.team_a_path) or "")
    team_b = str(get_path(event, args.team_b_path) or "")
    if not event_id or not team_a or not team_b:
        return []
    starts_at = value_to_str(get_path(event, args.starts_at_path))
    sport = value_to_str(get_path(event, args.sport_path)) or args.provider
    league = value_to_str(get_path(event, args.league_path)) or ""
    rows = []
    bookmakers = get_path(event, args.bookmakers_path)
    if not isinstance(bookmakers, list):
        bookmakers = [event]
    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue
        book_name = (
            value_to_str(get_path(bookmaker, args.bookmaker_key_path))
            or value_to_str(get_path(bookmaker, args.bookmaker_title_path))
            or args.provider
        )
        markets = get_path(bookmaker, args.markets_path)
        if not isinstance(markets, list):
            markets = [bookmaker]
        for market in markets:
            if not isinstance(market, dict):
                continue
            market_key = value_to_str(get_path(market, args.market_key_path))
            if market_key and market_key not in {"h2h", "moneyline", "match_winner", "winner"}:
                continue
            odds = outcomes_to_two_way_odds(get_path(market, args.outcomes_path), team_a, team_b, args)
            if odds is None:
                continue
            rows.append(
                OddsRow(
                    event_id=event_id,
                    bookmaker=book_name,
                    sport=sport,
                    league=league,
                    team_a=team_a,
                    team_b=team_b,
                    odds_a=odds[0],
                    odds_b=odds[1],
                    starts_at=starts_at,
                    observed_at=now_iso(),
                    market_slug=market_slug_map.get(event_id),
                )
            )
    return rows


def outcomes_to_two_way_odds(
    outcomes: Any,
    team_a: str,
    team_b: str,
    args: argparse.Namespace | None = None,
) -> tuple[Decimal, Decimal] | None:
    if not isinstance(outcomes, list):
        return None
    prices: dict[str, Decimal] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        name_key = args.outcome_name_path if args else "name"
        price_key = args.outcome_price_path if args else "price"
        name = value_to_str(get_path(outcome, name_key))
        price = to_decimal(get_path(outcome, price_key))
        if not name or price is None or price <= 1:
            continue
        prices[normalize_name(name)] = price
    odds_a = prices.get(normalize_name(team_a))
    odds_b = prices.get(normalize_name(team_b))
    if odds_a is None or odds_b is None:
        return None
    return odds_a, odds_b


def write_rows(
    rows: list[OddsRow],
    path: Path,
    append: bool,
    fmt: str,
    merge_existing: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if merge_existing and path.exists() and not append:
        rows = merge_rows(load_existing_rows(path, fmt), rows)
    mode = "a" if append else "w"
    if fmt == "jsonl":
        with path.open(mode, encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row_to_json(row), sort_keys=True) + "\n")
        return
    if fmt == "json":
        existing = []
        if append and path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                existing = payload if isinstance(payload, list) else []
            except json.JSONDecodeError:
                existing = []
        path.write_text(json.dumps(existing + [row_to_json(row) for row in rows], indent=2) + "\n", encoding="utf-8")
        return
    with path.open(mode, encoding="utf-8", newline="") as handle:
        fields = list(row_to_json(rows[0]).keys()) if rows else [
            "event_id",
            "bookmaker",
            "sport",
            "league",
            "team_a",
            "team_b",
            "odds_a",
            "odds_b",
            "starts_at",
            "observed_at",
            "market_slug",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not append or path.stat().st_size == 0:
            writer.writeheader()
        for row in rows:
            writer.writerow(row_to_json(row))


def load_existing_rows(path: Path, fmt: str) -> list[OddsRow]:
    if not path.exists():
        return []
    try:
        if fmt == "jsonl":
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        elif fmt == "json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload if isinstance(payload, list) else []
        else:
            with path.open("r", encoding="utf-8", newline="") as handle:
                records = list(csv.DictReader(handle))
    except (OSError, json.JSONDecodeError):
        return []
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        row = odds_row_from_record(record)
        if row is not None:
            rows.append(row)
    return rows


def merge_rows(existing: list[OddsRow], fresh: list[OddsRow]) -> list[OddsRow]:
    merged: dict[tuple[str, str], OddsRow] = {}
    order: list[tuple[str, str]] = []
    for row in existing + fresh:
        key = (row.event_id, row.bookmaker)
        if key not in merged:
            order.append(key)
        merged[key] = row
    return [merged[key] for key in order]


def odds_row_from_record(record: dict[str, Any]) -> OddsRow | None:
    odds_a = to_decimal(record.get("odds_a"))
    odds_b = to_decimal(record.get("odds_b"))
    event_id = value_to_str(record.get("event_id"))
    bookmaker = value_to_str(record.get("bookmaker"))
    team_a = value_to_str(record.get("team_a"))
    team_b = value_to_str(record.get("team_b"))
    observed_at = value_to_str(record.get("observed_at"))
    if not event_id or not bookmaker or not team_a or not team_b or not observed_at:
        return None
    if odds_a is None or odds_b is None:
        return None
    return OddsRow(
        event_id=event_id,
        bookmaker=bookmaker,
        sport=value_to_str(record.get("sport")) or "",
        league=value_to_str(record.get("league")) or "",
        team_a=team_a,
        team_b=team_b,
        odds_a=odds_a,
        odds_b=odds_b,
        starts_at=value_to_str(record.get("starts_at")),
        observed_at=observed_at,
        market_slug=value_to_str(record.get("market_slug")),
    )


def row_to_json(row: OddsRow) -> dict[str, str | None]:
    return {
        "event_id": row.event_id,
        "bookmaker": row.bookmaker,
        "sport": row.sport,
        "league": row.league,
        "team_a": row.team_a,
        "team_b": row.team_b,
        "odds_a": format_decimal(row.odds_a),
        "odds_b": format_decimal(row.odds_b),
        "starts_at": row.starts_at,
        "observed_at": row.observed_at,
        "market_slug": row.market_slug,
    }


def load_market_slug_map(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return {str(key): str(value) for key, value in payload.items()}
    if isinstance(payload, list):
        output = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            event_id = row.get("event_id") or row.get("eventId") or row.get("id")
            slug = row.get("market_slug") or row.get("slug") or row.get("polymarket_slug")
            if event_id and slug:
                output[str(event_id)] = str(slug)
        return output
    return {}


def http_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    verify_ssl: bool = True,
) -> Any:
    return json.loads(http_get_text(url, params, verify_ssl=verify_ssl))


def http_get_text(
    url: str,
    params: dict[str, Any] | None = None,
    verify_ssl: bool = True,
) -> str:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=browser_headers(url))
    context = ssl_context(verify_ssl)
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OddsCollectorError(
            f"GET {url} failed with HTTP {exc.code}: {body[:300]}{http_error_hint(url, body)}"
        ) from exc
    except URLError as exc:
        raise OddsCollectorError(f"GET {url} failed: {exc}{ssl_hint(exc)}") from exc


def ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    if not verify_ssl:
        return ssl._create_unverified_context()
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def browser_headers(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else "",
    }


def ssl_hint(exc: Exception) -> str:
    reason = getattr(exc, "reason", None)
    text = f"{exc} {reason or ''}".lower()
    if isinstance(exc, URLError) and "certificate" in text:
        return " (retry with --insecure-ssl, or use --render-html so Chromium handles the page)"
    return ""


def http_error_hint(url: str, body: str) -> str:
    text = body.lower()
    if "nginx/1.20.2" in text and "api.the-odds-api.com" in url:
        return (
            " (this looks like a local DNS/proxy/ISP edge response, not The Odds API JSON; "
            "try a VPN/hotspot or disabling ISP security DNS filtering)"
        )
    return ""


def parse_json_from_text(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    for pattern in (
        r"window\.__INITIAL_STATE__\s*=\s*({.*?})\s*;",
        r"window\.__NUXT__\s*=\s*({.*?})\s*;",
        r"window\.__APP_STATE__\s*=\s*({.*?})\s*;",
        r"window\.__PRELOADED_STATE__\s*=\s*({.*?})\s*;",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    return None


def render_page_text(url: str, wait_ms: int, ignore_https_errors: bool = False) -> str:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise OddsCollectorError(
            "--render-html requires Playwright: `python3 -m pip install playwright` "
            "and `python3 -m playwright install chromium`."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(ignore_https_errors=ignore_https_errors)
            page.goto(url, wait_until="networkidle", timeout=30000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


def get_path(obj: Any, path: str) -> Any:
    if not path:
        return obj
    current = obj
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if parsed.is_nan():
        return None
    return parsed


def amount_value(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        return to_decimal(value.get("value"))
    return to_decimal(value)


def parse_json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def normalize_name(value: str) -> str:
    value = value.casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"\b(esports|e-sports|gaming|team|club|clan)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def slugify(value: str) -> str:
    value = normalize_name(value)
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "unknown"


def normalize_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def value_to_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


if __name__ == "__main__":
    raise SystemExit(main())
