.PHONY: help setup download download-karnataka download-west-bengal download-haryana build-db build-db-ac check-servable push-ac-db-dev migrate-translit search test clean

.DEFAULT_GOAL := help

VENV := venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# numpy==2.0.2 (pinned in pyproject.toml) has no prebuilt wheel past Python
# 3.12 and fails compiling from source on a bare `python3` that resolves to
# something newer (e.g. Homebrew's python3 tracks the latest release, which
# is 3.14 as of writing). Prefer the newest interpreter known to have a
# working wheel; /usr/bin/python3 is macOS's Apple-shipped Python (usually
# older, no Homebrew auto-upgrade risk) and is included as a last-resort
# rescue before falling back to whatever plain `python3` resolves to.
PYTHON := $(shell command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3.9 || (test -x /usr/bin/python3 && /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)' && echo /usr/bin/python3) || command -v python3)

RAW_DIR := data/raw
WB_RAW_DIR := data/raw/west_bengal
HR_RAW_DIR := data/raw/haryana
DB_DIR := data/db
MULTI_DB := $(DB_DIR)/multi_state_2002.sqlite

# Native output for per-AC-file serving (see build_db.py's --per-ac usage
# above) -- one <state>/<ac_code>-<contract>.p<patch>.sqlite per AC plus
# catalog/<state>.sqlite per state, mirroring exactly what the closed
# voter_search_engine app fetches from GCS.
AC_DB_LOCAL_DIR := $(DB_DIR)/ac

# Every deploy goes to the dev bucket first (see "Deploying built data" in
# the README) -- promotion from dev to production is a separate,
# maintainer-only step that lives in voter_search_engine (closed), not
# here, so there's deliberately no GCS_AC_BUCKET_PROD/push-ac-db-prod
# target in this repo.
GCS_AC_BUCKET_DEV ?= oldvoterlist-ac-db-dev
GCP_PROJECT ?= oldvoterlist-prod

# The 19 Kolkata ACs whose 2002 roll is Latin-typeset (name search works);
# the other ~275 West Bengal ACs are Bengali-typeset with no ToUnicode map,
# so names can't be extracted -- see states/west_bengal.py's module
# docstring. This is the practical default for `make download-west-bengal`.
WB_KOLKATA_LATIN_ACS := AC141,AC142,AC143,AC144,AC145,AC154,AC153,AC155,AC156,AC157,AC158,AC159,AC160,AC146,AC147,AC148,AC149,AC150,AC151

# A demo-sized spread of Haryana's 44 usable (text-layer) ACs, one per
# district, rather than the full ~7,800-part statewide set -- see
# states/haryana.py for why the other 46 ACs (page scans) aren't fetchable
# at all. This is the practical default for `make download-haryana`.
HR_DEMO_ACS := HR02,HR22,HR25,HR31,HR48,HR61,HR66,HR74,HR86

# `make` with no target, or `make help`, lists every command below with a
# one-line description -- pulled from the `## ...` comment on each target
# line. Keep new targets annotated the same way.
help:
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

# The single command that gets a fresh clone running: creates the venv
# (idempotent) and installs this package editable, so `python -m <module>`
# works for every script under scripts/ without any sys.path hackery.
setup: ## Fresh-clone bootstrap: venv + editable install. Run this first.
	test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .
	$(PIP) install pytest
	mkdir -p $(RAW_DIR) $(DB_DIR)

download: download-karnataka download-west-bengal download-haryana ## Fetch the demo slice of all three live states

# `make download-karnataka` fetches all 224 ACs; `make download-karnataka
# AC=A085` fetches just one.
download-karnataka: ## Fetch Karnataka's 2002 roll (all 224 ACs; AC=<code> for just one)
ifdef AC
	$(PY) -m download_2002_all --ac $(AC) --out-dir $(RAW_DIR)
else
	$(PY) -m download_2002_all --out-dir $(RAW_DIR)
endif

# `make download-west-bengal` fetches only the 19 Latin-typeset Kolkata ACs
# (the demo-ready subset); `make download-west-bengal AC=AC146` (or a
# comma-separated list) fetches specific ACs instead.
download-west-bengal: ## Fetch West Bengal's 2002 roll (19 Kolkata ACs by default; AC= to override)
ifdef AC
	$(PY) -m download_west_bengal --ac $(AC) --out-dir $(WB_RAW_DIR)
else
	$(PY) -m download_west_bengal --ac $(WB_KOLKATA_LATIN_ACS) --out-dir $(WB_RAW_DIR)
endif

# `make download-haryana` fetches the 9-AC demo spread; `make
# download-haryana AC=HR61` (or a comma-separated list) fetches specific ACs.
download-haryana: ## Fetch Haryana's 2002 roll (demo AC spread by default; AC= to override)
ifdef AC
	$(PY) -m download_haryana --ac $(AC) --out-dir $(HR_RAW_DIR)
else
	$(PY) -m download_haryana --ac $(HR_DEMO_ACS) --out-dir $(HR_RAW_DIR)
endif

# `make build-db AC=A085` builds a single-AC DB (data/db/A085.sqlite).
# `make build-db STATES=karnataka,west_bengal,haryana` combines every listed
# state's raw files into $(MULTI_DB). `make build-db` alone defaults to all
# three live states combined.
build-db: ## Build a SQLite DB from downloaded rolls (STATES=a,b,c; AC= for one AC)
ifdef AC
	$(PY) -m build_db $(RAW_DIR)/$(AC).csv $(DB_DIR)/$(AC).sqlite
else ifdef STATES
	$(PY) -m build_db --states $(STATES) $(MULTI_DB)
else
	$(PY) -m build_db --states karnataka,west_bengal,haryana $(MULTI_DB)
endif

# One-time backfill of full_name_latin/full_relative_name_latin on an
# already-built DB (new build-db runs do this automatically). Safe to
# re-run -- only fills still-NULL rows.
migrate-translit: ## Backfill Latin-transliteration columns on an already-built DB
	$(PY) -m migrate_translit $${DB_PATH:-$(MULTI_DB)}

# `make build-db-ac STATES=haryana` builds just Haryana's per-AC files;
# `make build-db-ac` alone defaults to all three live states. Always
# contract=c1, patch=0 today -- there's no PATCH= override yet, so
# rebuilding an AC that was already pushed overwrites that same p0 file
# in place rather than publishing a new patch revision alongside it. Fine
# for the current single-collaborator, small-scale workflow; revisit if
# that ever needs to change (e.g. republishing needs an old patch kept
# live for in-flight requests). WORKERS=N caps the process pool build_db.py
# fans per-AC parsing out across (default: cpu_count - 1); an interrupted
# run is safe to just re-run -- already-finalized AC files are skipped, not
# rebuilt.
build-db-ac: ## Build per-AC .sqlite files + catalogs into $(AC_DB_LOCAL_DIR) (STATES=a,b,c, default karnataka,west_bengal,haryana; WORKERS=n)
	PYTHONUNBUFFERED=1 $(PY) -m build_db --states $(if $(STATES),$(STATES),karnataka,west_bengal,haryana) --per-ac $(AC_DB_LOCAL_DIR) $(if $(WORKERS),--workers $(WORKERS),)

# The gate between "it built" and "it can be served". Every check it runs is
# for something that produces a completely normal-looking build -- right row
# counts, names that parse, searches that score -- while being wrong or
# unreachable in the serving app: a mis-stamped roll year, a blank district
# (the picker's primary tier), a missing source_url, or empty *_latin columns
# on a non-Latin state, whose rows then match nothing anybody can type. None
# of that needs the closed app to detect, which is why it lives here: a
# contributor adding a state can answer "is this servable?" themselves.
# `make check-servable PATH_=data/db/multi_state_2002.sqlite` checks a
# combined DB instead; STATE= narrows to one state. Non-zero exit means a
# blocker.
check-servable: ## Check built data is actually servable (PATH_= to override, STATE=a,b)
	@test "$(CHECK)" != "0" || { echo "check-servable skipped (CHECK=0)"; exit 0; }; \
	$(PY) -m check_servable $(if $(PATH_),$(PATH_),$(AC_DB_LOCAL_DIR)) $(if $(STATE),--state $(STATE),)

# Gated on check-servable, deliberately: the failure this prevents is a push
# of data that builds and searches perfectly while being wrong or unreachable
# in the app, which has happened -- production once ran a whole state's worth
# of per-AC files that predated the source_url column, with the live
# self-check reporting 150/150. That's a push-time condition, not a code one,
# which is why it's here rather than in pytest. Override with
# `make push-ac-db-dev CHECK=0` only when you know what the blocker is and
# mean to ship past it.
#
# Pushes newly-built per-AC data to the dev bucket only -- see
# GCS_AC_BUCKET_DEV's comment above for why there's no prod equivalent
# here. Requires gcloud auth'd as an account with roles/storage.objectAdmin
# on the bucket (ask the maintainer for access) -- doesn't attempt to
# create the bucket if missing/unreachable, unlike voter_search_engine's
# equivalent maintainer-only target, since objectAdmin alone can't create
# buckets; a collaborator hitting the describe check below should ask for
# access rather than being handed bucket-creation rights just to unblock
# this.
push-ac-db-dev: check-servable ## Copy locally-built per-AC files (make build-db-ac) up to the dev GCS bucket
	@gcloud storage buckets describe "gs://$(GCS_AC_BUCKET_DEV)" --project=$(GCP_PROJECT) >/dev/null 2>&1 || \
		{ echo "gs://$(GCS_AC_BUCKET_DEV) not reachable -- check 'gcloud auth login'/'gcloud config set project $(GCP_PROJECT)', or ask the maintainer for bucket access"; exit 1; }
	gcloud storage rsync -r $(AC_DB_LOCAL_DIR) gs://$(GCS_AC_BUCKET_DEV) --project=$(GCP_PROJECT)

# `make search NAME="Ramesh Kumar"` queries $(MULTI_DB). `make search
# DB=data/db/A085.sqlite NAME="..." ARGS="--ac A085 --limit 10"` overrides
# the DB and/or passes through any other search.py flag (--relative, --ac,
# --part, --gender, --age, --algorithm, --min-score, --limit -- see
# scripts/search.py's module docstring for the full list).
search: ## Query a built DB. NAME="..." required, DB= and ARGS= optional
	$(PY) -m search $(if $(DB),$(DB),$(MULTI_DB)) --name "$(NAME)" $(ARGS)

# The two connector tests that need real fixture ZIPs (HR47.zip, HR02.zip,
# AC146.zip -- see CLAUDE.md's "Running the tests" section) will fail until
# those ACs are downloaded once via download-haryana/download-west-bengal.
test: ## Run pytest (two pre-existing failures are known -- see README)
	$(PY) -m pytest

clean: ## Remove the venv and all downloaded/built data
	rm -rf $(VENV) $(DB_DIR) $(RAW_DIR)
