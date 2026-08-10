"""
Parse raw Karnataka CEO roll CSV(s) into a normalized SQLite database with
an FTS5 index for search. Supports both a single AC (the original POC path)
and combining many ACs into one database (for full-state coverage).

Column parsing lives in states/karnataka.py (KarnatakaConnector.parse_raw)
so this script and the download pipeline share one source of truth for the
raw format.

Usage:
    build_db.py <raw_csv_path> <sqlite_db_path>
        Build/overwrite a DB from a single AC's raw CSV (matches the
        original POC's file naming, e.g. data/raw/A085.csv).

    build_db.py --combine <raw_dir> <sqlite_db_path>
        Build/overwrite one combined DB from every "<AC_CODE>.csv" file in
        <raw_dir> (as produced by scripts/download_2002_all.py).
"""
import glob
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.base import Constituency
from states.karnataka import KarnatakaConnector

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
    gender TEXT
);
"""

INSERT_SQL = """
INSERT INTO voters (
    state, roll_year, district, ac_code, ac_name, part_no, serial_no,
    local_ref, full_name, full_relative_name, relation_code,
    relation_label, age, gender
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

RELATION_LABELS = {"F": "Father", "H": "Husband", "M": "Mother", "O": "Other/Guardian"}


def _records_to_rows(records):
    return [
        (
            r.state, r.roll_year, r.district, r.ac_code, r.ac_name,
            r.part_no, r.serial_no, r.local_ref, r.full_name,
            r.full_relative_name, r.relation_code,
            RELATION_LABELS.get(r.relation_code, r.relation_code),
            r.age, r.gender,
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
    ac = Constituency(ac_code=ac_code, ac_name="", district="")

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
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    csv_paths = sorted(glob.glob(os.path.join(raw_dir, "*.csv")))
    total = 0
    for path in csv_paths:
        ac_code = os.path.splitext(os.path.basename(path))[0]
        ac = Constituency(ac_code=ac_code, ac_name="", district="")
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


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--combine":
        build_combined(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3:
        build_single(sys.argv[1], sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
