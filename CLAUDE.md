# voter-search-pipeline

State-pluggable connectors, fuzzy matching, and a search CLI for India's
digitized 2002-era electoral rolls — the open-source data pipeline behind
[oldvoterlist.avadhuri.ai](https://oldvoterlist.avadhuri.ai). The Flask
app/UI/deployment that serves it live in a separate, closed repo — nothing
in this repo depends on that one, and you don't need access to it to work
here.

## Quick start

```
git clone <this repo>
cd voter-search-pipeline
make setup              # venv + editable install
make download-haryana AC=HR47,HR02 && make download-west-bengal AC=AC146  # tests need these, see below
make test                # pytest
make download            # demo slice of all 3 live states (Karnataka/WB/Haryana)
make build-db             # combines them into data/db/multi_state_2002.sqlite
make search NAME="Ramesh Kumar"   # query the DB you just built
```

`make help` lists every target with a one-line description — that's the
up-to-date source of truth for what's runnable, more so than this file.

## Architecture

- `states/base.py` — the connector interface (`StateConnector`:
  `list_constituencies()`, `fetch_raw()`, `parse_raw()`) plus the shared
  `VoterRecord`/`Constituency` shapes every state normalizes into.
- `states/<state>.py` — one connector module per state (`karnataka.py`,
  `west_bengal.py`, `haryana.py`). All state-specific weirdness (PDF vs
  CSV, legacy font decoding, which ACs are even fetchable) lives here.
- `states/registry.py` — single source of truth for which states exist,
  their connector class, and where their raw files live
  (`raw_dir`/`raw_glob`). Both `build_db.py` and the closed app import
  `STATE_CONNECTORS` from here.
- `states/meta/*.json` — small, committed, per-state AC lists (generated
  from `list_constituencies()`, never hand-typed). Ships as installed
  package data (`pyproject.toml`'s `package-data`), so `pip install -e .`
  is enough for `list_constituencies()` to work with no raw data
  downloaded yet.
- `scripts/build_db.py` — parses raw files via the registry into one
  SQLite DB (`voters` table + FTS5 index). Single-AC, single-state
  (`--combine`, legacy Karnataka-only), or multi-state (`--states a,b,c`).
- `scripts/matching.py` — the fuzzy-matching algorithm registry
  (rapidfuzz WRatio, Jaro-Winkler) plus vectorized batch scoring
  (`score_fields_batch`) — shared by `search.py` and the closed app so
  scoring behavior never drifts between them.
- `scripts/transliteration.py` — bridges a Latin-script query against a
  Devanagari-script state's data (Haryana). Rule-based
  (`indic_transliteration` → ITRANS), not ML — see its module docstring
  for why (`ai4bharat-transliteration` depends on `fairseq`, whose PyPI
  sdist is broken).
- `scripts/search.py` — the query CLI (see "Querying a DB" below).
- `scripts/download_<state>.py` — one downloader per state with a fetch
  script, each resumable (skips files that already exist).

## Install

```
pip install -e .
# or, for the embedding-eval tooling under scripts/eval_embeddings.py:
pip install -e .[eval]
```

`make setup` does this for you inside a `venv/`. Either way, editable
install is what makes `python -m build_db`, `python -m search`, etc. work
as plain module invocations — no `sys.path.insert` hacks needed anywhere
in this repo.

## Downloading data

All three live states support `make download-<state>` with a sensible
default AC slice and an `AC=` override (single code or comma-list) — see
`make help` for the exact defaults (Karnataka defaults to all 224 ACs;
West Bengal and Haryana default to a demo subset since their sources are
per-part PDFs, much slower to fetch statewide). `make download` runs all
three at their defaults.

Downloaded raw files land under `data/raw/` (gitignored — **never commit
downloaded rolls, zips, or PDFs into this repo**, regenerable by design,
and real voter-roll data shouldn't sit in git history). If you need a
specific AC for testing a parser change, `make download-<state> AC=<code>`
fetches just that one.

## Building a DB

`make build-db` (no args) builds the combined 3-state demo DB at
`data/db/multi_state_2002.sqlite`. `make build-db STATES=karnataka` (or
any other state or comma-list) builds just those states; `make build-db
AC=A085` builds a single-AC DB. See `scripts/build_db.py`'s module
docstring for the exact CLI shapes if you need something the Makefile
doesn't wrap.

**`build_db.py` never commits until the very end** — a combined
multi-state build has zero recoverable progress if killed partway. Build
and test a new/changed state standalone first (`make build-db
STATES=<state>`) before folding it into a bigger combined build.

## Querying a DB

`make search NAME="Ramesh Kumar"` queries the default multi-state DB.
`make search DB=data/db/A085.sqlite NAME="..." ARGS="--ac A085 --relative \"Suresh Kumar\" --limit 10"`
overrides the DB and passes through any other flag `scripts/search.py`
supports (`--relative`, `--ac`, `--part`, `--gender`, `--age`
`--age-tolerance`, `--algorithm wratio|jaro_winkler`, `--min-score`,
`--limit`) — see that file's module docstring for the full CLI. This is
the same matching/scoring code path the closed Flask app uses, so results
here are representative of what the live site would show.

## Running the tests

```
make test
```

On a fresh clone, `make test` reports (confirmed by actually running it,
not assumed): **2 failed, 40 passed, 5 skipped**. Both are expected, not a
broken setup:

- **5 skips** — `test_haryana_connector.py` and `test_west_bengal_connector.py`
  need real fixture ZIPs (`data/raw/haryana/HR47.zip`/`HR02.zip`,
  `data/raw/west_bengal/AC146.zip`) that, per the no-committed-downloads
  policy above, aren't in git — they skip cleanly with "pilot raw data not
  downloaded" rather than failing. Fetch those specific ACs to get full
  coverage locally:
  ```
  make download-haryana AC=HR47,HR02
  make download-west-bengal AC=AC146
  ```
- **2 failures** — `test_cross_reference.py::test_finds_aged_up_match` and
  `test_karnataka_connector.py::test_parse_raw_normalizes_rows_and_skips_malformed`
  are pre-existing, unrelated to anything you're likely touching. Don't
  feel obligated to fix them as a side quest.

## Adding a new state

This is the main way to contribute. Short version:

1. Recon the state's CEO portal (format, CAPTCHA real-or-decorative,
   per-AC vs per-part granularity).
2. Write `states/<state>.py` implementing `StateConnector`.
3. Register it in `states/registry.py`.
4. Write `scripts/download_<state>.py` if the fetch is nontrivial.
5. Extract `VoterRecord.locality` wherever the source supports it (cover
   pages / header rows often carry village/area data that's easy to miss).
6. Commit `states/meta/<state>_ac_meta.json`, generated from real portal
   data.
7. Write tests against real fixture data.
8. Open a normal PR — no separate staging step.

**Read `TODO.md` before starting** — it's the full brief (acceptance
criteria, gotchas learned on Karnataka/West Bengal/Haryana, testing
requirements, which reference connector to study first) and is kept more
current than any summary here.

## Gotchas

- **Legacy/non-Unicode fonts show up often in older PDFs.** Haryana's
  DK-RAJ 8-bit font had no `/ToUnicode` CMap and needed a custom
  transcoder (`states/haryana_dkraj.py`). Don't trust `pdfplumber`'s
  default extraction on an older PDF without spot-checking real output.
- **Scanned/non-text ACs are a permanent gap, not a bug.** `parse_raw`
  should raise clearly (`UnparseableRollError`) rather than return a
  guess — a search feeding SIR verification is worse than useless if it's
  silently wrong.
- **CAPTCHAs are sometimes decorative — verify, don't assume.** Haryana's
  page shows one that never validates server-side.
  Devanagari-script states need `scripts/transliteration.py`'s rule-based
  bridge, not an ML one — see that module's docstring for why. You
  shouldn't need to touch it or `matching.py` when adding a state; set
  `"script": "devanagari"` in the registry entry and the existing bridge
  handles the rest.
- **Locality mapping via an external dataset is a known-hard, unsolved,
  separate problem** — don't attempt it inline while adding a state. See
  `TODO.md`'s "Locality mapping" section if you want to take this on as
  its own contribution.
- **Never commit downloaded raw files or built DBs** — `data/raw/` and
  `data/db/` are gitignored on purpose.
- **`make push-ac-db-dev` must stay two-phase: files first, catalog
  second.** The per-state catalog is the sole authority for which patch the
  serving app fetches — it builds each AC's filename from the catalog's
  `patch` column with no fallback to a lower patch — so a catalog that
  lands ahead of the files it names 404s every affected AC until the gap
  closes. A single `gcloud storage rsync -r` of the whole tree does exactly
  the wrong thing, because `catalog/` sorts ahead of the state
  directories and therefore uploads *first*. That window is seconds for a
  one-AC fix and hours for a multi-state push. Hence
  `AC_CATALOG_EXCLUDE` in phase 1 and a catalog-only rsync in phase 2. The
  closed repo's `gcp-run-push-ac-db` was fixed for this first; this target
  shipped with the one-rsync version and matches it now. If you add a
  bucket-to-bucket or prod variant, it needs the same two phases.

## Where this fits with the closed repo

The closed repo (Flask app, templates, deployment) depends on this one via
a pinned pip git-URL dependency in its `requirements.txt` — it bumps the
pinned tag when it wants a change made here, it doesn't track this repo's
`main` automatically. You don't need that repo, or access to it, to
contribute here: clone this repo, `make setup`, and you have everything
needed to add a state, fix a matching bug, or improve the CLI.
