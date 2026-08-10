import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_db
from cross_reference import find_candidates

SOURCE_2002 = "BANGALORE URBAN,A085,Shivajinagar,5,201,,Shivaram,,Ramaiah,,F,20,M\r\n"


def _build_db_with_2025_rows(tmp_path, rows_2025):
    raw_csv = tmp_path / "A085.csv"
    raw_csv.write_text(SOURCE_2002)
    db_path = tmp_path / "A085.sqlite"
    build_db.build_single(str(raw_csv), str(db_path), roll_year=2002)

    conn = sqlite3.connect(db_path)
    conn.executemany(build_db.INSERT_SQL, rows_2025)
    conn.commit()
    conn.close()
    return db_path


def _row_2025(full_name, full_relative_name, age, gender="M", ac_code="A085"):
    return (
        "karnataka", 2025, "BANGALORE URBAN", ac_code, "Shivajinagar",
        5, 999, "", full_name, full_relative_name, "F", "Father", age, gender,
    )


def test_no_current_roll_data_gives_helpful_note(tmp_path):
    db_path = _build_db_with_2025_rows(tmp_path, [])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    source = conn.execute("SELECT * FROM voters WHERE roll_year = 2002").fetchone()

    candidates, note = find_candidates(conn, source, target_roll_year=2025, algorithm="wratio")

    assert candidates == []
    assert "No 2025 roll data loaded" in note
    conn.close()


def test_finds_aged_up_match(tmp_path):
    # source is age 20 in 2002 -> expect ~43 in 2025 (23 years later)
    db_path = _build_db_with_2025_rows(tmp_path, [
        _row_2025("Shivaram", "Ramaiah", age=43),      # should match
        _row_2025("Someone Else", "Nobody", age=43),   # should not match (name)
        _row_2025("Shivaram", "Ramaiah", age=70),       # should not match (age out of range)
    ])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    source = conn.execute("SELECT * FROM voters WHERE roll_year = 2002").fetchone()

    candidates, note = find_candidates(conn, source, target_roll_year=2025, algorithm="wratio")

    assert note is None
    assert len(candidates) == 1
    assert candidates[0][1]["full_name"] == "Shivaram"
    conn.close()
