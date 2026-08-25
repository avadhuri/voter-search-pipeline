import sqlite3

import pytest

import build_db
from states.base import Constituency, StateConnector, VoterRecord
from states.karnataka import CSV_URL_TEMPLATE, KarnatakaConnector

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

    # Karnataka rows get the per-AC CSV URL (no per-part granularity exists),
    # same for every row of the AC regardless of part_no/serial_no.
    source_urls = {
        r[0] for r in conn.execute("SELECT source_url FROM voters")
    }
    assert source_urls == {CSV_URL_TEMPLATE.format(ac_code="A085")}
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
        "SELECT state_id, label, acs_total, acs_digitized, locality_coverage, roll_year "
        "FROM state_coverage WHERE state_id = 'karnataka'"
    ).fetchone()
    assert row is not None
    state_id, label, acs_total, acs_digitized, locality_coverage, roll_year = row
    assert label == "Karnataka"
    # Resolved per state now rather than hardcoded -- Karnataka genuinely is a
    # 2002 roll, so this asserts the resolution agrees with the old constant.
    # See tests/test_roll_years.py for the non-2002 path.
    assert roll_year == 2002
    assert acs_digitized == 2
    assert acs_total >= acs_digitized
    # Karnataka's source CSVs carry no locality column (see VoterRecord.locality
    # docstring / CLAUDE.md's "Karnataka's locality gap") -- confirms the "none"
    # branch, not just the "full" happy path.
    assert locality_coverage == "none"
    conn.close()


def test_build_per_ac_matches_combined(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    (raw_dir / "A012.csv").write_text(A012_ROWS)

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

    combined_path = tmp_path / "combined.sqlite"
    build_db.build_multi_state(["karnataka"], str(combined_path))
    combined_conn = sqlite3.connect(combined_path)
    combined_total = combined_conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    combined_conn.close()

    out_dir = tmp_path / "per_ac"
    build_db.build_per_ac(["karnataka"], str(out_dir), contract="c1", patch=0)

    a085_path = out_dir / "karnataka" / "A085-c1.p0.sqlite"
    a012_path = out_dir / "karnataka" / "A012-c1.p0.sqlite"
    assert a085_path.exists()
    assert a012_path.exists()

    per_ac_total = 0
    for path, expected_ac in ((a085_path, "A085"), (a012_path, "A012")):
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT ac_code, source_url FROM voters").fetchall()
        assert all(r[0] == expected_ac for r in rows)
        assert all(r[1] == CSV_URL_TEMPLATE.format(ac_code=expected_ac) for r in rows)
        per_ac_total += len(rows)
        # voters_fts is confirmed dead weight for per-AC files -- shouldn't exist.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "voters_fts" not in tables
        conn.close()

    assert per_ac_total == combined_total == 3

    catalog_path = out_dir / "catalog" / "karnataka.sqlite"
    assert catalog_path.exists()
    cat_conn = sqlite3.connect(catalog_path)
    coverage_row = cat_conn.execute(
        "SELECT acs_digitized, locality_coverage FROM state_coverage WHERE state_id = 'karnataka'"
    ).fetchone()
    assert coverage_row == (2, "none")

    ac_index_rows = {
        row[0]: row for row in cat_conn.execute(
            "SELECT ac_code, contract, patch, row_count, has_locality FROM ac_index"
        )
    }
    assert ac_index_rows["A012"] == ("A012", "c1", 0, 1, 0)
    assert ac_index_rows["A085"] == ("A085", "c1", 0, 2, 0)

    # Karnataka's source CSVs carry no locality column -- catalog_locality
    # table must exist (schema always created) but stay empty here.
    locality_count = cat_conn.execute("SELECT COUNT(*) FROM catalog_locality").fetchone()[0]
    assert locality_count == 0
    cat_conn.close()


class _FakeLocalityConnector(StateConnector):
    """Minimal connector with locality data, for exercising catalog_locality's
    happy path -- Karnataka's real fixtures have no locality column to test with."""

    state_id = "fakestate"

    def list_constituencies(self):
        return [Constituency(ac_code="F001", ac_name="Fake AC", district="Fake District")]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        return [
            VoterRecord(
                state=self.state_id, district=ac.district, ac_code=ac.ac_code,
                ac_name=ac.ac_name, part_no=1, serial_no=1, local_ref="",
                full_name="Test Person", full_relative_name="Test Relative",
                relation_code="F", age=30, gender="M", roll_year=roll_year,
                locality="Fake Village",
            ),
            VoterRecord(
                state=self.state_id, district=ac.district, ac_code=ac.ac_code,
                ac_name=ac.ac_name, part_no=1, serial_no=2, local_ref="",
                full_name="Another Person", full_relative_name="Another Relative",
                relation_code="H", age=40, gender="F", roll_year=roll_year,
                locality="Fake Village",
            ),
        ]


def test_build_per_ac_populates_catalog_locality(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "F001.csv").write_text("placeholder\n")

    monkeypatch.setitem(
        build_db.STATE_CONNECTORS,
        "fakestate",
        {
            "connector_cls": _FakeLocalityConnector,
            "label": "Fake State",
            "raw_dir": str(raw_dir),
            "raw_glob": "*.csv",
            "script": "latin",
        },
    )

    out_dir = tmp_path / "per_ac"
    build_db.build_per_ac(["fakestate"], str(out_dir), contract="c1", patch=0)

    catalog_path = out_dir / "catalog" / "fakestate.sqlite"
    cat_conn = sqlite3.connect(catalog_path)

    coverage_row = cat_conn.execute(
        "SELECT locality_coverage FROM state_coverage WHERE state_id = 'fakestate'"
    ).fetchone()
    assert coverage_row == ("full",)

    has_locality = cat_conn.execute(
        "SELECT has_locality FROM ac_index WHERE ac_code = 'F001'"
    ).fetchone()
    assert has_locality == (1,)

    localities = {
        row[0] for row in cat_conn.execute(
            "SELECT locality FROM catalog_locality WHERE state = 'fakestate' AND ac_code = 'F001'"
        )
    }
    assert localities == {"Fake Village"}
    cat_conn.close()


# --- meta/raw-file disagreements stop the build instead of degrading ------

class _DupeMetaConnector(StateConnector):
    """Mimics a generated meta file that lists the same AC more than once --
    the real instance had 44 entries for 32 distinct ac_no values."""

    def list_constituencies(self):
        return [
            Constituency(ac_code="1", ac_name="First", district="D1"),
            Constituency(ac_code="2", ac_name="Second", district="D2"),
            Constituency(ac_code="1", ac_name="First Again", district="D9"),
        ]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        return []


class _TwoACConnector(StateConnector):
    def list_constituencies(self):
        return [Constituency(ac_code="F001", ac_name="First", district="D1")]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        return []


def _register(monkeypatch, state_id, cls, raw_dir):
    monkeypatch.setitem(build_db.STATE_CONNECTORS, state_id, {
        "connector_cls": cls,
        "label": state_id.title(),
        "raw_dir": str(raw_dir),
        "raw_glob": "*.csv",
        "script": "latin",
    })


def test_a_meta_declaring_one_ac_twice_stops_the_build(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "1.csv").write_text("placeholder\n")
    _register(monkeypatch, "dupestate", _DupeMetaConnector, raw_dir)

    with pytest.raises(build_db.DuplicateConstituencyError) as exc:
        build_db.build_multi_state(["dupestate"], str(tmp_path / "out.sqlite"))
    # Names the offending code and how many times, so the fix doesn't need
    # a diff of the meta file to locate.
    assert "1 x2" in str(exc.value)


def test_a_raw_file_for_an_undeclared_ac_stops_the_build(tmp_path, monkeypatch):
    """Used to silently produce an AC with blank district and ac_name --
    unreachable in the picker (district is its primary tier) and
    unrecognizable once reached, with every search-quality check green."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "F001.csv").write_text("placeholder\n")
    (raw_dir / "F999.csv").write_text("placeholder\n")
    _register(monkeypatch, "strayfile", _TwoACConnector, raw_dir)

    with pytest.raises(build_db.UnknownConstituencyError) as exc:
        build_db.build_multi_state(["strayfile"], str(tmp_path / "out.sqlite"))
    assert "F999" in str(exc.value)
    assert "strayfile" in str(exc.value)


def test_the_same_two_guards_apply_on_the_per_ac_path(tmp_path, monkeypatch):
    """--per-ac is the production build path; the guards are worth nothing
    if they only cover the combined one."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "F999.csv").write_text("placeholder\n")
    _register(monkeypatch, "strayfile", _TwoACConnector, raw_dir)
    with pytest.raises(build_db.UnknownConstituencyError):
        build_db.build_per_ac(["strayfile"], str(tmp_path / "per_ac"), workers=1)

    dupe_raw = tmp_path / "raw2"
    dupe_raw.mkdir()
    (dupe_raw / "1.csv").write_text("placeholder\n")
    _register(monkeypatch, "dupestate", _DupeMetaConnector, dupe_raw)
    with pytest.raises(build_db.DuplicateConstituencyError):
        build_db.build_per_ac(["dupestate"], str(tmp_path / "per_ac2"), workers=1)
