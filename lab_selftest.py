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
            self.assertGreater(buy["price"], 50000)
            self.assertLess(sell["price"], 50000)
            self.assertGreater(after_sell["fees_paid"], 0)
            self.assertGreaterEqual(len(after_sell["curve"]), 2)
            self.assertEqual(after_sell["trades"][0]["side"], "SELL")
            self.assertLess(after_sell["unrealized_pnl"], 0)

    def test_five_dollar_paper_sale_and_equity_curve(self):
        with patch.object(l, "market", side_effect=self.ticker_quote):
            l.portfolio()
            l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":10})
            receipt=l.paper_order({"ticker":"BTC-USD","side":"SELL","amount":5})
            state=l.portfolio()
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["side"],"SELL")
            self.assertAlmostEqual(state["cash"], 90+5-0.005, places=3)
            self.assertEqual(state["closed_trades"],1)
            self.assertAlmostEqual(len(state["trades"]),2)
            self.assertGreaterEqual(len(state["curve"]),3)

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

    def test_risk_asset_limit_blocks_new_paper_buy(self):
        import lab_risk as risk
        with patch.object(l, "market", side_effect=self.ticker_quote):
            with self.assertRaisesRegex(ValueError, "25 USD"):
                l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":26})
            self.assertEqual(l.portfolio()["cash"],100)
            l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":10})
            risk_data=risk.risk_dashboard()
            self.assertEqual(len(risk_data["stops"]),1)
            self.assertEqual(risk_data["stops"][0]["ticker"],"BTC-USD")

    def test_stop_trigger_is_virtual_and_idempotent(self):
        import lab_risk as risk
        with patch.object(l, "market", side_effect=self.ticker_quote):
            l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":10})
            stop=risk.stop_control("BTC-USD",5)
            self.assertGreater(stop["stop_price"],0)
        def quote(ticker):
            return {"ticker":ticker,"price":45000,"fresh":True,"as_of":datetime.now(timezone.utc).isoformat()}
        with patch.object(l,"market",side_effect=quote):
            a=risk.check_stops()
            b=risk.check_stops()
            portfolio=l.portfolio()
        self.assertEqual(len(a["triggered"]),1)
        self.assertEqual(len(b["triggered"]),0)
        self.assertEqual(len(portfolio["positions"]),0)
        self.assertEqual(portfolio["closed_trades"],1)
        self.assertEqual(len(risk.risk_dashboard()["stops"]),0)

    def test_loss_limit_restricts_buys_but_not_sells(self):
        import lab_risk as risk
        with patch.object(l,"market",side_effect=self.ticker_quote):
            l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":10})
            with l.connect() as db:
                db.execute("INSERT INTO trades(at,ticker,side,qty,price,fee,realized,amount) VALUES(?,?,?,?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(),"ETH-USD","SELL",0.01,2000,0,-4,20))
            with self.assertRaisesRegex(ValueError,"Günlük gerçekleşmiş"):
                l.paper_order({"ticker":"ETH-USD","side":"BUY","amount":5})
            result=l.paper_order({"ticker":"BTC-USD","side":"SELL","amount":5})
            self.assertEqual(result["side"],"SELL")
            self.assertTrue(risk.risk_dashboard()["buys_paused"])

    def test_performance_and_legacy_ai_observation(self):
        import lab_risk as risk
        root=Path(self.temp.name)/"results"
        report=root/"reports"/"BTC-USD_20261026_154600"
        report.mkdir(parents=True)
        (report/"complete_report.md").write_text("# Report\\n- Rating: Hold\\n")
        history=risk.decision_history(root,check_due=False)
        self.assertEqual(len(history["records"]),1)
        self.assertEqual(history["records"][0]["decision"],"BEKLE")
        self.assertIsNone(history["records"][0]["observed_price"])
        with patch.object(l,"market",side_effect=self.ticker_quote):
            l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":10})
            summary=risk.performance()
        self.assertEqual(summary["daily"][0]["trades"],1)
        self.assertGreater(summary["fees"],0)

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
        self.assertTrue(report["open_position"])
        self.assertGreater(report["unrealized_pnl"], 0)
        self.assertEqual(report["closed_trades"], 0)
        self.assertGreater(report["transaction_fees"], 0)
        self.assertFalse(report["baseline_fee_included"])


if __name__ == "__main__":
    unittest.main()
