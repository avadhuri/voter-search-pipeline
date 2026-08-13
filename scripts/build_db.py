"""
Parse raw state roll files into normalized SQLite database(s). Supports a
single AC (the original POC path), one state's ACs combined (the original
--combine path, Karnataka-only), multiple states combined into one DB
(--states), and one small file per (state, ac_code) plus a per-state
catalog (--states ... --per-ac) -- the native artifact set for per-AC-file
serving (see voter_search_engine's per-AC-file-serving plan; no combined DB
is built or needed for that path).

Column/row parsing lives in each states/<state>.py connector's parse_raw()
so this script and the download pipeline share one source of truth for each
state's raw format. states/registry.py maps a state_id to its connector
class and where its raw files live, so this script doesn't hardcode that
per state.

Usage:
    build_db.py <raw_csv_path> <sqlite_db_path>
        Build/overwrite a DB from a single Karnataka AC's raw CSV (matches
        the original POC's file naming, e.g. data/raw/A085.csv).

    build_db.py --combine <raw_dir> <sqlite_db_path>
        Build/overwrite one combined DB from every "<AC_CODE>.csv" file in
        <raw_dir> (as produced by scripts/download_2002_all.py). Karnataka
        only -- kept as the original POC-regression path.

    build_db.py --states karnataka,west_bengal <sqlite_db_path>
        Build/overwrite one combined DB across every listed state, each
        read from its states/registry.py raw_dir/raw_glob. Still useful for
        local CLI/dev use (scripts/search.py, ad hoc queries) even though
        production serving has moved to --per-ac below.

    build_db.py --states karnataka,west_bengal --per-ac <out_dir> [--contract c1] [--patch 0] [--workers N]
        Build one <out_dir>/<state>/<ac_code>-<contract>.p<patch>.sqlite per
        AC, plus one <out_dir>/catalog/<state>.sqlite per state (a small
        state_coverage + ac_index summary, no voter rows). contract is the
        schema/shape version consumed by the serving app; patch is a content
        revision within a fixed contract, bumped explicitly (not
        auto-detected) whenever an AC's data is rebuilt/republished. Each
        AC's parse+write is independent (own raw file in, own sqlite file
        out) so this fans out across a process pool -- --workers caps it
        (default: cpu_count - 1). An AC whose output file already exists
        and was fully finalized by a prior run is skipped, not rebuilt --
        makes an interrupted run resumable by just re-running the same
        command, and lets a bigger --workers value be applied retroactively
        without redoing already-finished ACs.
"""
import datetime
import glob
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.base import Constituency
from states.karnataka import KarnatakaConnector
from states.registry import STATE_CONNECTORS
from transliteration import backfill_latin_columns

VOTERS_SCHEMA = """
DROP TABLE IF EXISTS voters;
CREATE TABLE voters (
    id INTEGER PRIMARY KEY,
    state TEXT,
    roll_year INTEGER,
    district TEXT,
    ac_code TEXT,
    ac_name TEXT,
    part_no INTEGER,
    serial_no INTEGER,
    local_ref TEXT,
    full_name TEXT,
    full_name_latin TEXT,
    full_relative_name TEXT,
    full_relative_name_latin TEXT,
    relation_code TEXT,
    relation_label TEXT,
    age INTEGER,
    gender TEXT,
    remark TEXT,
    locality TEXT
);
"""

SCHEMA = VOTERS_SCHEMA + """
DROP TABLE IF EXISTS state_coverage;
CREATE TABLE state_coverage (
    state_id TEXT PRIMARY KEY,
    label TEXT,
    acs_total INTEGER,
    acs_digitized INTEGER,
    locality_coverage TEXT,
    built_at TEXT
);
"""

CATALOG_SCHEMA = """
DROP TABLE IF EXISTS state_coverage;
CREATE TABLE state_coverage (
    state_id TEXT PRIMARY KEY,
    label TEXT,
    acs_total INTEGER,
    acs_digitized INTEGER,
    locality_coverage TEXT,
    built_at TEXT
);

DROP TABLE IF EXISTS ac_index;
CREATE TABLE ac_index (
    state TEXT,
    ac_code TEXT,
    ac_name TEXT,
    district TEXT,
    contract TEXT,
    patch INTEGER,
    row_count INTEGER,
    file_size_bytes INTEGER,
    has_locality INTEGER,
    PRIMARY KEY (state, ac_code, contract)
);
"""

INSERT_SQL = """
INSERT INTO voters (
    state, roll_year, district, ac_code, ac_name, part_no, serial_no,
    local_ref, full_name, full_relative_name, relation_code,
    relation_label, age, gender, remark, locality
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

STATE_COVERAGE_INSERT_SQL = """
INSERT INTO state_coverage (
    state_id, label, acs_total, acs_digitized, locality_coverage, built_at
) VALUES (?,?,?,?,?,?)
"""

AC_INDEX_INSERT_SQL = """
INSERT INTO ac_index (
    state, ac_code, ac_name, district, contract, patch,
    row_count, file_size_bytes, has_locality
) VALUES (?,?,?,?,?,?,?,?,?)
"""

RELATION_LABELS = {"F": "Father", "H": "Husband", "M": "Mother", "O": "Other/Guardian"}


def _load_ac_lookup():
    """ac_code -> Constituency, from states/meta/ac_meta.json (via the connector's
    own loader, so this and list_constituencies() can't drift)."""
    return {ac.ac_code: ac for ac in KarnatakaConnector().list_constituencies()}


def _resolve_ac(ac_code, ac_lookup):
    """Known AC -> its real metadata; unknown code (shouldn't happen given
    filenames come from ac_meta.json in the first place) -> blank fields,
    same as the old behavior, rather than raising."""
    return ac_lookup.get(ac_code) or Constituency(ac_code=ac_code, ac_name="", district="")


def _records_to_rows(records):
    return [
        (
            r.state, r.roll_year, r.district, r.ac_code, r.ac_name,
            r.part_no, r.serial_no, r.local_ref, r.full_name,
            r.full_relative_name, r.relation_code,
            RELATION_LABELS.get(r.relation_code, r.relation_code),
            r.age, r.gender, r.remark, r.locality,
        )
        for r in records
    ]


def _finalize(conn):
    conn.executescript(
        """
        CREATE INDEX idx_voters_ac_part ON voters(ac_code, part_no, serial_no);
        CREATE INDEX idx_voters_district ON voters(district);

        DROP TABLE IF EXISTS voters_fts;
        CREATE VIRTUAL TABLE voters_fts USING fts5(
            full_name, full_relative_name, content='voters', content_rowid='id'
        );
        INSERT INTO voters_fts(rowid, full_name, full_relative_name)
            SELECT id, full_name, full_relative_name FROM voters;
        """
    )
    conn.commit()


def _finalize_voters_only(conn):
    """Same as _finalize but for a per-AC file: indexes only, no voters_fts --
    confirmed dead weight for this shape (app.py/search.py/cross_reference.py
    never query it)."""
    conn.executescript(
        """
        CREATE INDEX idx_voters_ac_part ON voters(ac_code, part_no, serial_no);
        CREATE INDEX idx_voters_district ON voters(district);
        """
    )
    conn.commit()


def build_single(raw_csv_path, db_path, roll_year=2002):
    """Build a DB from one AC's raw CSV, inferring ac_code from the filename."""
    ac_code = os.path.splitext(os.path.basename(raw_csv_path))[0]
    connector = KarnatakaConnector()
    ac = _resolve_ac(ac_code, _load_ac_lookup())

    with open(raw_csv_path, "rb") as f:
        raw = f.read()
    records = connector.parse_raw(raw, ac, roll_year)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(INSERT_SQL, _records_to_rows(records))
    backfill_latin_columns(conn)
    _finalize(conn)

    total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"Loaded {total} records from {ac_code} into {db_path}.")
    conn.close()


def build_combined(raw_dir, db_path, roll_year=2002):
    """Build one DB from every <AC_CODE>.csv file in raw_dir."""
    connector = KarnatakaConnector()
    ac_lookup = _load_ac_lookup()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    csv_paths = sorted(glob.glob(os.path.join(raw_dir, "*.csv")))
    total = 0
    for path in csv_paths:
        ac_code = os.path.splitext(os.path.basename(path))[0]
        ac = _resolve_ac(ac_code, ac_lookup)
        with open(path, "rb") as f:
            raw = f.read()
        records = connector.parse_raw(raw, ac, roll_year)
        conn.executemany(INSERT_SQL, _records_to_rows(records))
        total += len(records)
        print(f"  {ac_code}: {len(records)} records")

    backfill_latin_columns(conn)
    _finalize(conn)
    grand_total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"Loaded {grand_total} records from {len(csv_paths)} ACs into {db_path}.")
    conn.close()


def build_multi_state(state_ids, db_path, roll_year=2002):
    """Build one DB combining every listed state's raw files, per
    states/registry.py's connector class + raw_dir/raw_glob for each.

    Also populates state_coverage -- one row per state summarizing how much
    of it is digitized (acs_digitized/acs_total, from raw files present vs.
    list_constituencies()) and whether locality data (village/town, see
    VoterRecord.locality) was actually extracted for it. This is what lets
    the serving app show roadmap/coverage info straight from the DB, with
    no compile-time knowledge of which states exist.
    """
    unknown = [s for s in state_ids if s not in STATE_CONNECTORS]
    if unknown:
        raise SystemExit(f"Unknown state(s): {', '.join(unknown)}. Known: {', '.join(STATE_CONNECTORS)}")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    built_at = datetime.datetime.utcnow().isoformat()

    grand_total = 0
    for state_id in state_ids:
        info = STATE_CONNECTORS[state_id]
        connector = info["connector_cls"]()
        ac_lookup = {ac.ac_code: ac for ac in connector.list_constituencies()}
        paths = sorted(glob.glob(os.path.join(info["raw_dir"], info["raw_glob"])))
        state_total = 0
        acs_with_locality = set()
        for path in paths:
            ac_code = os.path.splitext(os.path.basename(path))[0]
            ac = _resolve_ac(ac_code, ac_lookup)
            with open(path, "rb") as f:
                raw = f.read()
            records = connector.parse_raw(raw, ac, roll_year)
            conn.executemany(INSERT_SQL, _records_to_rows(records))
            state_total += len(records)
            if any(r.locality for r in records):
                acs_with_locality.add(ac_code)
            print(f"  [{state_id}] {ac_code}: {len(records)} records")
        print(f"{state_id}: {state_total} records from {len(paths)} files")
        grand_total += state_total

        if not paths:
            locality_coverage = "none"
        elif len(acs_with_locality) == len(paths):
            locality_coverage = "full"
        elif acs_with_locality:
            locality_coverage = "partial"
        else:
            locality_coverage = "none"
        conn.execute(STATE_COVERAGE_INSERT_SQL, (
            state_id, info["label"], len(ac_lookup), len(paths),
            locality_coverage, built_at,
        ))

    translit_count = backfill_latin_columns(conn, state_ids=state_ids)
    if translit_count:
        print(f"Transliterated {translit_count} distinct Devanagari name strings to Latin script.")
    _finalize(conn)
    check_total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"\nLoaded {check_total} records across {len(state_ids)} state(s) into {db_path}.")
    conn.close()


def _ac_output_is_complete(ac_db_path):
    """True if ac_db_path exists and was fully finalized by a prior run --
    detected by the presence of the index _finalize_voters_only() creates
    last, not just file existence (a process killed mid-write, e.g. Ctrl-C
    or an OOM, leaves a file on disk that opens fine but was never
    finalized). Used to make a --per-ac run resumable: re-running the same
    command after an interruption skips every AC a prior run actually
    finished instead of redoing it."""
    if not os.path.exists(ac_db_path):
        return False
    try:
        conn = sqlite3.connect(ac_db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_voters_ac_part'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _build_one_ac(task):
    """Runs in a worker process (see build_per_ac's ProcessPoolExecutor) --
    parses one AC's raw file and writes its own small sqlite file. Takes a
    single tuple arg (not **kwargs) since ProcessPoolExecutor.submit pickles
    whatever it's given, and a plain tuple keeps that unambiguous. Every
    input is passed explicitly (connector_cls, ac metadata, roll_year)
    rather than re-derived from STATE_CONNECTORS/globals inside the worker
    -- a spawned process gets a fresh import of this module, so it would
    only see the *unpatched* registry, silently breaking callers (e.g.
    tests) that monkeypatch STATE_CONNECTORS in the parent process.

    Independent of every other AC's work -- own output file, own sqlite
    connection, no shared state -- which is what makes this safe to fan
    out across processes at all. build_per_ac() aggregates the per-AC
    results (state_total, ac_index_rows, acs_with_locality) after every
    future resolves, back in the parent."""
    state_id, connector_cls, path, ac_db_path, contract, patch, ac, roll_year = task
    ac_code = ac.ac_code

    if _ac_output_is_complete(ac_db_path):
        conn = sqlite3.connect(ac_db_path)
        try:
            row_count = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
            has_locality = conn.execute(
                "SELECT 1 FROM voters WHERE locality IS NOT NULL AND locality != '' LIMIT 1"
            ).fetchone() is not None
        finally:
            conn.close()
        return {
            "ac_code": ac_code, "ac_name": ac.ac_name, "district": ac.district,
            "row_count": row_count, "file_size_bytes": os.path.getsize(ac_db_path),
            "has_locality": has_locality, "skipped": True,
        }

    connector = connector_cls()
    with open(path, "rb") as f:
        raw = f.read()
    records = connector.parse_raw(raw, ac, roll_year)

    ac_conn = sqlite3.connect(ac_db_path)
    ac_conn.executescript(VOTERS_SCHEMA)
    ac_conn.executemany(INSERT_SQL, _records_to_rows(records))
    backfill_latin_columns(ac_conn, state_ids=[state_id])
    _finalize_voters_only(ac_conn)
    ac_conn.close()

    has_locality = any(r.locality for r in records)
    return {
        "ac_code": ac_code, "ac_name": ac.ac_name, "district": ac.district,
        "row_count": len(records), "file_size_bytes": os.path.getsize(ac_db_path),
        "has_locality": has_locality, "skipped": False,
    }


def build_per_ac(state_ids, out_dir, contract="c1", patch=0, roll_year=2002, workers=None):
    """Build one small <ac_code>-<contract>.p<patch>.sqlite per (state,
    ac_code), plus one small catalog.sqlite per state -- the native per-AC
    serving artifact set (no combined DB is built or needed for this path;
    see voter_search_engine's per-AC-file-serving plan).

    Each AC's parse+write is independent (own raw file in, own sqlite file
    out, no shared connection) -- fanned out across a ProcessPoolExecutor
    rather than run one at a time, since this is CPU-bound (legacy-font
    PDF decoding for Devanagari states, transliteration backfill for all)
    and a single-threaded loop leaves every other core idle for the whole
    build. contract/patch are explicit inputs, not auto-detected from
    content -- republishing an AC's data (bug fix, added locality,
    re-digitized) is a deliberate act of bumping --patch, not something
    this script infers on its own.
    """
    unknown = [s for s in state_ids if s not in STATE_CONNECTORS]
    if unknown:
        raise SystemExit(f"Unknown state(s): {', '.join(unknown)}. Known: {', '.join(STATE_CONNECTORS)}")

    if workers is None:
        workers = max(1, (os.cpu_count() or 2) - 1)

    built_at = datetime.datetime.utcnow().isoformat()
    catalog_dir = os.path.join(out_dir, "catalog")
    os.makedirs(catalog_dir, exist_ok=True)

    grand_total = 0
    for state_id in state_ids:
        info = STATE_CONNECTORS[state_id]
        connector_cls = info["connector_cls"]
        connector = connector_cls()
        ac_lookup = {ac.ac_code: ac for ac in connector.list_constituencies()}
        paths = sorted(glob.glob(os.path.join(info["raw_dir"], info["raw_glob"])))
        state_dir = os.path.join(out_dir, state_id)
        os.makedirs(state_dir, exist_ok=True)

        tasks = []
        for path in paths:
            ac_code = os.path.splitext(os.path.basename(path))[0]
            ac = _resolve_ac(ac_code, ac_lookup)
            ac_db_path = os.path.join(state_dir, f"{ac_code}-{contract}.p{patch}.sqlite")
            tasks.append((state_id, connector_cls, path, ac_db_path, contract, patch, ac, roll_year))

        results = []
        pool_size = max(1, min(workers, len(tasks) or 1))
        with ProcessPoolExecutor(max_workers=pool_size) as pool:
            futures = [pool.submit(_build_one_ac, t) for t in tasks]
            for fut in as_completed(futures):
                result = fut.result()
                results.append(result)
                tag = "skip (already built)" if result["skipped"] else "built"
                print(f"  [{state_id}] {result['ac_code']} ({tag}): {result['row_count']} records")

        results.sort(key=lambda r: r["ac_code"])
        state_total = sum(r["row_count"] for r in results)
        acs_with_locality = {r["ac_code"] for r in results if r["has_locality"]}
        ac_index_rows = [
            (
                state_id, r["ac_code"], r["ac_name"], r["district"], contract, patch,
                r["row_count"], r["file_size_bytes"], int(r["has_locality"]),
            )
            for r in results
        ]

        print(f"{state_id}: {state_total} records across {len(paths)} AC file(s) ({pool_size} worker(s))")
        grand_total += state_total

        if not paths:
            locality_coverage = "none"
        elif len(acs_with_locality) == len(paths):
            locality_coverage = "full"
        elif acs_with_locality:
            locality_coverage = "partial"
        else:
            locality_coverage = "none"

        catalog_path = os.path.join(catalog_dir, f"{state_id}.sqlite")
        cat_conn = sqlite3.connect(catalog_path)
        cat_conn.executescript(CATALOG_SCHEMA)
        cat_conn.execute(STATE_COVERAGE_INSERT_SQL, (
            state_id, info["label"], len(ac_lookup), len(paths),
            locality_coverage, built_at,
        ))
        cat_conn.executemany(AC_INDEX_INSERT_SQL, ac_index_rows)
        cat_conn.commit()
        cat_conn.close()
        print(f"  wrote catalog {catalog_path}")

    print(f"\nLoaded {grand_total} records across {len(state_ids)} state(s) into {out_dir} (per-AC).")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--combine":
        build_combined(sys.argv[2], sys.argv[3])
    elif "--per-ac" in sys.argv and "--states" in sys.argv:
        state_ids = sys.argv[sys.argv.index("--states") + 1].split(",")
        out_dir = sys.argv[sys.argv.index("--per-ac") + 1]
        contract = sys.argv[sys.argv.index("--contract") + 1] if "--contract" in sys.argv else "c1"
        patch = int(sys.argv[sys.argv.index("--patch") + 1]) if "--patch" in sys.argv else 0
        workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else None
        build_per_ac(state_ids, out_dir, contract=contract, patch=patch, workers=workers)
    elif len(sys.argv) == 4 and sys.argv[1] == "--states":
        build_multi_state(sys.argv[2].split(","), sys.argv[3])
    elif len(sys.argv) == 3:
        build_single(sys.argv[1], sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
