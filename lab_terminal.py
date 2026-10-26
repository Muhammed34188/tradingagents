"""Private, non-executing BTC/ETH paper trading terminal.

Uses delayed public Yahoo Finance price data (when available), NOT exchange quotes.
No broker connections, leverage, short positions, or LLM-triggered orders.
"""
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ASSETS = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum"}
ROOT = Path("/home/appuser/.tradingagents")
DB = ROOT / "paper_portfolio.sqlite3"
COMMISSION = 0.001
SLIPPAGE = 0.0005
MUTEX = threading.RLock()
CACHE = {}

def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Geçersiz sayısal değer.")
    return value

def valid_ticker(ticker):
    if ticker not in ASSETS:
        raise ValueError("Sadece BTC-USD ve ETH-USD destekleniyor.")
    return ticker

def get_series(ticker, period="5d", interval="5m", ttl=90):
    valid_ticker(ticker)
    if (period, interval) not in {("5d","5m"), ("1y","1d")}:
        raise ValueError("İzin verilmeyen veri aralığı.")
    key = (ticker, period, interval)
    with MUTEX:
        cached = CACHE.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
    try:
        frame = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False, timeout=12)
        if frame is None or frame.empty:
            raise ValueError("Fiyat kaynağı boş sonuç döndürdü.")
        rows = []
        for dt, r in frame.iterrows():
            try:
                op, close = finite(r["Open"]), finite(r["Close"])
                if min(op,close) <= 0:
                    continue
                stamp = dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                rows.append({"time": stamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
                             "open": op, "close": close})
            except (KeyError, ValueError, TypeError):
                continue
        if len(rows) < 2:
            raise ValueError("Yeterli doğrulanabilir fiyat barı yok.")
        with MUTEX:
            CACHE[key] = (time.monotonic(), rows)
        return rows
    except Exception as exc:
        raise ValueError("Yahoo Finance fiyat verisi şu an alınamıyor; daha sonra deneyin.") from exc

def market(ticker):
    bars = get_series(ticker)
    latest = bars[-1]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(latest["time"])).total_seconds()
    fresh = -300 <= age <= 1200
    return {
        "ticker": ticker, "price": round(latest["close"],2),
        "as_of": latest["time"], "fresh": fresh,
        "delayed": True, "source": "Yahoo Finance (gecikmeli / gösterge niteliğinde)",
        "chart": [{"time": p["time"],"price":round(p["close"],2)} for p in bars[-220:]],
        "change_pct": round((latest["close"]/bars[0]["close"]-1)*100,2),
    }

def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK(id=1),
        cash REAL NOT NULL CHECK(cash >= -0.00001),
        initial REAL NOT NULL)""")
    db.execute("""INSERT OR IGNORE INTO account(id,cash,initial) VALUES(1,100.0,100.0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS positions (
        ticker TEXT PRIMARY KEY, qty REAL NOT NULL, avg_cost REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
        qty REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
        realized REAL NOT NULL, amount REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
        nav REAL NOT NULL)""")
    return db

def portfolio(mark_prices=None):
    with MUTEX:
        with connect() as db:
            acc = db.execute("SELECT cash,initial FROM account WHERE id=1").fetchone()
            ps = db.execute("SELECT ticker,qty,avg_cost FROM positions WHERE qty>0.000000001").fetchall()
            trades = [dict(x) for x in db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 60").fetchall()]
            realized_row = db.execute("SELECT COALESCE(SUM(realized),0) AS pnl, COUNT(*) AS cnt, COALESCE(SUM(CASE WHEN realized>0 THEN 1 ELSE 0 END),0) AS wins FROM trades WHERE side='SELL'").fetchone()
            nav = float(acc["cash"])
            missing = []
            positions = []
            for pos in ps:
                ticker = pos["ticker"]
                price = (mark_prices or {}).get(ticker)
                if price is None:
                    try:
                        quote = market(ticker)
                        if quote["fresh"]:
                            price=quote["price"]
                    except ValueError:
                        pass
                if price is None:
                    missing.append(ticker)
                    value = None
                else:
                    value = float(pos["qty"]) * float(price)
                    nav += value
                positions.append({"ticker":ticker,"qty":pos["qty"],"avg_cost":pos["avg_cost"],
                                  "price":price,"value":round(value,4) if value is not None else None})
            if not missing:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                last = db.execute("SELECT at FROM equity ORDER BY id DESC LIMIT 1").fetchone()
                if not last or (datetime.fromisoformat(now)-datetime.fromisoformat(last["at"])).total_seconds() >= 60:
                    db.execute("INSERT INTO equity(at,nav) VALUES(?,?)", (now,nav))
            curve = [{"time":x["at"],"nav":round(x["nav"],4)} for x in db.execute("SELECT at,nav FROM equity ORDER BY id DESC LIMIT 400").fetchall()][::-1]
            peak=0.0
            worst=0.0
            for point in curve:
                peak=max(peak,point["nav"])
                if peak>0:worst=min(worst,100*(point["nav"]/peak-1))
            count=realized_row["cnt"]
            return {
                "starting_cash":100.0,
                "cash":round(acc["cash"],4),"equity":round(nav,4) if not missing else None,
                "unpriced":missing,"positions":positions,"trades":trades,
                "realized_pnl":round(realized_row["pnl"],4),
                "total_pnl":round(nav-100,4) if not missing else None,
                "win_rate":round(100*realized_row["wins"]/count,2) if count else None,
                "closed_trades":count,"max_drawdown_pct":round(worst,2),
                "curve":curve,
                "notice":"Sadece sanal portföy. Ücret varsayımı işlem başına %0,1; fiyatlar gecikmeli Yahoo verisidir."
            }

def paper_order(payload):
    ticker=valid_ticker(payload.get("ticker"))
    side=payload.get("side")
    if side not in ("BUY","SELL"):
        raise ValueError("Yalnızca AL veya SAT işlemi yapılabilir.")
    amount=finite(payload.get("amount"))
    if amount < 1 or amount > 10000:
        raise ValueError("Sanal işlem tutarı 1–10.000 USD arasında olmalı.")
    quote=market(ticker)
    if not quote["fresh"]:
        raise ValueError("Fiyat güncel değil; sanal işlem yapılmadı.")
    px=quote["price"]
    if px <= 0:
        raise ValueError("Geçersiz fiyat.")
    at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with MUTEX:
        db=connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            acc=db.execute("SELECT cash FROM account WHERE id=1").fetchone()
            existing=db.execute("SELECT qty,avg_cost FROM positions WHERE ticker=?", (ticker,)).fetchone()
            qty0=existing["qty"] if existing else 0.0
            avg0=existing["avg_cost"] if existing else 0.0
            if side=="BUY":
                if amount > acc["cash"] + 1e-7:
                    raise ValueError("Sanal nakit yetersiz.")
                fee=amount*COMMISSION/(1+COMMISSION)
                spend=amount-fee
                qty=spend/px
                newqty=qty0+qty
                newavg=(qty0*avg0+amount)/newqty
                cash=max(0.0,acc["cash"]-amount)
                realized=0.0
            else:
                qty=amount/px
                if qty0<=0 or qty>qty0+1e-8:
                    raise ValueError("Bu miktarda BTC/ETH sanal pozisyonun yok.")
                qty=min(qty,qty0)
                gross=qty*px
                fee=gross*COMMISSION
                cash=acc["cash"]+gross-fee
                newqty=max(0.0,qty0-qty)
                newavg=avg0 if newqty>1e-10 else 0.0
                realized=gross-fee-qty*avg0
            db.execute("UPDATE account SET cash=? WHERE id=1",(cash,))
            db.execute("""INSERT INTO positions(ticker,qty,avg_cost) VALUES(?,?,?)
                         ON CONFLICT(ticker) DO UPDATE SET qty=excluded.qty,avg_cost=excluded.avg_cost""",
                       (ticker,newqty,newavg))
            db.execute("INSERT INTO trades(at,ticker,side,qty,price,fee,realized,amount) VALUES(?,?,?,?,?,?,?,?)",
                       (at,ticker,side,qty,px,fee,realized,amount))
            db.execute("COMMIT")
            return {"ok":True,"ticker":ticker,"side":side,"filled_usd":round(amount,4),
                    "price":px,"qty":round(qty,9),"fee":round(fee,5),"realized":round(realized,5),
                    "notice":"Yalnızca sanal emir. Gerçek borsaya talimat gönderilmedi."}
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

def signal(results_dir, ticker):
    ticker=valid_ticker(ticker)
    root=results_dir/"reports"
    if not root.is_dir():
        return {"signal":None,"message":"Henüz yapay zekâ analiz raporu yok."}
    folders=sorted((p for p in root.glob(ticker+"_????????_??????") if p.is_dir()),reverse=True)
    for folder in folders:
        doc=folder/"complete_report.md"
        if not doc.is_file():
            continue
        try:
            body=doc.read_text(encoding="utf-8",errors="replace")
        except OSError:
            continue
        rating=re.search(r"(?im)^-\s*Rating:\s*([^\r\n]+)",body[:1600])
        if not rating:
            continue
        label=rating.group(1).strip().lower()
        if re.search(r"\b(hold|wait|pass|neutral)\b",label):
            decision="BEKLE"
        elif re.search(r"\b(buy|long)\b",label):
            decision="AL"
        elif re.search(r"\b(sell|short)\b",label):
            decision="SAT"
        else:
            decision=None
        return {"signal":decision,"rating":rating.group(1).strip()[:120],
                "report_id":folder.name,
                "date":folder.name.split("_")[1],
                "note":"Modelin eski analiz raporundan çıkarılan görüşü; güncel emir veya fiyat sinyali değildir."}
    return {"signal":None,"message":"Kayıtlı raporda güvenilir biçimde okunabilen nihai karar yok."}

def backtest(ticker):
    valid_ticker(ticker)
    bars=get_series(ticker,"1y","1d",ttl=600)
    if len(bars)<65:
        raise ValueError("Geçmiş test için yeterli fiyat geçmişi yok.")
    closes=[b["close"] for b in bars]
    cash=100.0
    qty=0.0
    cost=0.0
    peak=100.0
    dd=0.0
    closed=0
    wins=0
    trades=0
    total_fees=0.0
    closed_pnl=0.0
    curve=[]
    # Signal uses only the completed PRIOR trading day, executed at the next bar OPEN.
    # This prevents using the current close to trade at that same close.
    for i in range(50,len(bars)):
        prev20=sum(closes[i-20:i])/20
        prev50=sum(closes[i-50:i])/50
        target_long=prev20>prev50
        op=bars[i]["open"]
        if target_long and qty<=0 and cash>0:
            fill=op*(1+SLIPPAGE)
            qty=(cash/(1+COMMISSION))/fill
            buy_fee=cash-(qty*fill)
            total_fees+=buy_fee
            cost=cash
            cash=0.0
            trades+=1
        elif not target_long and qty>0:
            fill=op*(1-SLIPPAGE)
            proceeds=qty*fill*(1-COMMISSION)
            total_fees+=qty*fill*COMMISSION
            closed_pnl+=proceeds-cost
            cash+=proceeds
            closed+=1
            if proceeds>cost:
                wins+=1
            qty=0.0
            cost=0.0
            trades+=1
        eq=cash+qty*closes[i]
        peak=max(peak,eq)
        dd=min(dd,100*(eq/peak-1))
        curve.append({"date":bars[i]["time"][:10],"value":round(eq,3)})
    final=cash+qty*closes[-1]
    open_value=qty*closes[-1] if qty>0 else 0.0
    unrealized=open_value-cost if qty>0 else 0.0
    # The comparison uses price-only buy-and-hold, without any trading cost.
    # It is intentionally labeled differently from the fee-adjusted SMA strategy.
    return {
        "ticker":ticker,"strategy":"SMA20 > SMA50 → LONG, aksi → nakit",
        "open_position":bool(qty>0),"open_value":round(open_value,3),
        "unrealized_pnl":round(unrealized,3),
        "closed_pnl":round(closed_pnl,3),
        "transaction_fees":round(total_fees,4),
        "baseline_fee_included":False,
        "alpha_pp":round((final/100-1)*100-(closes[-1]/closes[49]-1)*100,2),
        "risk_alert":(
            "Bu örnekte %20'nin üzerinde maksimum düşüş var. "
            "Sanal stratejinin canlı işlem için uygunluğu kanıtlanmadı."
            if dd <= -20 else
            "Bu sonuç tek dönemlik tarihsel simülasyondur; gerçek yatırım kararına uygunluk göstermez."
        ),
        "return_pct":round((final/100-1)*100,2),
        "buy_hold_pct":round((closes[-1]/closes[49]-1)*100,2),
        "final_equity":round(final,3),
        "max_drawdown_pct":round(dd,2),
        "closed_trades":closed,"wins":wins,"win_rate":round(wins/closed*100,2) if closed else None,
        "trade_events":trades,
        "start":bars[50]["time"][:10],"end":bars[-1]["time"][:10],
        "curve":curve[::max(1,len(curve)//180)],
        "method":"Yalnızca geçmiş günlük OHLC. Önceki günün SMA20/SMA50 sinyali, sonraki gün açılışında işlem. Stratejiye işlem başına %0,1 komisyon ve %0,05 kayma dahil. Al-tut karşılaştırmasına komisyon/kayma dahil değildir. Vergi, fonlama ve tüm piyasa spreadi dahil değildir. Bu test yapay zekâ ajanlarının tarihsel kararlarını ölçmez; gelecek performansı göstermez."
    }
