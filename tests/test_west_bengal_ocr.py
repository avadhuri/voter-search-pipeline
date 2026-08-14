"""Tests for the Bengali name-field OCR path (states/west_bengal_ocr.py).

The mapping tests are hermetic -- they build a PDF in memory and stub the
engine out -- because what can actually go wrong there is the crop->result
bookkeeping, not the recognition. Getting that wrong attaches one voter's name
to a different voter, which is silent and much worse than a bad transcription,
so it is worth testing without needing Tesseract installed.

The end-to-end test does need Tesseract plus a downloaded Bengali AC, and
skips without them, following the same convention as the locality tests in
test_west_bengal_connector.py.
"""
import io
import os
import zipfile

import pytest

from states import west_bengal_ocr
from states.base import Constituency
from states.west_bengal import COL_NAME, COL_RELNAME, WestBengalConnector, _page_rows

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "west_bengal")
BENGALI_ZIP = os.path.join(RAW_DIR, "AC001.zip")     # Coochbehar, Bengali-typeset
LATIN_ZIP = os.path.join(RAW_DIR, "AC146.zip")       # Kolkata, Latin-typeset


def _one_line_pdf(lines):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=50 * len(lines) + 50)
    for i, text in enumerate(lines):
        page.insert_text((20, 40 + 50 * i), text, fontsize=20)
    out = doc.tobytes()
    doc.close()
    return out


def _rects(n):
    return [(15, 20 + 50 * i, 180, 45 + 50 * i) for i in range(n)]


def test_ocr_cells_maps_each_result_back_to_its_own_cell(monkeypatch):
    pdf = _one_line_pdf(["alpha", "beta", "gamma"])
    monkeypatch.setattr(west_bengal_ocr, "ensure_available", lambda: None)
    monkeypatch.setattr(
        west_bengal_ocr.subprocess, "run",
        lambda *a, **k: _completed(b"alpha\n\x0cbeta\n\x0cgamma\n\x0c"))

    got = west_bengal_ocr.ocr_cells(pdf, [(0, [r]) for r in _rects(3)])

    assert got == ["alpha", "beta", "gamma"]


def test_ocr_cells_joins_the_lines_of_a_wrapped_cell(monkeypatch):
    """A name too long for its column wraps, so one cell owns two rects."""
    pdf = _one_line_pdf(["first", "second", "third"])
    monkeypatch.setattr(west_bengal_ocr, "ensure_available", lambda: None)
    monkeypatch.setattr(
        west_bengal_ocr.subprocess, "run",
        lambda *a, **k: _completed(b"first\n\x0csecond\n\x0cthird\n\x0c"))

    r = _rects(3)
    got = west_bengal_ocr.ocr_cells(pdf, [(0, [r[0], r[1]]), (0, [r[2]])])

    assert got == ["first second", "third"]


def test_ocr_cells_refuses_a_result_count_it_cannot_map(monkeypatch):
    """Silently zipping mismatched lists would label voters with each other's
    names, so a short result set has to be an error, not a best guess."""
    pdf = _one_line_pdf(["a", "b", "c"])
    monkeypatch.setattr(west_bengal_ocr, "ensure_available", lambda: None)
    monkeypatch.setattr(
        west_bengal_ocr.subprocess, "run", lambda *a, **k: _completed(b"a\n\x0cb\n"))

    with pytest.raises(west_bengal_ocr.OcrUnavailableError, match="cannot map"):
        west_bengal_ocr.ocr_cells(pdf, [(0, [r]) for r in _rects(3)])


def test_ocr_cells_leaves_a_cell_with_no_glyphs_empty(monkeypatch):
    pdf = _one_line_pdf(["only"])
    monkeypatch.setattr(west_bengal_ocr, "ensure_available", lambda: None)
    monkeypatch.setattr(
        west_bengal_ocr.subprocess, "run", lambda *a, **k: _completed(b"only\n\x0c"))

    got = west_bengal_ocr.ocr_cells(pdf, [(0, []), (0, [_rects(1)[0]])])

    assert got == ["", "only"]


def _completed(stdout):
    import subprocess
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")


def test_ocr_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("WB_OCR", raising=False)
    assert WestBengalConnector().ocr is False
    monkeypatch.setenv("WB_OCR", "1")
    assert WestBengalConnector().ocr is True
    monkeypatch.setenv("WB_OCR", "0")
    assert WestBengalConnector().ocr is False
    assert WestBengalConnector(ocr=True).ocr is True


@pytest.mark.skipif(not os.path.exists(LATIN_ZIP), reason="pilot raw data not downloaded")
def test_a_latin_ac_is_never_sent_to_ocr(monkeypatch):
    """Its names decode exactly from the text layer; OCR could only be worse."""
    def explode(*a, **k):
        raise AssertionError("OCR ran on an AC whose names already decoded")

    monkeypatch.setattr(west_bengal_ocr, "ocr_cells", explode)
    records = _parse_first_part(LATIN_ZIP, "AC146", ocr=True)

    assert records
    assert any(r.full_name for r in records)


@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="AC001 raw data not downloaded")
def test_bengali_names_stay_empty_with_a_remark_when_ocr_is_off():
    records = _parse_first_part(BENGALI_ZIP, "AC001", ocr=False)

    assert records
    assert not any(r.full_name or r.full_relative_name for r in records)
    assert all("not decodable yet" in r.remark for r in records)
    # the closed-vocabulary and numeric columns are recovered either way
    assert any(r.age for r in records)
    assert any(r.relation_code for r in records)


@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="AC001 raw data not downloaded")
@pytest.mark.skipif(not west_bengal_ocr.is_available(), reason="tesseract + ben model not installed")
def test_ocr_fills_bengali_names_with_real_bengali_text():
    records = _parse_first_part(BENGALI_ZIP, "AC001", ocr=True)

    assert records
    named = [r for r in records if r.full_name]
    assert len(named) > 0.9 * len(records)
    assert all("read by OCR" in r.remark for r in named)
    # real Bengali script, and specifically not the private-use code points
    # that the undecodable text layer yields
    for r in named:
        letters = [c for c in r.full_name if c.isalpha()]
        assert letters and all(0x0980 <= ord(c) < 0x0A00 for c in letters), r.full_name


@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="AC001 raw data not downloaded")
def test_name_cells_carry_the_box_the_glyphs_were_drawn_in():
    """The crop coordinates OCR relies on -- if these drift, OCR reads the
    wrong part of the page and there is no error to notice."""
    import pdfplumber

    with zipfile.ZipFile(BENGALI_ZIP) as zf:
        member = sorted(n for n in zf.namelist() if n.startswith("part"))[0]
        pdf_bytes = zf.read(member)

    checked = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        geometry = None
        for page in pdf.pages:
            rows, geometry = _page_rows(page, geometry)
            for cells, boxes in rows:
                for col in (COL_NAME, COL_RELNAME):
                    if not cells[col]:
                        continue
                    assert boxes[col], "a cell with text must report where it was drawn"
                    for x0, top, x1, bottom in boxes[col]:
                        assert x0 < x1 and top < bottom
                        assert 0 <= x0 and bottom <= page.height
                        checked += 1
    assert checked > 100


def _parse_first_part(zip_path, ac_code, ocr):
    connector = WestBengalConnector(ocr=ocr)
    ac = Constituency(ac_code=ac_code, ac_name="test", district="test")
    with zipfile.ZipFile(zip_path) as zf:
        member = sorted(n for n in zf.namelist() if n.startswith("part"))[0]
        return connector._parse_part(zf.read(member), ac, 2002, 1, member)
