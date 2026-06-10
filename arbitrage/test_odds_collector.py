import argparse
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from odds_collector import (
    collect_generic_json,
    collect_odds_api_io,
    collect_the_odds_api,
    main,
    parse_egamersworld_text,
    outcomes_to_two_way_odds,
    write_rows,
)


class OddsCollectorTests(unittest.TestCase):
    def test_outcomes_to_two_way_odds_matches_team_names(self):
        odds = outcomes_to_two_way_odds(
            [{"name": "FaZe Clan", "price": "1.57"}, {"name": "NAVI", "price": "2.28"}],
            "FaZe Clan",
            "NAVI",
        )

        self.assertEqual(odds, (Decimal("1.57"), Decimal("2.28")))

    def test_collect_the_odds_api_normalizes_h2h_rows(self):
        events = [
            {
                "id": "evt-1",
                "sport_key": "esports_cs2",
                "sport_title": "CS2",
                "commence_time": "2026-06-10T20:00:00Z",
                "home_team": "FaZe Clan",
                "away_team": "NAVI",
                "bookmakers": [
                    {
                        "key": "ggbet",
                        "last_update": "2026-06-10T19:00:00Z",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "FaZe Clan", "price": 1.57},
                                    {"name": "NAVI", "price": 2.28},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
        args = argparse.Namespace(
            api_key="key",
            sport=["esports_cs2"],
            regions="eu",
            markets="h2h",
            bookmakers="ggbet",
            max_events_per_sport=10,
        )

        with patch("odds_collector.http_get_json", return_value=events):
            rows = collect_the_odds_api(args, {"evt-1": "faze-vs-navi"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].bookmaker, "ggbet")
        self.assertEqual(rows[0].market_slug, "faze-vs-navi")
        self.assertEqual(rows[0].odds_a, Decimal("1.57"))

    def test_collect_odds_api_io_fetches_events_and_multi_odds(self):
        events = [
            {
                "id": 123456,
                "sport": {"name": "Esports", "slug": "esports"},
                "league": {"name": "IEM Cologne", "slug": "iem-cologne"},
                "home": "FaZe Clan",
                "away": "NAVI",
                "date": "2026-06-10T20:00:00Z",
                "status": "pending",
            }
        ]
        odds = [
            {
                "id": 123456,
                "home": "FaZe Clan",
                "away": "NAVI",
                "date": "2026-06-10T20:00:00Z",
                "status": "pending",
                "sport": {"name": "Esports", "slug": "esports"},
                "league": {"name": "IEM Cologne", "slug": "iem-cologne"},
                "bookmakers": {
                    "GG.BET": [
                        {
                            "name": "ML",
                            "odds": [{"home": "1.57", "away": "2.28"}],
                            "updatedAt": "2026-06-10T19:00:00Z",
                        }
                    ],
                    "Pinnacle": [
                        {
                            "name": "ML",
                            "odds": [{"home": "1.60", "away": "2.20"}],
                        }
                    ],
                },
            }
        ]
        args = argparse.Namespace(
            odds_api_io_key=None,
            odds_api_io_keys="key-a,key-b",
            api_key=None,
            sport=["esports"],
            bookmakers="GG.BET,Pinnacle",
            max_events_per_sport=10,
        )

        def fake_get_json(url, params=None):
            if url.endswith("/v3/events"):
                self.assertEqual(params["apiKey"], "key-a")
                return events
            if url.endswith("/v3/odds/multi"):
                self.assertEqual(params["apiKey"], "key-b")
                self.assertEqual(params["eventIds"], "123456")
                return odds
            raise AssertionError(url)

        with patch("odds_collector.http_get_json", side_effect=fake_get_json):
            rows = collect_odds_api_io(args, {"123456": "faze-vs-navi"})

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].event_id, "123456")
        self.assertEqual(rows[0].bookmaker, "GG.BET")
        self.assertEqual(rows[0].market_slug, "faze-vs-navi")
        self.assertEqual(rows[1].odds_a, Decimal("1.60"))

    def test_collect_generic_json_with_configurable_paths(self):
        payload = {
            "matches": [
                {
                    "id": "evt-1",
                    "game": "cs2",
                    "league": {"name": "Major"},
                    "teams": {"a": "FaZe Clan", "b": "NAVI"},
                    "books": [
                        {
                            "name": "ggbet",
                            "markets": [
                                {
                                    "type": "moneyline",
                                    "prices": [
                                        {"team": "FaZe Clan", "decimal": "1.57"},
                                        {"team": "NAVI", "decimal": "2.28"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            args = argparse.Namespace(
                provider="ggbet-json",
                source_file=str(source),
                source_url=None,
                events_path="matches",
                event_id_path="id",
                team_a_path="teams.a",
                team_b_path="teams.b",
                starts_at_path="start",
                sport_path="game",
                league_path="league.name",
                bookmakers_path="books",
                bookmaker_key_path="name",
                bookmaker_title_path="title",
                markets_path="markets",
                market_key_path="type",
                outcomes_path="prices",
                outcome_name_path="team",
                outcome_price_path="decimal",
            )
            rows = collect_generic_json(args, {"evt-1": "faze-vs-navi"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].bookmaker, "ggbet")
        self.assertEqual(rows[0].sport, "cs2")
        self.assertEqual(rows[0].league, "Major")

    def test_collect_public_html_from_next_data(self):
        html = """
        <html><body>
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"matches":[{"id":"evt-1","game":"cs2","teams":{"a":"FaZe Clan","b":"NAVI"},"books":[{"name":"ggbet","markets":[{"type":"moneyline","prices":[{"team":"FaZe Clan","decimal":"1.57"},{"team":"NAVI","decimal":"2.28"}]}]}]}]}}}
        </script>
        </body></html>
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "page.html"
            source.write_text(html, encoding="utf-8")
            args = argparse.Namespace(
                provider="public-html",
                source_file=str(source),
                source_url=None,
                render_html=False,
                wait_ms=0,
                events_path="props.pageProps.matches",
                event_id_path="id",
                team_a_path="teams.a",
                team_b_path="teams.b",
                starts_at_path="start",
                sport_path="game",
                league_path="league",
                bookmakers_path="books",
                bookmaker_key_path="name",
                bookmaker_title_path="title",
                markets_path="markets",
                market_key_path="type",
                outcomes_path="prices",
                outcome_name_path="team",
                outcome_price_path="decimal",
            )
            rows = collect_generic_json(args, {})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].bookmaker, "ggbet")
        self.assertEqual(rows[0].odds_b, Decimal("2.28"))

    def test_parse_egamersworld_visible_match_rows(self):
        text = (
            "CCT Europe 2026 Series #4 #146 illwill 10.06.26 13:30 Live Bo3 #236 G2 Ares "
            "1.44 2.55 Make a bet 1.45 2.59 Make a bet "
            "United21 Season 50 #755 Clutchain 11.06.26 04:00 Bo3 #844 xept "
            "1.55 2.29 Make a bet 1.54 2.34 Make a bet"
        )

        rows = parse_egamersworld_text(text, "cs2", {})

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0].event_id, "egw-cs2-illwill-vs-g2-ares-2026-06-10")
        self.assertEqual(rows[0].bookmaker, "egamersworld-1")
        self.assertEqual(rows[0].odds_a, Decimal("1.44"))
        self.assertEqual(rows[1].bookmaker, "egamersworld-2")
        self.assertEqual(rows[2].team_a, "Clutchain")
        self.assertEqual(rows[2].team_b, "xept")

    def test_write_rows_jsonl(self):
        payload = {
            "matches": [
                {
                    "id": "evt-1",
                    "home_team": "FaZe Clan",
                    "away_team": "NAVI",
                    "bookmakers": [
                        {
                            "key": "ggbet",
                            "markets": [
                                {
                                    "key": "h2h",
                                    "outcomes": [
                                        {"name": "FaZe Clan", "price": "1.57"},
                                        {"name": "NAVI", "price": "2.28"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            output = Path(tmpdir) / "odds.jsonl"
            source.write_text(json.dumps(payload), encoding="utf-8")
            args = argparse.Namespace(
                provider="generic-json",
                source_file=str(source),
                source_url=None,
                events_path="matches",
                event_id_path="id",
                team_a_path="home_team",
                team_b_path="away_team",
                starts_at_path="commence_time",
                sport_path="sport_key",
                league_path="sport_title",
                bookmakers_path="bookmakers",
                bookmaker_key_path="key",
                bookmaker_title_path="title",
                markets_path="markets",
                market_key_path="key",
                outcomes_path="outcomes",
                outcome_name_path="name",
                outcome_price_path="price",
            )
            rows = collect_generic_json(args, {})
            write_rows(rows, output, append=False, fmt="jsonl")
            lines = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["event_id"], "evt-1")

    def test_write_rows_merges_existing_by_event_and_bookmaker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "odds.jsonl"
            output.write_text(
                json.dumps(
                    {
                        "event_id": "evt-1",
                        "bookmaker": "book-a",
                        "sport": "cs2",
                        "league": "old",
                        "team_a": "FaZe Clan",
                        "team_b": "NAVI",
                        "odds_a": "1.50",
                        "odds_b": "2.40",
                        "starts_at": None,
                        "observed_at": "2026-06-10T00:00:00Z",
                        "market_slug": None,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rows = collect_generic_json(
                argparse.Namespace(
                    provider="generic-json",
                    source_file=str(Path(__file__).resolve().parent / "provider_odds.example.json"),
                    source_url=None,
                    events_path="matches",
                    event_id_path="id",
                    team_a_path="teams.a",
                    team_b_path="teams.b",
                    starts_at_path="start",
                    sport_path="game",
                    league_path="league.name",
                    bookmakers_path="books",
                    bookmaker_key_path="name",
                    bookmaker_title_path="title",
                    markets_path="markets",
                    market_key_path="type",
                    outcomes_path="prices",
                    outcome_name_path="team",
                    outcome_price_path="decimal",
                ),
                {},
            )
            rows = [
                rows[0].__class__(
                    event_id="evt-1",
                    bookmaker="book-a",
                    sport=rows[0].sport,
                    league=rows[0].league,
                    team_a=rows[0].team_a,
                    team_b=rows[0].team_b,
                    odds_a=rows[0].odds_a,
                    odds_b=rows[0].odds_b,
                    starts_at=rows[0].starts_at,
                    observed_at=rows[0].observed_at,
                    market_slug=rows[0].market_slug,
                )
            ]
            write_rows(rows, output, append=False, fmt="jsonl", merge_existing=True)
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["odds_a"], "1.57")
        self.assertEqual(records[0]["league"], "example-cup")

    def test_main_does_not_overwrite_output_on_empty_collect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "odds.jsonl"
            original = '{"event_id":"evt-1"}\n'
            output.write_text(original, encoding="utf-8")
            argv = [
                "odds_collector.py",
                "--provider",
                "egamersworld-auto",
                "--once",
                "--output",
                str(output),
            ]
            with patch("sys.argv", argv), patch("odds_collector.collect", return_value=[]):
                code = main()

            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
