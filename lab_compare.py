"""Fixed, non-optimized and reproducible historical comparison.

All methods share the same bars and the same first eligible execution date,
including the flat-cash and cost-adjusted buy-and-hold benchmarks.
AI agent decisions are NOT simulated by these strategies.
"""
import math
import lab_terminal as terminal

START_CASH=100.0
LOOKBACK=50
PLANS=(
    ("cash","Nakit (işlemsiz)",None,None),
    ("buyhold","Al-tut (maliyetli)",None,None),
    ("sma20_50","SMA20 / SMA50",20,50),
    ("sma10_30","SMA10 / SMA30",10,30),
)
MIN_BARS=65

def _round(value, places=2):
    return round(float(value),places)

def calculate(bars,ticker):
    terminal.valid_ticker(ticker)
    if len(bars)<MIN_BARS:
        raise ValueError("Karşılaştırma için en az 65 günlük fiyat verisi gerekli.")
    # Reject invalid/out-of-order rows; monotonic ordering matters for causal tests.
    dates=[p["time"][:10] for p in bars]
    if len(set(dates))!=len(dates) or dates!=sorted(dates):
        raise ValueError("Günlük fiyat barları sıralı ve tekrarsız olmalı.")
    for p in bars:
        if not all(math.isfinite(float(p[k])) and float(p[k])>0 for k in ("open","close")):
            raise ValueError("Geçersiz günlük OHLC değeri.")

    closes=[float(p["close"]) for p in bars]
    results=[]
    for code,label,fast,slow in PLANS:
        cash=START_CASH
        qty=0.0
        entry_cost=0.0
        max_nav=START_CASH
        max_dd=0.0
        total_fees=0.0
        trades=0
        closed=0
        winners=0
        history=[]
        for i in range(LOOKBACK,len(bars)):
            opening=float(bars[i]["open"])
            if code=="cash":
                wants_long=False
            elif code=="buyhold":
                wants_long=True
            else:
                # Crucially: prices stop at i-1; never trade using today's close.
                prev_fast=sum(closes[i-fast:i])/fast
                prev_slow=sum(closes[i-slow:i])/slow
                wants_long=prev_fast>prev_slow
            if wants_long and qty<=0 and cash>0:
                fill=opening*(1+terminal.SLIPPAGE)
                notional=cash/(1+terminal.COMMISSION)
                fee=notional*terminal.COMMISSION
                qty=notional/fill
                entry_cost=cash
                cash=0.0
                total_fees+=fee
                trades+=1
            elif not wants_long and qty>0:
                fill=opening*(1-terminal.SLIPPAGE)
                gross=qty*fill
                fee=gross*terminal.COMMISSION
                proceeds=gross-fee
                total_fees+=fee
                if proceeds>entry_cost:
                    winners+=1
                closed+=1
                trades+=1
                cash+=proceeds
                qty=0.0
                entry_cost=0.0
            nav=cash+qty*closes[i]
            max_nav=max(max_nav,nav)
            max_dd=min(max_dd,100*(nav/max_nav-1))
            history.append({"date":dates[i],"equity":_round(nav,4)})
        final=history[-1]["equity"]
        results.append({
            "id":code,"name":label,"return_pct":_round((final/START_CASH-1)*100),
            "ending_equity":_round(final,4),
            "max_drawdown_pct":_round(max_dd),
            "trades":trades,"closed_trades":closed,"winning_closed":winners,
            "win_rate_pct":_round(100*winners/closed) if closed else None,
            "fees_usd":_round(total_fees,4),
            "open_position":bool(qty>0),
            "curve":history[::max(1,len(history)//160)]
        })
    benchmark=next(r for r in results if r["id"]=="buyhold")
    for row in results:
        row["vs_buyhold_pp"]=_round(row["return_pct"]-benchmark["return_pct"])
    return {
        "ticker":ticker,"price_source":"Yahoo Finance (gecikmeli günlük OHLC)",
        "data_start":dates[0],"comparison_start":dates[LOOKBACK],
        "comparison_end":dates[-1],"observations":len(bars)-LOOKBACK,
        "warmup_bars":LOOKBACK,"starting_usd":START_CASH,
        "models":results,
        "method":"Aynı BTC/ETH günlük veri serisi ve aynı başlangıç günü. Her günün AL/SAT kararı yalnızca ÖNCEKİ günün kapanmış fiyatlarından; sanal emir ertesi günün açılışında. Al-tut dahil alışta %0,1 komisyon + %0,05 kayma, satışta aynı maliyet. Nakit işlemsizdir. Açık pozisyon dönem sonunda kapanmaz. Net getiriler son kapanış fiyatından markalanır; vergi, fonlama, spreadin tamamı yok.",
        "limitations":"Tek sabit 1 yıllık dönem, örneklem dışı doğrulama değil. Bunlar TradingAgents AI ajanlarının kararlarının geriye dönük testi DEĞİLDİR. En yüksek tarihsel getiri gelecek başarı veya uygulanabilir bir işlem stratejisi anlamına gelmez.",
    }

def compare(ticker):
    terminal.valid_ticker(ticker)
    bars=terminal.get_series(ticker,"1y","1d",ttl=600)
    return calculate(bars,ticker)
