"""
Warhammer 40k Unit Scraper — Wahapedia (10th Edition)
======================================================
Scrapes unit datasheets from wahapedia.ru using Selenium (for JS rendering)
and BeautifulSoup (for parsing).

Requirements:
    pip install selenium beautifulsoup4 lxml webdriver-manager

Usage:
    python wahapedia_scraper.py                        # scrapes Space Marines (default)
    python wahapedia_scraper.py --faction necrons       # scrapes Necrons
    python wahapedia_scraper.py --faction all           # scrapes every faction
    python wahapedia_scraper.py --headless false        # show browser window

Output:
    JSON file per faction in ./output/
"""

import argparse
import json
import time
import re
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# ── Faction slug list (matches Wahapedia URL slugs) ──────────────────────────
FACTIONS = {
    "space-marines":        "Space Marines",
    "necrons":              "Necrons",
    "chaos-space-marines":  "Chaos Space Marines",
    "tyranids":             "Tyranids",
    "orks":                 "Orks",
    "aeldari":              "Aeldari",
    "tau-empire":           "T'au Empire",
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
OUTPUT_DIR = Path("./output")


# ── Driver setup ─────────────────────────────────────────────────────────────
def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


# ── Parsing helpers ───────────────────────────────────────────────────────────
def clean(text: str) -> str:
    """Strip whitespace and normalise dashes."""
    return re.sub(r"\s+", " ", text).strip()


def parse_stat_block(datasheet_soup) -> dict:
    """
    Extract M / T / SV / W / LD / OC stats from the unit stat row.
    Wahapedia renders these in a table with class 'dsCharacteristic' or similar.
    """
    stats = {}
    # Stat labels vary slightly; look for the characteristic table
    stat_table = datasheet_soup.find("div", class_=re.compile(r"wh-characteristics|dsCharacteristic", re.I))
    if not stat_table:
        return stats

    labels = [clean(td.get_text()) for td in stat_table.find_all("div", class_=re.compile(r"wh-char-label|label", re.I))]
    values = [clean(td.get_text()) for td in stat_table.find_all("div", class_=re.compile(r"wh-char-value|value", re.I))]

    for label, value in zip(labels, values):
        if label:
            stats[label] = value
    return stats


def parse_weapons(datasheet_soup) -> list[dict]:
    """Extract ranged and melee weapon profiles."""
    weapons = []
    # Weapon rows are typically in a table; look for rows with Range/A/BS/S/AP/D cols
    weapon_tables = datasheet_soup.find_all("table", class_=re.compile(r"wh-weapon|weapon", re.I))
    for table in weapon_tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [clean(td.get_text()) for td in row.find_all("td")]
            if len(cells) >= 6:
                weapons.append({
                    "name":   cells[0],
                    "range":  cells[1],
                    "A":      cells[2],
                    "BS_WS":  cells[3],
                    "S":      cells[4],
                    "AP":     cells[5],
                    "D":      cells[6] if len(cells) > 6 else "",
                    "abilities": cells[7] if len(cells) > 7 else "",
                })
    return weapons


def parse_abilities(datasheet_soup) -> list[dict]:
    """Extract unit abilities (name + description)."""
    abilities = []
    # Abilities are in divs with class containing 'ability'
    for ab in datasheet_soup.find_all("div", class_=re.compile(r"wh-ability|ability-description", re.I)):
        name_tag = ab.find(class_=re.compile(r"name|title", re.I))
        desc_tag = ab.find(class_=re.compile(r"desc|text|rule", re.I))
        name = clean(name_tag.get_text()) if name_tag else ""
        desc = clean(desc_tag.get_text()) if desc_tag else clean(ab.get_text())
        if name or desc:
            abilities.append({"name": name, "description": desc})
    return abilities


def parse_keywords(datasheet_soup) -> list[str]:
    """Extract the keywords line."""
    kw_div = datasheet_soup.find(class_=re.compile(r"keywords|keyword", re.I))
    if not kw_div:
        return []
    text = clean(kw_div.get_text())
    # Strip leading label like "KEYWORDS:" 
    text = re.sub(r"^KEYWORDS\s*:?\s*", "", text, flags=re.I)
    return [k.strip() for k in text.split(",") if k.strip()]


def parse_datasheet(ds_soup, unit_name: str) -> dict:
    """Assemble all fields for a single unit datasheet."""
    return {
        "name":      unit_name,
        "stats":     parse_stat_block(ds_soup),
        "weapons":   parse_weapons(ds_soup),
        "abilities": parse_abilities(ds_soup),
        "keywords":  parse_keywords(ds_soup),
    }


# ── Main scraping logic ───────────────────────────────────────────────────────
def scrape_faction(driver: webdriver.Chrome, slug: str, faction_name: str) -> list[dict]:
    url = BASE_URL.format(slug=slug)
    print(f"\n[→] Fetching {faction_name}  ({url})")
    driver.get(url)

    # Wait for at least one datasheet section to appear
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='datasheet'], [class*='Datasheet']"))
        )
    except Exception:
        print(f"  [!] Timed out waiting for datasheets on {url}")
        return []

    # Extra wait for dynamic content
    time.sleep(2)

    soup = BeautifulSoup(driver.page_source, "lxml")

    # ── Locate individual datasheet containers ────────────────────────────────
    # Wahapedia wraps each unit in a div with class containing 'datasheet'
    datasheets = soup.find_all("div", class_=re.compile(r"\bdatasheet\b", re.I))

    if not datasheets:
        # Fallback: try any section with an h3/h2 unit name
        datasheets = soup.find_all("section", class_=re.compile(r"unit|card", re.I))

    units = []
    for ds in datasheets:
        # Unit name is usually in the first heading inside the datasheet div
        name_tag = ds.find(re.compile(r"h[1-4]"), class_=re.compile(r"name|title|unit", re.I))
        if not name_tag:
            name_tag = ds.find(re.compile(r"h[1-4]"))
        unit_name = clean(name_tag.get_text()) if name_tag else "Unknown"

        unit_data = parse_datasheet(ds, unit_name)
        units.append(unit_data)
        print(f"  [✓] {unit_name}")

    return units


def save_json(data: list[dict], faction_slug: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{faction_slug}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [💾] Saved {len(data)} units → {out_path}")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Scrape Wahapedia 40k datasheets")
    parser.add_argument(
        "--faction", default="space-marines",
        help="Faction slug (e.g. necrons) or 'all' for every faction"
    )
    parser.add_argument(
        "--headless", default="true", choices=["true", "false"],
        help="Run Chrome headlessly (default: true)"
    )
    args = parser.parse_args()

    headless = args.headless.lower() == "true"
    driver = make_driver(headless=headless)

    try:
        if args.faction == "all":
            targets = FACTIONS.items()
        elif args.faction in FACTIONS:
            targets = [(args.faction, FACTIONS[args.faction])]
        else:
            print(f"Unknown faction '{args.faction}'. Available slugs:")
            for slug in FACTIONS:
                print(f"  {slug}")
            return

        for slug, name in targets:
            units = scrape_faction(driver, slug, name)
            if units:
                save_json(units, slug)
            # Be polite — don't hammer the server
            time.sleep(3)
    finally:
        driver.quit()

    print("\n[✅] Done.")


if __name__ == "__main__":
    main()