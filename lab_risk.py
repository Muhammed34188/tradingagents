"""Paper-only risk controls, daily performance and auditable AI signal observations.

Virtual stops are evaluated ONLY when the signed-in terminal calls /api/risk/check.
Price source is Yahoo Finance, potentially delayed. No broker/exchange orders.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import lab_terminal as base

STOP_PCT=5.0
MAX_ASSET_USD=25.0
MAX_RISK_USD=2.0
MAX_DAILY_LOSS_USD=3.0
MAX_DRAWDOWN_PCT=10.0
STAGES=(("h24",timedelta(hours=24)),("d7",timedelta(days=7)))

def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS virtual_stops(
        ticker TEXT PRIMARY KEY, stop_price REAL NOT NULL, armed_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS risk_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ticker TEXT NOT NULL,
        event TEXT NOT NULL, detail TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS ai_observations(
        report_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, decision TEXT NOT NULL,
        report_date TEXT NOT NULL, observed_at TEXT, observed_price REAL,
        price_source TEXT NOT NULL, h24_at TEXT, h24_price REAL,
        d7_at TEXT, d7_price REAL)""")

def risk_event(db,ticker,event,detail):
    db.execute("INSERT INTO risk_events(at,ticker,event,detail) VALUES(?,?,?,?)",
               (utc(),ticker,event,detail))

def validate_buy(db,ticker,amount,qty0,execution_price):
    """Call under base.MUTEX and same DB transaction as virtual buy."""
    setup(db)
    if amount < 1:
        raise ValueError("En az 1 USD sanal alım gerekir.")
    preexisting = qty0*execution_price
    if preexisting + amount > MAX_ASSET_USD + 1e-7:
        raise ValueError("Varlık başına sanal pozisyon limiti 25 USD. Alım engellendi.")
    if amount*STOP_PCT/100 > MAX_RISK_USD:
        raise ValueError("Sanal işlemde varsayımsal stop riski 2 USD'yi aşamaz.")
    today=utc()[:10]
    sold=db.execute("""SELECT COALESCE(SUM(realized),0) AS pnl
                       FROM trades WHERE side='SELL' AND substr(at,1,10)=?""",(today,)).fetchone()["pnl"]
    if sold <= -MAX_DAILY_LOSS_USD:
        raise ValueError("Günlük gerçekleşmiş sanal zarar limiti aşıldı; yeni AL kapalı.")
    last=db.execute("SELECT nav FROM equity ORDER BY id DESC LIMIT 1").fetchone()
    peak=db.execute("SELECT MAX(nav) AS v FROM equity").fetchone()["v"]
    if last and peak and peak>0 and 100*(float(last["nav"])/float(peak)-1)<=-MAX_DRAWDOWN_PCT:
        raise ValueError("Portföy sanal maksimum düşüş sınırında; yeni AL kapalı.")

def arm_after_buy(db,ticker,execution_price):
    setup(db)
    new_stop=execution_price*(1-STOP_PCT/100)
    prev=db.execute("SELECT stop_price FROM virtual_stops WHERE ticker=?",(ticker,)).fetchone()
    if prev:
        new_stop=max(new_stop,prev["stop_price"])
    db.execute("""INSERT INTO virtual_stops(ticker,stop_price,armed_at) VALUES(?,?,?)
                ON CONFLICT(ticker) DO UPDATE SET stop_price=excluded.stop_price,
                armed_at=excluded.armed_at""",(ticker,new_stop,utc()))
    risk_event(db,ticker,"STOP_ARMED","Sanal stop güncellendi (otomatik kâr garantisi yok).")

def clear_if_closed(db,ticker,remaining_qty):
    setup(db)
    if remaining_qty<1e-9:
        db.execute("DELETE FROM virtual_stops WHERE ticker=?",(ticker,))

def stop_control(ticker,pct=STOP_PCT):
    base.valid_ticker(ticker)
    pct=float(pct)
    if not 1<=pct<=20:
        raise ValueError("Sanal stop yüzdesi 1 ile 20 arasında olmalı.")
    quote=base.market(ticker)
    if not quote["fresh"]:
        raise ValueError("Güncel kabul edilebilir veri yok; sanal stop kurulmadı.")
    with base.MUTEX:
        with base.connect() as db:
            setup(db)
            pos=db.execute("SELECT qty FROM positions WHERE ticker=?",(ticker,)).fetchone()
            if not pos or pos["qty"]<=1e-9:
                raise ValueError("Stop için açık sanal pozisyon gerekiyor.")
            stop=quote["price"]*(1-pct/100)
            db.execute("""INSERT INTO virtual_stops(ticker,stop_price,armed_at) VALUES(?,?,?)
                        ON CONFLICT(ticker) DO UPDATE SET stop_price=excluded.stop_price,
                        armed_at=excluded.armed_at""",(ticker,stop,utc()))
            risk_event(db,ticker,"STOP_ARMED","Mevcut sanal pozisyon için stop kuruldu.")
            return {"ticker":ticker,"stop_price":round(stop,2),"pct":pct,
                    "disclaimer":"Yalnızca terminal açıkken periyodik kontrol edilir; garantili stop emri değildir."}

def risk_dashboard():
    with base.MUTEX:
        with base.connect() as db:
            setup(db)
            stops=[dict(x) for x in db.execute("SELECT * FROM virtual_stops ORDER BY ticker")]
            today=utc()[:10]
            today_result=db.execute("""SELECT COALESCE(SUM(realized),0) AS pnl FROM trades
                        WHERE side='SELL' AND substr(at,1,10)=?""",(today,)).fetchone()["pnl"]
            events=[dict(x) for x in db.execute(
                "SELECT at,ticker,event,detail FROM risk_events ORDER BY id DESC LIMIT 15"
            )]
            last=db.execute("SELECT nav FROM equity ORDER BY id DESC LIMIT 1").fetchone()
            peak=db.execute("SELECT MAX(nav) AS value FROM equity").fetchone()["value"]
            dd=round(100*(last["nav"]/peak-1),3) if last and peak and peak>0 else 0.0
            return {"stops":stops,"events":events,"daily_realized_pnl":round(today_result,4),
                "max_asset_usd":MAX_ASSET_USD,"max_risk_usd":MAX_RISK_USD,
                "max_daily_loss_usd":MAX_DAILY_LOSS_USD,"max_drawdown_pct":MAX_DRAWDOWN_PCT,
                "drawdown_now_pct":dd,
                "buys_paused":today_result<=-MAX_DAILY_LOSS_USD or dd<=-MAX_DRAWDOWN_PCT,
                "monitor_note":"Sanal stop yalnızca terminal açıkken yaklaşık 90 saniyede bir kontrol edilir. Fiyat gecikmesi, bağlantı kesintisi ve boşluklar vardır; garanti değildir."}

def check_stops():
    """Called by explicit authenticated POST; never from a GET or on server startup."""
    with base.MUTEX:
        with base.connect() as db:
            setup(db)
            active=[dict(r) for r in db.execute("SELECT ticker,stop_price FROM virtual_stops")]
    triggered=[]
    errors=[]
    for stop in active:
        ticker=stop["ticker"]
        try:
            q=base.market(ticker)
            if not q["fresh"]:
                errors.append(ticker+": fiyat güncel değil")
                continue
            if q["price"]>stop["stop_price"]:
                continue
            with base.MUTEX:
                with base.connect() as db:
                    setup(db)
                    db.execute("BEGIN IMMEDIATE")
                    current=db.execute("SELECT stop_price FROM virtual_stops WHERE ticker=?",(ticker,)).fetchone()
                    pos=db.execute("SELECT qty,avg_cost FROM positions WHERE ticker=?",(ticker,)).fetchone()
                    if not current or not pos or pos["qty"]<=1e-9 or q["price"]>current["stop_price"]:
                        db.execute("ROLLBACK")
                        continue
                    fill=q["price"]*(1-base.SLIPPAGE)
                    qty=pos["qty"]
                    gross=qty*fill
                    fee=gross*base.COMMISSION
                    proceeds=gross-fee
                    realized=proceeds-qty*pos["avg_cost"]
                    cash=db.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"]
                    db.execute("UPDATE account SET cash=? WHERE id=1",(cash+proceeds,))
                    db.execute("UPDATE positions SET qty=0,avg_cost=0 WHERE ticker=?",(ticker,))
                    at=utc()
                    db.execute("INSERT INTO trades(at,ticker,side,qty,price,fee,realized,amount) VALUES(?,?,?,?,?,?,?,?)",
                               (at,ticker,"SELL",qty,fill,fee,realized,gross))
                    db.execute("DELETE FROM virtual_stops WHERE ticker=?",(ticker,))
                    risk_event(db,ticker,"VIRTUAL_STOP_TRIGGERED",
                               "Gecikmeli veri üzerinden sanal pozisyon kapatıldı.")
                    db.execute("COMMIT")
                    triggered.append({"ticker":ticker,"fill_price":round(fill,2),
                                      "gross_usd":round(gross,4),"realized_usd":round(realized,4)})
        except (ValueError,OSError,sqlite3.Error) as exc:
            errors.append(ticker+": sanal stop kontrolü yapılamadı")
    if triggered:
        try: base.portfolio(record_trade=True)
        except (ValueError,OSError,sqlite3.Error): pass
    return {"triggered":triggered,"warnings":errors,"checked_at":utc(),
            "monitor_note":"Bu kontrol gerçek emir değildir; gecikmeli fiyatla sanal kapanış yapabilir."}

def performance():
    data=base.portfolio()
    with base.MUTEX:
        with base.connect() as db:
            day_rows=[dict(x) for x in db.execute("""SELECT substr(at,1,10) AS day,
              SUM(CASE WHEN side='SELL' THEN realized ELSE 0 END) AS realized,
              SUM(fee) AS fees,COUNT(*) AS trades
              FROM trades GROUP BY substr(at,1,10) ORDER BY day DESC LIMIT 30""")]
            points=[dict(x) for x in db.execute(
                "SELECT at,nav FROM equity ORDER BY id ASC LIMIT 1500")]
    daily_equity={}
    for p in points:
        daily_equity[p["at"][:10]]=round(p["nav"],4)
    return {"starting_cash":data["starting_cash"],"equity":data["equity"],
        "cash":data["cash"],"realized_pnl":data["realized_pnl"],
        "unrealized_pnl":data["unrealized_pnl"],"total_pnl":data["total_pnl"],
        "fees":data["fees_paid"],"win_rate":data["win_rate"],
        "closed_trades":data["closed_trades"],"drawdown":data["max_drawdown_pct"],
        "daily":[{"day":r["day"],"realized":round(r["realized"],4),
                  "fees":round(r["fees"],4),"trades":r["trades"],
                  "last_equity":daily_equity.get(r["day"])} for r in day_rows],
        "nav_curve":data["curve"],"unpriced":data["unpriced"]}

def observe_report(ticker, folder, root):
    """Take the FIRST verifiable quoted price after report creation, no retroactive lookahead."""
    base.valid_ticker(ticker)
    folder=Path(folder)
    if not folder.name.startswith(ticker+"_"):
        return {"recorded":False,"reason":"Rapor kimliği uyuşmuyor."}
    parsed=base.signal(root,ticker)
    if parsed.get("report_id")!=folder.name or parsed.get("signal") not in ("AL","SAT","BEKLE"):
        return {"recorded":False,"reason":"Doğrulanabilir rapor kararı bulunamadı."}
    quote=None
    try:
        candidate=base.market(ticker)
        if candidate["fresh"]: quote=candidate
    except ValueError:
        pass
    with base.MUTEX:
        with base.connect() as db:
            setup(db)
            db.execute("""INSERT OR IGNORE INTO ai_observations(
                report_id,ticker,decision,report_date,observed_at,observed_price,price_source)
                VALUES(?,?,?,?,?,?,?)""",(
                folder.name,ticker,parsed["signal"],parsed["date"],
                quote["as_of"] if quote else None,quote["price"] if quote else None,
                "Yahoo delayed (first observed on/after report; not execution price)"
            ))
    return {"recorded":True,"quote_available":quote is not None}

def decision_history(root, check_due=True):
    """Only future observations are evaluated. No false reconstructed historical quotes."""
    # Existing reports predate this telemetry system. Archive their decision,
    # but NEVER assign a newly discovered price to the historical report date.
    for ticker in base.ASSETS:
        latest=base.signal(root,ticker)
        report_id=latest.get("report_id")
        if report_id and latest.get("signal") in ("AL","SAT","BEKLE"):
            with base.MUTEX:
                with base.connect() as db:
                    setup(db)
                    db.execute("""INSERT OR IGNORE INTO ai_observations(
                        report_id,ticker,decision,report_date,observed_at,observed_price,price_source)
                        VALUES(?,?,?,?,NULL,NULL,?)""",
                        (report_id,ticker,latest["signal"],latest["date"],
                         "Archived decision (historical quote was not captured)"))
    with base.MUTEX:
        with base.connect() as db:
            setup(db)
            rows=[dict(r) for r in db.execute("SELECT * FROM ai_observations ORDER BY report_id DESC LIMIT 50")]
    if check_due:
        now=datetime.now(timezone.utc)
        for ticker in base.ASSETS:
            due=[r for r in rows if r["ticker"]==ticker and r["observed_price"] is not None
                 and any(r[stage+"_at"] is None
                         and datetime.fromisoformat(r["observed_at"])+offset<=now
                         for stage,offset in STAGES)]
            if not due: continue
            try:
                quote=base.market(ticker)
                if not quote["fresh"]: continue
                current_at=datetime.fromisoformat(quote["as_of"])
            except ValueError: continue
            with base.MUTEX:
                with base.connect() as db:
                    setup(db)
                    for row in due:
                        initial=datetime.fromisoformat(row["observed_at"])
                        for stage,offset in STAGES:
                            if row[stage+"_at"] is None and current_at>=initial+offset:
                                db.execute("UPDATE ai_observations SET "+stage+"_at=?, "+stage+"_price=? WHERE report_id=? AND "+stage+"_at IS NULL",
                                           (quote["as_of"],quote["price"],row["report_id"]))
    with base.MUTEX:
        with base.connect() as db:
            setup(db)
            rows=[dict(r) for r in db.execute("SELECT * FROM ai_observations ORDER BY report_id DESC LIMIT 50")]
    for row in rows:
        p=row["observed_price"]
        for stage,_ in STAGES:
            price=row[stage+"_price"]
            pct=round(100*(price/p-1),2) if p and price else None
            row[stage+"_return_pct"]=pct
            # HOLD does not imply a direction: report price movement, no win label.
            row[stage+"_direction_correct"]=(
                pct>0 if row["decision"]=="AL" else
                pct<0 if row["decision"]=="SAT" else None
            ) if pct is not None else None
    return {"records":rows,"disclaimer":"AI takibi yalnızca ilk kaydedilen gecikmeli fiyat üzerinden yapılır; 24s/7g kontrolleri ilk uygun gözlemde hesaplanır. BEKLE için kazanma oranı verilmez. Geçmiş raporun o anki fiyatı sonradan uydurulmaz."}
