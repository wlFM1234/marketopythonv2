# pricingnotices.py
# Pulls today's Pricing Notice articles from the Fastmarkets News API,
# classifies them into four verticals, and injects each vertical's HTML
# block into its own Marketo program My Token.
#
# Tokens updated (same program, hardcoded names):
#   {{my.CarbonNotices}}   {{my.ForestNotices}}
#   {{my.AgsNotices}}      {{my.MetalsNotices}}
#
# Required env / GitHub secrets:
#   FASTMARKETS_SERVICE_NAME
#   FASTMARKETS_SERVICE_KEY
#   MARKETO_BASE_URL
#   MARKETO_CLIENT_ID
#   MARKETO_CLIENT_SECRET
#   MARKETO_PROGRAM_ID
#   MARKETO_SC_ID          (smart campaign to schedule after token updates; 0 = skip)
#   NEWS_LOOKBACK_DAYS     (default 2)

import os
import sys
import datetime as dt
import time
import requests
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from typing import Optional
from marketo_auth import marketo_request, get_valid_mkto_token

sys.stdout.reconfigure(line_buffering=True)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── Shared credentials ────────────────────────────────────────────────────────
FM_SERVICE_NAME       = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY        = os.getenv("FASTMARKETS_SERVICE_KEY", "").strip()
MARKETO_BASE_URL      = os.getenv("MARKETO_BASE_URL", "").strip().rstrip("/")
MARKETO_CLIENT_ID     = os.getenv("MARKETO_CLIENT_ID", "").strip()
MARKETO_CLIENT_SECRET = os.getenv("MARKETO_CLIENT_SECRET", "").strip()
APPROVE_ON_SAVE       = os.getenv("APPROVE_ON_SAVE", "1").strip() == "1"
NEWS_LOOKBACK_DAYS    = int(os.getenv("NEWS_LOOKBACK_DAYS", "2"))
SCHEDULE_HOUR_UK      = int(os.getenv("SCHEDULE_HOUR_UK", "12"))
MARKETO_PROGRAM_ID    = int(os.getenv("MARKETO_PROGRAM_ID", "0"))

# ── Endpoints ─────────────────────────────────────────────────────────────────
FM_AUTH_URL        = "https://auth.fastmarkets.com/connect/token"
FM_NEWS_SEARCH_URL = "https://api.fastmarkets.com/news/v3/Articles/Search"

# ── Exceptions ────────────────────────────────────────────────────────────────
class FastmarketsAuthError(Exception):
    pass


# ═════════════════════════════════════════════════════════════════════════════
# Vertical definitions — priority order: Carbon > Forest Products > Agriculture > Metals
# Each article is assigned to the FIRST vertical whose include terms match and
# whose exclude terms do not.  token_name is the Marketo My Token name (no my. prefix).
# ═════════════════════════════════════════════════════════════════════════════

VERTICAL_DEFS: list[dict] = [
    {
        "name": "Carbon",
        "token_name": "CarbonNotices",
        "sc_id": 20919,
        "include": [
            "carbon credit", "carbon offset", "carbon allowance", "carbon market",
            "carbon permit", "carbon removal", "carbon sequestration",
            "eu allowance", "european allowance", "eu ets", "emissions trading",
            "eua price", "eua spot", "eua future",
            "voluntary carbon", "vcu price",
            "redd+", "redd +",
            " ifm credit", "ifm offset", " ifm assessment", " ifm price",
            "biochar",
            "renewable diesel", "hydrotreated vegetable oil", " hvo price",
            "sustainable aviation fuel", " saf price",
            "biodiesel", "biofuel price",
            "nature-based solution", "nature-based credit",
        ],
        "exclude": [
            "low-carbon steel", "low carbon steel",
            "low-carbon alumin", "low carbon alumin",
            "ferro-chrome", "ferrochrome", "ferro chrome",
            "electrode graphite", "graphite electrode",
        ],
    },
    {
        "name": "Forest Products",
        "token_name": "ForestNotices",
        "sc_id": 20918,
        "include": [
            "pulp", "paper", "kraft", "newsprint", "containerboard",
            "linerboard", "testliner", "fluting", "liner board",
            "corrugated", "tissue", "cartonboard", "board price",
            "lumber", "timber", "softwood", "hardwood", "plywood",
            "oriented strand board", "osb ", " osb,", " osb.",
            "medium density fibreboard", "mdf ", " mdf,",
            "particleboard", "chipboard",
            "log ", "logs ", "roundwood", "spf ", " spf,", "s-p-f",
            "sawn wood", "sawmill", "woodchip", "wood chip", "wood pulp",
            "occ ", " occ,", "old corrugated", "recovered fibre",
            "pix ", "rotogravure", "lbkp", "nbkp", "bskp", "bhkp",
            "dissolving pulp",
            "random lengths",
            "yellow pine", "kiln-dried", "kiln dried",
        ],
        "exclude": [],
    },
    {
        "name": "Agriculture",
        "token_name": "AgsNotices",
        "sc_id": 20917,
        "include": [
            "soy", "soya", "palm", "sunflower", "rapeseed", "canola",
            "vegetable oil", "crude palm oil", "cpo ", "rbd", "olein",
            "wheat", "corn ", "maize", "barley", " rice ", "sorghum",
            "parboiled", "japonica", "paddy rice",
            "milling wheat", "feed wheat", "durum",
            "grain import", "grain export", "grain price", "grain market",
            "grain freight", "grain cfr", "grain fob", "grain cif",
            "sugar", "sweetener", "fructose", "glucose", "starch",
            "soybean meal", "soy meal", "rapeseed meal", "sunflower meal",
            "fishmeal", "fish meal", "meat and bone meal", "meat-and-bone",
            "poultry meal", "blood meal", "feather meal", "distiller",
            "ddgs", "amino acid", "lysine", "methionine", "threonine",
            "fertiliser", "fertilizer", "urea", "ammonia", "nitrate",
            "potash", "potassium", "phosphate", "dap ", "map ", "npk ",
            "ammonium sulphate", "ammonium sulfate",
            "coffee", "cocoa", "cotton", "hemp",
            "rubber", "natural rubber",
            "hides", "hide,", "hide price", "hide market", "hide index",
            "leather", "bovine", "wet-blue",
            "coconut oil", "palm kernel", "tallow",
            "cooking oil",
            "biostarch", "corn starch",
            "agriculture price", "agriculture prices",
        ],
        "exclude": [
            "grain oriented", "grain-oriented",
        ],
    },
    {
        "name": "Metals",
        "token_name": "MetalsNotices",
        "sc_id": 20577,
        # Empty include = catch-all: anything not claimed by a higher-priority vertical lands here.
        "include": [],
        "exclude": [],
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# Date helpers
# ═════════════════════════════════════════════════════════════════════════════

def parse_article_date(published: str) -> Optional[dt.date]:
    if not published:
        return None
    try:
        if "T" in published:
            return dt.datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        return dt.datetime.strptime(published, "%Y-%m-%d").date()
    except Exception:
        return None


def expected_publish_date(today: Optional[dt.date] = None) -> dt.date:
    """Today for weekdays, last Friday for weekends/Monday."""
    if today is None:
        today = dt.date.today()
    wd = today.weekday()  # 0=Mon … 6=Sun
    if wd == 0:           # Monday → Friday
        return today - dt.timedelta(days=3)
    elif wd in (5, 6):    # Weekend → Friday
        return today - dt.timedelta(days=wd - 4)
    return today          # Tue–Fri → today


# ═════════════════════════════════════════════════════════════════════════════
# Fastmarkets News API
# ═════════════════════════════════════════════════════════════════════════════

def fm_get_news_token() -> str:
    if not FM_SERVICE_NAME or not FM_SERVICE_KEY:
        raise FastmarketsAuthError("Missing FASTMARKETS_SERVICE_NAME/KEY in .env")
    payload = {
        "grant_type": "servicekey",
        "client_id":  "service_client",
        "scope":      "fastmarkets.news.api fastmarkets.search.api",
        "serviceName": FM_SERVICE_NAME,
        "serviceKey":  FM_SERVICE_KEY,
    }
    r = requests.post(FM_AUTH_URL, data=payload,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise FastmarketsAuthError(f"FM news auth failed: {r.text}")
    return tok


def _news_search(token: str, params: dict, size: int = 200) -> list[dict]:
    """Single News API search call; handles GET-vs-POST 405 fallback."""
    headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
    p = {**params, "Size": size}
    r = requests.get(FM_NEWS_SEARCH_URL, headers=headers, params=p, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_NEWS_SEARCH_URL, headers=headers, data=p, timeout=45)
    r.raise_for_status()
    return r.json().get("articles") or []


def fetch_todays_pricing_notices(token: str, lookback_days: int = 2) -> list[dict]:
    """
    Pull pricing notices published in the last 24 hours.
    lookback_days controls how far back the API search window goes (keep >= 2).
    Returns articles sorted newest-first, deduplicated by articleId.
    """
    now      = dt.datetime.now(tz=dt.timezone.utc)
    cutoff   = now - dt.timedelta(hours=24)
    from_date = (now - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    print(f"  Fetching pricing notices published since {cutoff.strftime('%Y-%m-%d %H:%M')} UTC")

    # Primary: Topic=Pricing Notice
    try:
        arts = _news_search(token, {
            "FromDate": from_date,
            "Topic":    "Pricing Notice",
        })
        print(f"  Topic=Pricing Notice returned {len(arts)} article(s)")
    except Exception as e:
        print(f"  Topic filter failed ({e}), falling back to title keyword search")
        arts = []

    # Fallback: broad pull then title-filter (less precise — use only if Topic fails)
    if not arts:
        arts = _news_search(token, {"FromDate": from_date}, size=500)
        before = len(arts)
        arts = [
            a for a in arts
            if "pricing notice" in (a.get("title") or "").lower()
            or "price notice"   in (a.get("title") or "").lower()
        ]
        print(f"  Broad search: {before} total -> {len(arts)} after title filter")

    # Keep only articles published in the last 24 hours
    def published_dt(article: dict) -> Optional[dt.datetime]:
        raw = article.get("publishedDate", "")
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None

    arts = [a for a in arts if (pub := published_dt(a)) and pub >= cutoff]
    print(f"  After 24h filter: {len(arts)} article(s)")

    # Deduplicate and sort newest-first
    seen = set()
    deduped = []
    for a in sorted(arts, key=lambda x: x.get("publishedDate") or "", reverse=True):
        aid = a.get("articleId") or a.get("id") or a.get("title")
        if aid not in seen:
            seen.add(aid)
            deduped.append(a)

    return deduped


# ═════════════════════════════════════════════════════════════════════════════
# Vertical matching
# ═════════════════════════════════════════════════════════════════════════════

def article_matches_vertical(article: dict, vertical: dict) -> bool:
    """Returns True if any include keyword matches and no exclude keyword matches."""
    include = vertical.get("include") or vertical.get("keywords", [])
    exclude = vertical.get("exclude", [])
    if not include:
        return True  # catch-all

    text = " ".join(filter(None, [
        article.get("title",   ""),
        article.get("summary", ""),
    ])).lower()

    if not any(kw in text for kw in include):
        return False
    if any(ex in text for ex in exclude):
        return False
    return True


def assign_articles_to_verticals(
    articles: list[dict],
    verticals: list[dict],
) -> dict[str, list[dict]]:
    """
    Assigns each article to the FIRST matching vertical (priority order).
    Returns {vertical_name: [articles]}.
    """
    result = {v["name"]: [] for v in verticals}
    for article in articles:
        assigned = False
        for v in verticals:
            if article_matches_vertical(article, v):
                result[v["name"]].append(article)
                assigned = True
                break
        if not assigned:
            print(f"  [!] No vertical matched: {article.get('title', '(no title)')}")
    return result


# ═════════════════════════════════════════════════════════════════════════════
# HTML rendering
# ═════════════════════════════════════════════════════════════════════════════

import re as _re
from html.parser import HTMLParser as _HTMLParser

def _ascii_html(text: str) -> str:
    return _re.sub(r"[^\x00-\x7F]", lambda m: f"&#{ord(m.group(0))};", text)


class _ContentCleaner(_HTMLParser):
    """Strip all HTML tags from article body except <ul>, <ol>, <li>, <br>.
    Non-ASCII characters in text nodes are converted to numeric HTML entities.
    Existing &entities; and &#refs; are passed through unchanged."""

    _KEEP = {"ul", "ol", "li"}
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, _):
        if tag in self._SKIP:
            self._skip = True
        elif tag == "br":
            self._out.append("<br>")
        elif tag in self._KEEP:
            self._out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = False
        elif tag in self._KEEP:
            self._out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip:
            self._out.append(_ascii_html(data))

    def handle_entityref(self, name):
        self._out.append(f"&{name};")

    def handle_charref(self, name):
        self._out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self._out).strip()


def _clean_content(html: str) -> str:
    p = _ContentCleaner()
    p.feed(html or "")
    return p.result()


def render_article_html(article: dict) -> str:
    """Render a single pricing notice as plain text with bold title."""
    title    = _ascii_html(article.get("title") or "Pricing Notice")
    content  = _clean_content(article.get("content") or article.get("summary") or "")
    pub      = article.get("publishedDate") or ""
    pub_disp = ""
    if pub:
        d = parse_article_date(pub)
        pub_disp = d.strftime("%d/%m/%Y") if d else pub

    pub_line = (
        f'<p style="margin:0 0 8px 0; font-size:12px; color:#666;">{pub_disp}</p>'
        if pub_disp else ""
    )

    return (
        '<div style="margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid #e0e0e0;">'
        f'<p style="margin:0 0 2px 0; font-size:16px; font-weight:bold; line-height:1.3;">{title}</p>'
        f'{pub_line}'
        f'<div style="font-size:14px; line-height:1.6;">{content}</div>'
        '</div>'
    )


def render_vertical_html(_vertical_name: str, articles: list[dict]) -> str:
    """
    Combine all articles for one vertical into a single injectable HTML string.
    Returns a placeholder string if no articles were found.
    """
    if not articles:
        return '<p style="color:#999; font-style:italic; font-size:14px;">No pricing notices found for today.</p>'

    body = "".join(render_article_html(a) for a in articles)
    count_note = (
        f'<p style="font-size:11px; color:#aaa; margin-top:8px;">'
        f'{len(articles)} notice{"s" if len(articles) != 1 else ""} · '
        f'{dt.date.today().strftime("%d/%m/%Y")}</p>'
    )
    return f'<span style="font-size:14px;">{body}{count_note}</span>'


# ═════════════════════════════════════════════════════════════════════════════
# Marketo helpers
# ═════════════════════════════════════════════════════════════════════════════

def update_program_token(program_id: int, token_name: str, value: str):
    """Delete then recreate a My Token on a program (plain POST upsert silently no-ops)."""
    base = f"{MARKETO_BASE_URL}/rest/asset/v1/folder/{program_id}/tokens"
    # Best-effort delete — failures are intentionally ignored
    requests.post(
        f"{base}/delete.json",
        headers={"Authorization": f"Bearer {get_valid_mkto_token()}"},
        data={"name": token_name, "type": "rich text", "folderType": "Program"},
        timeout=30,
    )
    return marketo_request(
        "POST", f"{base}.json",
        data={"name": token_name, "value": value, "type": "rich text", "folderType": "Program"},
        timeout=30,
    )


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


def schedule_smart_campaign_in(sc_id: int, delay_minutes: int = 10):
    """Schedule a smart campaign to run delay_minutes from now."""
    if not sc_id:
        return
    run_at = (
        dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(minutes=delay_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{MARKETO_BASE_URL}/rest/v1/campaigns/{sc_id}/schedule.json"
    marketo_request("POST", url, json={"input": {"runAt": run_at}}, timeout=30)
    print(f"  Campaign {sc_id} scheduled for {run_at} UTC (+{delay_minutes} min)")


def schedule_smart_campaign(sc_id: int):
    """Schedule campaign for SCHEDULE_HOUR_UK:00 UK time today (must be ≥5 min away)."""
    if not sc_id:
        print("   sc_id=0, skipping campaign schedule.")
        return

    uk_tz   = ZoneInfo("Europe/London")
    now_uk  = dt.datetime.now(tz=uk_tz)
    run_uk  = now_uk.replace(hour=SCHEDULE_HOUR_UK, minute=0, second=0, microsecond=0)

    if run_uk <= now_uk + dt.timedelta(minutes=5):
        raise RuntimeError(
            f"Schedule time {run_uk.strftime('%H:%M %Z')} is too close or has passed "
            f"(now: {now_uk.strftime('%H:%M %Z')}). Cannot schedule campaign {sc_id}."
        )

    run_at = run_uk.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{MARKETO_BASE_URL}/rest/v1/campaigns/{sc_id}/schedule.json"
    marketo_request("POST", url, json={"input": {"runAt": run_at}}, timeout=30)
    print(f"   Campaign {sc_id} scheduled for {SCHEDULE_HOUR_UK:02d}:00 UK ({run_at} UTC)")


# ═════════════════════════════════════════════════════════════════════════════
# Orchestration
# ═════════════════════════════════════════════════════════════════════════════

def run():
    # ── Validate env ──────────────────────────────────────────────────────
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

    # ── Fastmarkets: fetch pricing notices ────────────────────────────────
    print("--- Fastmarkets: Auth ---")
    fm_token = fm_get_news_token()

    print(f"--- Fetching Pricing Notices (lookback {NEWS_LOOKBACK_DAYS}d) ---")
    articles = fetch_todays_pricing_notices(fm_token, lookback_days=NEWS_LOOKBACK_DAYS)

    if not articles:
        print("No pricing notices found. Nothing to inject.")
        return

    print(f"Found {len(articles)} notice(s):")
    for a in articles:
        print(f"  {a.get('publishedDate','?')[:16]}  {a.get('title','(no title)')}")

    # ── Classify into verticals ───────────────────────────────────────────
    assignments = assign_articles_to_verticals(articles, VERTICAL_DEFS)
    for v in VERTICAL_DEFS:
        n = len(assignments[v["name"]])
        print(f"  {v['name']}: {n} article(s)")

    # ── Marketo: update all four tokens ──────────────────────────────────
    for v in VERTICAL_DEFS:
        vname      = v["name"]
        token_name = v["token_name"]
        sc_id      = v["sc_id"]
        varts      = assignments[vname]
        html       = render_vertical_html(vname, varts)
        print(f"--- Updating {{{{my.{token_name}}}}} ({len(varts)} article(s)) ---")
        update_program_token(MARKETO_PROGRAM_ID, token_name, html)
        print("  Token updated.")
        if varts and sc_id:
            schedule_smart_campaign_in(sc_id, delay_minutes=10)
            print(f"  Campaign {sc_id} scheduled.")

    print("Done.")


if __name__ == "__main__":
    run()