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
- `states/eci_codes.py` — the one table mapping a `state_id` onto the
  Election Commission's own state/UT code (S01–S29, U01–U09). Two build-time
  joins key off it — `roll_years.py` (which roll year to stamp) and
  `source_urls.py` (which per-part workbook to read) — and a state present in
  one and absent from the other is a half-wired state, so there is one copy.
  Declaring a code here is the whole wiring cost for both.
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
  (`--combine`), or multi-state (`--states a,b,c`). The first two predate
  the registry and take a raw path rather than a state, so `--state`
  (`STATE=` in the Makefile) says which one — defaulting to Karnataka
  because that is the state they always meant, not because they do anything
  Karnataka-specific.
- `scripts/matching.py` — the fuzzy-matching algorithm registry
  (rapidfuzz WRatio, Jaro-Winkler) plus vectorized batch scoring
  (`score_fields_batch`) — shared by `search.py` and the closed app so
  scoring behavior never drifts between them.
- `scripts/transliteration.py` — bridges a Latin-script query against a
  non-Latin-script state's data. Rule-based (`indic_transliteration` →
  ITRANS), not ML — see its module docstring for why
  (`ai4bharat-transliteration` depends on `fairseq`, whose PyPI sdist is
  broken), and for the measured per-script quality, which is the reason a
  connector's own romanization outranks it.
- `scripts/check_servable.py` — the gate between "it built" and "it can be
  served" (`make check-servable`). Everything it looks for produces a
  completely normal-looking build; see "Is it servable?" below.
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
AC=A085` builds a single-AC DB (add `STATE=haryana` to build one AC of a
state other than Karnataka — `make build-db AC=HR02 STATE=haryana`). See `scripts/build_db.py`'s module
docstring for the exact CLI shapes if you need something the Makefile
doesn't wrap.

**A build stops on a meta/raw-file disagreement rather than degrading.**
Two guards in `build_db.py`, both for failures that are invisible
downstream. `_ac_lookup()` refuses a state whose `list_constituencies()`
returns the same `ac_code` twice (`DuplicateConstituencyError`) — building
the dict alone keeps the last entry and silently drops the rest, so an AC
builds fine under another AC's name and district while `acs_total`
under-counts. `_resolve_ac()` raises (`UnknownConstituencyError`) on a raw
file naming an AC the meta doesn't declare, instead of the blank
`ac_name`/`district` it used to fall back to — a blank district makes an AC
unreachable in the serving app's picker, whose primary tier is district, and
a blank name makes it unrecognizable once reached. Neither shows up in any
search-quality check: those drive searches by explicit `(state, ac_code)`.
A build is offline, re-runnable and watched, so stopping is the right
failure direction there; this is not the serving side's degrade-don't-break
rule.

**A connector's own romanization outranks the rule-based one.** For a
non-Latin-script state the `*_latin` columns are the only thing a
Latin-script query scores against, so getting them wrong makes rows that
build, parse and rank perfectly while being unfindable by the queries real
users type — the same invisible-downstream class as `roll_year` below. A
connector that extracted a Latin reading alongside the native name puts it
on `VoterRecord.full_name_latin` / `full_relative_name_latin`;
`backfill_latin_columns()` then fills exactly the rows left blank and never
overwrites one. The precedence is deliberate, not incidental: rule-based
ITRANS output is a transliteration scheme rather than a name, and the gap
between the two is script-dependent and in one case severe — measured with
the app's own scorer, Devanagari ~95 and Telugu ~93, but Gurmukhi ~78 and
**Tamil ~73** (ச romanizes as "jh", so Selvam becomes "jhelvam"), and
Malayalam chillu forms (ൻ ൽ ൾ) plus Tamil ன have no mapping in any target
scheme and survive into the output as native characters. A build therefore
prints how many distinct names came out with native-script residue, with
examples: that is a state that wants a connector-supplied `full_name_latin`,
and saying so at build time is the alternative to a user discovering it by
not finding themselves. `to_latin()` picks its scheme from the string's own
first Indic codepoint rather than from the registry tag, because a single
state's roll can span scripts (Puducherry: Tamil, Telugu and Malayalam).
`states/base.py`'s `VoterRecord` and `tests/test_transliteration.py` carry
the rest.

**Each state is stamped with its own roll year, resolved at build time.**
The "2002 rolls" are not all from 2002 — the intensive-revision cycle ran
per state across 2002–2006 — and `roll_year` is not decoration: the serving
app derives an elector's year of birth as `roll_year - age` for its
(required) year-of-birth filter, so a state stamped 2002 when its roll is
2006 mis-targets every one of its electors by four years, and reads to a
user as "you were never on the roll" rather than as a bug. Nothing
downstream can catch it — the rows parse, score and rank exactly as they
should. `states/roll_years.py` resolves it per state (registry entry's own
`roll_year`, else `states/meta/sir_source_urls/state_roll_years.json` by
ECI state code, else 2002); a new state declares its ECI code in that
module's `STATE_CODES` and needs nothing else. That JSON is **derived, not
hand-maintained** — `make roll-years` regenerates it from the
`state_roll_years.xlsx` it shipped beside (a received artifact, see that
directory's README), and `tests/test_roll_years.py` fails if the committed
JSON has drifted from the workbook. A wrong year gets fixed in the workbook
and regenerated; editing the JSON alone won't survive the next run. `--roll-year YYYY` forces
one year across the whole build and exists only as an escape hatch — omit
it unless you specifically mean to.

**`build_db.py` never commits until the very end** — a combined
multi-state build has zero recoverable progress if killed partway. Build
and test a new/changed state standalone first (`make build-db
STATES=<state>`) before folding it into a bigger combined build.

## Is it servable?

```
make build-db-ac STATES=<state>
make check-servable                 # or PATH_=<a built .sqlite>, STATE=a,b
```

**"It built" and "it can be served" are different questions, and only the
first one is easy.** Every check `check_servable.py` runs is for something
that produces a build with the right row count, names that parse, searches
that score and every search-quality assertion green — while the state is
wrong or unreachable in the serving app in a way a user reads as an answer
rather than as a bug. A mis-stamped `roll_year` (the app derives year of
birth as `roll_year - age`, and that field is required, so a state three
years off tells every elector they were never on the roll); a blank
`district` (the picker's primary tier — an AC nobody can navigate to); a
missing `source_url` (a result nobody can check against the original page,
in a tool built for exactly that); empty `*_latin` columns on a non-Latin
state (rows that match nothing anybody can type). None of it is visible
from a row count or a spot-check of names.

It runs against a per-AC directory — the layout the app actually fetches —
or a combined `.sqlite`, and reports per state rather than per file. It
exits non-zero on a BLOCKER, which is why `make push-ac-db-dev` depends on
it: the failure being prevented is a *push*, and it has happened —
production once served a whole state's worth of per-AC files that predated
the `source_url` column, with the live self-check reporting 150/150.
`CHECK=0` overrides, for when you know what the blocker is and mean to ship
past it. WARNINGs never block; they're degradations a maintainer may
knowingly ship.

This lives here, not in the closed app, so a contributor adding a state can
answer "is this servable?" without it. `tests/test_check_servable.py` drives
each tripwire by producing the exact breakage it exists for — a test that
only asserted "clean data passes" would have passed against every gap this
check was written for.

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
not assumed): **1 failed, 104 passed, 5 skipped**. Both the failure and the
skips are expected, not a broken setup:

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
- **1 failure** — `test_karnataka_connector.py::test_parse_raw_normalizes_rows_and_skips_malformed`
  is pre-existing, unrelated to anything you're likely touching. Don't feel
  obligated to fix it as a side quest. (`test_cross_reference.py::test_finds_aged_up_match`
  used to be a second one; it turned out to be a stale hand-built row tuple
  in the test itself, positionally coupled to `build_db.INSERT_SQL` and two
  columns short. Fixed, with a docstring saying what to do the next time a
  column is added.)

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
  Non-Latin-script states need `scripts/transliteration.py`'s rule-based
  bridge, not an ML one — see that module's docstring for why. You
  shouldn't need to touch it or `matching.py` when adding a state; set
  `"script"` in the registry entry to the script the rolls are actually in
  and the existing bridge handles the rest.
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
