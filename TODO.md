# Adding state coverage — requirements for the next contributor

This repo (`voter-search-pipeline`) is the open-source data pipeline behind
[oldvoterlist.avadhuri.ai](https://oldvoterlist.avadhuri.ai) — fuzzy search
over India's digitized 2002-era electoral rolls, built primarily for SIR
(Special Intensive Revision) verification. See `README.md` for the repo
layout and `states/base.py`'s docstring for the connector contract; this
file is the fuller brief for taking on **new-state coverage** as an
ongoing workstream, not a one-off task.

## Where things stand

Three states are live today, each exercising a different flavor of source
data:

- **Karnataka** — all 224 ACs, flat CSV, no CAPTCHA. The easy case;
  `states/karnataka.py` is the shortest connector and worth reading first.
- **West Bengal** — only 19 Kolkata ACs (the Latin-typeset subset). The
  other ~275 ACs are Bengali-typeset PDFs with no `/ToUnicode` CMap, so name
  search can't work on them regardless of effort — a source-format ceiling,
  not a gap to close.
- **Haryana** — a demo spread of ACs, legacy 8-bit-font Devanagari PDFs
  (`states/haryana_dkraj.py` decodes them). Only 44 of 90 ACs have any
  usable text layer; the other 46 are page scans, correctly refused
  (`UnparseableRollError`) rather than silently returning nothing.

The project's actual goal is **all-India coverage**, not just these three.
`scripts/app.py` (closed repo) currently lists Maharashtra, Tamil Nadu, and
Uttar Pradesh as "planned" — reasonable starting candidates — but any state
with a digitized, text-extractable 2002 roll is in scope. Pick one, recon
it, and go; there's no fixed order.

## Goal

Add one new state per contribution, following the pattern the three live
states already establish: a `StateConnector` subclass, registered, tested
against real fetched data, with locality extracted where the source
supports it. Each state you add is independently mergeable and independently
useful — you don't need to wait for or coordinate with anyone else's
state work.

## The interface you're implementing

Everything state-specific lives behind three methods
(`states/base.py`):

```python
class StateConnector:
    state_id: str = None                       # short slug, e.g. "maharashtra"
    def list_constituencies(self) -> list[Constituency]: ...
    def fetch_raw(self, ac: Constituency, roll_year: int) -> bytes: ...
    def parse_raw(self, raw: bytes, ac: Constituency, roll_year: int) -> list[VoterRecord]: ...
```

`Constituency` is `ac_code, ac_name, district, total_parts, extra` (`extra`
is a free-form dict for anything state-specific worth carrying through —
Haryana uses it for `roll_format`). `VoterRecord` is the common shape every
state normalizes into — `state, district, ac_code, ac_name, part_no,
serial_no, local_ref, full_name, full_relative_name, relation_code, age,
gender, roll_year, remark, locality`. Don't add new fields to `VoterRecord`
for state-specific data; if something doesn't fit the existing shape, it's
either `extra` on the `Constituency`, or genuinely out of scope for this
pass — ask first rather than growing the shared schema.

Two conventions worth internalizing before writing a connector, both
visible directly in `states/haryana.py`'s module docstring (read it in
full — it's the densest worked example of the discipline below):

- **`fetch_raw` returns one blob per AC, always**, even if the source
  publishes per-part files (Haryana bundles ~200 part PDFs into one
  in-memory ZIP). Keeps `build_db.py`'s "one raw artifact per AC" convention
  uniform across states with wildly different source layouts.
- **Never guess.** If a page/part/AC can't be parsed reliably (a scanned AC,
  an unrecognized cover-page layout, an ambiguous field), raise
  (`UnparseableRollError` or similar) rather than returning an empty or
  best-guess result that looks like a successful parse. A search feeding
  SIR verification is worse than useless if it's silently wrong.

## Process

1. **Recon the state's CEO portal.** Format (CSV/PDF/scanned image),
   CAPTCHA or not (check whether it's actually enforced server-side before
   assuming you need to solve it — Haryana's turned out to be client-side
   decoration that never validates; verify per-state, don't assume either
   way), per-AC vs. per-part granularity, whether a legacy/non-Unicode font
   is in play. Write this up as a module docstring in step 2, not a
   separate doc — `states/haryana.py`'s docstring is the template: data
   source URLs/endpoints, what's fetchable vs. not, table layout, any
   decoding gotchas.
2. **Write `states/<state>.py`** implementing `StateConnector`.
3. **Register it** in `states/registry.py`'s `STATE_CONNECTORS` dict —
   `connector_cls`, `label`, `raw_dir`, `raw_glob`, `script` (`"latin"` or
   `"devanagari"` — this flag is what tells `transliteration.py` which
   states need `*_latin` columns backfilled at build/search time).
4. **Write `scripts/download_<state>.py`** if the fetch is nontrivial
   (argparse `--ac` comma-list, `--out-dir`, `--force`, `--limit`,
   resumable/skip-existing — `download_haryana.py` is the fullest example,
   `download_west_bengal.py` the other reference point).
5. **Extract locality where the source supports it.** `VoterRecord.locality`
   exists specifically for village/town/area data that's often sitting
   unused in a cover page or header row (see Haryana/West Bengal's cover-page
   parsing) — check for it before assuming a state has none. If the source
   genuinely has no locality field (Karnataka's flat CSV has none), that's a
   real ceiling, not something to work around with an external dataset —
   see "Locality mapping" below.
6. **Add `states/meta/<state>_ac_meta.json`** — the small, committed,
   per-state AC list (`pyproject.toml`'s `package-data` ships it with the
   installed package). Generate it once from `list_constituencies()`'s
   source of truth, don't hand-type it.
7. **Write tests** — see Testing below.
8. **Open a PR.** No separate staging step or maintainer hand-off; normal
   review.

## Data & inputs you'll need

- Direct access to the state's CEO (Chief Electoral Officer) portal — no
  special credentials expected; all three live states are fetched
  unauthenticated.
- A pilot batch of real raw files (a handful of ACs, not the whole state)
  downloaded into `data/raw/<state>/`, to develop and test the parser
  against. `data/raw/` is gitignored — regenerable, not committed — but a
  **small number of real fixture files** (1-2 ACs' worth, enough to exercise
  the parser's edge cases) should be committed alongside the tests that
  reference them, the same way `data/raw/haryana/HR47.zip` and
  `data/raw/west_bengal/AC146.zip` already are — check `.gitignore`'s
  exceptions before assuming everything under `data/raw/` is excluded.
- Roll year: this project's scope is specifically the **2002-era** rolls
  (`roll_year=2002` in practice everywhere today) — that's the SIR-relevant
  vintage. `VoterRecord.roll_year` exists as a field so a different year is
  technically representable, but don't fetch a different year without
  checking first; it's not what this pass of work is for.

## Gotchas, learned the hard way on KA/WB/HR

- **Legacy/non-Unicode fonts show up often in older PDFs.** Haryana's
  DK-RAJ 8-bit font had no `/ToUnicode` CMap and needed a custom transcoder
  (`states/haryana_dkraj.py`) built by reading the font's own glyph mapping
  directly — don't assume `pdfplumber`'s default text extraction is
  trustworthy without spot-checking real output against known names.
- **Scanned/non-text ACs are a legitimate, permanent gap, not a bug to
  fix.** If a PDF has no real text layer (or only an unusable OCR layer),
  the correct behavior is `parse_raw` raising clearly, and that AC simply
  isn't in this project's scope until someone builds a real OCR pipeline
  (out of scope here). Don't build a workaround that returns low-confidence
  guesses.
- **CAPTCHAs are sometimes real, sometimes decorative — check, don't
  assume.** Haryana's page shows one but never validates it server-side.
  Don't burn time on CAPTCHA-solving infrastructure before confirming the
  CAPTCHA is actually enforced.
- **Devanagari-script states need the rule-based transliteration bridge,
  not an ML one.** `scripts/transliteration.py` uses `indic_transliteration`
  (rule-based, ITRANS) deliberately — `ai4bharat-transliteration` depends on
  `fairseq`, whose PyPI sdist is broken (missing `fairseq/version.txt`,
  confirmed, long-standing upstream bug). Set `"script": "devanagari"` in
  the registry entry and the existing bridge handles the rest; you
  shouldn't need to touch `transliteration.py` or `matching.py` at all —
  WRatio already scores ITRANS output against natural English spelling well.
- **Locality mapping via an external dataset is a known-hard, unsolved
  problem — don't attempt it as part of adding a state.** If a state's
  source format has no village/locality field at all (like Karnataka's flat
  CSV), closing that gap needs a village→(district, ac_code) mapping
  matching the **pre-2008 delimitation** boundaries (2002 rolls use the old
  AC boundaries; most public datasets are post-2008 and don't apply). No
  reusable dataset has been found for any state so far — this is
  deliberately a separate, standalone contribution (a data file + join
  step consumed by `build_db.py`), not something to solve inline while
  adding a new connector. If your target state's *source PDFs/CSVs*
  themselves carry a village/cover-page field (like Haryana/West Bengal),
  that's a completely different, much easier case — extract it directly
  per step 5 above.
- **`build_db.py` never commits until the very end** — a combined
  multi-state build has zero recoverable progress if killed partway. Build
  and test your new state standalone first
  (`python -m build_db --states <newstate> <out>.sqlite`) before merging it
  into a larger combined build.

## Testing requirements

Follow `tests/test_haryana_connector.py` / `tests/test_west_bengal_connector.py`'s
pattern:

- Tests run against **real fetched fixture files** (a small number,
  committed per "Data & inputs" above), not synthetic/hand-built data —
  parsing bugs in this project have consistently shown up only against real
  source quirks (padding conventions, hybrid font layouts, unrecognized
  cover-page formats), not idealized inputs.
- Spot-check specific real records by name/district/AC against manually
  verified ground truth (e.g. `test_list_constituencies_reads_real_ac_meta`
  asserting an exact AC's name and district) — not just "parsing didn't
  crash."
- Cover the connector's own edge cases explicitly — padding/normalization
  helpers, any hybrid or unusual sub-format your recon turned up (see
  `HYBRID_ZIP` in the Haryana tests for the pattern: a second, differently-
  shaped fixture file specifically for a layout variant).
- `pytest` should stay green except the two pre-existing, unrelated known
  failures noted in `README.md` — don't let your change introduce new ones,
  and don't feel obligated to fix those two either.

## Acceptance criteria

A new state is done when all of the following hold:

1. `states/<state>.py` implements `StateConnector` fully (`list_constituencies`,
   `fetch_raw`, `parse_raw`), with a module docstring describing the data
   source, format, and any decoding/parsing gotchas — same depth as
   `states/haryana.py`'s.
2. Registered in `states/registry.py`'s `STATE_CONNECTORS`, including an
   accurate `"script"` value (`"latin"`/`"devanagari"`).
3. `states/meta/<state>_ac_meta.json` committed, generated from real portal
   data (not hand-typed), covering every AC the connector claims to support.
4. `scripts/download_<state>.py` exists if the fetch is nontrivial, following
   the existing `--ac`/`--out-dir`/`--force`/`--limit`, resumable pattern.
5. `VoterRecord.locality` is populated wherever the source data supports it
   (cover page, header row, etc.) — or, if the source genuinely has none,
   that's stated explicitly in the module docstring as a known ceiling, not
   left silently empty with no explanation.
6. Unparseable ACs (scans, unrecognized layouts) raise clearly rather than
   returning empty/guessed data, and are documented (which ACs, why) in the
   module docstring.
7. Tests exist against real fixture data (committed per "Data & inputs"),
   asserting specific real records, not just "doesn't crash" — and the full
   `pytest` run is green apart from the two pre-existing known failures.
8. `python -m build_db --states <newstate> <out>.sqlite` succeeds end-to-end
   against a real downloaded batch and produces a sane row count (spot-check
   against the portal's own AC/elector counts if available).
9. `README.md`'s state list (if it names specific states — check current
   content) or an equivalent note reflects the new state's real coverage
   (full state vs. a subset, and why, same honesty as the WB/Haryana
   entries already do).
10. Opened as a normal PR against this repo — no separate staging step.

## Reference implementations, ranked by how much they teach

1. `states/haryana.py` + `states/haryana_dkraj.py` — legacy-font decoding,
   cover-page locality extraction, per-part-to-per-AC bundling, explicit
   unparseable-AC handling. Read this one in full before starting.
2. `states/west_bengal.py` — a state with real coverage limits (Bengali
   script) honestly scoped down rather than worked around.
3. `states/karnataka.py` — the simple/easy case (flat CSV), good for
   understanding the baseline shape before tackling anything harder.
