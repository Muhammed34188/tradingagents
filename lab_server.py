"""Private, research-only mobile panel for TradingAgents on Railway."""
import base64
import hmac
import json
import os
import re
import subprocess
import threading
from datetime import date, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import lab_terminal
import lab_risk
import sqlite3

PASSWORD = os.environ.get("LAB_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("LAB_PASSWORD must be configured; refusing to expose the panel.")
DATA = Path(os.environ.get("TRADINGAGENTS_RESULTS_DIR", "/home/appuser/.tradingagents/results"))
LOG = Path("/home/appuser/.tradingagents/lab-run.log")
TICKERS = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum"}
PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY",
}
PROVIDER = os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "openai").lower()
LOCK = threading.Lock()
STATE = {"running": False, "status": "Henüz analiz yapılmadı", "ticker": None,
         "date": None, "started": None, "finished": None, "exit_code": None,
         "report_file": None,
         "daily_date": None, "daily_count": 0}
MAX_DAILY_RUNS = 4

PAGE = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingAgents Lab</title>
<style>
:root{color-scheme:dark;font-family:ui-sans-serif,-apple-system,system-ui,Arial,sans-serif;background:#0a1020;color:#edf2ff}
*{box-sizing:border-box}body{margin:0;padding:28px 18px;min-height:100vh}
main{max-width:630px;margin:auto}.top{display:flex;gap:12px;align-items:center;margin-bottom:22px}
.mark{background:#132f30;border:1px solid #1c7260;color:#62ffc4;padding:15px;border-radius:17px;font-size:23px}
h1{font-size:25px;margin:0}.sub{color:#9aa8c5;font-size:13px;margin-top:5px}
.card{background:#111a2c;border:1px solid #293650;border-radius:18px;padding:22px;margin:14px 0}
small{color:#9aa8c5}label{display:block;font-size:13px;margin:18px 0 8px;color:#c8d1ea}
select,input{font-size:16px;width:100%;padding:13px;border:1px solid #34415c;background:#0c1526;color:#fff;border-radius:12px}
button{width:100%;padding:15px;margin-top:22px;border:none;border-radius:12px;background:#56e2aa;color:#081c19;font-weight:800;font-size:16px}
button:disabled{opacity:.45}.tag{background:#183d32;color:#68e7b4;padding:6px 10px;border-radius:10px;font-size:12px}
.st{color:#b7c7e5;font-size:14px;white-space:pre-wrap}.notice{border-left:3px solid #caac67;padding:10px 14px;color:#b8c1d8;background:#1d2434;font-size:13px;line-height:1.65}
a{color:#79e9bc}.footer{font-size:12px;color:#8794b2;margin-top:26px;line-height:1.6}
</style></head><body><main>
<div class="top"><div class="mark">◈</div><div><h1>TradingAgents Lab</h1>
<div class="sub">Çoklu yapay zekâ • Kripto araştırması • Sanal test</div></div></div>
<div class="card" style="border-color:#277c65;background:#12332f"><h2>TradingAgents Terminal</h2>
<p class="st">BTC / ETH gecikmeli grafik, AL / SAT / BEKLE özeti, 100$ sanal portföy ve geçmişe dönük strateji testleri.</p>
<a href="/terminal" style="display:block;background:#55dfaf;color:#063528;text-align:center;padding:13px;border-radius:12px;text-decoration:none;font-weight:800">Terminali aç →</a>
</div>
<div class="card"><span class="tag" id="provider">Servis kontrol ediliyor</span>
<h2 style="font-size:20px;margin:18px 0 4px">Yeni piyasa analizi</h2>
<small>BTC/ETH kararları araştırma amaçlıdır; emir gönderilmez.</small>
<label for="ticker">Varlık</label>
<select id="ticker"><option value="BTC-USD">Bitcoin (BTC-USD)</option><option value="ETH-USD">Ethereum (ETH-USD)</option></select>
<label for="day">Analiz tarihi</label><input type="date" id="day">
<button id="run" disabled>Analizi başlat</button></div>
<div class="card"><h2 style="font-size:18px;margin:0 0 12px">Çalışma durumu</h2>
<div class="st" id="status">Yükleniyor...</div>
<p id="report"></p></div>
<div class="card"><h2 style="font-size:18px;margin:0 0 12px">Kaydedilen analizler</h2>
<div class="st" id="history">Arşiv yükleniyor...</div></div>
<div class="card"><h2 style="font-size:18px;margin:0 0 12px">Sistem tanısı</h2>
<div class="st" id="diagnostics">Durum kontrol ediliyor...</div></div>
<div class="notice">Bu araştırma yazılımı kazanç garantisi vermez. API sağlayıcısı işlem başına ücret alabilir. Günlük en fazla 4 analiz ve aynı anda tek analiz sınırlaması vardır. Gerçek borsa API bağlantısı veya otomatik al-sat özelliği yoktur.</div>
<div class="footer">TauricResearch/TradingAgents açık kaynak projesini kullanır. Bu panel yalnızca özel laboratuvar ortamıdır. Yeni analizler kalıcı Railway diskinde saklanır. Veri güvenliği için ayrıca düzenli yedekleme önerilir.</div>
</main><script>
const el=id=>document.getElementById(id);
el('day').value=new Date().toLocaleDateString('en-CA');
async function poll(){
 try{
 const res=await fetch('/api/status',{cache:'no-store'});
 if(!res.ok) throw Error('Sunucu yanıtı '+res.status);
 const s=await res.json();
 el('provider').textContent=s.provider_ready?'Yapay zekâ bağlantısı hazır':'API anahtarı bekleniyor';
 el('run').disabled=!!s.running||!s.provider_ready||s.daily_remaining===0;
 el('status').textContent=s.status+'\n'+(s.ticker?'Varlık: '+s.ticker+' | Tarih: '+s.date+'\n':'')+
 'Kalan günlük analiz: '+s.daily_remaining;
 el('report').replaceChildren();
 if(s.report_url){let a=document.createElement('a');a.href=s.report_url;a.textContent='Tam analiz raporunu aç';a.target='_blank';a.rel='noopener';el('report').appendChild(a);}
 const dr=await fetch('/api/diagnostics',{cache:'no-store'});
 if(dr.ok) {
   const dg=await dr.json();
   el('diagnostics').textContent=
     (dg.home_correct?'Çalışma klasörü: doğru':'Çalışma klasörü: yapılandırma hatası')+'\n'+
     (dg.storage_writable?'Disk yazma erişimi: hazır':'Disk yazma erişimi: kontrol gerekli')+'\n'+
     (dg.category==='none'?'Son hata: yok':'Önceki hata: '+dg.message);
 }
 const hr=await fetch('/api/history',{cache:'no-store'});
 if(!hr.ok)throw Error('Arşiv yanıtı '+hr.status);
 const history=(await hr.json()).reports || [];
 const container=el('history');container.replaceChildren();
 if(!history.length){container.textContent='Kalıcı diskte henüz kayıtlı rapor yok.';}
 for(const item of history){
   const wrap=document.createElement('div');wrap.style.padding='9px 0';
   const a=document.createElement('a');a.href=item.url;a.target='_blank';a.rel='noopener';
   a.textContent=item.ticker+' • '+item.saved_at;
   wrap.appendChild(a);container.appendChild(wrap);
 }
 }catch(err){el('status').textContent='Bağlantı hatası: '+err.message;}
}
el('run').onclick=async()=>{
 if(!confirm('Yapay zekâ analizi API kullanım maliyeti doğurabilir. Başlatılsın mı?'))return;
 el('run').disabled=true;
 try{
 const res=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json','X-Lab-Action':'1'},
 body:JSON.stringify({ticker:el('ticker').value,date:el('day').value})});
 const body=await res.json();if(!res.ok)alert(body.error||'Başlatılamadı');
 }catch(err){alert('Bağlantı hatası: '+err.message)}
 await poll();
};
poll();setInterval(poll,7000);
</script></body></html>"""

def authorized(handler):
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        payload = base64.b64decode(header[6:], validate=True).decode("utf-8")
        name, password = payload.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(name, "admin") and hmac.compare_digest(password, PASSWORD)

def valid_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return date.fromisoformat(value) <= date.today()
    except ValueError:
        return False

def provider_ready():
    return bool(os.environ.get(PROVIDER_KEYS.get(PROVIDER, "_UNKNOWN_KEY"), "").strip())

def report_path(ticker, day):
    # The upstream CLI writes to results/reports/<ticker>_<timestamp>/complete_report.html.
    # Only expose the file produced by the current run; never a stale report.
    if ticker not in TICKERS or not valid_day(day):
        return None
    with LOCK:
        path = STATE.get("report_file")
        matching = STATE.get("ticker") == ticker and STATE.get("date") == day
    if not matching or not path:
        return None
    candidate = Path(path)
    expected_root = (DATA / "reports").resolve()
    if expected_root not in candidate.resolve().parents:
        return None
    return candidate if candidate.is_file() else None

def newest_report(ticker, since_ns):
    parent = DATA / "reports"
    if not parent.is_dir():
        return None
    matches = [
        p for p in parent.glob(ticker + "_*/complete_report.html")
        if p.is_file() and p.stat().st_mtime_ns >= since_ns - 3_000_000_000
    ]
    return max(matches, key=lambda p: p.stat().st_mtime_ns) if matches else None

REPORT_FOLDER = re.compile(r"^(BTC-USD|ETH-USD)_(\d{8})_(\d{6})$")

def archived_report(folder):
    if not isinstance(folder, str) or not REPORT_FOLDER.fullmatch(folder):
        return None
    path = DATA / "reports" / folder / "complete_report.html"
    root = (DATA / "reports").resolve()
    if root not in path.resolve().parents or not path.is_file() or path.is_symlink():
        return None
    return path

def report_history():
    root = DATA / "reports"
    if not root.is_dir():
        return []
    reports = []
    for folder in root.iterdir():
        match = REPORT_FOLDER.fullmatch(folder.name)
        if not match or not folder.is_dir():
            continue
        file = archived_report(folder.name)
        if not file:
            continue
        try:
            stamp = datetime.strptime(match.group(2) + match.group(3), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        reports.append({
            "id": folder.name,
            "ticker": match.group(1),
            "saved_at": stamp.strftime("%d.%m.%Y %H:%M"),
            "url": "/report?id=" + folder.name,
            "sort_key": stamp.isoformat(),
        })
    reports.sort(key=lambda x: x["sort_key"], reverse=True)
    for item in reports:
        item.pop("sort_key")
    return reports[:40]

def diagnostic_status():
    """Read a safe, categorized summary of the previous run; never expose raw logs."""
    home = os.environ.get("HOME", "")
    writable = os.access(str(DATA), os.W_OK) if DATA.exists() else os.access(str(DATA.parent), os.W_OK)
    result = {"category": "none", "message": "Hata kaydı bulunamadı.",
              "home_correct": home == "/home/appuser", "storage_writable": writable}
    if not LOG.is_file():
        return result
    try:
        tail = LOG.read_text(encoding="utf-8", errors="replace")[-30000:].lower()
    except OSError:
        result.update(category="log_unavailable", message="Hata kaydı okunamadı.")
        return result
    failures = [
        ("permissionerror", "storage_permission", "Dosya yazma izni hatası; disk / HOME ayarları kontrol edilmeli."),
        ("permission denied", "storage_permission", "Dosya veya klasör izni hatası tespit edildi."),
        ("insufficient_quota", "api_quota", "OpenAI API kotası veya ödeme limiti yetersiz."),
        ("ratelimiterror", "api_rate_limit", "OpenAI istek sınırı veya kullanım kotası aşıldı."),
        ("authenticationerror", "api_auth", "OpenAI API kimlik doğrulama hatası."),
        ("invalid_api_key", "api_auth", "OpenAI API anahtarı geçersiz."),
        ("model_not_found", "model_unavailable", "Seçilen OpenAI modeli kullanılamıyor."),
        ("notfounderror", "model_unavailable", "İstenen model veya kaynak bulunamadı."),
        ("badrequesterror", "api_request", "Model API isteğini kabul etmedi; parametreler gözden geçirilmeli."),
        ("no terminal to answer", "missing_configuration", "CLI ortam değişkenlerinde eksik ayar var."),
        ("filenotfounderror", "missing_file", "Bir dosya bulunamadı."),
        ("connectionerror", "network", "Piyasa verisi veya model servisine erişim sorunu."),
        ("traceback (most recent call last)", "unknown_exception", "Python çalışma hatası görüldü; ayrıntılı log geliştirici incelemesi gerektirir."),
    ]
    for needle, kind, message in failures:
        if needle in tail:
            result.update(category=kind, message=message)
            break
    return result

def snapshot():
    with LOCK:
        if STATE["daily_date"] != date.today().isoformat():
            STATE["daily_date"] = date.today().isoformat()
            STATE["daily_count"] = 0
        s = dict(STATE)
    s["daily_remaining"] = max(0, MAX_DAILY_RUNS - s["daily_count"])
    s["provider_ready"] = provider_ready()
    s["report_url"] = (
        "/report?ticker=" + s["ticker"] + "&date=" + s["date"]
        if report_path(s["ticker"], s["date"]) else None
    )
    s.pop("daily_date", None)
    s.pop("daily_count", None)
    s.pop("report_file", None)
    return s

def worker(ticker, day):
    import time
    started_ns = time.time_ns()
    cmd = ["tradingagents", "--ticker", ticker, "--date", day,
           "--analysts", "market,news", "--save", "--no-show", "--html"]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    code = -1
    detail = "İşlem tamamlanamadı."
    try:
        with LOG.open("w", encoding="utf-8") as out:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, timeout=1800,
                                    check=False, env=os.environ.copy())
            code = result.returncode
            created = newest_report(ticker, started_ns) if code == 0 else None
            if code == 0 and created:
                with LOCK:
                    STATE["report_file"] = str(created)
                try:
                    outcome=lab_risk.observe_report(ticker,created.parent,DATA)
                    print("AI signal log: stored="+str(outcome.get("recorded")),flush=True)
                except (ValueError,RuntimeError,OSError,sqlite3.Error) as exc:
                    print("AI signal log unavailable: "+type(exc).__name__,flush=True)
                detail = "Analiz tamamlandı. Tam raporu açabilirsiniz."
            elif code == 0:
                detail = ("Analiz işlemi bitti ancak rapor dosyası oluşmadı. "
                          "Yeniden çalıştırmadan önce Railway kayıtları incelenmeli.")
            else:
                detail = ("Analiz hata ile durdu. API erişimini veya Railway "
                          "analiz kayıtlarını kontrol edin.")
    except subprocess.TimeoutExpired:
        detail = "Analiz 30 dakikalık çalışma sınırını aştı ve sonlandırıldı."
    except Exception:
        detail = "Analiz başlatılırken servis hatası oluştu."
    finally:
        with LOCK:
            STATE.update(running=False, status=detail, exit_code=code,
                         finished=datetime.now().isoformat(timespec="seconds"))

class Handler(BaseHTTPRequestHandler):
    def reply(self, code, content, typ="application/json", extra=None):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(code)
        self.send_header("Content-Type", typ + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json_reply(self, code, value):
        self.reply(code, json.dumps(value, ensure_ascii=False))

    def verify(self):
        if authorized(self):
            return True
        self.json_reply(401, {"error": "Giriş gerekli."}) if False else self.reply(
            401, "Kimlik doğrulama gerekli.", "text/plain",
            {"WWW-Authenticate": 'Basic realm="TradingAgents Lab", charset="UTF-8"'})
        return False

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == "/health":
            return self.json_reply(200, {"ok": True})
        if not self.verify():
            return
        if route.path == "/terminal":
            path = Path(__file__).with_name("lab_terminal.html")
            return self.reply(200, path.read_bytes(), "text/html", {
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
        if route.path in ("/api/risk", "/api/performance", "/api/decisions"):
            try:
                if route.path == "/api/risk":
                    value=lab_risk.risk_dashboard()
                elif route.path == "/api/performance":
                    value=lab_risk.performance()
                else:
                    value=lab_risk.decision_history(DATA)
                return self.json_reply(200,value)
            except (ValueError,OSError,RuntimeError,sqlite3.Error) as exc:
                print("Dashboard GET issue: "+type(exc).__name__,flush=True)
                return self.json_reply(503,{"error":"Kayıtlı veriler şu anda okunamıyor."})
        if route.path in ("/api/market", "/api/portfolio", "/api/ai-signal", "/api/backtest"):
            q = parse_qs(route.query)
            try:
                if route.path == "/api/portfolio":
                    value = lab_terminal.portfolio()
                else:
                    ticker = q.get("ticker", [""])[0]
                    lab_terminal.valid_ticker(ticker)
                    if route.path == "/api/market":
                        value = lab_terminal.market(ticker)
                    elif route.path == "/api/ai-signal":
                        value = lab_terminal.signal(DATA, ticker)
                    else:
                        value = lab_terminal.backtest(ticker)
                return self.json_reply(200, value)
            except ValueError as exc:
                return self.json_reply(422, {"error": str(exc)})
            except (OSError, RuntimeError) as exc:
                print("Terminal GET issue: " + type(exc).__name__, flush=True)
                return self.json_reply(503, {"error": "Veri servisi geçici olarak kullanılamıyor."})
        if route.path == "/":
            return self.reply(200, PAGE, "text/html",
                              {"Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
        if route.path == "/api/status":
            return self.json_reply(200, snapshot())
        if route.path == "/api/history":
            return self.json_reply(200, {"reports": report_history()})
        if route.path == "/api/diagnostics":
            return self.json_reply(200, diagnostic_status())
        if route.path == "/report":
            q = parse_qs(route.query)
            if "id" in q:
                report = archived_report(q.get("id", [""])[0])
            else:
                ticker = q.get("ticker", [""])[0]
                day = q.get("date", [""])[0]
                if ticker not in TICKERS or not valid_day(day):
                    return self.json_reply(400, {"error": "Geçersiz rapor."})
                report = report_path(ticker, day)
            if report is None or not report.is_file():
                return self.json_reply(404, {"error": "Rapor bulunamadı."})
            return self.reply(200, report.read_bytes(), "text/html", {
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'"})
        return self.json_reply(404, {"error": "Bulunamadı"})

    def do_POST(self):
        if not self.verify():
            return
        if urlsplit(self.path).path in ("/api/risk/stop","/api/risk/check"):
            if self.headers.get("X-Lab-Action") != "1" or not self.headers.get("Content-Type","").startswith("application/json"):
                return self.json_reply(403,{"error":"Güvenlik kontrolü başarısız."})
            try:
                size=int(self.headers.get("Content-Length","0"))
                if size<2 or size>512: raise ValueError("İstek boyutu geçersiz.")
                payload=json.loads(self.rfile.read(size))
                if urlsplit(self.path).path == "/api/risk/stop":
                    value=lab_risk.stop_control(payload.get("ticker"),payload.get("pct",5))
                else:
                    value=lab_risk.check_stops()
                return self.json_reply(200,value)
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
                return self.json_reply(422,{"error":str(exc)})
            except (OSError,RuntimeError,sqlite3.Error) as exc:
                print("Risk action error: "+type(exc).__name__,flush=True)
                return self.json_reply(503,{"error":"Sanal risk servisine erişilemiyor."})
        if urlsplit(self.path).path == "/api/paper/order":
            if self.headers.get("X-Lab-Action") != "1" or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.json_reply(403, {"error": "Güvenlik kontrolü başarısız."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 1024:
                    raise ValueError("Geçersiz istek.")
                payload = json.loads(self.rfile.read(length))
                value = lab_terminal.paper_order(payload)
                return self.json_reply(200, value)
            except (TypeError, KeyError, ValueError, json.JSONDecodeError) as exc:
                return self.json_reply(422, {"error": str(exc)})
            except Exception as exc:
                print("Paper order issue: " + type(exc).__name__, flush=True)
                return self.json_reply(503, {"error": "Sanal emir tamamlanamadı; veri erişimini kontrol edin."})
        if urlsplit(self.path).path != "/api/run":
            return self.json_reply(404, {"error": "Bulunamadı"})
        if self.headers.get("X-Lab-Action") != "1" or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.json_reply(403, {"error": "Güvenlik kontrolü başarısız."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2000:
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
            ticker, day = payload["ticker"], payload["date"]
            if ticker not in TICKERS or not valid_day(day):
                raise ValueError()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return self.json_reply(400, {"error": "Geçerli sembol ve tarih seç."})
        if not provider_ready():
            return self.json_reply(409, {"error": "Railway'de sağlayıcı API anahtarı tanımlanmalı."})
        with LOCK:
            today = date.today().isoformat()
            if STATE["daily_date"] != today:
                STATE["daily_date"], STATE["daily_count"] = today, 0
            if STATE["running"]:
                return self.json_reply(409, {"error": "Önceki analiz halen çalışıyor."})
            if STATE["daily_count"] >= MAX_DAILY_RUNS:
                return self.json_reply(429, {"error": "Bugünkü analiz sınırına ulaşıldı."})
            STATE["daily_count"] += 1
            STATE.update(running=True, status="Analistler çalışıyor...", ticker=ticker, date=day,
                         started=datetime.now().isoformat(timespec="seconds"),
                         finished=None, exit_code=None, report_file=None)
        threading.Thread(target=worker, args=(ticker, day), daemon=True).start()
        return self.json_reply(202, {"ok": True})

    def log_message(self, fmt, *args):
        # No request bodies, authorization headers or API keys in server logs.
        pass

if __name__ == "__main__":
    status = diagnostic_status()
    # Startup self-test does not invoke the LLM or incur API costs.
    try:
        check = subprocess.run(
            ["tradingagents", "--help"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=35, check=False,
        )
        cli_ok = (check.returncode == 0)
    except (OSError, subprocess.TimeoutExpired):
        cli_ok = False
    print(
        "TradingAgents startup check: "
        f"cli_ok={cli_ok} home_correct={status['home_correct']} "
        f"storage_writable={status['storage_writable']} "
        f"prior_error_category={status['category']}",
        flush=True,
    )
    port = int(os.environ.get("PORT", "8080"))
    print(f"TradingAgents Lab listening on {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
