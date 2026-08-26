# -*- coding: utf-8 -*-
"""The per-part nameless-name alarm.

Two real cases motivate this instrument, and the test that matters most here
is the one that would have gone the other way under the design we started
with. Both Haryana HR22 part 52 and West Bengal AC025 write an accurate,
specific remark on every row whose name they lost -- so a check that skipped
"explained" blanks would have been silent on both of the cases it exists to
catch. The discriminator is not whether the connector said something; it is
whether there is a name on the page to recover.

Only the Haryana one is a bug. West Bengal's is a documented refusal to run
Darjeeling's Devanagari parts through a Bengali glyph table, which is the
connector behaving correctly -- and the rows are just as nameless, just as
served, and just as counted. The alarm does not distinguish them, on purpose.
"""
import collections
import sqlite3

import pytest

import build_db
from states.base import (NAME_ABSENT_IN_SOURCE, NAME_UNREAD, Constituency,
                         StateConnector, VoterRecord)


# The two remarks the real cases actually carry, verbatim from the built data
# and from states/haryana.py. Kept as literals so these tests keep meaning
# what they say even if either connector's wording changes.
HARYANA_REMARK = "no voter name in row"
WEST_BENGAL_REMARK = (
    "name in an unrecognized Bengali-script font: no glyph table for it, so "
    "the name columns are not decodable"
)


def _rec(name="রমেশ মণ্ডল", part_no=1, remark="", **kw):
    return VoterRecord(
        state="west_bengal", district="D", ac_code="AC025", ac_name="N",
        part_no=part_no, serial_no=1, local_ref="", full_name=name,
        full_relative_name="", relation_code="F", age=40, gender="M",
        roll_year=2002, remark=remark, **kw)


def _part(rows, nameless, part_no=1, **kw):
    """One part's worth of records: `nameless` of `rows` have no name."""
    return ([_rec(name="", part_no=part_no, **kw) for _ in range(nameless)]
            + [_rec(part_no=part_no) for _ in range(rows - nameless)])


def test_an_accurate_remark_does_not_excuse_an_unread_name():
    """The whole reason this alarm does not look at `remark`.

    Both known cases explain themselves correctly and are still rows nobody
    can find. If this test ever fails because the alarm learned to read
    remarks, the two it was built for become invisible again.
    """
    for remark in (HARYANA_REMARK, WEST_BENGAL_REMARK):
        assert build_db._classify_name(_rec(name="", remark=remark)) == NAME_UNREAD


def test_an_undeclared_blank_counts_as_unread():
    """Silence does not buy silence -- the same rule as the decode census
    refusing to call zero scanned tokens a pass."""
    assert build_db._classify_name(_rec(name="")) == NAME_UNREAD


def test_a_name_declared_absent_in_the_source_is_not_counted_as_unread():
    rec = _rec(name="", name_absence=NAME_ABSENT_IN_SOURCE)
    assert build_db._classify_name(rec) == NAME_ABSENT_IN_SOURCE

    census = build_db._nameless_census(
        _part(400, 200, name_absence=NAME_ABSENT_IN_SOURCE))
    assert census["parts"][1][NAME_ABSENT_IN_SOURCE] == 200
    assert census["parts"][1][NAME_UNREAD] == 0
    assert build_db._nameless_alarms(census) == []


def test_a_declaration_on_a_row_that_has_a_name_is_ignored():
    """The field only ever explains an absence. A connector setting it on a
    row that did produce a name has not thereby made the name missing."""
    rec = _rec(name="রমেশ", name_absence=NAME_ABSENT_IN_SOURCE)
    assert build_db._classify_name(rec) == ""


@pytest.mark.parametrize("name", ["", "   ", "ঃঃ", "--", "...", "০৪"])
def test_a_name_with_no_letter_in_it_is_not_a_name(name):
    """vsp #35: 30 West Bengal rows carry the literal 'ঃঃ'. A placeholder
    evades every check a blank one trips -- it is non-empty, it has length,
    it occupies a row, and it is findable by nobody. '০৪' is Bengali digits,
    which are no more a name than Latin ones."""
    assert not build_db._is_usable_name(name)


@pytest.mark.parametrize("name", ["রমেশ মণ্ডল", "Shivaram", "राम", "ঃক"])
def test_a_real_name_survives_the_letter_test(name):
    """Including 'ঃক' -- one letter is enough. The test is for a name that is
    entirely punctuation, not for a name that contains any."""
    assert build_db._is_usable_name(name)


def test_a_part_trips_once_it_is_over_both_bars():
    census = build_db._nameless_census(_part(400, 200))
    alarms = build_db._nameless_alarms(census)
    assert alarms == [(1, 200, 400, 0.5)]


def test_a_handful_in_a_small_part_does_not_trip():
    """A 60-row part with 7 nameless rows is 11.7%, over the rate bar, and is
    noise. On the 22,941-part corpus this rule excludes exactly one part: a
    single-row part with a single nameless row."""
    census = build_db._nameless_census(_part(60, 7))
    assert census["parts"][1][NAME_UNREAD] == 7
    assert build_db._nameless_alarms(census) == []


def test_a_part_under_the_rate_does_not_trip_however_many_rows_it_has():
    census = build_db._nameless_census(_part(10_000, 500))   # 5%
    assert build_db._nameless_alarms(census) == []


def test_both_bars_are_needed_not_either():
    """Guards against the two bars being combined with `or`, which would make
    every large part trip on a handful and every tiny one trip on noise."""
    # over the rate bar, under the count bar
    assert build_db._nameless_alarms(build_db._nameless_census(_part(100, 19))) == []
    # over the count bar, under the rate bar
    assert build_db._nameless_alarms(build_db._nameless_census(_part(1000, 50))) == []


def test_an_unrecognized_declaration_is_counted_as_unread_and_reported(capsys):
    """A typo in a connector must not silently disarm the alarm for exactly
    the rows it was aimed at."""
    census = build_db._nameless_census(_part(400, 200, name_absence="sorce"))
    assert census["parts"][1][NAME_UNREAD] == 200
    assert census["unrecognized"] == collections.Counter({"sorce": 200})

    build_db._report_nameless([{"ac_code": "AC025", "nameless": census}])
    out = capsys.readouterr().out
    assert "'sorce'" in out
    assert "Counted as unread" in out


def test_the_report_names_the_part_and_its_rate(capsys):
    census = build_db._nameless_census(_part(828, 828))
    build_db._report_nameless([{"ac_code": "AC025", "nameless": census}])
    out = capsys.readouterr().out
    assert "AC025 part 1: 828/828 = 100.0% unread" in out
    assert "WARNING" in out


def test_the_report_stays_quiet_when_there_is_nothing_to_say(capsys):
    census = build_db._nameless_census(_part(1000, 0))
    build_db._report_nameless([{"ac_code": "AC025", "nameless": census}])
    assert capsys.readouterr().out == ""


def test_the_alarm_reports_and_never_raises():
    """"Report, don't drop." A data-quality heuristic that can abort a build
    is wrong -- the roll is what it is, and a build that refuses to finish
    leaves the site on older data over a judgement call. It is also what
    makes this change and a connector change land in either order without
    one breaking the other."""
    census = build_db._nameless_census(_part(828, 828))
    assert build_db._report_nameless([{"ac_code": "AC025", "nameless": census}]) is None


def test_an_unparseable_ac_contributes_nothing_rather_than_zero(capsys):
    """None, not an empty census: nothing was checked, and a zero would read
    as a clean result for an AC that never parsed."""
    build_db._report_nameless([{"ac_code": "AC038", "nameless": None}])
    assert capsys.readouterr().out == ""


def test_a_skipped_ac_is_read_back_from_its_file(tmp_path):
    """Without this, the alarm fires on a from-scratch build and is silent on
    every re-run -- which is when the output is read."""
    db = tmp_path / "AC025-c1.p0.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(build_db.VOTERS_SCHEMA)
    conn.executemany(
        build_db.INSERT_SQL,
        build_db._records_to_rows(_part(400, 200) + _part(400, 0, part_no=2)))
    conn.commit()

    census = build_db._nameless_census_from_db(conn)
    conn.close()

    assert census["reparsed"] is False
    assert census["parts"][1][NAME_UNREAD] == 200
    assert census["parts"][2][NAME_UNREAD] == 0
    assert build_db._nameless_alarms(census) == [(1, 200, 400, 0.5)]


def test_a_read_back_census_says_it_could_not_tell_the_two_apart(capsys):
    """The per-AC file carries no name_absence column, so a read-back counts
    every nameless row as unread. That over-counts once a connector declares
    NAME_ABSENT_IN_SOURCE -- the safe direction for an alarm, and one the
    report has to state rather than leave a reader to infer."""
    census = {"parts": {1: {"rows": 400, NAME_UNREAD: 200,
                            NAME_ABSENT_IN_SOURCE: 0}},
              "unrecognized": collections.Counter(), "reparsed": False}
    build_db._report_nameless([{"ac_code": "AC025", "nameless": census}])
    out = capsys.readouterr().out
    assert "skipped as already built" in out
    assert "AC025" in out


def test_source_blank_rows_are_reported_separately_from_the_alarm(capsys):
    """Counted and shown, not silently discarded: a state that starts
    declaring thousands of source-blank names is worth seeing even though it
    is not a defect."""
    census = build_db._nameless_census(
        _part(1000, 300, name_absence=NAME_ABSENT_IN_SOURCE))
    build_db._report_nameless([{"ac_code": "AC025", "nameless": census}])
    out = capsys.readouterr().out
    assert "300 row(s) of 1,000 declare no name in the source itself" in out
    assert "WARNING" not in out


def test_the_thresholds_sit_inside_the_band_the_measurements_bound():
    """The band is bounded below by the worst part that is not a defect (West
    Bengal's OCR corpus, 1.56%) and above by the mildest that is (Haryana
    HR22 part 52, 84%). Nothing measured lands between 0.02% and 100%, so
    this asserts the bracket rather than the value -- moving the threshold
    outside it means the calibration no longer supports it."""
    assert 0.05 <= build_db.NAMELESS_PART_RATE <= 0.50
    assert build_db.NAMELESS_PART_MIN_COUNT >= 2


# --- The alarm is actually wired into the builds ------------------------
#
# Everything above tests the instrument. These two test that it is plugged
# in: an alarm that works perfectly and is never called is the failure mode
# this whole round has been about.

class _NamelessConnector(StateConnector):
    """One AC, one part, every row nameless. Module-level rather than nested
    so ProcessPoolExecutor can pickle it onto a --per-ac worker."""

    state_id = "namelessstate"

    def list_constituencies(self):
        return [Constituency(ac_code="N001", ac_name="Nameless", district="D1")]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        return [
            VoterRecord(
                state=self.state_id, district=ac.district, ac_code=ac.ac_code,
                ac_name=ac.ac_name, part_no=1, serial_no=i, local_ref="",
                full_name="", full_relative_name="রমেশ", relation_code="F",
                age=40, gender="M", roll_year=roll_year,
                remark=WEST_BENGAL_REMARK)
            for i in range(1, 101)
        ]


def _register_nameless(monkeypatch, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "N001.csv").write_text("placeholder\n")
    monkeypatch.setitem(build_db.STATE_CONNECTORS, "namelessstate", {
        "connector_cls": _NamelessConnector,
        "label": "Namelessstate",
        "raw_dir": str(raw_dir),
        "raw_glob": "*.csv",
        "script": "latin",
    })


def test_a_combined_build_reports_the_alarm(tmp_path, monkeypatch, capsys):
    _register_nameless(monkeypatch, tmp_path)
    build_db.build_multi_state(
        ["namelessstate"], str(tmp_path / "out.sqlite"), roll_year=2002)
    out = capsys.readouterr().out
    assert "N001 part 1: 100/100 = 100.0% unread" in out


def test_a_per_ac_build_reports_the_alarm(tmp_path, monkeypatch, capsys):
    _register_nameless(monkeypatch, tmp_path)
    build_db.build_per_ac(
        ["namelessstate"], str(tmp_path / "out"), roll_year=2002, workers=1)
    out = capsys.readouterr().out
    assert "N001 part 1: 100/100 = 100.0% unread" in out


def test_the_build_still_finishes_and_still_publishes_the_rows(tmp_path, monkeypatch):
    """The alarm reports; it does not quarantine. A part with no readable
    names still holds serial numbers, ages and a relative's name, which is
    not nothing, and deciding to drop it is a separate call for a human."""
    _register_nameless(monkeypatch, tmp_path)
    db_path = tmp_path / "out.sqlite"
    build_db.build_multi_state(["namelessstate"], str(db_path), roll_year=2002)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 100
    finally:
        conn.close()
