# Ontario LTC Inspection Comparator

A self-contained tool that pulls the Ontario **Ministry of Long-Term Care**'s
public inspection reports, stores them locally, and renders a website comparing
how long-term care (LTC) homes compare over time — with a dashboard for each
home.

Pure **Python standard library only** — no dependencies, no build step.

## What it does

- **Fetches** the Ministry's public inspection reports for a set of homes
  (by city search, by a whole-region/whole-province term list, or by home number).
- **Parses** each report into structured records: every *finding* (written
  notification of non-compliance), its regulation section, the narrative, the
  stated source, and any **fine** (administrative monetary penalty, AMP) with
  its rationale.
- **Renders** a static site (`data/report/`):
  - `index.html` — all homes compared, with client-side **filter** (city, year
    range, fines-only) and **sort**, a findings-by-type breakdown, a methodology
    section, and **source links** to the Ministry's official pages.
  - `home_<number>.html` — one dashboard per home: findings/fines/inspections
    trends by year, finding-type mix filterable by time, the full listing of
    actual findings, and fine details.
  - `findings.csv` and `home_year.csv` — the underlying data.

Everything is **self-contained** (embedded CSS/JS/data), so the rendered site can
be dropped onto any static host.

## Data source

Ontario Ministry of Long-Term Care public reporting site:
<https://publicreports.mltc.gov.on.ca>

The tool reads the same data shown on that site, through its public (unauthenticated)
API. See [`docs/API.md`](docs/API.md).

**Attribution:** inspection data is © the Government of Ontario (King's Printer for
Ontario). This project is a third-party tool and analysis; it is not produced or
endorsed by the Ministry.

## Requirements

Python 3.9+. No third-party packages.

## Usage

```bash
# Fetch homes for a region (or all of Ontario via a term list)
python3 ltc.py fetch --cities Kitchener,Waterloo,Cambridge
python3 ltc.py fetch --geo geo/ontario.txt     # one search term per line
python3 ltc.py fetch --homes 2917              # a specific home

# Re-parse cached reports into findings (no network)
python3 ltc.py reparse

# Render the site + CSVs
python3 ltc.py report --region "Waterloo Region"

# Show what's cached
python3 ltc.py status
```

The cache is a local SQLite DB (`ltc.db`). Re-running `fetch` is **idempotent and
incremental** — already-fetched reports are skipped, so refreshes only pull new
reports.

## Project structure

```
ltc.py                  # the tool (fetch / reparse / report / status)
docs/API.md             # the data API in detail
docs/methodology.md     # how the metrics are calculated
data/report/            # rendered site + CSVs (generated; not committed)
ltc.db                  # local cache (generated; not committed)
geo/ontario.txt         # optional: whole-Ontario search-term list
```

## Acceptable use

This fetches a **public** dataset intended for public access. Before doing large
crawls or publishing data, review the source site's terms and the province's
open-data / Crown-copyright policy, keep access polite (the tool rate-limits and
fetches incrementally), and attribute the data. See [`docs/methodology.md`](docs/methodology.md).

## License

[MIT](LICENSE)
