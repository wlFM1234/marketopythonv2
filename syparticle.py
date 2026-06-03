# syp_news_inject.py
# Inject the latest "Southern Yellow Pine Daily Price Update" article into a single mktoText block,
# then schedule the smart campaign — but only if the article was published on the expected date.

import os
import datetime as dt
import requests
from dotenv import load_dotenv
from typing import Optional, Dict
from marketo_auth import marketo_request, get_valid_mkto_token, invalidate_mkto_token

# Load .env from this file's directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# === ENV / CONFIG ===
FM_SERVICE_NAME       = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY        = os.getenv("FASTMARKETS_SERVICE_KEY", "").strip()

MARKETO_BASE_URL      = os.getenv("MARKETO_BASE_URL", "").strip()
MARKETO_CLIENT_ID     = os.getenv("MARKETO_CLIENT_ID", "").strip()
MARKETO_CLIENT_SECRET = os.getenv("MARKETO_CLIENT_SECRET", "").strip()
MARKETO_EMAIL_ID      = int(os.getenv("MARKETO_EMAIL_ID") or 0)
MARKETO_SC_ID         = int(os.getenv("MARKETO_SC_ID", "0"))

NEWS_LOOKBACK_DAYS    = int(os.getenv("NEWS_LOOKBACK_DAYS", "30"))
APPROVE_ON_SAVE       = os.getenv("APPROVE_ON_SAVE", "1").strip() == "1"

# Marketo content target
LAST_RUN_FILE         = os.path.join(os.path.dirname(__file__), ".last_scheduled_run")

# Marketo content target
MKTO_CONTENT_HTML_ID  = "sec103-content"

# === Endpoints ===
FM_AUTH_URL             = "https://auth.fastmarkets.com/connect/token"
FM_NEWS_SEARCH_URL      = "https://api.fastmarkets.com/news/v3/Articles/Search"

MKTO_IDENTITY_URL       = f"{MARKETO_BASE_URL}/identity/oauth/token"
MKTO_UPDATE_CELL_TPL    = f"{MARKETO_BASE_URL}/rest/asset/v1/email" + "/{id}/content/{htmlId}.json"
MKTO_APPROVE_TPL        = f"{MARKETO_BASE_URL}/rest/asset/v1/email" + "/{id}/approveDraft.json"
MKTO_SCHEDULE_SC_TPL    = f"{MARKETO_BASE_URL}/rest/v1/campaigns" + "/{id}/schedule.json"
MKTO_CANCEL_SC_TPL      = f"{MARKETO_BASE_URL}/rest/v1/campaigns" + "/{id}/cancel.json"

# === Exceptions ===
class FastmarketsAuthError(Exception):
    pass

class ArticleNotFreshError(Exception):
    """Raised when the latest article isn't from the expected publish date."""
    pass


# === Date helpers ===
def expected_publish_date(today: Optional[dt.date] = None) -> dt.date:
    """
    Return the date we expect the article to have been published on:
    - Monday  → last Friday (3 days back)
    - Tue–Fri → yesterday
    Weekends are not trading days; if this script somehow runs on one, we also
    look back to the most recent Friday.
    """
    if today is None:
        today = dt.date.today()
    weekday = today.weekday()  # 0=Mon … 6=Sun
    if weekday == 0:       # Monday → Friday
        return today - dt.timedelta(days=3)
    elif weekday in (5, 6): # Saturday/Sunday → Friday
        days_back = weekday - 4  # Sat→1, Sun→2
        return today - dt.timedelta(days=days_back)
    else:                  # Tue–Fri → yesterday
        return today - dt.timedelta(days=1)


def parse_article_date(published: str) -> Optional[dt.date]:
    """Parse ISO-8601 publishedDate from the Fastmarkets API into a date."""
    if not published:
        return None
    try:
        if "T" in published:
            return dt.datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        return dt.datetime.strptime(published, "%Y-%m-%d").date()
    except Exception:
        return None


def article_is_fresh(article: Dict) -> bool:
    """Return True if the article's publish date matches the expected date."""
    pub_date = parse_article_date(article.get("publishedDate", ""))
    if pub_date is None:
        return False
    expected = expected_publish_date()
    return pub_date == expected


# === Fastmarkets helpers ===
def fm_get_access_token_news() -> str:
    if not FM_SERVICE_NAME or not FM_SERVICE_KEY:
        raise FastmarketsAuthError("Missing FASTMARKETS_SERVICE_NAME/KEY")
    payload = {
        "grant_type": "servicekey",
        "client_id": "service_client",
        "scope": "fastmarkets.news.api fastmarkets.search.api",
        "serviceName": FM_SERVICE_NAME,
        "serviceKey": FM_SERVICE_KEY,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(FM_AUTH_URL, data=payload, headers=headers, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise FastmarketsAuthError(f"News auth failed: {r.text}")
    return tok


def news_search_latest_syp_update(news_token: str, lookback_days: int = 30) -> Optional[Dict]:
    today = dt.date.today()
    from_date = (today - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    headers = {"Authorization": f"Bearer {news_token}", "cache-control": "no-cache"}
    params = {"FromDate": from_date, "Product": "Southern Yellow Pine", "Size": 50}
    r = requests.get(FM_NEWS_SEARCH_URL, headers=headers, params=params, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_NEWS_SEARCH_URL, headers=headers, data=params, timeout=45)
    r.raise_for_status()
    arts = r.json().get("articles") or []

    def _dt(a): return a.get("publishedDate") or ""
    arts.sort(key=_dt, reverse=True)

    TITLE_KEYS = [
        "Southern Yellow Pine Daily Price Update",
        "SYP Daily Price Update",
        "Southern Yellow Pine Daily Update",
    ]
    def _match(title: str) -> bool:
        t = (title or "").lower()
        return any(k.lower() in t for k in TITLE_KEYS)

    for a in arts:
        if _match(a.get("title", "")):
            return a
    for a in arts:
        if "southern yellow pine" in a.get("title", "").lower():
            return a

    # Widen by commodity
    params2 = {"FromDate": from_date, "Commodity": "Lumber/Sawn Timber", "Size": 50}
    r2 = requests.get(FM_NEWS_SEARCH_URL, headers=headers, params=params2, timeout=45)
    if r2.status_code == 405:
        r2 = requests.post(FM_NEWS_SEARCH_URL, headers=headers, data=params2, timeout=45)
    r2.raise_for_status()
    arts2 = r2.json().get("articles") or []
    arts2.sort(key=_dt, reverse=True)
    for a in arts2:
        if _match(a.get("title", "")) or "southern yellow pine" in a.get("title", "").lower():
            return a

    return None


def wrap_news_html(article: Dict) -> str:
    heading = '<span style="text-decoration: underline; font-size: 20px;"><strong>Southern Yellow Pine Daily Update</strong></span>'
    title = (article or {}).get("title") or "Southern Yellow Pine Daily Price Update"
    content_html = (article or {}).get("content") or ""
    pub = (article or {}).get("publishedDate") or ""
    pub_disp = ""
    if pub:
        try:
            d = parse_article_date(pub)
            pub_disp = d.strftime("%d/%m/%Y") if d else pub
        except Exception:
            pub_disp = pub

    meta = f'<div style="font-size:12px; color:#666;"><strong>{title}</strong>'
    if pub_disp:
        meta += f' &middot; {pub_disp}'
    meta += '</div>'

    return f'<span style="font-size:14px;">{heading}<br /><br />{meta}{content_html}</span>'


# === Marketo helpers ===
def inject_cell(email_id: int, html_id: str, value: str):
    url = MKTO_UPDATE_CELL_TPL.format(id=email_id, htmlId=html_id)
    return marketo_request(
        "POST", url,
        headers={"X-HTTP-Method-Override": "PUT"},
        data={"type": "Text", "value": value},
        timeout=30,
    )


def approve_draft(email_id: int):
    url = MKTO_APPROVE_TPL.format(id=email_id)
    return marketo_request("POST", url, timeout=30)


def load_last_scheduled_run() -> Optional[str]:
    """Return the runAt string saved from the previous schedule, or None."""
    try:
        with open(LAST_RUN_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_scheduled_run(run_at: str):
    """Persist the runAt string so the next execution can cancel it."""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(run_at)


def cancel_if_scheduled(sc_id: int):
    """
    Cancel any previously scheduled run using the runAt we saved locally.
    If there's no saved run, or Marketo says it's already gone (609/610/709),
    we treat that as a no-op.
    """
    run_at = load_last_scheduled_run()
    if not run_at:
        print("   No previously saved scheduled run — nothing to cancel.")
        return

    print(f"   Attempting to cancel previously scheduled run at {run_at} ...")
    url = MKTO_CANCEL_SC_TPL.format(id=sc_id)
    body = {"input": {"runAt": run_at}}
    for attempt in range(2):
        headers = {
            "Authorization": f"Bearer {get_valid_mkto_token()}",
            "Content-Type": "application/json",
        }
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("success"):
            print(f"   Existing scheduled run at {run_at} cancelled.")
            return
        codes = [e.get("code") for e in (j.get("errors") or [])]
        if set(codes) & {"601", "602"} and attempt == 0:
            invalidate_mkto_token()
            continue
        if set(codes).issubset({"609", "610", "709"}):
            print(f"   Run at {run_at} was already gone — nothing to cancel.")
            return
        raise RuntimeError(f"Failed to cancel smart campaign {sc_id}: {j}")


def cancel_smart_campaign(sc_id: int):
    """Cancel a scheduled smart campaign run."""
    url = MKTO_CANCEL_SC_TPL.format(id=sc_id)
    j = marketo_request("POST", url, timeout=30)
    print(f"   Smart campaign {sc_id} cancelled successfully.")
    return j


def schedule_smart_campaign(sc_id: int):
    """
    Schedule a Marketo smart campaign to run at 12:00 UK time today.
    UK time is Europe/London (handles GMT/BST automatically).
    Marketo requires runAt to be at least 5 minutes in the future.
    """
    if not sc_id:
        raise RuntimeError("MARKETO_SC_ID is not set in .env")

    from zoneinfo import ZoneInfo

    uk_tz = ZoneInfo("Europe/London")
    now_uk = dt.datetime.now(tz=uk_tz)
    run_at_uk = now_uk.replace(hour=12, minute=0, second=0, microsecond=0)

    if run_at_uk <= now_uk + dt.timedelta(minutes=5):
        raise RuntimeError(
            f"12:00 UK time ({run_at_uk.strftime('%H:%M %Z')}) is too close or has already passed "
            f"(current UK time: {now_uk.strftime('%H:%M %Z')}). Cannot schedule."
        )

    run_at = run_at_uk.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = MKTO_SCHEDULE_SC_TPL.format(id=sc_id)
    marketo_request("POST", url, json={"input": {"runAt": run_at}}, timeout=30)
    save_last_scheduled_run(run_at)
    print(f"   Smart campaign {sc_id} scheduled for 12:00 UK ({run_at} UTC)")


# === Main ===
def run():
    need = {
        "FASTMARKETS_SERVICE_NAME": FM_SERVICE_NAME,
        "FASTMARKETS_SERVICE_KEY": FM_SERVICE_KEY,
        "MARKETO_BASE_URL": MARKETO_BASE_URL,
        "MARKETO_CLIENT_ID": MARKETO_CLIENT_ID,
        "MARKETO_CLIENT_SECRET": MARKETO_CLIENT_SECRET,
    }
    missing = [k for k, v in need.items() if not v]
    if missing:
        raise RuntimeError(f"Missing .env values: {', '.join(missing)}")

    print("--- Fastmarkets: Auth (News scopes) ---")
    news_token = fm_get_access_token_news()

    print(f"--- News: Search latest SYP Daily Update (lookback={NEWS_LOOKBACK_DAYS}d) ---")
    article = news_search_latest_syp_update(news_token, lookback_days=NEWS_LOOKBACK_DAYS)
    if not article:
        raise RuntimeError("No recent SYP Daily Price Update article found.")

    pub = article.get("publishedDate", "(unknown)")
    title = article.get("title", "(no title)")
    print(f"Found article: {title} | published {pub}")

    # === Date freshness check ===
    expected = expected_publish_date()
    print(f"--- Date check: expected publish date = {expected} ---")

    if not article_is_fresh(article):
        article_date = parse_article_date(pub)
        print(
            f"⚠️  Article date ({article_date}) does not match expected ({expected}). "
            "No content will be injected and no campaign will be scheduled."
        )
        return  # Exit cleanly — nothing to do

    print("✅ Article is fresh. Proceeding with inject + schedule.")

    html_to_inject = wrap_news_html(article)

    print(f"-> Injecting into {MKTO_CONTENT_HTML_ID}")
    inject_cell(MARKETO_EMAIL_ID, MKTO_CONTENT_HTML_ID, html_to_inject)

    if APPROVE_ON_SAVE:
        print("--- Approving draft ---")
        approve_draft(MARKETO_EMAIL_ID)

    print(f"--- Checking/cancelling any existing scheduled run (ID={MARKETO_SC_ID}) ---")
    cancel_if_scheduled(MARKETO_SC_ID)

    print(f"--- Scheduling smart campaign (ID={MARKETO_SC_ID}) ---")
    schedule_smart_campaign(MARKETO_SC_ID)

    print("✅ Done.")


if __name__ == "__main__":
    run()