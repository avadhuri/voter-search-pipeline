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
