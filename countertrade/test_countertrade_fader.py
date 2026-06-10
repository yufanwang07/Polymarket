import argparse
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from countertrade_fader import (
    build_counter_orders,
    normalize_fill,
    rank_wallets,
    summarize_paper,
)


def args(**overrides):
    defaults = {
        "assume_buy": False,
        "min_source_price": Decimal("0.30"),
        "max_source_price": Decimal("0.70"),
        "max_slippage": Decimal("0.01"),
        "max_exposure_per_market": Decimal("75"),
        "fixed_dollars": Decimal("25"),
        "take_profit_pct": Decimal("0.20"),
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class CountertradeFaderTests(unittest.TestCase):
    def test_rank_wallets_selects_low_win_high_volume_wallet(self):
        fills = []
        for index in range(10):
            fills.append(
                normalize_fill(
                    {
                        "wallet": "0xbad",
                        "market_id": f"m{index}",
                        "outcome": "YES",
                        "action": "BUY",
                        "price": "0.50",
                        "size": "100",
                        "win": index < 2,
                    }
                )
            )
        for index in range(10):
            fills.append(
                normalize_fill(
                    {
                        "wallet": "0xgood",
                        "market_id": f"g{index}",
                        "outcome": "NO",
                        "action": "BUY",
                        "price": "0.50",
                        "size": "100",
                        "win": index < 8,
                    }
                )
            )

        ranked = rank_wallets(
            fills,
            min_decisions=5,
            min_volume=Decimal("100"),
            max_win_rate=Decimal("0.30"),
            unit="market",
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].wallet, "0xbad")
        self.assertEqual(ranked[0].win_rate, Decimal("0.2"))

    def test_build_counter_order_inverts_yes_with_fixed_sizing(self):
        fill = normalize_fill(
            {
                "wallet": "0xbad",
                "market_id": "m1",
                "outcome": "YES",
                "action": "BUY",
                "price": "0.60",
                "size": "10",
                "txHash": "abc",
            }
        )
        state = {"seen_fills": [], "market_exposure": {}, "orders": []}

        orders = build_counter_orders([fill], {"0xbad"}, state, args())

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].counter_outcome, "NO")
        self.assertEqual(orders[0].entry_price, Decimal("0.4000"))
        self.assertEqual(orders[0].quantity, Decimal("62.5000"))
        self.assertEqual(state["market_exposure"]["m1"], "25")

    def test_slippage_and_exposure_guards_skip_orders(self):
        too_expensive = normalize_fill(
            {
                "wallet": "0xbad",
                "market_id": "m1",
                "outcome": "NO",
                "action": "BUY",
                "price": "0.55",
                "yesAsk": "0.47",
                "txHash": "abc",
            }
        )
        exposure_hit = normalize_fill(
            {
                "wallet": "0xbad",
                "market_id": "m2",
                "outcome": "YES",
                "action": "BUY",
                "price": "0.50",
                "txHash": "def",
            }
        )

        self.assertEqual(
            build_counter_orders(
                [too_expensive],
                {"0xbad"},
                {"seen_fills": [], "market_exposure": {}, "orders": []},
                args(),
            ),
            [],
        )
        self.assertEqual(
            build_counter_orders(
                [exposure_hit],
                {"0xbad"},
                {"seen_fills": [], "market_exposure": {"m2": "60"}, "orders": []},
                args(),
            ),
            [],
        )

    def test_paper_summary_counts_fade_wins(self):
        summary = summarize_paper(
            [
                {"fade_won": True, "pnl": "25", "entry_price": "0.5", "quantity": "50"},
                {"fade_won": False, "pnl": "-25", "entry_price": "0.5", "quantity": "50"},
                {"fade_won": True, "pnl": "25", "entry_price": "0.5", "quantity": "50"},
            ]
        )

        self.assertEqual(summary["trades"], 3)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["win_rate"], "66.67%")
        self.assertEqual(summary["pnl"], "+$25")


if __name__ == "__main__":
    unittest.main()
