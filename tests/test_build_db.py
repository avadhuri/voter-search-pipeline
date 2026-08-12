import sqlite3

import build_db
from states.karnataka import KarnatakaConnector

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


def test_build_multi_state_populates_coverage(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    (raw_dir / "A012.csv").write_text(A012_ROWS)
    db_path = tmp_path / "multi.sqlite"

    monkeypatch.setitem(
        build_db.STATE_CONNECTORS,
        "karnataka",
        {
            "connector_cls": KarnatakaConnector,
            "label": "Karnataka",
            "raw_dir": str(raw_dir),
            "raw_glob": "*.csv",
            "script": "latin",
        },
    )

    build_db.build_multi_state(["karnataka"], str(db_path))

    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    assert total == 3

    row = conn.execute(
        "SELECT state_id, label, acs_total, acs_digitized, locality_coverage "
        "FROM state_coverage WHERE state_id = 'karnataka'"
    ).fetchone()
    assert row is not None
    state_id, label, acs_total, acs_digitized, locality_coverage = row
    assert label == "Karnataka"
    assert acs_digitized == 2
    assert acs_total >= acs_digitized
    # Karnataka's source CSVs carry no locality column (see VoterRecord.locality
    # docstring / CLAUDE.md's "Karnataka's locality gap") -- confirms the "none"
    # branch, not just the "full" happy path.
    assert locality_coverage == "none"
    conn.close()
