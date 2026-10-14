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
import copy
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
# Upper bound on a server-sent Retry-After, so one header cannot stall a run for hours.
MAX_RETRY_AFTER = 300

# ── Parsing (moved from notebooks/01_scrape_data.ipynb) ───────────────────────

def clean(text):
    return re.sub(r"\s+", " ", text).strip()

def to_snake(text):
    text = re.sub(r"['‘’]", "", text)          # drop apostrophes (ASCII and curly)
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

def clean_rich(tag):
    """Text of a tag with nested spans joined by spaces, minus the stray
    spaces get_text(" ") leaves before punctuation / inside brackets."""
    text = clean(tag.get_text(" ") if hasattr(tag, "get_text") else str(tag))
    text = re.sub(r"\s+([,.;:!?)\]])", r"\1", text)
    return re.sub(r"([(\[])\s+", r"\1", text)

def parse_weapons(ds):
    weapons = []
    for table in ds.find_all("table", class_="wTable"):
        for row in table.find_all("tr"):
            # Profile rows have 8 cells by position:
            # icon | name + keyword spans | range | A | BS/WS | S | AP | D.
            # Cells are indexed by position (never filtered), so an empty cell
            # cannot shift the columns. Header rows (RANGED/MELEE WEAPONS) and
            # the 2-cell "wTable2_long" duplicate rows are skipped.
            tds = row.find_all("td", recursive=False)
            if len(tds) != 8 or tds[1].find(class_="dsHeader"):
                continue
            name_cell = copy.copy(tds[1])
            kw_spans = name_cell.find_all("span", class_="kwb2")
            keywords = [clean_rich(s) for s in kw_spans]
            for s in kw_spans:
                s.extract()
            name = clean_rich(name_cell)
            if not name:
                continue
            values = [clean(td.get_text(" ")) for td in tds[2:8]]
            weapons.append({
                "name":      name,
                "range":     values[0],
                "A":         values[1],
                "BS_WS":     values[2],
                "S":         values[3],
                "AP":        values[4],
                "D":         values[5],
                "abilities": ", ".join(keywords),   # e.g. "assault, ignores cover"
            })
    return weapons

def _split_bold_abilities(ab):
    """Split one dsAbility div into (name, description) pairs: every <b>
    ending in ':' starts a new ability, whatever follows is its text."""
    found, name, parts = [], None, []
    for node in ab.children:
        if getattr(node, "name", None) == "b" and clean_rich(node).endswith(":"):
            if name:
                found.append((name, clean_rich(" ".join(parts))))
            name, parts = clean_rich(node)[:-1].strip(), []
        elif name is not None:
            parts.append(node.get_text(" ") if hasattr(node, "get_text") else str(node))
    if name:
        found.append((name, clean_rich(" ".join(parts))))
    return found

def parse_abilities(ds):
    abilities = []
    # NB: the "С" in "dsRightСol" is CYRILLIC CAPITAL LETTER ES (U+0421), not a
    # Latin C. Wahapedia's own markup uses this class name (checked 2026-10-13),
    # so do not "fix" it or the abilities column will no longer be found.
    right_col = ds.find("div", class_=lambda c: c and "dsRightСol" in c)
    if not right_col:
        return abilities
    # The right column is a flat list of dsHeader / dsAbility divs. Only read
    # the ABILITIES (and any *ABILITIES / DAMAGED) sections; UNIT COMPOSITION,
    # LEADER, TRANSPORT... also use "<b>...:</b>" but are not abilities.
    section = ""
    for div in right_col.find_all("div", recursive=False):
        classes = div.get("class") or []
        if "dsHeader" in classes:
            section = clean_rich(div)
            continue
        if "dsAbility" not in classes:
            continue
        if section.upper().startswith("DAMAGED"):
            abilities.append({"name": section.title(), "description": clean_rich(div), "type": "damaged"})
        elif "ABILITIES" in section.upper():
            kind = "wargear" if "WARGEAR" in section.upper() else "datasheet"
            text = clean_rich(div)
            m = re.match(r"(CORE|FACTION):\s*(.*)", text)
            if m:  # "CORE: Deep Strike, Leader" -> one entry per rule
                for rule in m.group(2).split(","):
                    if rule.strip():
                        abilities.append({"name": rule.strip(), "description": "", "type": m.group(1).lower()})
                continue
            for name, desc in _split_bold_abilities(div):
                abilities.append({"name": name, "description": desc, "type": kind})
    return abilities

def _keyword_list(col):
    """Comma-separated keyword groups -> list (multi-word keywords kept whole)."""
    keywords = []
    for group in col.find_all("span", recursive=False):
        for kw in clean_rich(group).split(","):
            kw = kw.strip()
            if kw and kw not in keywords:
                keywords.append(kw)
    return keywords

def parse_keywords(ds):
    """Unit keywords (all models + model-specific), e.g. ['INFANTRY', 'GREAT DEVOURER']."""
    kw_div = ds.find("div", class_="ds2colKW")
    if not kw_div:
        return []
    # class is "dsLeftСolKW" with a Cyrillic С, so match on the prefix
    left = kw_div.find("div", class_=lambda c: c and c.startswith("dsLeft"))
    return _keyword_list(left) if left else []

def parse_faction_keywords(ds):
    kw_div = ds.find("div", class_="ds2colKW")
    right = kw_div.find("div", class_=lambda c: c and c.startswith("dsRight")) if kw_div else None
    return _keyword_list(right) if right else []


def parse_datasheet(ds):
    name_tag = ds.find("div", class_="dsH2Header")
    name = clean(name_tag.find("div").get_text()) if name_tag else "Unknown"
    return {
        "name":             name,
        "id":               to_snake(name),
        "stats":            parse_stat_block(ds),
        "weapons":          parse_weapons(ds),
        "abilities":        parse_abilities(ds),
        "keywords":         parse_keywords(ds),
        "faction_keywords": parse_faction_keywords(ds),
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
            return min(MAX_RETRY_AFTER, max(0.0, float(retry_after)))
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
