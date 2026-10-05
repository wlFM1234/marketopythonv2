# basemetalsnl.py
# Pulls Fastmarkets base metals prices and injects them into Marketo
# program "My Tokens" (not email content), then schedules a smart campaign
# to send the following day.
#
# Tokens updated (program-scoped, {{my.RowNColumnM}}):
#   Row2Column1..3  ...  Row9Column1..3
#     Column1 = Name
#     Column2 = Last month Avg (previous full calendar month)
#     Column3 = % Change (last month avg vs the month before)
#
# Required env / GitHub secrets:
#   FASTMARKETS_SERVICE_NAME
#   FASTMARKETS_SERVICE_KEY
#   MARKETO_BASE_URL
#   MARKETO_CLIENT_ID
#   MARKETO_CLIENT_SECRET
#   MARKETO_PROGRAM_ID   (mapped from secret BASE_METALS_NL_PROGRAM_ID)
#   MARKETO_SC_ID         (mapped from secret BASE_METALS_NL_SC_ID; 0 = skip scheduling)

import sys
import os
import time
import datetime as dt
import requests
from dotenv import load_dotenv
from marketo_auth import marketo_request, get_valid_mkto_token

sys.stdout.reconfigure(line_buffering=True)

load_dotenv()

# =========================================
# Load configuration
# =========================================
FM_SERVICE_NAME = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY  = os.getenv("FASTMARKETS_SERVICE_KEY", "").strip()

MARKETO_BASE_URL      = os.getenv("MARKETO_BASE_URL", "").strip().rstrip("/")
MARKETO_CLIENT_ID     = os.getenv("MARKETO_CLIENT_ID", "").strip()
MARKETO_CLIENT_SECRET = os.getenv("MARKETO_CLIENT_SECRET", "").strip()
MARKETO_PROGRAM_ID    = int(os.getenv("MARKETO_PROGRAM_ID") or 0)
SMART_CAMPAIGN_ID     = int(os.getenv("MARKETO_SC_ID") or 0)

# Rows start at 2 (Row1 is the static header row in the template).
ROWS = [
    {"row": 2, "symbol": "MB-AL-0004"},
    {"row": 3, "symbol": "MB-AL-0020"},
    {"row": 4, "symbol": "MB-CU-0403"},
    {"row": 5, "symbol": "MB-CU-0002"},
    {"row": 6, "symbol": "MB-ZN-0001"},
    {"row": 7, "symbol": "MB-NI-0002"},
    {"row": 8, "symbol": "MB-PB-0006"},
    {"row": 9, "symbol": "MB-SN-0002"},
]

# =========================================
# Fastmarkets endpoints
# =========================================
FM_AUTH_URL    = "https://auth.fastmarkets.com/connect/token"
FM_INSTR_URL   = "https://api.fastmarkets.com/physical/v2/Instruments"
FM_HISTORY_URL = "https://api.fastmarkets.com/physical/v2/Prices/History"


class FastmarketsAuthError(Exception):
    pass


def fm_request_with_retry(method: str, url: str, *, retries: int = 4, backoff: float = 3.0, **kwargs):
    """requests.request with retry/backoff on transient network errors and 5xx responses."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"{r.status_code} server error", response=r)
            return r
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as exc:
            last_exc = exc
            if attempt == retries:
                break
            wait = backoff * attempt
            print(f"  ⚠ {method} {url} failed ({exc}); retrying in {wait:.0f}s "
                  f"(attempt {attempt}/{retries})...")
            time.sleep(wait)
    raise last_exc


def fm_get_access_token():
    if not FM_SERVICE_NAME or not FM_SERVICE_KEY:
        raise FastmarketsAuthError("Missing FASTMARKETS_SERVICE_NAME/KEY")
    payload = {
        "grant_type": "servicekey",
        "client_id": "service_client",
        "scope": "fastmarkets.physicalprices.api",
        "serviceName": FM_SERVICE_NAME,
        "serviceKey": FM_SERVICE_KEY,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = fm_request_with_retry("POST", FM_AUTH_URL, data=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise FastmarketsAuthError(f"Auth response missing token: {data}")
    return token


def fm_get_instrument(access_token: str, symbol: str):
    """Look up static instrument metadata (display name)."""
    headers = {"Authorization": f"Bearer {access_token}", "cache-control": "no-cache"}
    params = {"symbols": symbol}
    r = fm_request_with_retry("GET", FM_INSTR_URL, headers=headers, params=params, timeout=30)
    if r.status_code == 405:
        r = fm_request_with_retry("POST", FM_INSTR_URL, headers=headers, data=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    if not js.get("instruments"):
        return {}
    inst = js["instruments"][0]
    return {
        "name": inst.get("name") or inst.get("instrumentName") or inst.get("description") or "",
    }


def fm_get_prices_in_range(access_token: str, symbol: str, start: dt.date, end: dt.date):
    headers = {"Authorization": f"Bearer {access_token}", "cache-control": "no-cache"}
    params = {
        "symbols": symbol,
        "fromDate": start.strftime("%Y-%m-%d"),
        "toDate": end.strftime("%Y-%m-%d"),
        "fields": "mid,low,high,currency,assessmentDate,date",
    }
    r = fm_request_with_retry("GET", FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    if r.status_code == 405:
        r = fm_request_with_retry("POST", FM_HISTORY_URL, headers=headers, data=params, timeout=45)
    r.raise_for_status()
    js = r.json()
    insts = js.get("instruments") or []
    if not insts:
        return []
    prices = insts[0].get("prices") or []
    prices.sort(key=lambda p: p.get("date") or p.get("assessmentDate") or "")
    return prices


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


def fm_get_avg_mid_in_range(access_token: str, symbol: str, start: dt.date, end: dt.date):
    prices = fm_get_prices_in_range(access_token, symbol, start, end)
    mids = [m for m in (safe_mid(p) for p in prices) if m is not None]
    return sum(mids) / len(mids) if mids else None


def month_bounds(months_back: int):
    """(first_day, last_day) of the calendar month `months_back` before the current one."""
    first = dt.date.today().replace(day=1)
    for _ in range(months_back):
        first = (first - dt.timedelta(days=1)).replace(day=1)
    next_first = (first + dt.timedelta(days=32)).replace(day=1)
    return first, next_first - dt.timedelta(days=1)


def pct_change(current, previous) -> str:
    if current is None or previous in (None, 0):
        return "—"
    try:
        pct = ((float(current) - float(previous)) / float(previous)) * 100.0
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%"
    except Exception:
        return "—"


# =========================================
# Marketo helpers
# =========================================
def update_program_token(program_id: int, token_name: str, value: str):
    """Delete then recreate a My Token on a program (plain POST upsert silently no-ops)."""
    base = f"{MARKETO_BASE_URL}/rest/asset/v1/folder/{program_id}/tokens"
    # Best-effort delete — failures are intentionally ignored
    requests.post(
        f"{base}/delete.json",
        headers={"Authorization": f"Bearer {get_valid_mkto_token()}"},
        data={"name": token_name, "type": "text", "folderType": "Program"},
        timeout=30,
    )
    return marketo_request(
        "POST", f"{base}.json",
        data={"name": token_name, "value": value, "type": "text", "folderType": "Program"},
        timeout=30,
    )


def schedule_smart_campaign_in(sc_id: int, delay: dt.timedelta):
    """Schedule a smart campaign to run `delay` from now."""
    if not sc_id:
        print("  MARKETO_SC_ID=0, skipping campaign schedule.")
        return
    run_at = (dt.datetime.now(dt.timezone.utc) + delay).strftime("%Y-%m-%dT%H:%M:%S+0000")
    url = f"{MARKETO_BASE_URL}/rest/v1/campaigns/{sc_id}/schedule.json"
    marketo_request("POST", url, json={"input": {"runAt": run_at}}, timeout=30)
    print(f"📅 Campaign {sc_id} scheduled for {run_at}")


# =========================================
# Orchestration
# =========================================
def run():
    missing = [k for k, v in {
        "FASTMARKETS_SERVICE_NAME": FM_SERVICE_NAME,
        "FASTMARKETS_SERVICE_KEY":  FM_SERVICE_KEY,
        "MARKETO_BASE_URL":         MARKETO_BASE_URL,
        "MARKETO_CLIENT_ID":        MARKETO_CLIENT_ID,
        "MARKETO_CLIENT_SECRET":    MARKETO_CLIENT_SECRET,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing env values: {', '.join(missing)}")
    if not MARKETO_PROGRAM_ID:
        raise SystemExit("Missing MARKETO_PROGRAM_ID")

    fm_token = fm_get_access_token()
    last_start, last_end = month_bounds(1)
    prior_start, prior_end = month_bounds(2)
    print(f"Last month: {last_start} to {last_end}; month before: {prior_start} to {prior_end}")

    for item in ROWS:
        r  = item["row"]
        sy = item["symbol"]
        print(f"--- Row {r}: {sy} ---")

        inst = fm_get_instrument(fm_token, sy) or {}
        name = inst.get("name") or sy

        last_avg = fm_get_avg_mid_in_range(fm_token, sy, last_start, last_end)
        prior_avg = fm_get_avg_mid_in_range(fm_token, sy, prior_start, prior_end)

        mapping = {
            f"Row{r}Column1": name,
            f"Row{r}Column2": f"{last_avg:,.2f}" if last_avg is not None else "—",
            f"Row{r}Column3": pct_change(last_avg, prior_avg),
        }

        for token_name, value in mapping.items():
            value = value if (value is not None and str(value).strip()) else "—"
            print(f"  -> {{{{my.{token_name}}}}} = {value}")
            update_program_token(MARKETO_PROGRAM_ID, token_name, value)

    print("--- Scheduling smart campaign ---")
    schedule_smart_campaign_in(SMART_CAMPAIGN_ID, dt.timedelta(days=1))
    print("✅ All done.")


if __name__ == "__main__":
    run()
