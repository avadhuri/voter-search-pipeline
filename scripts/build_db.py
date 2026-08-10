"""
Parse raw state roll files into one normalized, multi-state SQLite database
with an FTS5 index for search. Supports a single AC (the original POC path),
one state's ACs combined (the original --combine path, Karnataka-only), and
multiple states combined into one DB (--states).

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
        read from its states/registry.py raw_dir/raw_glob. This is what
        the running app's DB_PATH should point at once more than one state
        has data.
"""
import glob
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.base import Constituency
from states.karnataka import KarnatakaConnector
from states.registry import STATE_CONNECTORS

SCHEMA = """
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
    full_relative_name TEXT,
    relation_code TEXT,
    relation_label TEXT,
    age INTEGER,
    gender TEXT,
    remark TEXT
);
"""

INSERT_SQL = """
INSERT INTO voters (
    state, roll_year, district, ac_code, ac_name, part_no, serial_no,
    local_ref, full_name, full_relative_name, relation_code,
    relation_label, age, gender, remark
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

RELATION_LABELS = {"F": "Father", "H": "Husband", "M": "Mother", "O": "Other/Guardian"}


def _load_ac_lookup():
    """ac_code -> Constituency, from data/ac_meta.json (via the connector's
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
            r.age, r.gender, r.remark,
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

    _finalize(conn)
    grand_total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"Loaded {grand_total} records from {len(csv_paths)} ACs into {db_path}.")
    conn.close()


def build_multi_state(state_ids, db_path, roll_year=2002):
    """Build one DB combining every listed state's raw files, per
    states/registry.py's connector class + raw_dir/raw_glob for each."""
    unknown = [s for s in state_ids if s not in STATE_CONNECTORS]
    if unknown:
        raise SystemExit(f"Unknown state(s): {', '.join(unknown)}. Known: {', '.join(STATE_CONNECTORS)}")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    grand_total = 0
    for state_id in state_ids:
        info = STATE_CONNECTORS[state_id]
        connector = info["connector_cls"]()
        ac_lookup = {ac.ac_code: ac for ac in connector.list_constituencies()}
        paths = sorted(glob.glob(os.path.join(info["raw_dir"], info["raw_glob"])))
        state_total = 0
        for path in paths:
            ac_code = os.path.splitext(os.path.basename(path))[0]
            ac = _resolve_ac(ac_code, ac_lookup)
            with open(path, "rb") as f:
                raw = f.read()
            records = connector.parse_raw(raw, ac, roll_year)
            conn.executemany(INSERT_SQL, _records_to_rows(records))
            state_total += len(records)
            print(f"  [{state_id}] {ac_code}: {len(records)} records")
        print(f"{state_id}: {state_total} records from {len(paths)} files")
        grand_total += state_total

    _finalize(conn)
    check_total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"\nLoaded {check_total} records across {len(state_ids)} state(s) into {db_path}.")
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--combine":
        build_combined(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "--states":
        build_multi_state(sys.argv[2].split(","), sys.argv[3])
    elif len(sys.argv) == 3:
        build_single(sys.argv[1], sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
