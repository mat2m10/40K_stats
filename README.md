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
pip install -r requirements.txt
```

To keep notebook outputs out of commits, activate the nbstripout filter
once per clone: `pip install nbstripout && nbstripout --install`.

## Usage

The scraper lives in `src/scrape.py` and can be run from the command line:

```bash
python src/scrape.py                    # all factions
python src/scrape.py tyranids necrons   # selected faction slugs
python src/scrape.py --help             # --out, --delay, --retries
```

It downloads the datasheets page of each faction in `FACTIONS`, one request
at a time with a pause between them (retrying network errors, HTTP 429 and
5xx with backoff), and writes one file per faction to
`data/raw_scraped_waha/<faction-slug>.json`. Each unit has `name`, `id`,
`stats` (M/T/SV/W/LD/OC), `weapons` (with their keywords in `abilities`),
`abilities` (each with a `type`: core, faction, datasheet, wargear or
damaged), `keywords` and `faction_keywords`.

The notebooks are run from the `notebooks/` folder (they use paths relative
to it), in order:

1. **`01_scrape_data.ipynb`**: runs the same scraper via `src/scrape.py`.
2. **`02_unit_composition.ipynb`**: loads a faction file (currently
   Tyranids) and creates one folder per unit under
   `data/miniatures/<faction>/<unit-id>/`.

`data/` is gitignored, so scraped data and generated folders stay local.

## Tests

The parser and the retry logic are tested offline against a small synthetic
datasheet (`tests/fixtures/datasheet_sample.html`):

```bash
pip install pytest
pytest
```

## Data source and disclaimer

All unit data comes from Wahapedia, and the game content is © Games Workshop.
This repository contains only code. Scraped output is deliberately kept out
of version control. If you run the scraper:

- keep it polite (sequential requests with a delay, as the scraper does) and
  scrape only what you need;
- check Wahapedia's terms and `robots.txt` first;
- do not redistribute the scraped data.

This is an unofficial fan project, and it is not affiliated with or
endorsed by Games Workshop or Wahapedia.
