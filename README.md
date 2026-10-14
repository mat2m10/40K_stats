# 40K_stats

*Statistics of the grim darkness of the future.*

This project scrapes Warhammer 40,000 (10th edition) unit datasheets from
[Wahapedia](https://wahapedia.ru/wh40k10ed/) into JSON, as the basis for
unit statistics (profiles, weapons, points efficiency, ...).

**Status:** early. The scraper works. The analysis notebook is still
exploratory, and no statistics have been produced yet.

## Setup

Python 3.12:

```bash
python -m venv .venv
source .venv/bin/activate
pip install aiohttp beautifulsoup4 lxml nest_asyncio jupyter
```

## Usage

Start Jupyter from the `notebooks/` folder (the notebooks use paths relative
to it) and run the notebooks in order:

1. **`01_scrape_data.ipynb`**: downloads the datasheets page of each faction
   listed in `FACTIONS`, one request at a time with a pause between them, and
   writes one file per faction to `data/raw_scraped_waha/<faction-slug>.json`.
   Each unit has `name`, `id`, `stats` (M/T/SV/W/LD/OC), `weapons`,
   `abilities` and `keywords`.
2. **`02_unit_composition.ipynb`**: loads a faction file (currently
   Tyranids) and creates one folder per unit under
   `data/miniatures/<faction>/<unit-id>/`.

`data/` is gitignored, so scraped data and generated folders stay local.

## Data source and disclaimer

All unit data comes from Wahapedia, and the game content is © Games Workshop.
This repository contains only code. Scraped output is deliberately kept out
of version control. If you run the scraper:

- keep it polite (sequential requests with a delay, as in the notebook) and
  scrape only what you need;
- check Wahapedia's terms and `robots.txt` first;
- do not redistribute the scraped data.

This is an unofficial fan project, and it is not affiliated with or
endorsed by Games Workshop or Wahapedia.
