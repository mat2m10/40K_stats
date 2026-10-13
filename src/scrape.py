"""
Wahapedia 40k (10th edition) datasheet scraper.

Usage:
    python src/scrape.py                          # all factions
    python src/scrape.py tyranids necrons         # selected faction slugs
    python src/scrape.py --out data/raw_scraped_waha --delay 2 --retries 4

Writes one JSON file per faction (list of units) to --out (default:
<repo>/data/raw_scraped_waha, created if missing). Requests are sequential,
with a polite delay between factions. Failed requests are retried with
exponential backoff on network errors and on HTTP 429 / 5xx (honouring
Retry-After); other statuses are not retried.
"""

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

FACTIONS = {
    "space-marines":        "Space Marines",
    "necrons":              "Necrons",
    "chaos-space-marines":  "Chaos Space Marines",
    "tyranids":             "Tyranids",
    "orks":                 "Orks",
    "aeldari":              "Aeldari",
    "t-au-empire":          "T'au Empire",
    "astra-militarum":      "Astra Militarum",
    "death-guard":          "Death Guard",
    "thousand-sons":        "Thousand Sons",
    "world-eaters":         "World Eaters",
    "genestealer-cults":    "Genestealer Cults",
    "drukhari":             "Drukhari",
    "adeptus-mechanicus":   "Adeptus Mechanicus",
    "grey-knights":         "Grey Knights",
    "adepta-sororitas":     "Adepta Sororitas",
    "imperial-knights":     "Imperial Knights",
    "chaos-knights":        "Chaos Knights",
    "leagues-of-votann":    "Leagues of Votann",
    "adeptus-custodes":     "Adeptus Custodes",
    "chaos-daemons":        "Chaos Daemons",
    "imperial-agents":      "Imperial Agents",
}

BASE_URL = "https://wahapedia.ru/wh40k10ed/factions/{slug}/datasheets.html"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "raw_scraped_waha"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# Datasheet pages are 2-3 MB and can take well over a minute to download.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=300)
RETRY_STATUSES = {429, 500, 502, 503, 504}

# ── Parsing (moved unchanged from notebooks/01_scrape_data.ipynb) ─────────────

def clean(text):
    return re.sub(r"\s+", " ", text).strip()

def to_snake(text):
    text = re.sub(r"[''']", "", text)          # drop apostrophes
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text) # non-alphanum → underscore
    return text.strip("_").lower()
    
def parse_stat_block(ds):
    stats = {}
    for wrap in ds.find_all("div", class_="dsCharWrap"):
        name_div  = wrap.find("div", class_="dsCharName")
        value_div = wrap.find("div", class_="dsCharValue")
        if name_div and value_div:
            label = clean(name_div.get_text())
            value = clean(value_div.get_text())
            if label:
                stats[label] = value
    return stats

def parse_weapons(ds):
    weapons = []
    for table in ds.find_all("table", class_="wTable"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [clean(td.get_text()) for td in row.find_all("td")]
            cells = [c for c in cells if c]  # drop empty
            if len(cells) >= 6 and cells[0] not in ("", "MELEE WEAPONS", "RANGED WEAPONS"):
                weapons.append({
                    "name":      cells[0],
                    "range":     cells[1],
                    "A":         cells[2],
                    "BS_WS":     cells[3],
                    "S":         cells[4],
                    "AP":        cells[5],
                    "D":         cells[6] if len(cells) > 6 else "",
                    "abilities": cells[7] if len(cells) > 7 else "",
                })
    return weapons

def parse_abilities(ds):
    abilities = []
    right_col = ds.find("div", class_=lambda c: c and "dsRightСol" in c)
    if not right_col:
        return abilities
    for ab in right_col.find_all("div", class_="dsAbility"):
        b_tag = ab.find("b")
        if b_tag and b_tag.get_text().strip().endswith(":"):
            name = clean(b_tag.get_text()).rstrip(":")
            b_tag.decompose()
            desc = clean(ab.get_text())
            abilities.append({"name": name, "description": desc})
    return abilities

def parse_keywords(ds):
    kw_div = ds.find("div", class_="ds2colKW")
    if not kw_div:
        return []
    return [clean(span.get_text()) for span in kw_div.find_all("span", class_="kwb")]


def parse_datasheet(ds):
    name_tag = ds.find("div", class_="dsH2Header")
    name = clean(name_tag.find("div").get_text()) if name_tag else "Unknown"
    return {
        "name":      name,
        "id":        to_snake(name),
        "stats":     parse_stat_block(ds),
        "weapons":   parse_weapons(ds),
        "abilities": parse_abilities(ds),
        "keywords":  parse_keywords(ds),
    }


def parse_page(html):
    """All datasheets on a faction page."""
    soup = BeautifulSoup(html, "lxml")
    datasheets = soup.find_all("div", class_=re.compile(r"\bdatasheet\b", re.I))
    return [parse_datasheet(ds) for ds in datasheets]

# ── Fetching ──────────────────────────────────────────────────────────────────

def _retry_delay(attempt, backoff, retry_after=None):
    """Seconds to wait before the next attempt (attempt is 1-based)."""
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass  # HTTP-date form: fall back to exponential backoff
    return backoff * 2 ** (attempt - 1) + random.uniform(0, 1)

async def fetch(session, slug, name, retries=4, backoff=2.0, sleep=asyncio.sleep):
    """GET one faction page. Returns (slug, name, status, html).

    Retries network errors / timeouts and HTTP 429 / 5xx up to `retries`
    attempts in total; any other non-200 status is returned immediately.
    status is 0 if every attempt failed without a response.
    """
    url = BASE_URL.format(slug=slug)
    status = 0
    for attempt in range(1, retries + 1):
        retry_after = None
        try:
            async with session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as r:
                status = r.status
                if status == 200:
                    html = await r.text()
                    print(f"[→] {name} ({status})")
                    return slug, name, status, html
                retry_after = r.headers.get("Retry-After")
            print(f"[!] {name} attempt {attempt}: HTTP {status}")
            if status not in RETRY_STATUSES:
                return slug, name, status, ""
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[!] {name} attempt {attempt} failed: {e!r}")
        if attempt < retries:
            await sleep(_retry_delay(attempt, backoff, retry_after))
    print(f"[✗] {name} giving up after {retries} attempts")
    return slug, name, status, ""

async def scrape_all(factions=None, output_dir=DEFAULT_OUTPUT_DIR, delay=1.0, retries=4):
    """Scrape `factions` ({slug: name}, default all) into output_dir/<slug>.json.

    Returns the list of slugs that failed.
    """
    factions = FACTIONS if factions is None else factions
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    async with aiohttp.ClientSession() as session:
        for i, (slug, name) in enumerate(factions.items()):
            if i:
                await asyncio.sleep(delay)  # polite gap between requests
            slug, name, status, html = await fetch(session, slug, name, retries=retries)
            if status != 200:
                print(f"[✗] {name} skipped ({status})")
                failed.append(slug)
                continue
            units = parse_page(html)
            out = output_dir / f"{slug}.json"
            out.write_text(json.dumps(units, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[✓] {name}: {len(units)} units → {out}")
    return failed

# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(description="Scrape Wahapedia 40k datasheets to JSON.")
    p.add_argument("factions", nargs="*", metavar="SLUG",
                   help=f"Faction slugs (default: all). Known: {', '.join(FACTIONS)}")
    p.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output folder")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between factions (default: 1)")
    p.add_argument("--retries", type=int, default=4, help="Attempts per page (default: 4)")
    args = p.parse_args(argv)

    unknown = [s for s in args.factions if s not in FACTIONS]
    if unknown:
        p.error(f"unknown faction slug(s): {', '.join(unknown)}")
    factions = {s: FACTIONS[s] for s in args.factions} if args.factions else FACTIONS

    failed = asyncio.run(scrape_all(factions, args.out, delay=args.delay, retries=args.retries))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
