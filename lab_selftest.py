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
        (report/"complete_report.md").write_text("# Report\n- Rating: Hold\n")
        history=risk.decision_history(root,check_due=False)
        self.assertEqual(len(history["records"]),1)
        self.assertEqual(history["records"][0]["decision"],"BEKLE")
        self.assertIsNone(history["records"][0]["observed_price"])
        with patch.object(l,"market",side_effect=self.ticker_quote):
            l.paper_order({"ticker":"BTC-USD","side":"BUY","amount":10})
            summary=risk.performance()
        self.assertEqual(summary["daily"][0]["trades"],1)
        self.assertGreater(summary["fees"],0)

    def test_ai_tracker_scores_only_timely_directional_predictions(self):
        import lab_risk as risk
        initial=datetime(2026,10,1,12,0,tzinfo=timezone.utc)
        rows=[
            ("one","BTC-USD","AL",100.0,initial+timedelta(hours=25),101.0,
             initial+timedelta(days=7,hours=2),104.0),
            ("two","ETH-USD","SAT",100.0,initial+timedelta(hours=26),90.0,
             initial+timedelta(days=9),120.0),
            ("three","BTC-USD","BEKLE",100.0,initial+timedelta(hours=24),99.0,
             None,None),
            ("four","ETH-USD","AL",100.0,initial+timedelta(hours=32),110.0,
             None,None)
        ]
        with l.connect() as db:
            risk.setup(db)
            for rid,symbol,decision,price,t24,p24,t7,p7 in rows:
                db.execute("""INSERT INTO ai_observations(
                    report_id,ticker,decision,report_date,observed_at,observed_price,
                    price_source,h24_at,h24_price,d7_at,d7_price)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid,symbol,decision,"2026-10-01",initial.isoformat(),price,"stub",
                     t24.isoformat() if t24 else None,p24,t7.isoformat() if t7 else None,p7))
        result=risk.decision_history(Path(self.temp.name)/"nothing",check_due=False)
        summary=result["summary"]
        self.assertEqual(summary["h24"]["evaluated"],2)
        self.assertEqual(summary["h24"]["correct"],2)
        self.assertEqual(summary["h24"]["accuracy_pct"],100.0)
        self.assertEqual(summary["h24"]["late_excluded"],1)
        self.assertEqual(summary["d7"]["evaluated"],1)
        self.assertEqual(summary["d7"]["late_excluded"],1)
        hold=next(x for x in result["records"] if x["decision"]=="BEKLE")
        self.assertIsNone(hold["h24_direction_correct"])
        late=next(x for x in result["records"] if x["report_id"]=="four")
        self.assertFalse(late["h24_timely"])
        self.assertIsNone(late["h24_direction_correct"])

    def test_fixed_strategy_comparison_includes_costs_and_cash(self):
        import lab_compare
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        bars=[{"time":(start+timedelta(days=i)).isoformat(),
               "open":100.0,"close":100.0} for i in range(85)]
        with patch.object(l,"get_series",return_value=bars):
            result=lab_compare.compare("BTC-USD")
        models={m["id"]:m for m in result["models"]}
        self.assertEqual(result["comparison_start"],bars[50]["time"][:10])
        self.assertEqual(result["observations"],35)
        self.assertEqual(models["cash"]["ending_equity"],100)
        self.assertEqual(models["cash"]["trades"],0)
        self.assertEqual(models["sma20_50"]["trades"],0)
        self.assertEqual(models["sma10_30"]["trades"],0)
        self.assertLess(models["buyhold"]["ending_equity"],100)
        self.assertGreater(models["buyhold"]["fees_usd"],0)
        self.assertEqual(models["buyhold"]["trades"],1)

    def test_comparison_never_uses_current_day_close_to_trade_at_open(self):
        import lab_compare
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        bars=[{"time":(start+timedelta(days=i)).isoformat(),
               "open":100.0,"close":100.0} for i in range(70)]
        bars[50]["close"]=250.0
        with patch.object(l,"get_series",return_value=bars):
            result=lab_compare.compare("ETH-USD")
        models={m["id"]:m for m in result["models"]}
        self.assertEqual(models["sma10_30"]["curve"][0]["equity"],100)
        self.assertEqual(models["sma20_50"]["curve"][0]["equity"],100)
        self.assertGreaterEqual(models["sma10_30"]["trades"],1)
        self.assertGreaterEqual(models["sma20_50"]["trades"],1)
        self.assertEqual(models["cash"]["ending_equity"],100)

    def test_comparison_rejects_unsorted_or_missing_series(self):
        import lab_compare
        from datetime import timezone
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        bars=[{"time":(start+timedelta(days=i)).isoformat(),
               "open":100.0,"close":100.0} for i in range(80)]
        with self.assertRaisesRegex(ValueError,"65"):
            lab_compare.calculate(bars[:20],"BTC-USD")
        bars[70]["time"]=bars[69]["time"]
        with self.assertRaisesRegex(ValueError,"tekrarsız"):
            lab_compare.calculate(bars,"ETH-USD")

    @staticmethod
    def walk_bars(size=350):
        import math
        start=datetime(2025,1,1,tzinfo=timezone.utc)
        return [{"time":(start+timedelta(days=i)).isoformat(),
                 "open":100+i*.06+6*math.sin(i/14),
                 "close":100+i*.06+6*math.sin(i/14)+math.sin(i/3)}
                for i in range(size)]

    def test_walkforward_heldout_folds_and_fee_baselines(self):
        import lab_walkforward as wf
        bars=self.walk_bars()
        with patch.object(l,"get_series",return_value=bars):
            r=wf.run("BTC-USD")
        self.assertEqual(r["fold_count"],3)
        self.assertEqual(r["oos_days"],150)
        self.assertEqual(r["status"],"EXPLORATORY_ONLY")
        self.assertEqual(r["folds"][0]["train_end"],bars[149]["time"][:10])
        self.assertEqual(r["folds"][0]["validation_start"],bars[150]["time"][:10])
        self.assertEqual(r["folds"][0]["test_start"],bars[200]["time"][:10])
        self.assertEqual(r["folds"][1]["test_start"],bars[250]["time"][:10])
        self.assertEqual(r["folds"][2]["test_end"],bars[349]["time"][:10])
        self.assertEqual(r["totals"]["cash"]["return_pct"],0)
        self.assertEqual(r["totals"]["cash"]["fees_usd"],0)
        self.assertGreater(r["totals"]["buyhold"]["fees_usd"],0)
        self.assertEqual(sum(x["days"] for x in r["totals"]["selected"]["regimes"]),150)

    def test_walkforward_first_selection_ignores_heldout_prices(self):
        import lab_walkforward as wf
        original=self.walk_bars()
        modified=[x.copy() for x in original]
        for index in range(215,235):
            modified[index]["close"]*=1.85
            modified[index]["open"]*=1.7
        base=wf.calculate(original,"ETH-USD")
        changed=wf.calculate(modified,"ETH-USD")
        a=base["folds"][0];b=changed["folds"][0]
        self.assertEqual(a["selected"],b["selected"])
        self.assertEqual(a["train_metrics"],b["train_metrics"])
        self.assertEqual(a["validation_metrics"],b["validation_metrics"])
        # The intentionally manipulated first held-out period CAN change its return.
        self.assertNotEqual(
            wf.simulate(original,200,250,"buyhold")["return_pct"],
            wf.simulate(modified,200,250,"buyhold")["return_pct"])

    def test_walkforward_no_lookahead_and_unfinished_day_excluded(self):
        import lab_walkforward as wf
        bars=self.walk_bars()
        candidate=[{"time":datetime(2026,10,26,tzinfo=timezone.utc).isoformat(),
                    "open":999999.0,"close":999999.0}]
        baseline=wf.calculate(bars,"BTC-USD")
        with_future=wf.calculate(bars+candidate,"BTC-USD")
        self.assertEqual(baseline["folds"],with_future["folds"])
        flat=[{"time":(datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(days=i)).isoformat(),
               "open":100.0,"close":100.0} for i in range(70)]
        flat[50]["close"]=500.0
        first=wf.simulate(flat,50,51,"sma10_30")
        self.assertEqual(first["trades"],0)
        self.assertEqual(first["ending_equity"],100.0)

    def test_walkforward_ai_matching_only_same_heldout_observations(self):
        import lab_walkforward as wf
        bars=self.walk_bars()
        day=bars[220]["time"]
        observations=[
            {"ticker":"BTC-USD","decision":"AL","observed_at":day,
             "observed_price":110.0,"h24_return_pct":2.5,"h24_timely":True,
             "d7_return_pct":-3.0,"d7_timely":True},
            {"ticker":"BTC-USD","decision":"SAT","observed_at":bars[221]["time"],
             "observed_price":112.0,"h24_return_pct":-1.0,"h24_timely":True,
             "d7_return_pct":None,"d7_timely":False},
            {"ticker":"BTC-USD","decision":"BEKLE","observed_at":day,
             "observed_price":110.0,"h24_return_pct":1.0,"h24_timely":True},
            {"ticker":"BTC-USD","decision":"AL","observed_at":bars[230]["time"],
             "observed_price":110.0,"h24_return_pct":9.0,"h24_timely":False},
            {"ticker":"ETH-USD","decision":"AL","observed_at":day,
             "observed_price":110.0,"h24_return_pct":2.5,"h24_timely":True}
        ]
        r=wf.calculate(bars,"BTC-USD",observations)
        h=r["ai_paired"]["horizons"]
        self.assertEqual(h["h24"]["matched"],2)
        self.assertEqual(h["h24"]["scores"]["AI"]["correct"],2)
        self.assertFalse(h["h24"]["adequate_sample"])
        self.assertEqual(h["d7"]["matched"],1)
        self.assertGreaterEqual(r["ai_paired"]["exclusions"]["hold_ignored"],1)

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
