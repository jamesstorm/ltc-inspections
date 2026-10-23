# Methodology

How the fetched inspection reports are turned into the numbers on the dashboards.

## Data source & rights

Inspection reports are published by the Ontario Ministry of Long-Term Care at
`publicreports.mltc.gov.on.ca`. The reports are Crown copyright (© King's Printer for
Ontario). This project is a **third-party tool and analysis**; it does not alter the data
and is not produced or endorsed by the Ministry. If you republish the data, keep clear
attribution to the Ministry / King's Printer for Ontario.

## What is fetched

For each home (found by a search term — city, town, or postal area):

1. Its **list of published inspection reports** (`getHomeDetails`).
2. The **full content of each report** (`getHomeInspectionDetails`), cached verbatim in the
   SQLite DB.

## Per-finding parse

Each report's findings field (`WN_Details__c`) is HTML. The parser splits it into
individual records on the finding headers and, for each, extracts:

- **type** — `WN` (written notification of non-compliance) or `AMP` (administrative
  monetary penalty, i.e. a fine).
- **category / title** — the finding's heading (e.g. "Plan of care", "Reporting certain
  matters to Director", "Infection Prevention and Control").
- **nc** — the finding number.
- **statute** — the regulation/section cited ("Non-compliance with: …").
- **fine** — the AMP dollar amount (0 for non-fine findings).
- **source** — the stated evidence ("Sources: …").
- **text** — the narrative of what was found.

The parser handles both known markup eras of the site. It is **best-effort**: the finding
*count* is reliable, but a small number of unusually-formatted reports may mis-segment a
line of text. The full source HTML is retained in the DB, so any report can be re-inspected
or the parser improved and re-run (`reparse`) without re-fetching.

## Metrics

- **Findings** — count of written-notification findings (the non-compliance items the
  Ministry issued). This is the headline number.
- **Fines** — sum of AMP amounts issued.
- **CO-related** — count of findings that reference a **compliance order** (the Ministry's
  most severe short-term enforcement; these are usually a home that failed to fix a prior
  order). A separately-issued order, if present, is captured in the finding text.
- **Inspections** — number of published reports.
- **By year** — each report is bucketed by the calendar year of its issue date.
- **By type** — findings grouped by their category heading.

## Caveats

- **Issued, not resolved.** A finding means the Ministry flagged non-compliance at that
  inspection. It does **not** mean the problem persists — follow-up reports often note that
  a prior order was found to be *in compliance*. A heavy year can reflect a serious problem
  **or** a thorough inspection that was later remediated.
- **Comparison is informational.** These counts are not a ranking of the quality of care a
  resident receives; they measure how many non-compliance items were issued, which is also
  influenced by inspection activity and complaint volume.
- **Coverage.** The scope is whatever search terms you fetch. City searches find homes in
  those cities; a few homes in very small communities may need explicit terms or home numbers.
- **Not legal advice** — for acceptable-use / rights questions about the data, see the
  README and the source site's policies.
