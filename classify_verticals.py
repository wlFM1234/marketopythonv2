# classify_verticals.py
# Fetches 1 year of pricing notices and applies refined vertical classification.
# Outputs full categorized list with unmatched/false-positive diagnostics.

import os, sys, json, datetime as dt, requests
from dotenv import load_dotenv

sys.stdout.reconfigure(line_buffering=True)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

FM_SERVICE_NAME = os.getenv("FASTMARKETS_SERVICE_NAME", "").strip()
FM_SERVICE_KEY  = os.getenv("FASTMARKETS_SERVICE_KEY", "").strip()
FM_AUTH_URL     = "https://auth.fastmarkets.com/connect/token"
FM_SEARCH_URL   = "https://api.fastmarkets.com/news/v3/Articles/Search"

# ── Refined vertical definitions ──────────────────────────────────────────────
# Priority order: Carbon > Forest Products > Agriculture > Metals
# Carbon uses specific market terms only (excludes metal-grade "carbon" descriptors)
# Agriculture excludes "grain oriented" (type of steel)

VERTICALS = [
    {
        "name": "Carbon",
        # Only genuine carbon market / clean-energy terms — NOT metal grade descriptors.
        # Avoid short substrings that appear in "Fastmarkets" (e.g. "ets") or common words.
        "include": [
            "carbon credit", "carbon offset", "carbon allowance", "carbon market",
            "carbon permit", "carbon removal", "carbon sequestration",
            # "carbon price" excluded — too generic (metal grades use it); use more specific ones:
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
        # Titles containing these are NOT Carbon (they're metal grades)
        "exclude": [
            "low-carbon steel", "low carbon steel",
            "low-carbon alumin", "low carbon alumin",
            "ferro-chrome", "ferrochrome", "ferro chrome",
            "electrode graphite", "graphite electrode",
        ],
    },
    {
        "name": "Forest Products",
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
        "include": [
            # Oilseeds
            "soy", "soya", "palm", "sunflower", "rapeseed", "canola",
            "vegetable oil", "crude palm oil", "cpo ", "rbd", "olein",
            # Grains — exclude "grain oriented" steel context
            "wheat", "corn ", "maize", "barley", " rice ", "sorghum",
            "parboiled", "japonica", "paddy rice",
            "milling wheat", "feed wheat", "durum",
            "grain import", "grain export", "grain price", "grain market",
            "grain freight", "grain cfr", "grain fob", "grain cif",
            # Sugar / Sweeteners
            "sugar", "sweetener", "fructose", "glucose", "starch",
            # Proteins & feed
            "soybean meal", "soy meal", "rapeseed meal", "sunflower meal",
            "fishmeal", "fish meal", "meat and bone meal", "meat-and-bone",
            "poultry meal", "blood meal", "feather meal", "distiller",
            "ddgs", "amino acid", "lysine", "methionine", "threonine",
            # Fertilisers
            "fertiliser", "fertilizer", "urea", "ammonia", "nitrate",
            "potash", "potassium", "phosphate", "dap ", "map ", "npk ",
            "ammonium sulphate", "ammonium sulfate",
            # Softs & fibres
            "coffee", "cocoa", "cotton", "hemp",
            "rubber", "natural rubber",
            # Hides / leather
            "hides", "hide,", "hide price", "hide market", "hide index",
            "leather", "bovine", "wet-blue",
            # Edible oils misc
            "coconut oil", "palm kernel", "tallow",
            "cooking oil",
            # Bio-based / starch
            "biostarch", "corn starch",
            # Generic agriculture label (for admin/schedule change articles)
            "agriculture price", "agriculture prices",
        ],
        # "grain oriented" is a type of electrical steel — exclude
        "exclude": [
            "grain oriented", "grain-oriented",
        ],
    },
    {
        "name": "Metals",
        # Metals is the catch-all — everything that doesn't match above lands here.
        # We still define keywords so it gets explicit matches too.
        "include": [
            # Base / minor metals
            "steel", "iron ore", "hrc ", "crc ", "hot rolled", "cold rolled",
            "scrap", "billet", "slab ", "bloom", "coil ",
            "aluminium", "aluminum", "alumin",
            "copper", "nickel", "zinc", "lead ", "tin ", "cobalt",
            "lithium", "manganese", "molybdenum", "vanadium", "tungsten",
            "chromium", "chrome", "ferro-", "ferroalloy",
            "stainless steel", "silicon steel",
            "titanium", "magnesium",
            # Precious
            "gold ", "silver ", "platinum", "palladium", "pgm",
            # Battery / EV metals
            "battery raw", "battery metal",
            "spodumene", "lepidolite", "petalite",
            "cathode", "precursor", "black mass",
            # Rare earths
            "rare earth", "neodymium", "praseodymium", "dysprosium",
            "terbium", "cerium", "lanthanum", "europium",
            # Smelting / intermediate
            "concentrate", "matte ", "blister",
            # Industrial minerals (no taxonomy field available)
            "fluorspar", "magnesia", "zircon", "barite", "boric acid", "borax",
            "fused alumina", "silicon carbide", "brown fused", "white fused",
            "calcium carbide", "calcium fluoride",
            # Aluminium raw materials
            "bauxite", "alumina",
            # Iron / steel upstream
            "pig iron", "hot-briquetted iron", "hbi ",
            "coking coal", "metallurgical coal",
            # Tube, pipe, wire
            "tube ", "pipe ", "linepipe", "wire rod", "rebar",
            # Recycling / methodology
            "recycling methodolog", "metals recycling", "metal recycling",
            "ferrous methodolog", "non-ferrous methodolog",
            "scrap methodology", "tem price",
            "west hrc", "west european hrc",
            # Minor/specialty metals
            "bismuth", "indium", "gallium", "germanium", "hafnium",
            "gadolinium", "antimony", "rhenium",
            "graphite flake",
            "bulk alloy",
            "silicon",
            "mhp ", " mhp,", " mhp.",
            "petroleum coke", "petcoke", "pet coke",
            # Generic admin terms that only appear in metals context
            "non-ferrous", "industrial mineral",
            "base metal",
            "raw material import", "raw material export",
        ],
        "exclude": [],
    },
]


def get_token():
    r = requests.post(FM_AUTH_URL, data={
        "grant_type": "servicekey", "client_id": "service_client",
        "scope": "fastmarkets.news.api fastmarkets.search.api",
        "serviceName": FM_SERVICE_NAME, "serviceKey": FM_SERVICE_KEY,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        sys.exit(f"Auth failed: {r.text}")
    print("OK Auth")
    return tok


def search(token, params, size=500):
    headers = {"Authorization": f"Bearer {token}", "cache-control": "no-cache"}
    r = requests.get(FM_SEARCH_URL, headers=headers, params={**params, "Size": size}, timeout=45)
    if r.status_code == 405:
        r = requests.post(FM_SEARCH_URL, headers=headers, data={**params, "Size": size}, timeout=45)
    if not r.ok:
        print(f"  HTTP {r.status_code}: {r.text[:300]}")
        return []
    body = r.json()
    return body.get("articles") or body.get("items") or []


def fetch_year(token):
    from_date = (dt.date.today() - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    print(f"Fetching Topic=Pricing Notice from {from_date}...")
    seen_ids, all_arts = set(), []
    for page in range(1, 5):  # pages 1-4 = up to 2000 results
        batch = search(token, {"FromDate": from_date, "Topic": "Pricing Notice", "Page": page})
        print(f"  Page {page}: {len(batch)} articles")
        if not batch:
            break
        new = 0
        for a in batch:
            aid = a.get("articleId") or a.get("id") or a.get("title", "")
            if aid not in seen_ids:
                seen_ids.add(aid)
                all_arts.append(a)
                new += 1
        print(f"  -> {new} new (total unique: {len(all_arts)})")
        if len(batch) < 500:
            break
    return all_arts


def classify(article, debug=False):
    text = " ".join(filter(None, [
        article.get("title", ""),
        article.get("summary", ""),
    ])).lower()

    for v in VERTICALS:
        matched_kw = next((kw for kw in v["include"] if kw in text), None)
        if not matched_kw:
            continue
        excluded_by = next((ex for ex in v["exclude"] if ex in text), None)
        if excluded_by:
            if debug:
                print(f"    [excluded from {v['name']} by '{excluded_by}']")
            continue
        if debug:
            print(f"    [{v['name']} via '{matched_kw}']")
        return v["name"]
    return None


def main():
    token = get_token()
    articles = fetch_year(token)
    print(f"\nTotal unique articles: {len(articles)}")

    buckets = {"Carbon": [], "Forest Products": [], "Agriculture": [], "Metals": [], "Unmatched": []}
    ag_metals_kw = {"steel", "iron", "copper", "nickel", "zinc", "alumin", "cobalt", "lithium", "tungsten", "silicon", "ferro", "scrap", "billet", "bismuth", "cadmium", "chrome", "stainless", "mhp", "pig iron"}
    for a in articles:
        v = classify(a)
        if v == "Agriculture":
            title_lower = (a.get("title") or "").lower()
            if any(m in title_lower for m in ag_metals_kw):
                print(f"[SUSPECT AGRICULTURE] {a.get('title','')}")
                classify(a, debug=True)
        buckets[v if v else "Unmatched"].append(a)

    # Print summary
    print("\n" + "=" * 70)
    print("CLASSIFICATION SUMMARY")
    print("=" * 70)
    for name, arts in buckets.items():
        print(f"  {name:<20} {len(arts):>4} articles")

    # Print each vertical's titles
    for name, arts in buckets.items():
        print(f"\n{'=' * 70}")
        print(f"{name.upper()} ({len(arts)})")
        print("=" * 70)
        for a in sorted(arts, key=lambda x: x.get("publishedDate", "")):
            date = (a.get("publishedDate") or "")[:10]
            title = a.get("title", "(no title)")
            print(f"  {date}  {title}")


if __name__ == "__main__":
    main()
