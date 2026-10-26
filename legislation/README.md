# Legislation reference corpus

Offline reference for the canonical text of the two instruments cited by LTC
inspection findings, used by the parser to (a) display clean, complete rule text
on each finding card and (b) determine the exact boundary between the regulation
quote and the incident narrative.

**Sources (public domain, Government of Ontario e-Laws):**
- O. Reg. 246/22 (under Fixing Long-Term Care Act, 2021) — all 32 consolidations,
  Mar 2022 → current: `https://www.ontario.ca/laws/regulation/220246` (+ `/v1..vN`)
- Fixing Long-Term Care Act, 2021 (S.O. 2021, c. 39, Sched. 1) — all 8 consolidations:
  `https://www.ontario.ca/laws/statute/21f39` (+ `/v1..vN`)

**Files:**
- `legislation_corpus.json` — minimal versioned corpus: only the sections actually
  cited by findings, each with the text of every consolidation period that a report
  citing it falls in (adjacent identical versions merged). ~950 KB.
- `corpus.py` — loader + tools: `Corpus()` (versioned `section_text`, n-gram index
  for splitting), `Corpus.parse_statute()` (tolerant citation parser),
  `Corpus.extract_provision()` (exact cited clause from section text).
- `build_corpus.py` — reproducible builder (fetches e-Laws, parses, reads `ltc.db`
  for cited sections, emits the JSON). Re-run when new reports cite sections not
  yet in the corpus.
- `_html/` — fetched e-Laws pages (git-ignored cache for the builder).

**Coverage (verified 2026-10-26 against the full cache, 27,231 statute-bearing findings):**
- 27,010 (100.0% of in-corpus citations) resolve to canonical section text; 26,850
  (99.4%) additionally extract the exact cited provision.
- 208 findings cite pre-2022 instruments (O. Reg. 79/10, LTCHA 2007) — not in the
  corpus; these fall back to the ministry's verbatim quote (current behavior).
  Extending the corpus with those instruments is an optional follow-up.
