"""Offline smoke tests: no LLM calls, no market requests, no exchange orders."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import lab_terminal as l


class PaperTradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root, self.old_db = l.ROOT, l.DB
        l.ROOT = Path(self.temp.name)
        l.DB = l.ROOT / "paper.sqlite3"

    def tearDown(self):
        l.ROOT, l.DB = self.old_root, self.old_db
        self.temp.cleanup()

    @staticmethod
    def ticker_quote(ticker):
        return {"ticker": ticker, "price": 50000 if ticker == "BTC-USD" else 2000,
                "fresh": True, "source": "stub"}

    def test_starting_balance(self):
        view = l.portfolio()
        self.assertEqual(view["cash"], 100)
        self.assertEqual(view["equity"], 100)

    def test_buy_sell_fee_and_holdings(self):
        with patch.object(l, "market", side_effect=self.ticker_quote):
            buy = l.paper_order({"ticker": "BTC-USD", "side": "BUY", "amount": 20})
            self.assertTrue(buy["ok"])
            after = l.portfolio()
            self.assertAlmostEqual(after["cash"], 80)
            self.assertEqual(len(after["positions"]), 1)
            sell = l.paper_order({"ticker": "BTC-USD", "side": "SELL", "amount": 10})
            self.assertTrue(sell["ok"])
            after_sell = l.portfolio()
            self.assertEqual(after_sell["closed_trades"], 1)
            self.assertGreater(after_sell["cash"], 89)
            self.assertLess(after_sell["cash"], 90.1)
            self.assertLess(after_sell["realized_pnl"], 0)
            self.assertEqual(len(after_sell["trades"]), 2)

    def test_cannot_overspend_or_short(self):
        with patch.object(l, "market", side_effect=self.ticker_quote):
            with self.assertRaises(ValueError):
                l.paper_order({"ticker": "BTC-USD", "side": "BUY", "amount": 101})
            with self.assertRaises(ValueError):
                l.paper_order({"ticker": "BTC-USD", "side": "SELL", "amount": 10})
            self.assertEqual(l.portfolio()["cash"], 100)

    def test_reject_stale_quotes(self):
        with patch.object(l, "market", return_value={"price": 100, "fresh": False}):
            with self.assertRaises(ValueError):
                l.paper_order({"ticker": "ETH-USD", "side": "BUY", "amount": 10})

    def test_signal_from_report(self):
        root = Path(self.temp.name) / "results"
        folder = root / "reports" / "BTC-USD_20261026_154600"
        folder.mkdir(parents=True)
        (folder / "complete_report.md").write_text(
            "# Report\n- Analysis date: 2026-10-26\n- Rating: Hold\n"
        )
        result = l.signal(root, "BTC-USD")
        self.assertEqual(result["signal"], "BEKLE")
        self.assertEqual(result["report_id"], folder.name)

    def test_backtest_has_one_bar_delay_and_no_network(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = []
        for i in range(85):
            # Increasing sequence: enter only after 50 completed prices
            c = 80 + i
            bars.append({"time": (start + timedelta(days=i)).isoformat(),
                         "open": c, "close": c})
        with patch.object(l, "get_series", return_value=bars):
            report = l.backtest("BTC-USD")
        self.assertEqual(report["start"], bars[50]["time"][:10])
        self.assertGreater(report["trade_events"], 0)
        self.assertGreater(report["final_equity"], 100)
        self.assertEqual(len(report["curve"]), 35)


if __name__ == "__main__":
    unittest.main()
