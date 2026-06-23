import sys
import os
import time
import datetime as dt
import requests
from dotenv import load_dotenv
from marketo_auth import marketo_request

sys.stdout.reconfigure(line_buffering=True)

# =========================================
# Load configuration from .env
# =========================================
load_dotenv()

FM_SERVICE_NAME = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY  = os.getenv("FASTMARKETS_SERVICE_KEY", "").strip()

MARKETO_BASE_URL      = os.getenv("MARKETO_BASE_URL", "").strip()
MARKETO_CLIENT_ID     = os.getenv("MARKETO_CLIENT_ID", "").strip()
MARKETO_CLIENT_SECRET = os.getenv("MARKETO_CLIENT_SECRET", "").strip()
TARGET_EMAIL_ID       = int(os.getenv("MARKETO_EMAIL_ID") or 0)

MAX_RETRIES = 8
SMART_CAMPAIGN_ID     = int(os.getenv("MARKETO_SC_ID") or 0)

# =========================================
# Fastmarkets endpoints
# =========================================
FM_AUTH_URL     = "https://auth.fastmarkets.com/connect/token"
FM_INSTR_URL    = "https://api.fastmarkets.com/physical/v2/Instruments"
FM_HISTORY_URL  = "https://api.fastmarkets.com/physical/v2/Prices/History"

class FastmarketsAuthError(Exception):
    pass

def fm_get_access_token():
    if not FM_SERVICE_NAME or not FM_SERVICE_KEY:
        raise FastmarketsAuthError("Missing FASTMARKETS_SERVICE_NAME/KEY in .env")
    payload = {
        "grant_type": "servicekey",
        "client_id": "service_client",
        "scope": "fastmarkets.physicalprices.api",
        "serviceName": FM_SERVICE_NAME,
        "serviceKey": FM_SERVICE_KEY
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(FM_AUTH_URL, data=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise FastmarketsAuthError(f"Auth response missing token: {data}")
    return token

def fm_get_instrument(access_token: str, symbol: str):
    headers = {"Authorization": f"Bearer {access_token}", "cache-control": "no-cache"}
    params  = {"symbols": symbol}
    r = requests.get(FM_INSTR_URL, headers=headers, params=params, timeout=30)
    if r.status_code == 405:
        r = requests.post(FM_INSTR_URL, headers=headers, data=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    if not js.get("instruments"):
        return {}
    inst = js["instruments"][0]
    return {
        "frequency": inst.get("frequency") or inst.get("priceFrequency") or "",
        "currency": inst.get("currency") or ""
    }

def fm_get_latest_two(access_token: str, symbol: str):
    headers = {"Authorization": f"Bearer {access_token}", "cache-control": "no-cache"}
    today = dt.date.today()
    from_date = (today - dt.timedelta(days=60)).strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")
    params = {
        "symbols": symbol,
        "fromDate": from_date,
        "toDate": to_date,
        "fields": "mid,low,high,currency,assessmentDate,date"
    }
    r = requests.get(FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    r.raise_for_status()
    js = r.json()
    insts = js.get("instruments") or []
    if not insts:
        return None, None
    prices = insts[0].get("prices") or []
    prices.sort(key=lambda p: p.get("date") or p.get("assessmentDate", ""))
    if not prices:
        return None, None
    latest = prices[-1]
    prev   = prices[-2] if len(prices) >= 2 else None
    return latest, prev

def fmt_date_uk(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        if "T" in date_str:
            d = dt.datetime.fromisoformat(date_str.replace("Z","+00:00")).date()
        else:
            d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        return d.strftime("%d/%m/%Y")
    except Exception:
        return date_str

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

def check_all_prices_are_today(fm_token: str, rows: list) -> bool:
    today = dt.date.today()
    for item in rows:
        symbol = item["symbol"]
        latest, _ = fm_get_latest_two(fm_token, symbol)
        if latest is None:
            print(f"  [{symbol}] No price data found.")
            return False
        price_date = get_price_date(latest)
        if price_date != today:
            print(f"  [{symbol}] Latest price date is {price_date}, not today ({today}).")
            return False
        print(f"  [{symbol}] ✓ Price is today ({price_date}).")
    return True

# =========================================
# Marketo helpers
# =========================================
def inject_cell(email_id: int, html_id: str, value: str):
    url = f"{MARKETO_BASE_URL}/rest/asset/v1/email/{email_id}/content/{html_id}.json"
    return marketo_request(
        "POST", url,
        headers={"X-HTTP-Method-Override": "PUT"},
        data={"type": "Text", "value": value},
        timeout=30,
    )

def approve_draft(email_id: int):
    time.sleep(2)
    url = f"{MARKETO_BASE_URL}/rest/asset/v1/email/{email_id}/approveDraft.json"
    return marketo_request("POST", url, timeout=30)

def schedule_campaign(campaign_id: int, run_at: dt.datetime):
    url = f"{MARKETO_BASE_URL}/rest/v1/campaigns/{campaign_id}/schedule.json"
    run_at_str = run_at.strftime("%Y-%m-%dT%H:%M:%S+0000")
    j = marketo_request("POST", url, json={"input": {"runAt": run_at_str}}, timeout=30)
    print(f"📅 Campaign {campaign_id} scheduled for {run_at_str}")
    return j

# =========================================
# Orchestration
# =========================================
def run():
    rows = [
        {"row": 1, "symbol": "AG-SYB-0032"},
        {"row": 2, "symbol": "AG-PLM-0013"},
    ]

    if not all([FM_SERVICE_NAME, FM_SERVICE_KEY, MARKETO_BASE_URL, MARKETO_CLIENT_ID, MARKETO_CLIENT_SECRET]):
        raise RuntimeError("Missing .env values (Fastmarkets and/or Marketo).")

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n=== Attempt {attempt}/{MAX_RETRIES}: Checking price dates ===")
        fm_token = fm_get_access_token()

        if check_all_prices_are_today(fm_token, rows):
            print("✓ All prices are today. Updating email now...")
            break
        else:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Prices still not today after {MAX_RETRIES} attempts. Aborting.")
            print(f"✗ Not all prices are today. Waiting 1 hour before retry...")
            for _ in range(12):  # 12 x 5 mins = 1 hour
                time.sleep(5 * 60)
                print("   ... waiting ...")

    for item in rows:
        r  = item["row"]
        sy = item["symbol"]
        print(f"--- Row {r}: {sy} ---")

        inst = fm_get_instrument(fm_token, sy) or {}
        latest, prev = fm_get_latest_two(fm_token, sy)

        freq = inst.get("frequency") or "Daily"
        currency = inst.get("currency") or "USD"
        assess_date = "—"
        mid_str = "—"
        pct_str = "—"

        if latest is not None:
            assess_date = fmt_date_uk(latest.get("date") or latest.get("assessmentDate"))
            currency = latest.get("currency") or currency

            latest_mid = latest.get("mid")
            if latest_mid is None:
                lo, hi = latest.get("low"), latest.get("high")
                latest_mid = (float(lo) + float(hi)) / 2.0 if (lo is not None and hi is not None) else None

            mid_str = f"{float(latest_mid):,.2f}" if latest_mid is not None else "—"

            if latest_mid is not None and prev:
                prev_mid = prev.get("mid")
                if prev_mid is None and (prev.get("low") is not None and prev.get("high") is not None):
                    prev_mid = (float(prev.get("low")) + float(prev.get("high"))) / 2.0
                if prev_mid:
                    try:
                        pct = ((float(latest_mid) - float(prev_mid)) / float(prev_mid)) * 100.0
                        sign = "+" if pct >= 0 else ""
                        pct_str = f"{sign}{pct:.2f}%"
                    except ZeroDivisionError:
                        pct_str = "—"

        mapping = {
            f"r{r}_c2": sy,
            f"r{r}_c3": freq,
            f"r{r}_c4": assess_date,
            f"r{r}_c5": currency,
            f"r{r}_c6": mid_str,
            f"r{r}_c7": pct_str,
        }

        for html_id, value in mapping.items():
            print(f"  -> {html_id} = {value}")
            inject_cell(TARGET_EMAIL_ID, html_id, value)

    print("--- Approving draft ---")
    approve_draft(TARGET_EMAIL_ID)
    print("✅ Email approved and LIVE.")

    print("--- Scheduling smart campaign ---")
    run_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    schedule_campaign(SMART_CAMPAIGN_ID, run_at)
    print("✅ All done.")

if __name__ == "__main__":
    run()