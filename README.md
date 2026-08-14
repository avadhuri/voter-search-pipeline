# voter-search-pipeline

State-pluggable connectors, fuzzy matching, and a search CLI for India's
digitized 2002-era electoral rolls. This is the open-source data pipeline
behind [oldvoterlist.avadhuri.ai](https://oldvoterlist.avadhuri.ai) — the
Flask app/UI/deployment that serves it stay in a separate, closed repo.

## What's here

- `states/` — the connector interface (`states/base.py`) plus one connector
  module per state (`states/karnataka.py`, `states/west_bengal.py`,
  `states/haryana.py`), each handling that state's raw-file format and
  quirks. `states/registry.py` is the source of truth for which states
  exist and where their raw files live.
- `scripts/build_db.py` — parses raw files via the registry into one SQLite
  DB (`voters` table + FTS5 index).
- `scripts/matching.py` — selectable fuzzy-matching algorithm registry
  (rapidfuzz WRatio, Jaro-Winkler), including vectorized batch scoring
  (`score_fields_batch`/`get_batch_scorer`, benchmarked in
  `scripts/bench_scoring.py`).
- `scripts/transliteration.py` — bridges a Latin-script query against a
  Devanagari-script state's data (see its module docstring for the
  rule-based-vs-ML tradeoff).
- `scripts/search.py` — a CLI for querying a built DB directly.
- `scripts/ocr_haryana.py` — the OCR preprocessing pass for Haryana's 46
  scanned ACs (see "OCR for scanned rolls" below).
- `scripts/cross_reference.py`, `scripts/migrate_translit.py`,
  `scripts/audit_raw_csv.py`, `scripts/eval_embeddings.py`,
  `scripts/download_*.py` — supporting tooling for building, auditing, and
  evaluating the pipeline.

## Install

```
pip install -e .
# or, for the embedding-eval tooling:
pip install -e .[eval]
```

## OCR for scanned rolls

44 of Haryana's 90 ACs publish a real (if legacy-font-encoded) text layer and
parse directly. The other 46 are page scans, and go through a separate OCR
pass first — separate because it is minutes per part and hours per AC, far
too slow to run implicitly inside `build_db`:

```
make download-haryana AC=HR18       # the scans
make ocr-haryana AC=HR18 PARTS=2    # PARTS= caps it, for a quick sample
make build-db STATES=haryana
```

`ocr-haryana` writes one `partNNNN.ocr.json` artifact next to each part PDF
*inside that AC's existing raw ZIP*, and skips parts already done, so it is
re-runnable and interrupt-safe. `states/haryana.py` reads those artifacts;
without them a scanned AC still raises `UnparseableRollError` rather than
looking like an empty roll.

This needs two things `pip install` can't provide, because the OCR engine is
a binary, not a Python package:

```
brew install tesseract            # or: apt-get install tesseract-ocr
curl -fLo "$(brew --prefix)/share/tessdata/hin.traineddata" \
  https://github.com/tesseract-ocr/tessdata_best/raw/main/hin.traineddata
```

Output is best-effort and every OCR'd row is marked as such in its `remark`
column. On the sampled parts, 94-99% of rows carry a name and 99-100% a
documented relation code. See `states/haryana_ocr.py` for how columns are
recovered from a rasterized page, and why Tesseract's `hin` model was chosen
over the Indic-OCR project's.

## Deploying built data to the dev bucket

Production serving reads one small `.sqlite` per constituency (not the
combined DB above), lazily fetched from GCS by the closed
`voter_search_engine` app. Every deploy lands in the **dev** bucket first;
promoting dev's data to production is a separate, maintainer-only step
that happens in that closed repo, not here — this repo only ever pushes
to dev.

If you've been granted `roles/storage.objectAdmin` on
`oldvoterlist-ac-db-dev` (ask the maintainer):

```
gcloud auth login                              # the Google account you were granted access with
gcloud config set project oldvoterlist-prod

make build-db-ac STATES=karnataka,west_bengal,haryana   # or just the state(s) you changed
make push-ac-db-dev
```

`make build-db-ac` writes `data/db/ac/<state>/<ac_code>-c1.p0.sqlite` plus
`data/db/ac/catalog/<state>.sqlite` per state; `make push-ac-db-dev`
rsyncs that whole tree to `gs://oldvoterlist-ac-db-dev` (only
new/changed files, safe to re-run). Run `make download-<state>` first for
any AC you haven't fetched yet.

## Adding a new state

1. Recon its CEO portal / data source (format, CAPTCHA or not, per-AC vs
   per-part granularity).
2. Write `states/<state>.py` implementing `StateConnector`.
3. Register it in `states/registry.py`'s `STATE_CONNECTORS` dict.
4. Write a `scripts/download_<state>.py` if the fetch is nontrivial enough
   to warrant a standalone script.
5. Build with `python -m build_db --states karnataka,west_bengal,haryana,<newstate> <out>.sqlite`.

Contributions welcome via normal PRs — this repo has no separate staging
step.

## Tests

```
pip install -e . pytest
pytest
```

Two tests are known-failing independent of any change here — see the
failure messages, both are pre-existing gaps in that state connector's
edge-case handling, not regressions.
