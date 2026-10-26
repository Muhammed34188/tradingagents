"""Walk-forward simulation and fair AI evaluation. No trading endpoints.

Methodology: predeclared SMA candidates, causal indicators, cost-adjusted next-open
execution, chronological expanding train + validation + held-out test windows.
Backtest runs are read-only and never use the user's live paper account database.
"""
from bisect import bisect_left
from datetime import datetime, timezone
import math

import lab_terminal as terminal
import lab_risk as risk

CANDIDATES=("sma10_30","sma15_45","sma20_50")
NAMES={"cash":"Nakit (işlemsiz)","buyhold":"Al-tut (maliyetli)",
       "sma10_30":"SMA10/SMA30","sma15_45":"SMA15/SMA45",
       "sma20_50":"SMA20/SMA50"}
WINDOW_TRAIN=120
WINDOW_VALIDATION=50
WINDOW_TEST=50
STEP=50
MAX_FOLDS=4
WARMUP=50
ALL_METHODS=("cash","buyhold")+CANDIDATES

def _score(m):
    # Declared before seeing the test subset. Commission is already in returns.
    return m["return_pct"]-0.20*abs(m["max_drawdown_pct"])

def _validate(bars):
    if len(bars)<WINDOW_TRAIN+WINDOW_VALIDATION+WINDOW_TEST:
        raise ValueError("Walk-forward için en az 220 tamamlanmış günlük bar gerekli.")
    days=[p["time"][:10] for p in bars]
    if days!=sorted(set(days)):
        raise ValueError("Geçmiş günler sıralı ve tekrarsız olmalı.")
    for row in bars:
        if any(not math.isfinite(float(row[k])) or float(row[k])<=0 for k in ("open","close")):
            raise ValueError("Geçersiz OHLC fiyatı.")
    return days

def _decision(closes,i,method):
    if method=="cash":return False
    if method=="buyhold":return True
    lengths={"sma10_30":(10,30),"sma15_45":(15,45),"sma20_50":(20,50)}
    a,b=lengths[method]
    # i-1 last completed close. Today's close is forbidden for execution at i open.
    return sum(closes[i-a:i])/a > sum(closes[i-b:i])/b

def _regime(closes,i):
    # Ex-ante classification based solely on the 20 previously completed days.
    prev_return=closes[i-1]/closes[i-21]-1
    if prev_return>=0.05:return "Yükseliş"
    if prev_return<=-0.05:return "Düşüş"
    return "Yatay"

def simulate(bars,first,last,method):
    if method not in NAMES or first<WARMUP or last>len(bars) or first>=last:
        raise ValueError("Strateji veya test penceresi geçersiz.")
    cash=100.0
    qty=0.0
    entry_cost=0.0
    total_fee=0.0
    events=closed=wins=0
    peak=100.0
    drawdown=0.0
    curve=[]
    regime_days={x:[] for x in ("Yükseliş","Düşüş","Yatay")}
    closes=[float(x["close"]) for x in bars]
    previous_nav=100.0
    for i in range(first,last):
        want=_decision(closes,i,method)
        open_price=float(bars[i]["open"])
        if want and qty==0 and cash>0:
            fill=open_price*(1+terminal.SLIPPAGE)
            notional=cash/(1+terminal.COMMISSION)
            fee=notional*terminal.COMMISSION
            qty=notional/fill
            entry_cost=cash
            total_fee+=fee
            cash=0.0
            events+=1
        elif not want and qty>0:
            fill=open_price*(1-terminal.SLIPPAGE)
            gross=qty*fill
            fee=gross*terminal.COMMISSION
            proceeds=gross-fee
            total_fee+=fee
            wins+=int(proceeds>entry_cost)
            closed+=1
            events+=1
            cash+=proceeds
            qty=0.0
            entry_cost=0.0
        nav=cash+qty*closes[i]
        if not math.isfinite(nav) or nav<0:
            raise ValueError("Geçersiz sanal portföy değeri.")
        daily_return=nav/previous_nav-1 if previous_nav else 0
        regime_days[_regime(closes,i)].append(daily_return)
        previous_nav=nav
        peak=max(peak,nav)
        drawdown=min(drawdown,100*(nav/peak-1))
        curve.append({"date":bars[i]["time"][:10],"equity":round(nav,6)})
    regimes={}
    for name,daily in regime_days.items():
        multiplier=1.0
        for ret in daily:multiplier*=1+ret
        regimes[name]={"days":len(daily),"compounded_return_pct":round((multiplier-1)*100,2),
                       "positive_days":sum(x>0 for x in daily)}
    return {"id":method,"name":NAMES[method],"return_pct":round((previous_nav-100),3),
            "ending_equity":round(previous_nav,5),"max_drawdown_pct":round(drawdown,2),
            "trades":events,"closed_trades":closed,"winning_closed":wins,
            "win_rate_pct":round(100*wins/closed,1) if closed else None,
            "fees_usd":round(total_fee,5),"open_position":qty>0,"curve":curve,
            "regimes":regimes}

def _aggregate(fold_items,method):
    """Stitch contiguous held-out folds; each fold trades from flat cash."""
    capital=100.0
    track=[]
    total_fees=total_events=total_closed=total_wins=0
    regimes={x:{"days":0,"factor":1.0,"positive_days":0} for x in ("Yükseliş","Düşüş","Yatay")}
    for item in fold_items:
        m=item["outcomes"][method]
        multiplier=capital/100
        track.extend({"date":p["date"],"equity":round(p["equity"]*multiplier,5)}
                     for p in m["curve"])
        total_fees+=m["fees_usd"]*multiplier
        total_events+=m["trades"]
        total_closed+=m["closed_trades"]
        total_wins+=m["winning_closed"]
        for key,part in m["regimes"].items():
            regimes[key]["days"]+=part["days"]
            regimes[key]["factor"]*=1+part["compounded_return_pct"]/100
            regimes[key]["positive_days"]+=part["positive_days"]
        capital=m["ending_equity"]*multiplier
    peak=100.0
    worst=0.0
    for point in track:
        peak=max(peak,point["equity"])
        worst=min(worst,100*(point["equity"]/peak-1))
    return {"return_pct":round(capital-100,2),"ending_equity":round(capital,2),
            "max_drawdown_pct":round(worst,2),"fees_usd":round(total_fees,4),
            "trade_events":total_events,"closed_trades":total_closed,
            "closed_win_rate_pct":round(100*total_wins/total_closed,1) if total_closed else None,
            "regimes":[{"label":name,"days":r["days"],
                        "compounded_return_pct":round((r["factor"]-1)*100,2),
                        "positive_days":r["positive_days"]} for name,r in regimes.items()],
            "curve":track[::max(1,len(track)//160)]}

def _ai_match(bars,days,folds,ticker,observations):
    """Same OOS dates & observed outcomes. Fixed SMA baselines are reconstructed
    from historical completed closes, not original timestamped live predictions.
    Thus an exploratory directional benchmark, NOT an executable AI backtest."""
    intervals=[(f["test_start"],f["test_end"]) for f in folds]
    counters={"hold_ignored":0,"missing_baseline":0,"outside_oos":0,
              "unmeasured":0,"late":0}
    out={}
    closes=[float(b["close"]) for b in bars]
    for horizon in ("h24","d7"):
        matched=[]
        for obs in observations:
            if obs.get("ticker")!=ticker: continue
            if obs.get("decision")=="BEKLE":
                counters["hold_ignored"]+=1 if horizon=="h24" else 0
                continue
            if obs.get("decision") not in ("AL","SAT"):continue
            when=obs.get("observed_at")
            initial=obs.get("observed_price")
            if not when or initial is None:
                if horizon=="h24":counters["missing_baseline"]+=1
                continue
            try:
                date=datetime.fromisoformat(when).astimezone(timezone.utc).date().isoformat()
            except (ValueError,TypeError):
                if horizon=="h24":counters["missing_baseline"]+=1
                continue
            # End is EXCLUSIVE; compare on the same actually held-out days.
            if not any(start<=date<end for start,end in intervals):
                if horizon=="h24":counters["outside_oos"]+=1
                continue
            value=obs.get(horizon+"_return_pct")
            if value is None:
                if horizon=="h24":counters["unmeasured"]+=1
                continue
            if not obs.get(horizon+"_timely"):
                if horizon=="h24":counters["late"]+=1
                continue
            i=bisect_left(days,date)
            if i<WARMUP:continue
            actual=1 if value>0 else -1 if value<0 else 0
            if actual==0:continue
            candidate={"AI":1 if obs["decision"]=="AL" else -1,
                       "Al-tut":1,
                       "SMA20/50":1 if _decision(closes,i,"sma20_50") else -1,
                       "SMA10/30":1 if _decision(closes,i,"sma10_30") else -1}
            matched.append({"date":date,"actual":actual,"directions":candidate})
        scores={}
        for name in ("AI","Al-tut","SMA20/50","SMA10/30"):
            total=len(matched)
            correct=sum(item["directions"][name]==item["actual"] for item in matched)
            scores[name]={"n":total,"correct":correct,
                          "accuracy_pct":round(100*correct/total,1) if total else None}
        out[horizon]={"matched":len(matched),"scores":scores,
                      "adequate_sample":len(matched)>=20}
    return {"horizons":out,"exclusions":counters,
            "note":"Yalnızca aynı OOS dönemde fiyatı ve zamanında 24s/7g sonucu kaydedilmiş AL/SAT gözlemleri. SMA yönleri sonradan tamamlanmış tarihsel mumlardan yeniden hesaplanır; bunlar önceden kaydedilmiş gerçek zamanlı tahminler değildir. Yön doğruluğu = işlem getirisi değildir. 20'nin altındaki örneklemlerde sıralama yapılmaz."}

def calculate(bars,ticker,observations=()):
    terminal.valid_ticker(ticker)
    # Incomplete UTC crypto daily candle must never enter historical simulation.
    today=datetime.now(timezone.utc).date().isoformat()
    completed=[b for b in bars if b["time"][:10]<today]
    days=_validate(completed)
    fold_items=[]
    folds=[]
    # Anchor the LAST OOS fold at the latest COMPLETED daily candle.
    # Otherwise recently recorded live AI decisions would be permanently
    # outside a historical OOS window (a moving-window selection bias).
    available=1+(len(completed)-(WINDOW_TRAIN+WINDOW_VALIDATION+WINDOW_TEST))//STEP
    fold_count=min(MAX_FOLDS,available)
    if fold_count<2:
        raise ValueError("En az 270 günlük fiyat olmadan iki OOS katlaması kurulamaz.")
    train_end=len(completed)-WINDOW_VALIDATION-WINDOW_TEST-(fold_count-1)*STEP
    while train_end+WINDOW_VALIDATION+WINDOW_TEST<=len(completed) and len(fold_items)<fold_count:
        val_end=train_end+WINDOW_VALIDATION
        test_end=val_end+WINDOW_TEST
        train_stats={c:simulate(completed,WARMUP,train_end,c) for c in CANDIDATES}
        top_two=sorted(CANDIDATES,key=lambda c:(-_score(train_stats[c]),c))[:2]
        val_stats={c:simulate(completed,train_end,val_end,c) for c in (*top_two,"cash")}
        selected=sorted(val_stats,key=lambda c:(-_score(val_stats[c]),c))[0]
        heldout={c:simulate(completed,val_end,test_end,c)
                 for c in ALL_METHODS}
        heldout["selected"]=heldout[selected]
        fold_items.append({"outcomes":heldout})
        folds.append({
            "fold":len(folds)+1,
            "train_start":days[0],"train_end":days[train_end-1],
            "validation_start":days[train_end],"validation_end":days[val_end-1],
            "test_start":days[val_end],"test_end":days[test_end-1],
            "selected":selected,"selected_label":NAMES[selected],
            "train_top_two":list(top_two),
            "train_metrics":{c:{"return_pct":train_stats[c]["return_pct"],
                                 "max_drawdown_pct":train_stats[c]["max_drawdown_pct"]}
                             for c in top_two},
            "validation_metrics":{c:{"return_pct":val_stats[c]["return_pct"],
                                      "max_drawdown_pct":val_stats[c]["max_drawdown_pct"]}
                                  for c in (*top_two,"cash")},
            "validation_selected_return_pct":val_stats[selected]["return_pct"],
            "test_return_pct":heldout[selected]["return_pct"],
            "test_max_drawdown_pct":heldout[selected]["max_drawdown_pct"],
            "test_trades":heldout[selected]["trades"],
            "test_regimes":heldout[selected]["regimes"]
        })
        train_end+=STEP
    if len(fold_items)<2:
        raise ValueError("Güvenilir değerlendirme için en az 2 ayrı OOS test dönemi gerekiyor.")
    totals={key:_aggregate(fold_items,key) for key in ALL_METHODS+("selected",)}
    selection=[f["selected"] for f in folds]
    overfit={
        "negative_heldout_periods":sum(f["test_return_pct"]<0 for f in folds),
        "validation_positive_test_negative":sum(f["validation_selected_return_pct"]>0 and f["test_return_pct"]<0 for f in folds),
        "distinct_selected_strategies":len(set(selection)),
        "selected_changes":sum(a!=b for a,b in zip(selection,selection[1:])),
        "no_positive_selected_oos":totals["selected"]["return_pct"]<=0,
        "large_selected_drawdown":totals["selected"]["max_drawdown_pct"]<=-20,
    }
    notes=[]
    if overfit["validation_positive_test_negative"]:
        notes.append("Doğrulamada iyi, sonraki testte zararlı katlamalar var: aşırı uyum olasılığı.")
    if overfit["selected_changes"]:
        notes.append("Pencereler arasında seçilen strateji değişiyor: kararlılık düşebilir.")
    if overfit["no_positive_selected_oos"]:
        notes.append("Seçilmiş yaklaşımın birleşik test getirisi pozitif değil.")
    if overfit["large_selected_drawdown"]:
        notes.append("Seçilmiş yaklaşımın örneklem dışı düşüşü %20 veya üzeri.")
    if not notes:notes=["Bu örneklemde bariz uyarı görülmemesi gelecekte başarı anlamına gelmez."]
    # Exclude observation date equal to test end? OOS fold end is inclusive in
    # display; compute exclusive end via next real daily date or next calendar day.
    from datetime import timedelta
    windows=[{"test_start":f["test_start"],
              "test_end":(datetime.fromisoformat(f["test_end"])+timedelta(days=1)).date().isoformat()}
             for f in folds]
    ai=_ai_match(completed,days,windows,ticker,observations)
    return {"ticker":ticker,"status":"EXPLORATORY_ONLY",
            "start":folds[0]["test_start"],"end":folds[-1]["test_end"],
            "folds":folds,"fold_count":len(folds),
            "oos_days":len(folds)*WINDOW_TEST,
            "train_days_initial":WINDOW_TRAIN,"validation_days":WINDOW_VALIDATION,
            "test_days":WINDOW_TEST,"warmup_days":WARMUP,
            "totals":totals,"overfit":overfit,"warnings":notes,"ai_paired":ai,
            "methods":"Sabit 3 SMA adayından eğitimde risk-cezalı getiriye göre 2 aday; doğrulamada bu ikisi ile nakit arasından seçim. Sonraki 50 gün seçimden tamamen ayrı OOS. Bu işlem her 50 günde, yalnızca geçmiş veriye erişerek tekrar edilir. Fold sermayesi dönem başında nakitten yeniden başlar; OOS NAV katlamalar arasında bileşik hesaplanır. İşlemler ertesi gün açılışında; %0,1 komisyon ve %0,05 kayma/işlem. Trend rejimleri yalnızca önceki 20 tamamlanmış günlük getiriden (±%5) ayrılır.",
            "limits":"Geçmiş 1 yıl / yaklaşık 2-4 fold küçük örneklemdir. Parametreler önceden sabitlense de yöntem ve veri seçimi araştırmacı önyargısına açık. Split dışı gelecek veri kullanılmaz, ancak piyasa/veri sağlayıcısı geçmişi revize edebilir. Vergi, likidite, tüm spread, fonlama ve gecikme dahil değil. Gerçek emir üretilmez; otomatik AI stratejisi testi değildir."}

def run(ticker,observations=()):
    terminal.valid_ticker(ticker)
    bars=terminal.get_series(ticker,"1y","1d",ttl=600)
    return calculate(bars,ticker,observations)
