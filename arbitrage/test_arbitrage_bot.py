import argparse
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arbitrage_bot import (
    Fill,
    FairMarket,
    MarketMapping,
    MarketOutcome,
    OrderBook,
    build_entry_intents,
    build_fair_markets,
    build_hedge_intents,
    devig_two_way,
    fetch_market_by_slug,
    fetch_market_book,
    match_outcome,
    normalize_odds_row,
    reconcile_live_state,
)


def args(**overrides):
    defaults = {
        "min_edge": Decimal("0.07"),
        "min_locked_edge": Decimal("0.07"),
        "min_spread": Decimal("0.05"),
        "quote_dollars": Decimal("25"),
        "max_shares": Decimal("100"),
        "tick_size": Decimal("0.01"),
        "hedge_cross": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class FakeExecutor:
    def list_open_orders(self, market_slugs):
        return []

    def get_position(self, market_slug):
        return {"marketMetadata": {"slug": market_slug}, "quantity": "12"}


class ArbitrageBotTests(unittest.TestCase):
    def test_devig_two_way_normalizes_decimal_odds(self):
        faze, navi, overround = devig_two_way(Decimal("1.57"), Decimal("2.28"))

        self.assertEqual(faze.quantize(Decimal("0.001")), Decimal("0.592"))
        self.assertEqual(navi.quantize(Decimal("0.001")), Decimal("0.408"))
        self.assertEqual(overround.quantize(Decimal("0.001")), Decimal("0.076"))

    def test_build_fair_markets_preserves_team_probability_mapping(self):
        rows = [
            normalize_odds_row(
                {
                    "event_id": "cs2-1",
                    "team_a": "FaZe Clan",
                    "team_b": "NAVI",
                    "odds_a": "1.57",
                    "odds_b": "2.28",
                }
            )
        ]

        fair = build_fair_markets(rows)["cs2-1"]

        self.assertGreater(fair.probabilities["FaZe Clan"], fair.probabilities["NAVI"])

    def test_team_matching_uses_aliases_and_normalized_names(self):
        mapping = MarketMapping(
            event_id="cs2-1",
            market_slug="faze-vs-navi",
            outcomes=(
                MarketOutcome("FaZe Esports", "ORDER_INTENT_BUY_LONG", aliases=("FaZe Clan",)),
                MarketOutcome("Natus Vincere", "ORDER_INTENT_BUY_SHORT", aliases=("NAVI",)),
            ),
        )

        self.assertEqual(match_outcome("FaZe Clan", mapping).intent, "ORDER_INTENT_BUY_LONG")
        self.assertEqual(match_outcome("NAVI", mapping).intent, "ORDER_INTENT_BUY_SHORT")

    def test_entry_intent_quotes_inside_fair_value_edge(self):
        fair = FairMarket(
            event_id="cs2-1",
            team_a="FaZe Clan",
            team_b="NAVI",
            probabilities={"FaZe Clan": Decimal("0.592"), "NAVI": Decimal("0.408")},
            books=1,
            overround=Decimal("0.076"),
            market_slug="faze-vs-navi",
        )
        mapping = MarketMapping(
            event_id="cs2-1",
            market_slug="faze-vs-navi",
            outcomes=(
                MarketOutcome("FaZe Clan", "ORDER_INTENT_BUY_LONG"),
                MarketOutcome("NAVI", "ORDER_INTENT_BUY_SHORT"),
            ),
        )
        books = {
            "faze-vs-navi:ORDER_INTENT_BUY_LONG": OrderBook(
                "ORDER_INTENT_BUY_LONG",
                best_bid=Decimal("0.51"),
                best_ask=Decimal("0.73"),
            ),
            "faze-vs-navi:ORDER_INTENT_BUY_SHORT": OrderBook(
                "ORDER_INTENT_BUY_SHORT",
                best_bid=Decimal("0.30"),
                best_ask=Decimal("0.45"),
            ),
        }

        intents = build_entry_intents(fair, mapping, books, args(), {"orders": []})

        by_intent = {intent.intent: intent for intent in intents}
        self.assertEqual(by_intent["ORDER_INTENT_BUY_LONG"].price, Decimal("0.52"))
        self.assertGreaterEqual(by_intent["ORDER_INTENT_BUY_LONG"].edge, Decimal("0.07"))
        self.assertEqual(by_intent["ORDER_INTENT_BUY_SHORT"].price, Decimal("0.31"))

    def test_hedge_intent_caps_complete_set_under_one(self):
        fair = FairMarket(
            event_id="cs2-1",
            team_a="FaZe Clan",
            team_b="NAVI",
            probabilities={"FaZe Clan": Decimal("0.592"), "NAVI": Decimal("0.408")},
            books=1,
            overround=Decimal("0.076"),
            market_slug="faze-vs-navi",
        )
        mapping = MarketMapping(
            event_id="cs2-1",
            market_slug="faze-vs-navi",
            outcomes=(
                MarketOutcome("FaZe Clan", "ORDER_INTENT_BUY_LONG"),
                MarketOutcome("NAVI", "ORDER_INTENT_BUY_SHORT"),
            ),
        )
        fill = Fill(
            event_id="cs2-1",
            market_slug="faze-vs-navi",
            outcome="FaZe Clan",
            intent="ORDER_INTENT_BUY_LONG",
            price=Decimal("0.52"),
            quantity=Decimal("50"),
            fill_id="fill-1",
            filled_at=None,
        )
        books = {
            "faze-vs-navi:ORDER_INTENT_BUY_SHORT": OrderBook(
                "ORDER_INTENT_BUY_SHORT",
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
            )
        }

        intents = build_hedge_intents(fair, mapping, books, [fill], args(), {"orders": []})

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].price, Decimal("0.41"))
        self.assertEqual(intents[0].edge, Decimal("0.07"))

    def test_us_book_derives_short_prices_from_long_book(self):
        payload = {
            "marketData": {
                "bids": [{"px": {"value": "0.52"}, "qty": "10"}],
                "offers": [{"px": {"value": "0.73"}, "qty": "8"}],
            }
        }

        with patch("arbitrage_bot.http_get_json", return_value=payload):
            books = fetch_market_book("faze-vs-navi")

        long_book = books["faze-vs-navi:ORDER_INTENT_BUY_LONG"]
        short_book = books["faze-vs-navi:ORDER_INTENT_BUY_SHORT"]
        self.assertEqual(long_book.best_bid, Decimal("0.52"))
        self.assertEqual(long_book.best_ask, Decimal("0.73"))
        self.assertEqual(short_book.best_bid, Decimal("0.27"))
        self.assertEqual(short_book.best_ask, Decimal("0.48"))

    def test_fetch_market_by_slug_falls_back_to_search(self):
        payload = {"markets": [{"slug": "wide-market", "marketSides": [{"description": "Yes"}]}]}

        def fake_get_json(url, params=None):
            if url.endswith("/wide-market"):
                from arbitrage_bot import ArbitrageBotError

                raise ArbitrageBotError("404")
            self.assertEqual(params, {"slug": "wide-market", "limit": 1})
            return payload

        with patch("arbitrage_bot.http_get_json", side_effect=fake_get_json):
            market = fetch_market_by_slug("wide-market")

        self.assertEqual(market["slug"], "wide-market")

    def test_reconcile_live_state_adds_fill_for_missing_entry_order_with_position(self):
        state = {
            "orders": [
                {
                    "kind": "entry",
                    "status": "pending",
                    "order_id": "order-1",
                    "event_id": "cs2-1",
                    "market_slug": "faze-vs-navi",
                    "outcome": "FaZe Clan",
                    "intent": "ORDER_INTENT_BUY_LONG",
                    "price": "0.52",
                    "quantity": "20",
                }
            ],
            "fills": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            reconcile_live_state(args(), FakeExecutor(), state, state_path)

        self.assertEqual(state["orders"][0]["status"], "filled")
        self.assertEqual(state["orders"][0]["filled_quantity"], "12")
        self.assertEqual(len(state["fills"]), 1)
        self.assertEqual(state["fills"][0]["fill_id"], "order-1")
        self.assertEqual(state["fills"][0]["intent"], "ORDER_INTENT_BUY_LONG")


if __name__ == "__main__":
    unittest.main()
