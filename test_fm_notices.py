# test_fm_notices.py
# Read-only exploration of the Fastmarkets News API for Pricing Notice articles.
# Runs three probes in sequence:
#   1. Broad search (no content filter) — prints raw JSON of first 3 articles so
#      we can see every field the API actually returns.
#   2. Filtered search using ContentType=Pricing Notice.
#   3. Filtered search using Topic=Pricing Notice (the label used on the web UI).
#   4. Fallback title-keyword filter if both typed filters come back empty.
# No Marketo code is touched.

import os
import sys
import json
import datetime as dt
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(line_buffering=True)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

FM_SERVICE_NAME = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY  = os.getenv("FASTMARKETS_SERVICE_KEY",  "").strip()

FM_AUTH_URL    = "https://auth.fastmarkets.com/connect/token"
FM_SEARCH_URL  = "https://api.fastmarkets.com/news/v3/Articles/Search"

LOOKBACK_DAYS  = 7
RAW_SAMPLE     = 3   # articles to dump in the broad probe


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token() -> str:
    if not FM_SERVICE_NAME or not FM_SERVICE_KEY:
        sys.exit("ERROR: FASTMARKETS_SERVICE_NAME / FASTMARKETS_SERVICE_KEY not set in .env")
    r = requests.post(
        FM_AUTH_URL,
        data={
            "grant_type":  "servicekey",
            "client_id":   "service_client",
            "scope":       "fastmarkets.news.api fastmarkets.search.api",
            "serviceName": FM_SERVICE_NAME,
            "serviceKey":  FM_SERVICE_KEY,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        sys.exit(f"Auth failed - no access_token in response:\n{r.text}")
    print("OK Authenticated\n")
    return tok


# ── Search wrapper ─────────────────────────────────────────────────────────────

def search(token: str, extra_params: dict, size: int = 50) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
    params  = {"Size": size, **extra_params}
    r = requests.get(FM_SEARCH_URL, headers=headers, params=params, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_SEARCH_URL, headers=headers, data=params, timeout=45)
    if not r.ok:
        print(f"  HTTP {r.status_code}: {r.text[:400]}")
        return []
    body = r.json()
    return body.get("articles") or body.get("items") or []


# ── Pretty summary of one article ─────────────────────────────────────────────

TAXONOMY_FIELDS = [
    "contentType", "type", "categories", "topics", "tags",
    "commodities", "products", "regions", "authors",
]

def summarise(article: dict, idx: int):
    print(f"  [{idx}] {article.get('title', '(no title)')}")
    print(f"       published : {article.get('publishedDate') or article.get('date', '?')}")
    for field in TAXONOMY_FIELDS:
        val = article.get(field)
        if val is not None:
            print(f"       {field:<14}: {val}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token     = get_token()
    from_date = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    print(f"Search window: {from_date} to today\n")

    # ── Probe 1: broad search, dump first N articles raw ──────────────────────
    sep = "-" * 60
    print(sep)
    print(f"PROBE 1 — broad search (no content filter), first {RAW_SAMPLE} raw articles")
    print(sep)
    broad = search(token, {"FromDate": from_date}, size=10)
    print(f"Returned {len(broad)} article(s) (requested 10)\n")
    if broad:
        print(f"--- Raw JSON of first {min(RAW_SAMPLE, len(broad))} articles ---\n")
        for art in broad[:RAW_SAMPLE]:
            print(json.dumps(art, indent=2, ensure_ascii=False))
            print()

    # ── Probe 2: ContentType filter ────────────────────────────────────────────
    print(sep)
    print("PROBE 2 — ContentType=Pricing Notice")
    print(sep)
    ct_results = search(token, {"FromDate": from_date, "ContentType": "Pricing Notice"})
    print(f"Returned {len(ct_results)} article(s)\n")
    for i, a in enumerate(ct_results, 1):
        summarise(a, i)

    # ── Probe 3: Topic filter (web UI label) ───────────────────────────────────
    print(sep)
    print("PROBE 3 — Topic=Pricing Notice  (web dashboard label)")
    print(sep)
    topic_results = search(token, {"FromDate": from_date, "Topic": "Pricing Notice"})
    print(f"Returned {len(topic_results)} article(s)\n")
    for i, a in enumerate(topic_results, 1):
        summarise(a, i)

    # ── Probe 4: title-keyword fallback ───────────────────────────────────────
    print(sep)
    print("PROBE 4 -- broad search -> title keyword filter ('pricing notice' / 'price notice')")
    print(sep)
    all_arts  = search(token, {"FromDate": from_date}, size=200)
    kw_results = [
        a for a in all_arts
        if "pricing notice" in (a.get("title") or "").lower()
        or "price notice"   in (a.get("title") or "").lower()
    ]
    print(f"Broad returned {len(all_arts)} article(s); {len(kw_results)} matched title filter\n")
    for i, a in enumerate(kw_results, 1):
        summarise(a, i)

    # ── Probe 5: individual article detail — does /Articles/{id} return more fields? ──
    print(sep)
    print("PROBE 5 -- single article fetch (/Articles/{id}) for first Topic result")
    print(sep)
    if topic_results:
        art_id = topic_results[0].get("id")
        headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
        r = requests.get(
            f"https://api.fastmarkets.com/news/v3/Articles/{art_id}",
            headers=headers, timeout=30,
        )
        print(f"  GET /Articles/{art_id}  ->  HTTP {r.status_code}")
        if r.ok:
            detail = r.json()
            print(f"  Keys in detail response: {sorted(detail.keys())}")
            print(f"  Raw JSON:\n{json.dumps(detail, indent=2, ensure_ascii=False)[:3000]}")
        else:
            print(f"  {r.text[:400]}")
    else:
        print("  No Topic results to probe.")
    print()

    # ── Probe 6: Market search parameter ─────────────────────────────────────
    print(sep)
    print("PROBE 6 -- Topic=Pricing Notice + Market=Battery Raw Materials")
    print(sep)
    market_results = search(token, {
        "FromDate": from_date,
        "Topic":    "Pricing Notice",
        "Market":   "Battery Raw Materials",
    })
    print(f"  Returned {len(market_results)} article(s)")
    for i, a in enumerate(market_results, 1):
        summarise(a, i)

    # ── Probe 7: Markets (plural) parameter ───────────────────────────────────
    print(sep)
    print("PROBE 7 -- Topic=Pricing Notice + Markets=Battery Raw Materials")
    print(sep)
    markets_results = search(token, {
        "FromDate": from_date,
        "Topic":    "Pricing Notice",
        "Markets":  "Battery Raw Materials",
    })
    print(f"  Returned {len(markets_results)} article(s)")
    for i, a in enumerate(markets_results, 1):
        summarise(a, i)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(sep)
    print("SUMMARY")
    print(sep)
    counts = {
        "ContentType filter":    len(ct_results),
        "Topic filter":          len(topic_results),
        "Title keyword filter":  len(kw_results),
        "Market param filter":   len(market_results),
        "Markets param filter":  len(markets_results),
    }
    for label, n in counts.items():
        status = "OK" if n else "--"
        print(f"  {status}  {label:<26} {n} article(s)")

    # Show all unique top-level keys seen across all probe results so we know
    # exactly what fields are available for downstream filtering.
    all_seen = broad + ct_results + topic_results + kw_results + market_results + markets_results
    if all_seen:
        all_keys = sorted({k for a in all_seen for k in a})
        print(f"\n  All keys seen across results:\n  {all_keys}")


if __name__ == "__main__":
    main()
