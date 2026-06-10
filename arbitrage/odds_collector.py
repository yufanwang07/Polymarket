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
DEFAULT_ESPORT_HINTS = ("esports", "counter", "cs2", "csgo", "dota", "league of legends", "lol", "valorant")
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


def main() -> int:
    load_env_file(Path.cwd() / ".env")
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    while True:
        rows = collect(args)
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
        default=os.getenv("ODDS_BOOKMAKERS", ",".join(DEFAULT_BOOKMAKERS)),
        help="comma-separated bookmaker keys for providers that support filtering",
    )
    parser.add_argument("--sport", action="append", default=[], help="provider sport key; can repeat")
    parser.add_argument("--markets", default=DEFAULT_MARKETS)
    parser.add_argument("--regions", default=DEFAULT_REGIONS)
    parser.add_argument("--max-events-per-sport", type=int, default=50)
    parser.add_argument("--api-key", default=os.getenv("THE_ODDS_API_KEY") or os.getenv("ODDS_API_IO_KEY"))
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


def collect_the_odds_api(args: argparse.Namespace, market_slug_map: dict[str, str]) -> list[OddsRow]:
    if not args.api_key:
        raise OddsCollectorError("Set THE_ODDS_API_KEY or pass --api-key.")

    sports = args.sport or discover_the_odds_api_esports(args.api_key)
    if not sports:
        raise OddsCollectorError("No esports sport keys found. Pass --sport explicitly.")

    rows = []
    bookmaker_filter = normalize_csv(args.bookmakers)
    for sport in sports:
        params = {
            "apiKey": args.api_key,
            "regions": args.regions,
            "markets": args.markets,
            "oddsFormat": "decimal",
        }
        if bookmaker_filter:
            params["bookmakers"] = ",".join(bookmaker_filter)
        events = http_get_json(f"{THE_ODDS_API_BASE_URL}/v4/sports/{sport}/odds", params)
        if not isinstance(events, list):
            logging.warning("Unexpected The Odds API response for %s", sport)
            continue
        for event in events[: max(args.max_events_per_sport, 0)]:
            if isinstance(event, dict):
                rows.extend(the_odds_api_event_rows(event, market_slug_map))
    return rows


def discover_the_odds_api_esports(api_key: str) -> list[str]:
    sports = http_get_json(f"{THE_ODDS_API_BASE_URL}/v4/sports", {"apiKey": api_key, "all": "true"})
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

    sports = args.sport or discover_odds_api_io_esports()
    if not sports:
        raise OddsCollectorError("No Odds-API.io esports sport slugs found. Pass --sport explicitly.")

    rows = []
    bookmakers = normalize_csv(args.bookmakers)
    bookmaker_param = ",".join(bookmakers) if bookmakers else None
    request_index = 0
    for sport_index, sport in enumerate(sports):
        events_key = rotating_key(api_keys, sport_index)
        events = odds_api_io_events(events_key, sport, args.max_events_per_sport)
        pending = [event for event in events if odds_api_io_event_is_open(event)]
        for chunk in chunks(pending, 10):
            request_index += 1
            odds_key = rotating_key(api_keys, sport_index + request_index)
            odds_payloads = odds_api_io_multi_odds(odds_key, chunk, bookmaker_param)
            for payload in odds_payloads:
                if isinstance(payload, dict):
                    rows.extend(odds_api_io_odds_rows(payload, market_slug_map))
    return rows


def odds_api_io_key_pool(args: argparse.Namespace) -> list[str]:
    keys = normalize_csv(getattr(args, "odds_api_io_keys", None))
    single = getattr(args, "odds_api_io_key", None) or getattr(args, "api_key", None)
    if single:
        keys.append(str(single).strip())
    output = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            output.append(key)
    return output


def rotating_key(keys: list[str], index: int) -> str:
    if not keys:
        raise OddsCollectorError("No Odds-API.io keys configured.")
    return keys[index % len(keys)]


def discover_odds_api_io_esports() -> list[str]:
    sports = http_get_json(f"{ODDS_API_IO_BASE_URL}/v3/sports")
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


def odds_api_io_events(api_key: str, sport: str, limit: int) -> list[dict[str, Any]]:
    payload = http_get_json(
        f"{ODDS_API_IO_BASE_URL}/v3/events",
        {"apiKey": api_key, "sport": sport, "limit": max(limit, 1)},
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
) -> list[Any]:
    event_ids = [str(event.get("id") or "") for event in events if event.get("id")]
    if not event_ids:
        return []
    params = {"apiKey": api_key, "eventIds": ",".join(event_ids)}
    if bookmakers:
        params["bookmakers"] = bookmakers
    payload = http_get_json(f"{ODDS_API_IO_BASE_URL}/v3/odds/multi", params)
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


def http_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    return json.loads(http_get_text(url, params))


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
        raise OddsCollectorError(f"GET {url} failed with HTTP {exc.code}: {body[:300]}") from exc


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
