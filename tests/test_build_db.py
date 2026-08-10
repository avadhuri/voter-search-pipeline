import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_db

A085_ROWS = (
    "BANGALORE URBAN,A085,Shivajinagar,5,201,,Shivaram,,Ramaiah,,F,45,M\r\n"
    "BANGALORE URBAN,A085,Shivajinagar,5,202,,Lakshmi,,Shivaram,,H,38,F\r\n"
)
A012_ROWS = (
    "MYSORE,A012,Mysore North,1,1,,Ravi,,Kumar,,F,30,M\r\n"
)


def test_build_single(tmp_path):
    raw_csv = tmp_path / "A085.csv"
    raw_csv.write_text(A085_ROWS)
    db_path = tmp_path / "A085.sqlite"

    build_db.build_single(str(raw_csv), str(db_path))

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT * FROM voters ORDER BY serial_no").fetchall()
    assert len(rows) == 2
    fts_count = conn.execute("SELECT COUNT(*) FROM voters_fts").fetchone()[0]
    assert fts_count == 2
    conn.close()


def test_build_combined(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    (raw_dir / "A012.csv").write_text(A012_ROWS)
    db_path = tmp_path / "combined.sqlite"

    build_db.build_combined(str(raw_dir), str(db_path))

    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    assert total == 3
    ac_codes = {r[0] for r in conn.execute("SELECT DISTINCT ac_code FROM voters")}
    assert ac_codes == {"A085", "A012"}
    conn.close()
