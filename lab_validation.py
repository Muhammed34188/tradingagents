"""Integrity and prospective evaluation tools. No exchange orders, no LLM calls."""
from datetime import datetime, timedelta, timezone
from collections import Counter
import math
import sqlite3

import lab_terminal as terminal
import lab_walkforward as wf

PROSPECTIVE=("cash","buyhold","sma10_30","sma15_45","sma20_50")
AUDIT_CASH_TOLERANCE=0.005
AUDIT_QTY_TOLERANCE=0.00000002

def _utc():
    return datetime.now(timezone.utc)

def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS forward_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        strategy TEXT NOT NULL,
        signal_day TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        baseline_close REAL NOT NULL,
        direction TEXT NOT NULL,
        source TEXT NOT NULL,
        target_day TEXT NOT NULL,
        evaluated_at TEXT,
        target_close REAL,
        movement_pct REAL,
        UNIQUE(ticker,strategy,signal_day))""")

def audit_portfolio(prices=None):
    """Replay immutable trades. Report discrepancies; do not edit existing account."""
    with terminal.MUTEX:
        with terminal.connect() as db:
            account=db.execute("SELECT cash,initial FROM account WHERE id=1").fetchone()
            stored=[dict(r) for r in db.execute("SELECT ticker,qty,avg_cost FROM positions")]
            trades=[dict(r) for r in db.execute("SELECT * FROM trades ORDER BY id ASC")]
            cash=float(account["initial"])
            inventory={}
            closed_pnl=0.0
            fees=0.0
            errors=[]
            for t in trades:
                symbol=t["ticker"]
                previous=inventory.get(symbol,{"qty":0.0,"basis":0.0})
                qty=float(t["qty"])
                fill=float(t["price"])
                fee=float(t["fee"])
                amount=float(t["amount"])
                if qty<=0 or fill<=0 or fee<0:
                    errors.append("Geçersiz işlem miktarı/fiyat/komisyon #"+str(t["id"]))
                    continue
                fees+=fee
                if t["side"]=="BUY":
                    expected_cost=qty*fill+fee
                    if abs(expected_cost-amount)>AUDIT_CASH_TOLERANCE:
                        errors.append("Alış tutarı uyuşmaz: #"+str(t["id"]))
                    cash-=amount
                    previous["qty"]+=qty
                    previous["basis"]+=amount
                elif t["side"]=="SELL":
                    if qty>previous["qty"]+AUDIT_QTY_TOLERANCE:
                        errors.append("Olmayan miktarda sanal satış #"+str(t["id"]))
                    basis_cost=(previous["basis"]/previous["qty"])*qty if previous["qty"] else 0.0
                    proceeds=qty*fill-fee
                    cash+=proceeds
                    closed_pnl+=proceeds-basis_cost
                    if abs((proceeds-basis_cost)-float(t["realized"]))>AUDIT_CASH_TOLERANCE:
                        errors.append("Gerçekleşen P/L uyuşmaz: #"+str(t["id"]))
                    previous["qty"]-=qty
                    previous["basis"]-=basis_cost
                    if previous["qty"]<AUDIT_QTY_TOLERANCE:
                        previous={"qty":0.0,"basis":0.0}
                else:
                    errors.append("Bilinmeyen işlem türü #"+str(t["id"]))
                inventory[symbol]=previous
            if abs(cash-float(account["cash"]))>AUDIT_CASH_TOLERANCE:
                errors.append("Banka bakiyesi sanal işlem defterinden farklı.")
            stored_map={p["ticker"]:p for p in stored}
            all_symbols=set(stored_map)|set(inventory)
            for symbol in all_symbols:
                expect=inventory.get(symbol,{"qty":0.0,"basis":0.0})
                actual=stored_map.get(symbol,{"qty":0.0,"avg_cost":0.0})
                actual_qty=float(actual["qty"])
                if abs(expect["qty"]-actual_qty)>AUDIT_QTY_TOLERANCE:
                    errors.append(symbol+": eldeki miktar defterden farklı.")
                expected_basis=expect["basis"]
                stored_basis=actual_qty*float(actual["avg_cost"])
                if abs(expected_basis-stored_basis)>AUDIT_CASH_TOLERANCE:
                    errors.append(symbol+": ortalama maliyet defterden farklı.")
            marks={}
            unrealized=0.0
            missing=[]
            for symbol,position in inventory.items():
                if position["qty"]<=AUDIT_QTY_TOLERANCE:continue
                mark=(prices or {}).get(symbol)
                if mark is None:
                    try:
                        quote=terminal.market(symbol)
                        if quote["fresh"]:mark=quote["price"]
                    except (OSError,ValueError):pass
                if mark is None:
                    missing.append(symbol)
                    continue
                mark=terminal.finite(mark)
                marks[symbol]=mark
                unrealized+=position["qty"]*mark-position["basis"]
            expected_nav=cash+sum(position["qty"]*marks[sym] for sym,position in inventory.items() if sym in marks)
            if not missing:
                identity=closed_pnl+unrealized-(expected_nav-float(account["initial"]))
                if abs(identity)>AUDIT_CASH_TOLERANCE:
                    errors.append("Toplam P/L = gerçekleşen + açık P/L eşitliği bozuldu.")
            return {
                "status":"PASS" if not errors else "MISMATCH",
                "errors":errors,"trade_count":len(trades),
                "starting_usd":round(float(account["initial"]),4),
                "ledger_cash":round(cash,5),
                "saved_cash":round(float(account["cash"]),5),
                "closed_pnl":round(closed_pnl,5),
                "open_pnl":round(unrealized,5) if not missing else None,
                "commission_total":round(fees,5),
                "total_nav":round(expected_nav,5) if not missing else None,
                "missing_quotes":missing,
                "note":"Eski sanal defter, miktar ve maliyet üzerinden bağımsız yeniden hesaplanır. Eksik güncel fiyat varsa açık P/L ve toplam NAV doğrulanmaz. Kayıtlara müdahale edilmez."
            }

def stability(report):
    folds=report["folds"]
    count=len(folds)
    positive_to_negative=sum(x["validation_selected_return_pct"]>0 and x["test_return_pct"]<0 for x in folds)
    changes=sum(a["selected"]!=b["selected"] for a,b in zip(folds,folds[1:]))
    negative_tests=sum(x["test_return_pct"]<0 for x in folds)
    gaps=[round(x["test_return_pct"]-x["validation_selected_return_pct"],2) for x in folds]
    frequency=dict(Counter(x["selected_label"] for x in folds))
    chosen=report["totals"]["selected"]
    # Fixed, descriptive gates; NEVER retroactively select the winning OOS strategy.
    flagged=(negative_tests>0 or positive_to_negative>0 or changes>0
             or chosen["max_drawdown_pct"]<-10 or chosen["return_pct"]<=0)
    return {
       "status":"FURTHER_PAPER_TESTING_REQUIRED",
       "selection_frequency":frequency,"switches":changes,
       "validation_positive_test_negative":positive_to_negative,
       "negative_test_folds":negative_tests,
       "validation_test_gap_pp":gaps,
       "min_gap_pp":min(gaps),
       "worst_oos_drawdown_pct":chosen["max_drawdown_pct"],
       "risk_flags_present":flagged,
       "fold_count":count,"oos_days":report["oos_days"],
       "note":"Adaylar veya skor fonksiyonu test sonucunu görünce değiştirilmedi. %10 OOS düşüş, dönemsel kayıplar ve strateji değişimleri izlenir. 3-4 katlama gerçek parayla işlem için yeterli kanıt sayılmaz."
    }

def trade_attribution(bars, report):
    """Replay selected fold trades for entry-regime attribution, not daily attribution."""
    today=_utc().date().isoformat()
    complete=[x for x in bars if x["time"][:10]<today]
    index={r["time"][:10]:i for i,r in enumerate(complete)}
    closes=[float(b["close"]) for b in complete]
    events=[]
    for fold in report["folds"]:
        start=index[fold["test_start"]]
        end=index[fold["test_end"]]+1
        method=fold["selected"]
        cash=100.0
        qty=0.0
        entry_cost=0.0
        entry_date=None
        entry_regime=None
        for i in range(start,end):
            direction=wf._decision(closes,i,method)
            opening=float(complete[i]["open"])
            if direction and qty==0 and cash>0:
                price=opening*(1+terminal.SLIPPAGE)
                paid=cash
                qty=(cash/(1+terminal.COMMISSION))/price
                cash=0.0
                entry_cost=paid
                entry_date=complete[i]["time"][:10]
                entry_regime=wf._regime(closes,i)
            elif not direction and qty>0:
                price=opening*(1-terminal.SLIPPAGE)
                proceeds=qty*price*(1-terminal.COMMISSION)
                pnl=proceeds-entry_cost
                events.append({"fold":fold["fold"],"ticker":report["ticker"],"strategy":method,
                    "entry_day":entry_date,"entry_regime":entry_regime,
                    "exit_day":complete[i]["time"][:10],"status":"CLOSED",
                    "net_pnl_usd":round(pnl,4),
                    "net_return_pct":round(pnl/entry_cost*100,2)})
                cash+=proceeds
                qty=0
                entry_cost=0
        if qty>0:
            value=qty*closes[end-1]
            pnl=value-entry_cost
            events.append({"fold":fold["fold"],"ticker":report["ticker"],"strategy":method,
                    "entry_day":entry_date,"entry_regime":entry_regime,
                    "exit_day":None,"status":"OPEN_AT_FOLD_END",
                    "net_pnl_usd":round(pnl,4),
                    "net_return_pct":round(pnl/entry_cost*100,2)})
        audit=wf.simulate(complete,start,end,method)
        replay_equity=cash+qty*closes[end-1]
        if abs(replay_equity-audit["ending_equity"])>0.002:
            raise ValueError("Rejim işlem muhasebesi ile backtest NAV uyuşmuyor.")
    return {"events":events,
            "closed_count":sum(e["status"]=="CLOSED" for e in events),
            "open_count":sum(e["status"]!="CLOSED" for e in events),
            "negative_trades":sum(e["net_pnl_usd"]<0 for e in events),
            "note":"İşlem rejimi girişten önceki tamamlanmış 20 günlük trenddir; bu tablo işlemdeki zararları açık/kapalı ayrımıyla gösterir. Fold başında sermaye tekrar 100$ olduğundan tutarlar bütün katlamalara tek sermaye gibi toplanamaz."}

def _signal_data(ticker,now=None,bars=None):
    terminal.valid_ticker(ticker)
    now=now or _utc()
    bars=bars if bars is not None else terminal.get_series(ticker,"1y","1d",ttl=600)
    today=now.date().isoformat()
    completed=[x for x in bars if x["time"][:10]<today]
    if len(completed)<51:
        raise ValueError("İleri test için yeterli tamamlanmış günlük mum yok.")
    last_day=completed[-1]["time"][:10]
    yesterday=(now.date()-timedelta(days=1)).isoformat()
    if last_day!=yesterday:
        raise ValueError("En son tamamlanmış günlük mum dün değil; yeni sinyal kilitlenmedi.")
    closes=[float(x["close"]) for x in completed]
    close=terminal.finite(completed[-1]["close"])
    if close<=0:
        raise ValueError("Geçersiz referans fiyat.")
    res=[]
    for method in PROSPECTIVE:
        direction="BEKLE" if method=="cash" else (
            "AL" if wf._decision(closes,len(closes),method) else "NAKIT")
        res.append({"ticker":ticker,"strategy":method,"signal_day":last_day,
                    "direction":direction,"baseline_close":close,
                    "target_day":today})
    return res

def record_forward(ticker,now=None,bars=None):
    """Only first, current timestamped snapshot can be saved; idempotent."""
    now=now or _utc()
    records=_signal_data(ticker,now,bars)
    new_count=0
    with terminal.MUTEX:
        with terminal.connect() as db:
            _table(db)
            db.execute("BEGIN IMMEDIATE")
            for p in records:
                cursor=db.execute("""INSERT OR IGNORE INTO forward_signals(
                   ticker,strategy,signal_day,recorded_at,baseline_close,direction,source,target_day)
                   VALUES(?,?,?,?,?,?,?,?)""",(
                     ticker,p["strategy"],p["signal_day"],now.isoformat(timespec="seconds"),
                     p["baseline_close"],p["direction"],"Yahoo Finance daily close (delayed)",
                     p["target_day"]))
                new_count+=cursor.rowcount
            db.execute("COMMIT")
    return {"ticker":ticker,"new":new_count,"existing":len(records)-new_count,
            "signal_day":records[0]["signal_day"],"target_day":records[0]["target_day"],
            "note":"Yalnızca son tamamlanmış mum üzerinden şimdi kaydedildi. Geçmişe sinyal eklenmedi. Gerçek emir gönderilmez."}

def evaluate_forward(ticker,now=None,bars=None):
    """Update completed NEXT-day close, never using the still-open today's bar."""
    terminal.valid_ticker(ticker)
    now=now or _utc()
    bars=bars if bars is not None else terminal.get_series(ticker,"1y","1d",ttl=600)
    done={b["time"][:10]:float(b["close"]) for b in bars if b["time"][:10]<now.date().isoformat()}
    updates=0
    with terminal.MUTEX:
        with terminal.connect() as db:
            _table(db)
            db.execute("BEGIN IMMEDIATE")
            pending=db.execute("""SELECT id,baseline_close,target_day FROM forward_signals
                                  WHERE ticker=? AND evaluated_at IS NULL""",(ticker,)).fetchall()
            for p in pending:
                if p["target_day"] not in done:continue
                ending=done[p["target_day"]]
                if not math.isfinite(ending) or ending<=0:continue
                move=100*(ending/p["baseline_close"]-1)
                result=db.execute("""UPDATE forward_signals SET evaluated_at=?,target_close=?,movement_pct=?
                         WHERE id=? AND evaluated_at IS NULL""",(
                         now.isoformat(timespec="seconds"),ending,move,p["id"]))
                updates+=result.rowcount
            db.execute("COMMIT")
    return {"updated":updates,"note":"Günlük sonuç, ilk kaydedilmiş referans kapanışından ertesi günün tamamlanmış kapanışına ölçülür. Aynı günün eksik mumu dikkate alınmaz. Kullanıcı tetiklemelidir; arka planda otomatik izlemez."}

def forward_status():
    with terminal.MUTEX:
        with terminal.connect() as db:
            _table(db)
            records=[dict(x) for x in db.execute("""SELECT ticker,strategy,signal_day,recorded_at,
                baseline_close,direction,target_day,evaluated_at,target_close,movement_pct
                FROM forward_signals ORDER BY signal_day DESC,ticker,strategy LIMIT 150""")]
    summary={}
    for t in terminal.ASSETS:
        subset=[x for x in records if x["ticker"]==t]
        scores=[]
        for method in PROSPECTIVE:
            eligible=[x for x in subset if x["strategy"]==method and x["movement_pct"] is not None]
            directional=[x for x in eligible if x["direction"]=="AL" and x["movement_pct"]!=0]
            # For long-only models NAKIT is a no-position decision, not a short call.
            n=len(directional)
            scores.append({"strategy":method,"observations":len(eligible),
                "long_predictions":n,
                "long_direction_success_pct":round(100*sum(x["movement_pct"]>0 for x in directional)/n,1) if n else None})
        summary[t]=scores
    return {"records":records,"summary":summary,
            "disclaimer":"Sinyaller kaydedildiği andan sonra yalnızca kullanıcı 'Sonuçları güncelle' dediğinde izlenir. Günlük kapanış gecikmelidir; orijinal veri sonradan revize edilebilir. NAKİT=pozisyon açmama, SAT/SHORT değil. Bu yalnızca ileriye dönük yön gözlemidir, gerçekleşen getiri değildir."}
