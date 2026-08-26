"""Where the serial column ends.

Every column boundary but this one is found by searching between the two
printed column numbers that bracket it. That assumes each column number sits
over its own column, and on real parts it does not: AC160 part 24 page 22
prints "1" at x=61.4 over serials that end at x=45.9, with the house number
starting at 76.9 -- the heading is inside the gutter it is supposed to
bracket. The search then misses the gutter's left half, reads what is left of
it as a leading blank (excluded by design, it is column slack elsewhere), and
settles on a 1pt gap inside the house cell instead. Column 1 comes out as
"20 87B", which is neither a serial nor blank, so every row on the page is
dropped -- and a row that never reaches _record cannot carry a remark, which
is why nothing downstream noticed.

These tests assert the computed boundary rather than a row count, on purpose.
Page 27 of that same part parses correctly today, but only because its
3-digit serials leave no interior blank run at all and the fallback happens
to pick the window's left edge; a row-count assertion would call that a pass
and go on calling it a pass after the geometry changed underneath it.
"""
import io
import os
import zipfile

import pytest

from states import west_bengal as wb
from states.base import Constituency
from states.west_bengal import (
    COL_HOUSE,
    COL_SL,
    _boundaries,
    _cell_text,
    _column_numbers_of,
    _page_rows,
    _rows_of,
    _split_row,
    _starts_with_serial,
    WestBengalConnector,
)

# x-geometry lifted from AC160 part 24 page 22, the part measured by hand.
ANCHORS = [61.4, 100.5, 187.4, 267.6, 335.2, 421.4, 456.7, 514.5]
SERIAL_GUTTER = (45.9, 76.9)   # right edge of the serials, left edge of the houses


def _chars(text, x0, x1, top):
    """One char box per character, spread evenly across [x0, x1]."""
    step = (x1 - x0) / len(text)
    return [
        {"text": ch, "x0": x0 + i * step, "x1": x0 + (i + 1) * step,
         "top": top, "bottom": top + 9.0}
        for i, ch in enumerate(text)
        if not ch.isspace()
    ]


def _row(spec, top):
    out = []
    for text, x0, x1 in spec:
        out.extend(_chars(text, x0, x1, top))
    return out


def _anchor_row(top=0.0):
    return _row([(str(i + 1), c - 2.5, c + 2.5) for i, c in enumerate(ANCHORS)], top)


def _data_row(serial, top, serial_x1=45.9):
    return _row(
        [
            (serial, 34.9, serial_x1),
            ("87B", 76.9, 94.6),
            ("BL", 97.5, 109.7),
            ("M", 112.6, 121.0),
            ("Dinesh", 139.0, 170.0),
            ("Gupta", 175.7, 203.1),
            ("Father", 245.0, 276.0),
            ("Ramjilal", 300.0, 340.0),
            ("Gupta", 345.0, 372.0),
            ("M", 415.0, 421.0),
            ("33", 448.0, 460.0),
            ("WB/22/160/066403", 476.0, 545.0),
        ],
        top,
    )


def test_the_column_number_row_reads_as_the_anchors_it_was_built_from():
    assert _column_numbers_of(_anchor_row()) == pytest.approx(ANCHORS)


def test_the_serial_boundary_lands_in_the_gutter_the_data_shows():
    rows = [_data_row("20", 20.0 + 10 * i) for i in range(24)]
    bounds = _boundaries(ANCHORS, rows)
    assert SERIAL_GUTTER[0] < bounds[1] < SERIAL_GUTTER[1]


def test_bracketing_by_the_column_numbers_alone_misses_it():
    """The defect, asserted -- so the fix cannot be quietly reverted."""
    rows = [_data_row("20", 20.0 + 10 * i) for i in range(24)]
    bounds = _boundaries(ANCHORS, rows, measure_serial=False)
    assert bounds[1] > SERIAL_GUTTER[1]


def test_a_row_the_old_geometry_dropped_splits_into_serial_and_house():
    rows = [_data_row("20", 20.0 + 10 * i) for i in range(24)]
    bounds = _boundaries(ANCHORS, rows)
    cells = [_cell_text(c) for c in _split_row(rows[0], bounds)]
    assert cells[COL_SL] == "20"
    assert cells[COL_HOUSE] == "87B BL M"


def test_a_three_digit_serial_is_measured_not_left_to_the_fallback():
    """Page 27 of the same part: wider serials, no interior blank run inside
    the anchor window, so the old code reached its emptiest-single-x fallback
    and landed correctly by luck. The boundary is now measured either way."""
    rows = [_data_row("797", 20.0 + 10 * i, serial_x1=51.4) for i in range(19)]
    bounds = _boundaries(ANCHORS, rows)
    assert 51.4 < bounds[1] < SERIAL_GUTTER[1]


def test_a_serial_printed_against_its_house_number_keeps_the_anchor_boundary():
    """When the serial and the house number touch, the two runs merge and the
    gutter after them is the one before the *name*. Measuring it would put the
    house number in the serial column on every row of the page, so a boundary
    past the next column's heading is refused and the anchor-derived one
    stands -- the anchors are a poor guide to where a column begins but a
    sound bound on how far it can reach."""
    rows = [
        _row([("20", 34.9, 45.9), ("87B", 46.9, 94.6), ("Dinesh", 139.0, 170.0)],
             20.0 + 10 * i)
        for i in range(6)
    ]
    fallback = _boundaries(ANCHORS, rows, measure_serial=False)
    assert _boundaries(ANCHORS, rows)[1] == fallback[1]


# --------------------------------------------------------------------------
# the same thing against the real part it was found in

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "west_bengal")
AC160_ZIP = os.path.join(RAW_DIR, "AC160.zip")


def _page(pageno):
    import pdfplumber

    with zipfile.ZipFile(AC160_ZIP) as zf:
        blob = zf.read("part0024.pdf")
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        page = pdf.pages[pageno]
        rows = _rows_of(page.chars)
        marks = [(i, c) for i, c in
                 ((i, _column_numbers_of(r)) for i, r in enumerate(rows)) if c]
        assert marks, "this page prints its own column numbers"
        start, centres = marks[0]
        data = [r for r in rows[start + 1:] if _starts_with_serial(r, centres)]
        return centres, data


@pytest.mark.skipif(not os.path.exists(AC160_ZIP), reason="raw data not downloaded")
@pytest.mark.parametrize("pageno", [22, 27])
def test_ac160_part_24_cuts_between_the_serial_and_the_house_number(pageno):
    centres, data = _page(pageno)
    bounds = _boundaries(centres, data)
    serial_right = max(max(ch["x1"] for ch in r if ch["x1"] < centres[0]) for r in data)
    house_left = min(
        min(ch["x0"] for ch in r if ch["x0"] > centres[0]) for r in data
    )
    assert serial_right < bounds[1] < house_left


@pytest.mark.skipif(not os.path.exists(AC160_ZIP), reason="raw data not downloaded")
def test_ac160_part_24_keeps_the_correction_pages_it_used_to_drop():
    import pdfplumber

    with zipfile.ZipFile(AC160_ZIP) as zf:
        blob = zf.read("part0024.pdf")
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        fallback, got = None, {}
        for i, page in enumerate(pdf.pages):
            rows, fallback = _page_rows(page, fallback)
            got[i] = len(rows)
    # pages 21-26 carried 141 electors and produced nothing at all
    assert [got[i] for i in range(21, 27)] == [19, 24, 25, 24, 25, 24]


# --------------------------------------------------------------------------
# the report that says a page produced nothing

class _StubPage:
    """Only what _page_rows touches: the chars and a page number."""

    def __init__(self, chars, page_number=1):
        self.chars = chars
        self.page_number = page_number


def _as_the_geometry_was(monkeypatch):
    """Put the serial boundary back where it was before this fix.

    A check that has never been seen to fire and a check that cannot fire
    print the same thing, so every test below drives the report through the
    geometry that actually lost the pages rather than trusting a clean run.
    """
    monkeypatch.setattr(
        wb, "_serial_boundary", lambda rows, centres, fallback: fallback
    )


def test_the_report_fires_on_the_geometry_that_dropped_the_pages(monkeypatch):
    _as_the_geometry_was(monkeypatch)
    chars = _anchor_row(10.0)
    for i in range(24):
        chars.extend(_data_row("20", 20.0 + 10 * i))
    dropped = []
    rows, _ = _page_rows(_StubPage(chars), None, dropped)
    assert rows == []
    assert dropped == [24]


def test_the_report_stays_quiet_once_the_page_reads(monkeypatch):
    chars = _anchor_row(10.0)
    for i in range(24):
        chars.extend(_data_row("20", 20.0 + 10 * i))
    dropped = []
    rows, _ = _page_rows(_StubPage(chars), None, dropped)
    assert len(rows) == 24
    assert dropped == []


def test_a_page_with_no_rows_at_all_is_not_a_dropped_page():
    """The cover and the summary hold no table. Reporting them would put a
    line on the build log for every part of every AC, which is how a real one
    goes unread."""
    dropped = []
    _page_rows(_StubPage(_anchor_row(10.0)), None, dropped)
    assert dropped == []


@pytest.mark.skipif(not os.path.exists(AC160_ZIP), reason="raw data not downloaded")
def test_ac160_part_24_is_named_on_the_build_log_under_the_old_geometry(
    monkeypatch, capsys
):
    _as_the_geometry_was(monkeypatch)
    with zipfile.ZipFile(AC160_ZIP) as zf:
        blob = zf.read("part0024.pdf")
    ac = Constituency(ac_code="AC160", ac_name="Belgachia West", district="Kolkata")
    dropped = []
    WestBengalConnector()._parse_part(blob, ac, 2002, 24, "part0024.pdf", dropped)
    assert [n for _, _, n in dropped] == [19, 24, 25, 24, 25, 24]
    assert sum(n for _, _, n in dropped) == 141

    wb._report_dropped_pages(ac, 165, dropped)
    said = capsys.readouterr().out
    assert "AC160" in said and "141 elector(s) not in this build" in said


@pytest.mark.skipif(not os.path.exists(AC160_ZIP), reason="raw data not downloaded")
def test_ac160_part_24_drops_nothing_now(capsys):
    with zipfile.ZipFile(AC160_ZIP) as zf:
        blob = zf.read("part0024.pdf")
    ac = Constituency(ac_code="AC160", ac_name="Belgachia West", district="Kolkata")
    dropped = []
    records = WestBengalConnector()._parse_part(
        blob, ac, 2002, 24, "part0024.pdf", dropped
    )
    assert dropped == []
    wb._report_dropped_pages(ac, 165, dropped)
    assert capsys.readouterr().out == ""
    # 797 main-roll serials + 9 additions + 160 corrections, as printed on the
    # part's own SUMMARY OF ELECTORS and SUPPLEMENT DETAILS pages
    assert len(records) == 966
