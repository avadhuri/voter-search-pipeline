import sqlite3

import pytest

import build_db
from states.base import (
    Constituency,
    StateConnector,
    UnparseableRollError,
    VoterRecord,
)
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


# --- the legacy single-state paths take a state, not a hardcoded one -------

def test_build_single_and_combined_default_to_karnataka():
    """The default is the whole reason these two functions could go a decade
    without naming a state: it's the state they always meant. Assert it is
    what the constant says, so changing the constant is a deliberate act
    rather than something the tests above would silently absorb."""
    assert build_db.DEFAULT_STATE_ID == "karnataka"


def test_build_single_builds_a_non_default_state(tmp_path, monkeypatch):
    """Neither legacy path does anything Karnataka-specific -- parse_raw()
    takes bytes and the connector decides what they are -- so passing a
    state_id must actually reach that state's connector, meta and roll year.
    Stubbed rather than driven off a real Haryana ZIP: what's under test is
    the dispatch, and the connector tests already cover real parsing."""
    seen = {}

    class StubConnector(StateConnector):
        def list_constituencies(self):
            return [Constituency(ac_code="XX01", ac_name="Stubbed", district="Nowhere")]

        def fetch_raw(self, ac, out_dir):  # pragma: no cover - unused here
            raise NotImplementedError

        def parse_raw(self, raw, ac, roll_year):
            seen["ac"] = ac
            seen["roll_year"] = roll_year
            return [
                VoterRecord(
                    state="stubland", district=ac.district, ac_code=ac.ac_code,
                    ac_name=ac.ac_name, part_no=1, serial_no=1, local_ref="1",
                    full_name="Stub Voter", full_relative_name="Stub Relative",
                    relation_code="F", age=40, gender="M", roll_year=roll_year,
                )
            ]

    monkeypatch.setitem(
        build_db.STATE_CONNECTORS, "stubland",
        {"connector_cls": StubConnector, "label": "Stubland",
         "raw_dir": str(tmp_path), "raw_glob": "*.zip", "script": "latin"},
    )
    raw = tmp_path / "XX01.zip"
    raw.write_bytes(b"stub-bytes")
    db_path = tmp_path / "XX01.sqlite"

    build_db.build_single(str(raw), str(db_path), state_id="stubland")

    assert seen["ac"].ac_code == "XX01"
    assert seen["ac"].district == "Nowhere"  # resolved via that state's meta
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM voters").fetchone()[0] == "stubland"
    conn.close()


def test_build_combined_globs_by_the_states_own_raw_glob(tmp_path, monkeypatch):
    """The old literal `*.csv` would have matched nothing for a ZIP state and
    reported "0 ACs" rather than saying the glob was wrong for it."""
    class StubConnector(StateConnector):
        def list_constituencies(self):
            return [Constituency(ac_code="XX01", ac_name="Stubbed", district="Nowhere")]

        def fetch_raw(self, ac, out_dir):  # pragma: no cover - unused here
            raise NotImplementedError

        def parse_raw(self, raw, ac, roll_year):
            return [
                VoterRecord(
                    state="stubland", district=ac.district, ac_code=ac.ac_code,
                    ac_name=ac.ac_name, part_no=1, serial_no=1, local_ref="1",
                    full_name="Stub Voter", full_relative_name="Stub Relative",
                    relation_code="F", age=40, gender="M", roll_year=roll_year,
                )
            ]

    monkeypatch.setitem(
        build_db.STATE_CONNECTORS, "stubland",
        {"connector_cls": StubConnector, "label": "Stubland",
         "raw_dir": str(tmp_path), "raw_glob": "*.zip", "script": "latin"},
    )
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "XX01.zip").write_bytes(b"stub-bytes")
    (raw_dir / "notes.csv").write_text("ignored\n")  # the old glob's only match
    db_path = tmp_path / "combined.sqlite"

    build_db.build_combined(str(raw_dir), str(db_path), state_id="stubland")

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 1
    conn.close()


def test_an_unknown_state_is_named_rather_than_crashing_on_a_key_error():
    with pytest.raises(SystemExit, match="Unknown state: atlantis"):
        build_db._state_connector("atlantis")


def test_a_republished_patch_leaves_the_catalog_pointing_at_a_file_that_exists(
    tmp_path, monkeypatch
):
    """Rebuilding an already-published state bumps --patch, and the catalog
    has to move with the filename.

    The serving app builds each AC's filename from the catalog's `patch`
    column and does not fall back to a lower patch, so a catalog and a set
    of files that disagree about the patch is not a degraded state -- it is
    a 404 on every AC of that state. `make build-db-ac` used to hardcode
    patch 0 while the live catalogs were at p1, which is exactly this
    disagreement produced by the supported path; the flag existed on
    build_db.py all along and only the Makefile didn't pass it. Asserted at
    a non-zero patch because every other test here builds at 0, which is
    the one value where a dropped patch argument still happens to agree.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "F001.csv").write_text("placeholder\n")
    _register(monkeypatch, "fakestate", _FakeLocalityConnector, raw_dir)

    out_dir = tmp_path / "per_ac"
    build_db.build_per_ac(["fakestate"], str(out_dir), contract="c1", patch=2)

    cat_conn = sqlite3.connect(out_dir / "catalog" / "fakestate.sqlite")
    try:
        rows = cat_conn.execute(
            "SELECT ac_code, contract, patch FROM ac_index"
        ).fetchall()
    finally:
        cat_conn.close()

    assert rows, "the build produced no catalog entries at all"
    for ac_code, contract, patch in rows:
        assert patch == 2
        named = out_dir / "fakestate" / f"{ac_code}-{contract}.p{patch}.sqlite"
        assert named.exists(), f"catalog names {named.name}, which was not built"


# --- ACs a connector declares it cannot parse -------------------------------
#
# Some states publish a minority of their ACs as page scans with no text
# layer. A connector that refuses to guess is behaving correctly, and the
# build has to treat those ACs as absent rather than as a fault. It did not:
# one scanned Haryana AC aborted a two-state per-AC build after 44 ACs had
# been written, discarding West Bengal (not yet started) and every catalog
# (written last).


class _ScannedACConnector(StateConnector):
    """Parses F001 and refuses F002, the way haryana.py refuses a scan."""

    state_id = "scanstate"

    def list_constituencies(self):
        return [
            Constituency(ac_code="F001", ac_name="Readable", district="D1"),
            Constituency(ac_code="F002", ac_name="Scanned", district="D1"),
        ]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        if ac.ac_code == "F002":
            raise UnparseableRollError(
                f"{ac.ac_code} ({ac.ac_name}) is published as page scans with no "
                f"usable text layer"
            )
        return [
            VoterRecord(
                state=self.state_id, district=ac.district, ac_code=ac.ac_code,
                ac_name=ac.ac_name, part_no=1, serial_no=1, local_ref="",
                full_name="Readable Person", full_relative_name="Readable Relative",
                relation_code="F", age=30, gender="M", roll_year=roll_year,
                locality="Village One",
            ),
        ]


class _AllScannedConnector(_ScannedACConnector):
    state_id = "allscanned"

    def parse_raw(self, raw, ac, roll_year):
        raise UnparseableRollError(f"{ac.ac_code} is a scan")


def _scan_raw(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "F001.csv").write_text("placeholder\n")
    (raw_dir / "F002.csv").write_text("placeholder\n")
    return raw_dir


def test_one_unparseable_ac_does_not_take_the_rest_of_the_build_with_it(
    tmp_path, monkeypatch
):
    raw_dir = _scan_raw(tmp_path)
    _register(monkeypatch, "scanstate", _ScannedACConnector, raw_dir)

    out_dir = tmp_path / "per_ac"
    build_db.build_per_ac(["scanstate"], str(out_dir), contract="c1", patch=0)

    # The readable AC is built and the scanned one simply is not there.
    assert (out_dir / "scanstate" / "F001-c1.p0.sqlite").exists()
    assert not (out_dir / "scanstate" / "F002-c1.p0.sqlite").exists()

    cat = sqlite3.connect(out_dir / "catalog" / "scanstate.sqlite")
    try:
        assert [r[0] for r in cat.execute("SELECT ac_code FROM ac_index")] == ["F001"]
        # acs_digitized counts ACs that were actually built. Counting the
        # scanned one would publish a state that looks more complete than it
        # is; locality_coverage "full" would be a claim about an AC with no
        # file at all.
        digitized, coverage = cat.execute(
            "SELECT acs_digitized, locality_coverage FROM state_coverage"
        ).fetchone()
        assert digitized == 1
        assert coverage == "full"
        # The invariant that actually broke in production: state_coverage's
        # headline count and ac_index -- the table the app searches -- have to
        # describe the same set of ACs. The live Haryana catalog advertised
        # "42 of 90 constituencies digitized" beside a voter total summed from
        # the 44 ACs its own ac_index holds, because acs_digitized counted raw
        # files rather than builds. app.py reads this field directly for the
        # per-state card, sums it for the site-wide constituency figure, and
        # gates a state's liveness on it being > 0, so a wrong value here is a
        # wrong public claim, not a cosmetic one.
        indexed = cat.execute("SELECT COUNT(*) FROM ac_index").fetchone()[0]
        assert digitized == indexed
    finally:
        cat.close()


def test_a_state_where_nothing_parses_stops_the_build(tmp_path, monkeypatch):
    """Skipping a minority is designed behaviour; skipping all of them means
    the connector or the raw files are broken. Publishing an empty catalog
    there would take the state off the site while every downstream check
    still reads green -- the same silent-success shape as the stale-data
    incident."""
    raw_dir = _scan_raw(tmp_path)
    _register(monkeypatch, "allscanned", _AllScannedConnector, raw_dir)

    with pytest.raises(UnparseableRollError) as exc:
        build_db.build_per_ac(["allscanned"], str(tmp_path / "out"), contract="c1", patch=0)
    assert "all 2 ACs" in str(exc.value)


def test_the_combined_path_skips_them_too(tmp_path, monkeypatch):
    """build_multi_state reads the same connectors and had the identical
    abort, so fixing only the per-AC path would leave `make build-db`
    holding the landmine."""
    raw_dir = _scan_raw(tmp_path)
    _register(monkeypatch, "scanstate", _ScannedACConnector, raw_dir)

    db_path = tmp_path / "combined.sqlite"
    build_db.build_multi_state(["scanstate"], str(db_path))

    conn = sqlite3.connect(db_path)
    try:
        assert [r[0] for r in conn.execute(
            "SELECT DISTINCT ac_code FROM voters")] == ["F001"]
        assert conn.execute(
            "SELECT acs_digitized FROM state_coverage").fetchone()[0] == 1
    finally:
        conn.close()


class _BrokenConnector(_ScannedACConnector):
    """A parser bug, not a declared scan. Module-level because the per-AC
    build runs connectors in a process pool, and a class defined inside a
    test function cannot be pickled across to a worker."""

    state_id = "brokenstate"

    def parse_raw(self, raw, ac, roll_year):
        raise ValueError("parser bug, not a scan")


def test_an_unexpected_error_still_stops_the_build(tmp_path, monkeypatch):
    """The skip is scoped to the one exception a connector raises to declare
    an AC unparseable. Broadening it to bare Exception would turn every real
    parser bug into a quietly smaller state."""
    raw_dir = _scan_raw(tmp_path)
    _register(monkeypatch, "brokenstate", _BrokenConnector, raw_dir)

    with pytest.raises(ValueError):
        build_db.build_per_ac(["brokenstate"], str(tmp_path / "out"), contract="c1", patch=0)


class _ThreeACConnector(StateConnector):
    """Three ACs whose raw files all sit in the same dir, one row each.

    Module-level because the per-AC build runs connectors in a process pool
    and a class defined inside a test function cannot be pickled across.
    """

    state_id = "scopestate"

    def list_constituencies(self):
        return [
            Constituency(ac_code="AC001", ac_name="One", district="D1"),
            Constituency(ac_code="AC002", ac_name="Two", district="D1"),
            Constituency(ac_code="AC003", ac_name="Three", district="D1"),
        ]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        return [VoterRecord(
            state=self.state_id, district=ac.district, ac_code=ac.ac_code,
            ac_name=ac.ac_name, part_no=1, serial_no=1, local_ref="",
            full_name=f"Person {ac.ac_code}", full_relative_name="Relative",
            relation_code="F", age=30, gender="M", roll_year=roll_year,
            locality="Village One",
        )]


def _scope_raw(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    for code in ("AC001", "AC002", "AC003"):
        (raw_dir / f"{code}.csv").write_text("placeholder\n")
    return raw_dir


def test_acs_restricts_the_per_ac_build_to_the_named_acs(tmp_path, monkeypatch):
    raw_dir = _scope_raw(tmp_path)
    _register(monkeypatch, "scopestate", _ThreeACConnector, raw_dir)
    out = tmp_path / "out"

    build_db.build_per_ac(["scopestate"], str(out), patch=1, workers=1,
                          ac_codes=["AC001", "AC003"])

    built = sorted(p.name for p in (out / "scopestate").glob("*.sqlite"))
    assert built == ["AC001-c1.p1.sqlite", "AC003-c1.p1.sqlite"]
    # The catalog has to agree with the files, not with the connector's
    # full list of constituencies -- the app fetches what ac_index names.
    cat = sqlite3.connect(str(out / "catalog" / "scopestate.sqlite"))
    assert [r[0] for r in cat.execute(
        "SELECT ac_code FROM ac_index ORDER BY ac_code")] == ["AC001", "AC003"]
    assert cat.execute(
        "SELECT acs_digitized FROM state_coverage").fetchone()[0] == 2
    cat.close()


def test_an_ac_with_no_raw_file_stops_the_build(tmp_path, monkeypatch):
    raw_dir = _scope_raw(tmp_path)
    _register(monkeypatch, "scopestate", _ThreeACConnector, raw_dir)

    # Deliberately fatal, not skipped: a typo'd or stale AC list that quietly
    # builds fewer ACs than asked for is indistinguishable, downstream, from
    # a state that genuinely is that small.
    with pytest.raises(SystemExit) as exc:
        build_db.build_per_ac(["scopestate"], str(tmp_path / "out"),
                              workers=1, ac_codes=["AC001", "AC999"])
    assert "AC999" in str(exc.value)
    assert "AC001" not in str(exc.value)
    assert str(raw_dir) in str(exc.value)


def test_the_combined_path_scopes_the_same_way(tmp_path, monkeypatch):
    raw_dir = _scope_raw(tmp_path)
    _register(monkeypatch, "scopestate", _ThreeACConnector, raw_dir)
    db = tmp_path / "combined.sqlite"

    build_db.build_multi_state(["scopestate"], str(db), ac_codes=["AC002"])

    conn = sqlite3.connect(str(db))
    assert [r[0] for r in conn.execute(
        "SELECT DISTINCT ac_code FROM voters")] == ["AC002"]
    conn.close()


def test_without_acs_the_scope_is_every_raw_file(tmp_path, monkeypatch):
    raw_dir = _scope_raw(tmp_path)
    _register(monkeypatch, "scopestate", _ThreeACConnector, raw_dir)
    out = tmp_path / "out"

    build_db.build_per_ac(["scopestate"], str(out), workers=1)

    assert len(list((out / "scopestate").glob("*.sqlite"))) == 3


def test_ac_codes_are_matched_case_and_whitespace_insensitively(tmp_path, monkeypatch):
    # The list arrives off a comma-split of a make variable, so " ac001 "
    # is a realistic input, not a contrived one.
    raw_dir = _scope_raw(tmp_path)
    _register(monkeypatch, "scopestate", _ThreeACConnector, raw_dir)
    out = tmp_path / "out"

    build_db.build_per_ac(["scopestate"], str(out), workers=1,
                          ac_codes=[" ac001 ", "Ac002", ""])

    built = sorted(p.name for p in (out / "scopestate").glob("*.sqlite"))
    assert built == ["AC001-c1.p0.sqlite", "AC002-c1.p0.sqlite"]


def _karnataka_two_ac_raw(tmp_path, monkeypatch):
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
    return raw_dir


def test_scoped_rebuild_refuses_to_drop_acs_the_catalog_already_serves(tmp_path, monkeypatch):
    """The failure this guards is invisible everywhere else.

    A catalog is rewritten from its run's results alone, so building only the
    ACs you are adding silently un-serves every AC you left out -- with a
    clean build log, correct row counts, a green search-quality suite (it
    drives explicit (state, ac_code) pairs) and freshness guards that see
    nothing stale, because what remains genuinely isn't. Only the count of
    what is covered is wrong, which is a public claim, not bookkeeping.
    """
    _karnataka_two_ac_raw(tmp_path, monkeypatch)
    out_dir = tmp_path / "per_ac"

    build_db.build_per_ac(["karnataka"], str(out_dir), contract="c1", patch=0)
    cat = out_dir / "catalog" / "karnataka.sqlite"
    conn = sqlite3.connect(cat)
    assert {r[0] for r in conn.execute("SELECT ac_code FROM ac_index")} == {"A012", "A085"}
    conn.close()

    with pytest.raises(SystemExit) as excinfo:
        build_db.build_per_ac(
            ["karnataka"], str(out_dir), contract="c1", patch=1, ac_codes=["A085"]
        )
    message = str(excinfo.value)
    assert "A012" in message, "the refusal must name what it would have dropped"
    assert "--allow-catalog-shrink" in message

    # ...and the catalog it refused to write is untouched, so the state is
    # still fully served after the aborted run.
    conn = sqlite3.connect(cat)
    assert {r[0] for r in conn.execute("SELECT ac_code FROM ac_index")} == {"A012", "A085"}
    conn.close()


def test_full_scope_rebuild_reindexes_rather_than_reparses(tmp_path, monkeypatch):
    """Passing every AC at the current patch is the supported way to extend a
    published state: the already-built files are detected complete and skipped,
    so the cost is a COUNT(*) each, and the catalog still comes out whole."""
    _karnataka_two_ac_raw(tmp_path, monkeypatch)
    out_dir = tmp_path / "per_ac"
    build_db.build_per_ac(["karnataka"], str(out_dir), contract="c1", patch=0)

    a012 = out_dir / "karnataka" / "A012-c1.p0.sqlite"
    before = a012.stat().st_mtime_ns

    build_db.build_per_ac(
        ["karnataka"], str(out_dir), contract="c1", patch=0, ac_codes=["A085", "A012"]
    )

    assert a012.stat().st_mtime_ns == before, "an already-built AC must not be rewritten"
    conn = sqlite3.connect(out_dir / "catalog" / "karnataka.sqlite")
    assert {r[0] for r in conn.execute("SELECT ac_code FROM ac_index")} == {"A012", "A085"}
    assert conn.execute(
        "SELECT acs_digitized FROM state_coverage WHERE state_id = 'karnataka'"
    ).fetchone()[0] == 2
    conn.close()


def test_allow_catalog_shrink_lets_a_deliberate_narrowing_through(tmp_path, monkeypatch):
    _karnataka_two_ac_raw(tmp_path, monkeypatch)
    out_dir = tmp_path / "per_ac"
    build_db.build_per_ac(["karnataka"], str(out_dir), contract="c1", patch=0)

    build_db.build_per_ac(
        ["karnataka"], str(out_dir), contract="c1", patch=1,
        ac_codes=["A085"], allow_catalog_shrink=True,
    )
    conn = sqlite3.connect(out_dir / "catalog" / "karnataka.sqlite")
    assert {r[0] for r in conn.execute("SELECT ac_code FROM ac_index")} == {"A085"}
    conn.close()
