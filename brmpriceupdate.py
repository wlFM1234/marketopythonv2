import sys
import os
import time
import datetime as dt
import requests
import pandas as pd
import html
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
import threading

sys.stdout.reconfigure(line_buffering=True)

"""
BRMPricesWithRationales_API_v14
--------------------------------
Adds multi-email support. The script now publishes to one or two Marketo Email assets in a single run.
"""

# =========================================
# Load configuration from .env
# =========================================
load_dotenv()
FM_SERVICE_NAME = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY = os.getenv("FASTMARKETS_SERVICE_KEY", "").strip()
MARKETO_BASE_URL = os.getenv("MARKETO_BASE_URL", "").strip().rstrip("/")
MARKETO_CLIENT_ID = os.getenv("MARKETO_CLIENT_ID", "").strip()
MARKETO_CLIENT_SECRET = os.getenv("MARKETO_CLIENT_SECRET", "").strip()
EMAIL_ID_1 = os.getenv("MARKETO_EMAIL_ID", "").strip()
EMAIL_ID_2 = os.getenv("MARKETO_EMAIL_ID2", "").strip()
if not EMAIL_ID_1:
    raise SystemExit("Missing MARKETO_EMAIL_ID in .env")
TARGET_EMAIL_IDS = []
for _eid in (EMAIL_ID_1, EMAIL_ID_2):
    if _eid:
        try:
            TARGET_EMAIL_IDS.append(int(_eid))
        except ValueError:
            raise SystemExit(f"Invalid email id: {_eid}")
TARGET_EMAIL_IDS = sorted(set(TARGET_EMAIL_IDS))

TTM_ID_1 = os.getenv("TRIPLE_TEXT_ID_1", "ttm_col1").strip()
TTM_ID_2 = os.getenv("TRIPLE_TEXT_ID_2", "ttm_col2").strip()
TTM_ID_3 = os.getenv("TRIPLE_TEXT_ID_3", "ttm_col3").strip()

RATIONALES_SOURCE = os.getenv("RATIONALES_SOURCE", "api").strip().lower()
RATIONALES_XLSX_PATH = os.getenv("RATIONALES_XLSX_PATH", r"C:\\Python\\BRMUpdate\\rationales.xlsx").strip()
FASTM_ADDIN_PROGID = os.getenv("FASTMARKETS_COM_ADDIN_PROGID", "").strip()

TILE_SYMBOLS_HARDCODED = ["MB-LI-0033", "MB-LI-0029", "MB-LI-0012"]

MARKETO_MIN_INTERVAL_SEC = float(os.getenv("MARKETO_MIN_INTERVAL_SEC", "0.23"))

MAX_RETRIES = 8
_sc_ids_raw = os.getenv("MARKETO_SC_IDS", "").strip()
if not _sc_ids_raw:
    raise SystemExit("Missing MARKETO_SC_IDS in .env")
SMART_CAMPAIGN_IDS = [int(x.strip()) for x in _sc_ids_raw.split(",") if x.strip()]

# Symbols to check for today's date before proceeding
DATE_CHECK_SYMBOLS = [
    "MB-LI-0033", "MB-LI-0029", "MB-LI-0040",
    "MB-LI-0036", "MB-LI-0012", "MB-CO-0020"
]

# =========================================
# Fastmarkets endpoints
# =========================================
FM_AUTH_URL = "https://auth.fastmarkets.com/connect/token"
FM_INSTR_URL = "https://api.fastmarkets.com/physical/v2/Instruments"
FM_HISTORY_URL = "https://api.fastmarkets.com/physical/v2/Prices/History"

class FastmarketsAuthError(Exception):
    pass

# ---------- helpers ----------

def fmt_ddMonYY(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        if "T" in date_str:
            d = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        else:
            d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        return d.strftime("%d-%b-%y")
    except Exception:
        return date_str

def range_string_2dp(low, high, sym: str = "") -> str:
    if high is None or low is None:
        return "-"
    pre = sym or ""
    return f"{pre}{float(high):,.2f} - {pre}{float(low):,.2f}"

def safe_mid(row: dict):
    if not row:
        return None
    mid = row.get("mid")
    if mid is not None:
        try:
            return float(mid)
        except Exception:
            return None
    lo, hi = row.get("low"), row.get("high")
    try:
        if lo is not None and hi is not None:
            return (float(lo) + float(hi)) / 2.0
    except Exception:
        return None
    return None

def pct_change(current: float, previous: float) -> str:
    if current is None or previous in (None, 0):
        return "-"
    try:
        pct = ((float(current) - float(previous)) / float(previous)) * 100.0
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%"
    except Exception:
        return "-"

def dollar_change_str(current: float, previous: float, sym: str) -> str:
    if current is None or previous is None:
        return "-"
    try:
        delta = float(current) - float(previous)
        sign = "+" if delta >= 0 else ""
        return f"{sign}{sym}{abs(delta):,.2f}" if sym else f"{sign}{delta:,.2f}"
    except Exception:
        return "-"

SMART_MAP = {
    "\u2019": "'", "\u2018": "'", "\u201C": '"', "\u201D": '"', "\u2013": "-", "\u2014": "-", "\u00A0": " ",
}

def normalize_smart_punctuation(s: str) -> str:
    if s is None:
        return ""
    return "".join(SMART_MAP.get(ch, ch) for ch in str(s))

_CURR = {"USD":"$", "EUR":"€", "GBP":"£", "CNY":"¥", "JPY":"¥", "AUD":"A$", "CAD":"C$"}

def curr_symbol(code: str) -> str:
    return _CURR.get((code or '').upper(), '')

# ---------- Fastmarkets ----------

def fm_get_access_token():
    if not FM_SERVICE_NAME or not FM_SERVICE_KEY:
        raise FastmarketsAuthError("Missing FASTMARKETS_SERVICE_NAME/KEY in .env")
    payload = {
        "grant_type": "servicekey", "client_id": "service_client", "scope": "fastmarkets.physicalprices.api",
        "serviceName": FM_SERVICE_NAME, "serviceKey": FM_SERVICE_KEY,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(FM_AUTH_URL, data=payload, headers=headers, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise FastmarketsAuthError(f"Auth failed: {r.text}")
    return tok

def fm_get_frequency(token: str, symbol: str) -> str:
    headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
    params = {"symbols": symbol}
    r = requests.get(FM_INSTR_URL, headers=headers, params=params, timeout=30)
    if r.status_code == 405:
        r = requests.post(FM_INSTR_URL, headers=headers, data=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    insts = js.get("instruments") or []
    if not insts:
        return "daily"
    inst = insts[0]
    return inst.get("frequency") or inst.get("priceFrequency") or "daily"

def fm_get_prices_in_range(token: str, symbol: str, start_date: dt.date, end_date: dt.date):
    headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
    params = {
        "symbols": symbol,
        "fromDate": start_date.strftime("%Y-%m-%d"),
        "toDate": end_date.strftime("%Y-%m-%d"),
        "fields": "mid,low,high,currency,assessmentDate,date",
    }
    r = requests.get(FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    r.raise_for_status()
    js = r.json()
    insts = js.get("instruments") or []
    if not insts:
        return []
    prices = insts[0].get("prices") or []
    prices.sort(key=lambda p: p.get("date") or p.get("assessmentDate", ""))
    return prices

def fm_get_latest_prices(token: str, symbol: str):
    today = dt.date.today()
    from_date = today - dt.timedelta(days=60)
    prices = fm_get_prices_in_range(token, symbol, from_date, today)
    if not prices:
        return None, None
    return prices[-1], prices[-2] if len(prices) >= 2 else None

def fm_get_mtd_avg_mid(token: str, symbol: str):
    today = dt.date.today()
    start = today.replace(day=1)
    prices = fm_get_prices_in_range(token, symbol, start, today)
    mids = [safe_mid(p) for p in prices if safe_mid(p) is not None]
    if not mids:
        return None, None
    ccy = prices[-1].get("currency") if prices else None
    return sum(mids) / len(mids), ccy

def get_price_date(row) -> dt.date | None:
    raw = row.get("date") or row.get("assessmentDate")
    if not raw:
        return None
    try:
        if "T" in raw:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        return dt.datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None

def check_all_prices_are_today(fm_token: str) -> bool:
    today = dt.date.today()
    for symbol in DATE_CHECK_SYMBOLS:
        latest, _ = fm_get_latest_prices(fm_token, symbol)
        if latest is None:
            print(f"  [{symbol}] No price data found.")
            return False
        price_date = get_price_date(latest)
        if price_date != today:
            print(f"  [{symbol}] Latest price date is {price_date}, not today ({today}).")
            return False
        print(f"  [{symbol}] ✓ Price is today ({price_date}).")
    return True

def build_metrics(latest, prev):
    cur_mid = safe_mid(latest)
    pre_mid = safe_mid(prev)
    ccy = latest.get("currency") or ""
    sym = curr_symbol(ccy)
    rng = range_string_2dp(latest.get("low"), latest.get("high"), sym)
    pct_str = pct_change(cur_mid, pre_mid)
    raw_date = latest.get("date") or latest.get("assessmentDate") or ""
    pub = fmt_ddMonYY(raw_date)
    return cur_mid, pre_mid, rng, pct_str, pub, ccy

def fm_get_latest_rationale(token: str, symbol: str, lookback_days: int = 365) -> str:
    today = dt.date.today()
    from_date = today - dt.timedelta(days=lookback_days)
    headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
    params = {
        "symbols": symbol,
        "fromDate": from_date.strftime("%Y-%m-%d"),
        "toDate": today.strftime("%Y-%m-%d"),
        "fields": "pricingRationale,assessmentDate,date",
    }
    r = requests.get(FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    r.raise_for_status()
    js = r.json()
    insts = js.get("instruments") or []
    if not insts:
        return ""
    prices = insts[0].get("prices") or []
    prices.sort(key=lambda p: p.get("date") or p.get("assessmentDate", ""))
    for p in reversed(prices):
        rat = p.get("pricingRationale") or p.get("PricingRationale") or ""
        if rat and str(rat).strip():
            return str(rat).strip()
    return ""

# ---------- Marketo ----------

_mkto_last_call = 0.0
_mkto_lock = threading.Lock()

def marketo_get_token():
    url = f"{MARKETO_BASE_URL}/identity/oauth/token"
    params = {"grant_type": "client_credentials", "client_id": MARKETO_CLIENT_ID, "client_secret": MARKETO_CLIENT_SECRET}
    res = requests.get(url, params=params, timeout=30)
    res.raise_for_status()
    j = res.json()
    tok = j.get("access_token")
    if not tok:
        raise RuntimeError(f"Marketo auth failed: {j}")
    return tok

def _mkto_throttle():
    global _mkto_last_call
    with _mkto_lock:
        now = time.time()
        wait = MARKETO_MIN_INTERVAL_SEC - (now - _mkto_last_call)
        if wait > 0:
            time.sleep(wait)
        _mkto_last_call = time.time()

def inject_text(token: str, email_id: int, html_id: str, value: str):
    _mkto_throttle()
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504]))
    session.mount("https://", adapter)
    url = f"{MARKETO_BASE_URL}/rest/asset/v1/email/{email_id}/content/{html_id}.json"
    headers = {"Authorization": f"Bearer {token}", "X-HTTP-Method-Override": "PUT"}
    payload = {"type": "Text", "value": normalize_smart_punctuation(value)}
    res = session.post(url, headers=headers, data=payload, timeout=30)
    res.raise_for_status()
    j = res.json()
    if not j.get("success"):
        raise RuntimeError(f"Failed to update {html_id} on email {email_id}: {j}")
    return j

def approve_draft(token: str, email_id: int):
    time.sleep(2)
    url = f"{MARKETO_BASE_URL}/rest/asset/v1/email/{email_id}/approveDraft.json"
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.post(url, headers=headers, timeout=30)
    res.raise_for_status()
    j = res.json()
    if not j.get("success"):
        raise RuntimeError(f"Approval failed for email {email_id}: {j}")
    return j

def schedule_campaign(token: str, campaign_id: int, run_at: dt.datetime):
    url = f"{MARKETO_BASE_URL}/rest/v1/campaigns/{campaign_id}/schedule.json"
    headers = {"Authorization": f"Bearer {token}"}
    run_at_str = run_at.strftime("%Y-%m-%dT%H:%M:%S+0000")
    payload = {"input": {"runAt": run_at_str}}
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    j = res.json()
    if not j.get("success"):
        raise RuntimeError(f"Campaign scheduling failed for {campaign_id}: {j}")
    print(f"📅 Campaign {campaign_id} scheduled for {run_at_str}")
    return j

def marketo_get_email_content_ids(token: str, email_id: int) -> list:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504]))
    session.mount("https://", adapter)
    def fetch(status):
        for attempts in range(1, 6):
            try:
                url = f"{MARKETO_BASE_URL}/rest/asset/v1/email/{email_id}/content.json"
                headers = {"Authorization": f"Bearer {token}"}
                params = {"status": status}
                res = session.get(url, headers=headers, params=params, timeout=30)
                res.raise_for_status()
                js = res.json()
                ids = []
                for item in js.get("result", []):
                    hid = item.get("id") or item.get("htmlId")
                    if hid: ids.append(hid)
                return ids
            except (requests.exceptions.ConnectionError, urllib3.exceptions.ProtocolError):
                if attempts < 5:
                    time.sleep(1.2 * attempts); continue
                raise
    ids = fetch("draft")
    if not ids: ids = fetch("approved")
    return sorted(set(ids))

def resolve_content_id(target_id: str, available_ids: list) -> str | None:
    if target_id in available_ids:
        return target_id
    candidates = [i for i in available_ids if i.startswith(target_id)]
    if candidates:
        candidates.sort(key=lambda s: (len(s), s))
        return candidates[0]
    return None

# ---------- Excel fallback ----------

def read_rationales_from_excel(xlsx_path: str):
    df = pd.read_excel(xlsx_path, engine="openpyxl", header=None)
    def cell(r, c):
        try:
            val = df.iloc[r, c]
            if val is None: return ""
            s = normalize_smart_punctuation(str(val)).replace("\r\n","\n").replace("\r","\n")
            lines = [html.escape(line) for line in s.split("\n")]
            return "<br>".join(lines)
        except Exception:
            return ""
    return cell(2, 1), cell(2, 2), cell(2, 3)

# =========================================
# Orchestration
# =========================================

def run():
    rows = [
        {"row": 1, "symbol": "MB-LI-0033"},
        {"row": 2, "symbol": "MB-LI-0029"},
        {"row": 3, "symbol": "MB-LI-0040"},
        {"row": 4, "symbol": "MB-LI-0036"},
        {"row": 5, "symbol": "MB-LI-0012"},
        {"row": 6, "symbol": "MB-CO-0020"},
        {"row": 7, "symbol": "MB-NI-0247"},
        {"row": 8, "symbol": "MB-GRA-0036"},
        {"row": 9, "symbol": "MB-GRA-0042"},
    ]

    if not all([FM_SERVICE_NAME, FM_SERVICE_KEY, MARKETO_BASE_URL, MARKETO_CLIENT_ID, MARKETO_CLIENT_SECRET]):
        raise RuntimeError("Missing .env values")

    # ── Polling loop ─────────────────────────────────────────────────────────
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n=== Attempt {attempt}/{MAX_RETRIES}: Checking price dates ===")
        fm_tok = fm_get_access_token()

        if check_all_prices_are_today(fm_tok):
            print("✓ All prices are today. Proceeding...")
            break
        else:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Prices still not today after {MAX_RETRIES} attempts. Aborting.")
            print(f"✗ Not all prices are today. Waiting 1 hour before retry...")
            time.sleep(60 * 60)
    # ─────────────────────────────────────────────────────────────────────────

    print("--- Marketo: Authenticate ---")
    mkto_tok = marketo_get_token()

    # ---------- Precompute table data ----------
    table_data = {}
    for item in rows:
        r, sy = item["row"], item["symbol"]
        freq = fm_get_frequency(fm_tok, sy) or "daily"
        latest, prev = fm_get_latest_prices(fm_tok, sy)
        cur_mid = pre_mid = None
        rng = pct_str = pub = ccy = None
        if latest:
            cur_mid, pre_mid, rng, pct_str, pub, ccy = build_metrics(latest, prev)
        sym = curr_symbol(ccy or "")
        delta_str = dollar_change_str(cur_mid, pre_mid, sym)
        mtd_avg, mtd_ccy = fm_get_mtd_avg_mid(fm_tok, sy)
        mtd_sym = curr_symbol((mtd_ccy or ccy) or "")
        mtd_str = f"{mtd_sym}{mtd_avg:,.2f}" if mtd_avg is not None else "-"
        table_data[r] = {
            "symbol": sy,
            "freq": freq,
            "cur_mid": f"{cur_mid:,.2f}" if cur_mid is not None else "-",
            "rng": rng or "-",
            "pre_mid": f"{pre_mid:,.2f}" if pre_mid is not None else "-",
            "delta": delta_str,
            "pub": pub or "-",
            "mtd": mtd_str,
        }

    # ---------- Precompute tile data ----------
    tile_syms = TILE_SYMBOLS_HARDCODED
    tile_data = []
    for sy in tile_syms:
        latest, prev = fm_get_latest_prices(fm_tok, sy)
        if not latest:
            tile_data.append({"code": sy, "metrics": "-", "pub": "-"})
            continue
        _cur_mid, _pre_mid, rng, pct_str, pub, _ccy = build_metrics(latest, prev)
        metrics = f"{rng} | {pct_str}"
        tile_data.append({"code": sy, "metrics": metrics, "pub": pub})

    # ---------- Precompute rationales ----------
    rat_vals = []
    for symb in tile_syms:
        latest, _ = fm_get_latest_prices(fm_tok, symb)
        rat = (latest or {}).get("pricingRationale") or (latest or {}).get("PricingRationale")
        if not (rat and str(rat).strip()):
            rat = fm_get_latest_rationale(fm_tok, symb, lookback_days=365)
        text = normalize_smart_punctuation(rat or "").strip()
        if not text and RATIONALES_SOURCE == "excel":
            try:
                r1, r2, r3 = read_rationales_from_excel(RATIONALES_XLSX_PATH)
                text = {0: r1, 1: r2, 2: r3}[len(rat_vals)]
            except Exception:
                text = ""
        if text and '\n' in text and '<br>' not in text:
            safe = "<br>".join(html.escape(line) for line in text.splitlines())
        else:
            safe = text
        rat_vals.append(safe)

    # ---------- Publish to each email ----------
    for email_id in TARGET_EMAIL_IDS:
        print(f"--- Updating Marketo Email {email_id} ---")
        avail_ids = marketo_get_email_content_ids(mkto_tok, email_id)

        id1 = resolve_content_id(TTM_ID_1, avail_ids)
        id2 = resolve_content_id(TTM_ID_2, avail_ids)
        id3 = resolve_content_id(TTM_ID_3, avail_ids)
        missing = [name for name, val in [("TRIPLE_TEXT_ID_1", id1), ("TRIPLE_TEXT_ID_2", id2), ("TRIPLE_TEXT_ID_3", id3)] if val is None]
        if missing:
            raise SystemExit("Rationale ids not found on Email " + str(email_id) + ": " + ", ".join(missing))

        for r in sorted(table_data.keys()):
            rowv = table_data[r]
            inject_text(mkto_tok, email_id, f"r{r}_c1", rowv["symbol"])
            inject_text(mkto_tok, email_id, f"r{r}_c2", rowv["pub"])
            inject_text(mkto_tok, email_id, f"r{r}_c3", rowv["freq"])
            inject_text(mkto_tok, email_id, f"r{r}_c4", rowv["cur_mid"])
            inject_text(mkto_tok, email_id, f"r{r}_c5", rowv["rng"])
            inject_text(mkto_tok, email_id, f"r{r}_c6", rowv["pre_mid"])
            inject_text(mkto_tok, email_id, f"r{r}_c7", rowv["delta"])
            inject_text(mkto_tok, email_id, f"r{r}_c8", rowv["mtd"])

        for target_id, safe in zip((id1, id2, id3), rat_vals):
            inject_text(mkto_tok, email_id, target_id, safe or "-")

        for idx, t in enumerate(tile_data, start=1):
            inject_text(mkto_tok, email_id, f"tile{idx}_code", t["code"])
            inject_text(mkto_tok, email_id, f"tile{idx}_metrics", t["metrics"])
            inject_text(mkto_tok, email_id, f"tile{idx}_pub", t["pub"])

        print(f"--- Approving draft for Email {email_id} ---")
        approve_draft(mkto_tok, email_id)
        print(f"✅ Email {email_id} approved and live.")

    # ---------- Schedule smart campaigns ----------
    print("--- Scheduling smart campaigns ---")
    run_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    for campaign_id in SMART_CAMPAIGN_IDS:
        schedule_campaign(mkto_tok, campaign_id, run_at)

    print("✅ All done.")

if __name__ == "__main__":
    try:
        _ = range_string_2dp
    except NameError:
        raise SystemExit("Helpers not initialized.")
    run()