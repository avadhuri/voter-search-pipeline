"""
The servability tripwires, each driven by the exact breakage it exists for.

`check_servable.py` is the only check in this repo that answers "would the
serving app show this state correctly?" rather than "did the build finish?".
Every failure it looks for shares one property: the build is completely
normal downstream. Right row count, names that parse, searches that score,
every search-quality assertion green -- and the state is wrong or
unreachable in a way a user reads as an answer rather than as a bug.

So each test here *produces* that breakage in a real built DB and asserts
the checker names it. A test that only asserted "clean data passes" would
have passed against every one of the four gaps this round of work found.
"""
import sqlite3

import pytest

import build_db
import check_servable
from states.base import Constituency, StateConnector, VoterRecord


class _Connector(StateConnector):
    """One AC, two rows, all fields good. Each test breaks one thing.

    Class attributes rather than constructor arguments because build_per_ac
    fans parsing across a process pool, which pickles the class.
    """

    state_id = "faketest"
    script = "latin"
    overrides = {}

    def list_constituencies(self):
        return [Constituency(ac_code="A001", ac_name="Fake AC", district="Fake District")]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        base = dict(
            state=self.state_id, district=ac.district, ac_code=ac.ac_code,
            ac_name=ac.ac_name, part_no=1, local_ref="", relation_code="F",
            age=30, gender="M", roll_year=roll_year,
            full_name="Ramesh Kumar", full_relative_name="Suresh Kumar",
        )
        base.update(self.overrides)
        return [
            VoterRecord(serial_no=1, **base),
            VoterRecord(serial_no=2, **base),
        ]


def _build(tmp_path, monkeypatch, script="latin", roll_year=None, source_url=True, **overrides):
    # faketest has no states/eci_codes.py entry, so resolve_source_url()
    # correctly returns "" for it. Stubbed here so every other test starts
    # from a clean build rather than a permanent source_url blocker; the one
    # test that cares about that blocker passes source_url=False.
    if source_url:
        monkeypatch.setattr(
            build_db, "resolve_source_url", lambda r: "https://example.invalid/roll.pdf")
    monkeypatch.setattr(_Connector, "overrides", overrides)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A001.csv").write_text("placeholder\n")
    monkeypatch.setitem(build_db.STATE_CONNECTORS, "faketest", {
        "connector_cls": _Connector,
        "label": "Faketest",
        "raw_dir": str(raw_dir),
        "raw_glob": "*.csv",
        "script": script,
    })
    db_path = tmp_path / "out.sqlite"
    build_db.build_multi_state(["faketest"], str(db_path), roll_year=roll_year)
    return str(db_path)


def _findings(path, level=None):
    found = check_servable.check(path)
    return [f for f in found if level is None or f.level == level]


def _messages(path, level):
    return " | ".join(f.message for f in _findings(path, level))


# --- the baseline: a good build says nothing alarming ----------------------

def test_a_clean_build_has_no_blockers(tmp_path, monkeypatch):
    path = _build(tmp_path, monkeypatch)
    assert _findings(path, "BLOCKER") == []
    assert check_servable.main([path]) == 0


# --- roll year -------------------------------------------------------------

def test_a_roll_year_outside_the_revision_cycle_blocks(tmp_path, monkeypatch):
    """The app derives year of birth as roll_year - age and its
    year-of-birth field is required, so a wrong year mis-targets every
    elector in the state -- and reads to them as "you were never on the
    roll"."""
    path = _build(tmp_path, monkeypatch, roll_year=2025)
    assert "2002-2006" in _messages(path, "BLOCKER")


def test_a_roll_year_disagreeing_with_the_metadata_warns_rather_than_blocks(tmp_path, monkeypatch):
    """--roll-year is a real escape hatch and a state can genuinely be
    re-rolled, so this isn't a blocker -- but roll_year is the most silent
    field in the schema and never passes unremarked."""
    path = _build(tmp_path, monkeypatch, roll_year=2005)
    assert _findings(path, "BLOCKER") == []
    assert "roll_years.py resolves" in _messages(path, "WARNING")


# --- the picker's two required labels --------------------------------------

@pytest.mark.parametrize("column", ["district", "ac_name"])
def test_a_blank_picker_label_blocks(tmp_path, monkeypatch, column):
    """district is the picker's primary tier: blank means an AC no user can
    navigate to. A blank ac_name means one nobody recognizes once reached.
    Both were silently possible until build_db's _resolve_ac started
    raising, and neither is visible to a search-quality check, which drives
    searches by explicit (state, ac_code)."""
    path = _build(tmp_path, monkeypatch, **{column: ""})
    message = _messages(path, "BLOCKER")
    assert f"no {column}" in message
    # Names offending rows so the fix doesn't need a query to locate.
    assert "A001/p1/s1" in message


# --- verifiability ---------------------------------------------------------

def test_a_missing_source_url_blocks_and_says_where_to_declare_it(tmp_path, monkeypatch):
    """This one is the whole reason the check exists: source_url would have
    come out empty for every incoming state, and a result a user can't check
    against the original page is a failure of the tool's entire purpose --
    with nothing about the build looking wrong."""
    # faketest isn't in eci_codes.STATE_CODES, which is exactly the real
    # condition -- a state nobody declared a code for.
    path = _build(tmp_path, monkeypatch, source_url=False)
    message = _messages(path, "BLOCKER")
    assert "no source_url" in message
    assert "states/eci_codes.py" in message


# --- the Latin bridge ------------------------------------------------------

def test_a_non_latin_state_with_empty_latin_columns_blocks(tmp_path, monkeypatch):
    """Rows that exist and match nothing anybody can type. The realistic way
    in is data built before the latin columns were populated at all -- an
    older per-AC file still sitting in the bucket, which is exactly the shape
    of the stale-data incident the freshness guards were added for."""
    path = _build(tmp_path, monkeypatch, script="devanagari", full_name="सुभाष")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE voters SET full_name_latin = ''")
    conn.commit()
    conn.close()
    assert "no full_name_latin" in _messages(path, "BLOCKER")


def test_a_script_with_no_romanization_scheme_at_all_is_reported(tmp_path, monkeypatch):
    """non_latin_state_ids() keys off script != "latin" rather than an
    allow-list precisely so this state is routed through the backfill. The
    backfill has no scheme for Meetei Mayek and passes the string through, so
    the latin column ends up a copy of the native one -- which the residue
    check catches instead of letting it look filled."""
    path = _build(tmp_path, monkeypatch, script="meetei_mayek", full_name="ꯔꯃꯦꯁ")
    assert _findings(path, "BLOCKER") == []
    assert "native-script characters" in _messages(path, "WARNING")


def test_a_latin_registered_state_carrying_latin_columns_warns(tmp_path, monkeypatch):
    """The other direction, and the likelier mistake: a connector that
    extracted both scripts but left script='latin' in the registry. Nothing
    fails -- script='latin' is exactly what stops the backfill running, so
    the state's other rows silently never get a Latin key."""
    path = _build(tmp_path, monkeypatch, full_name_latin="Ramesh Kumar")
    assert "registered script='latin'" in _messages(path, "WARNING")


def test_residue_from_an_incomplete_romanization_is_reported(tmp_path, monkeypatch):
    """Malayalam chillu forms and Tamil n have no mapping in any target
    scheme and survive into the Latin column as native characters. A warning
    rather than a blocker: the rows are matchable, just badly, and the fix
    is a connector-supplied romanization for that state."""
    path = _build(tmp_path, monkeypatch, script="malayalam", full_name="കൃഷ്ണൻ")
    message = _messages(path, "WARNING")
    assert "native-script characters" in message
    assert "ൻ" in message


# --- names and ages --------------------------------------------------------

def test_an_empty_name_warns_with_enough_detail_to_find_the_cluster(tmp_path, monkeypatch):
    """The real instance was 1,136 electors across two Haryana parts, 936 of
    which reached production unnoticed. Printing part/serial is what makes
    "is this one part or the whole AC?" answerable at a glance."""
    path = _build(tmp_path, monkeypatch, full_name="")
    message = _messages(path, "WARNING")
    assert "empty full_name" in message
    assert "p1/s1" in message


def test_an_unusable_age_is_reported_as_informational(tmp_path, monkeypatch):
    """The serving app deliberately spares age-less rows from its
    year-of-birth filter rather than hiding them, so this is a share to
    eyeball, not a failure."""
    path = _build(tmp_path, monkeypatch, age=0)
    assert _findings(path, "BLOCKER") == []
    assert "no usable age" in _messages(path, "WARNING")


# --- the CLI contract ------------------------------------------------------

def test_the_exit_code_is_what_a_makefile_can_gate_on(tmp_path, monkeypatch, capsys):
    path = _build(tmp_path, monkeypatch, district="")
    assert check_servable.main([path]) == 1
    assert "not servable as built" in capsys.readouterr().out


def test_a_per_ac_directory_is_checked_file_by_file(tmp_path, monkeypatch):
    """The per-AC layout is what the serving app actually fetches, and one
    bad AC among 44 has to be named rather than averaged away.

    The breakage is applied to the built file rather than to the connector
    because build_per_ac fans parsing across a process pool: a monkeypatched
    class attribute doesn't survive into the workers.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A001.csv").write_text("placeholder\n")
    monkeypatch.setitem(build_db.STATE_CONNECTORS, "faketest", {
        "connector_cls": _Connector, "label": "Faketest",
        "raw_dir": str(raw_dir), "raw_glob": "*.csv", "script": "latin",
    })
    out_dir = tmp_path / "ac"
    build_db.build_per_ac(["faketest"], str(out_dir), workers=1)

    ac_file = out_dir / "faketest" / "A001-c1.p0.sqlite"
    conn = sqlite3.connect(ac_file)
    conn.execute("UPDATE voters SET district = ''")
    conn.commit()
    conn.close()

    # catalog/ holds no voters table; walking into it must not be an error.
    assert (out_dir / "catalog" / "faketest.sqlite").exists()
    assert "no district" in _messages(str(out_dir), "BLOCKER")



# --- the sample view -------------------------------------------------------
#
# Not a check: whether a romanization is *name-shaped* is the one judgement
# in this script a human has to make. `geMdI kaura` for गेंदी कौर passes every
# assertion here -- it is a perfectly good fuzzy-match key and a name nobody
# would type or read. So these tests cover the properties that make the view
# trustworthy to look at, not the names themselves.

def _named_db(tmp_path, rows, with_latin=True):
    """A voters table built by hand, so a test can control every name.

    The shared _Connector emits two identical rows, which is right for the
    checks and useless for sampling.
    """
    path = str(tmp_path / "named.sqlite")
    conn = sqlite3.connect(path)
    latin_col = ", full_name_latin TEXT" if with_latin else ""
    conn.execute(f"CREATE TABLE voters (state TEXT, full_name TEXT{latin_col})")
    if with_latin:
        conn.executemany("INSERT INTO voters VALUES (?, ?, ?)",
                         [("faketest", n, l) for n, l in rows])
    else:
        conn.executemany("INSERT INTO voters VALUES (?, ?)",
                         [("faketest", n) for n, _ in rows])
    conn.commit()
    return path, conn


def test_the_sample_is_the_same_rows_for_everyone_who_runs_it(tmp_path):
    """The load-bearing property. A sample seeded from Python's own hash()
    would be a different set of rows in every process, so "the 100 rows I
    looked at" and "the 100 rows you looked at" would silently be different
    data -- which defeats the entire point of sending someone a sample."""
    rows = [(f"नाम{i}", f"naama{i}") for i in range(500)]
    path, conn = _named_db(tmp_path, rows)
    first = check_servable.sample_names(conn, "faketest", 20, check_servable._seeded("faketest"))
    second = check_servable.sample_names(conn, "faketest", 20, check_servable._seeded("faketest"))
    assert first == second
    assert len(first) == 20


def test_the_sample_is_drawn_from_across_the_state_not_off_the_front(tmp_path):
    """An extraction that degrades partway through -- a font boundary, a
    switch to scanned pages -- is invisible in a LIMIT 100, because the front
    of the file is the part that worked."""
    rows = [(f"नाम{i}", f"naama{i}") for i in range(1000)]
    path, conn = _named_db(tmp_path, rows)
    got = check_servable.sample_names(conn, "faketest", 40, check_servable._seeded("faketest"))
    indices = sorted(int(n.removeprefix("नाम")) for n, _ in got)
    assert max(indices) > 700, f"sample never reached the tail: {indices[-5:]}"


def test_a_file_predating_the_latin_columns_samples_nothing_rather_than_raising(tmp_path):
    """Same stale-per-AC-file case the checks handle: the column simply is
    not there, and querying one that isn't raises. A contributor pointing
    this at an older build should get an empty sample, not a traceback."""
    path, conn = _named_db(tmp_path, [("नाम", "naama")], with_latin=False)
    assert check_servable.sample_names(conn, "faketest", 10, check_servable._seeded("x")) == []


def test_a_name_the_scheme_could_not_romanize_is_marked_in_the_view(tmp_path, capsys):
    """Residue and an empty romanization are the two failures a reader would
    otherwise have to spot by eye, in a wall of 100 names."""
    path, _ = _named_db(tmp_path, [("गेंदी", "geMdI"), ("കൃഷ്ണൻ", "kRRiShNaൻ"), ("बीना", "")])
    check_servable.print_samples(path, {"faketest"}, 10)
    out = capsys.readouterr().out
    assert "residue: ൻ" in out
    assert "NO full_name_latin" in out
