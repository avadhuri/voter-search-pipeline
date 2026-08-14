"""Tests for the Bengali name-field OCR path (states/west_bengal_ocr.py).

The mapping tests are hermetic -- they build a PDF in memory and stub the
engine out -- because what can actually go wrong there is the word/crop ->
cell bookkeeping, not the recognition. Getting that wrong attaches one voter's
name to a different voter, which is silent and much worse than a bad
transcription, so it is worth testing without needing Tesseract installed or a
Cloud Vision bill.

Both engines are covered, each with `ENGINE` pinned explicitly, so these keep
testing what they mean to whichever one is the default at the time.

The end-to-end tests need real raw data plus a working engine, and skip
without them, following the same convention as the locality tests in
test_west_bengal_connector.py.
"""
import io
import os
import subprocess
import zipfile

import pytest

from states import west_bengal_ocr as ocr
from states.base import Constituency
from states.west_bengal import COL_NAME, COL_RELNAME, WestBengalConnector, _page_rows

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "west_bengal")
BENGALI_ZIP = os.path.join(RAW_DIR, "AC001.zip")     # Coochbehar, Bengali-typeset
LATIN_ZIP = os.path.join(RAW_DIR, "AC146.zip")       # Kolkata, Latin-typeset


def _pdf(lines_per_page):
    """A PDF with one text line per entry, `lines_per_page` entries per page."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for count in lines_per_page:
        page = doc.new_page(width=200, height=50 * count + 50)
        for i in range(count):
            page.insert_text((20, 40 + 50 * i), "x", fontsize=20)
    out = doc.tobytes()
    doc.close()
    return out


def _rects(n):
    """The rect of each of the n lines `_pdf` draws on a page."""
    return [(15, 20 + 50 * i, 180, 45 + 50 * i) for i in range(n)]


# --------------------------------------------------------------------------
# Cloud Vision (the default engine)
# --------------------------------------------------------------------------

def _word(text, rect):
    """A Vision word annotation drawn inside `rect`, in rendered-image pixels."""
    x0, top, x1, bottom = rect
    z = ocr.ZOOM
    return {
        "symbols": [{"text": c} for c in text],
        "boundingBox": {"vertices": [
            {"x": round(x0 * z) + 1, "y": round(top * z) + 1},
            {"x": round(x1 * z) - 1, "y": round(top * z) + 1},
            {"x": round(x1 * z) - 1, "y": round(bottom * z) - 1},
            {"x": round(x0 * z) + 1, "y": round(bottom * z) - 1},
        ]},
    }


def _page(*words):
    return {"fullTextAnnotation": {"pages": [{"blocks": [{"paragraphs": [
        {"words": list(words)}]}]}]}}


@pytest.fixture
def vision(monkeypatch):
    """Pins the Vision engine and answers its HTTP calls from `responses`."""
    monkeypatch.setattr(ocr, "ENGINE", "vision")
    monkeypatch.setattr(ocr, "ensure_available", lambda: None)

    def fake_post(payload):
        fake_post.sent.append(payload)
        taken = fake_post.responses[:len(payload["requests"])]
        del fake_post.responses[:len(payload["requests"])]
        return {"responses": taken}

    fake_post.sent = []
    fake_post.responses = []
    monkeypatch.setattr(ocr, "_vision_post", fake_post)
    return fake_post


def test_vision_maps_each_word_back_to_the_cell_it_was_drawn_in(vision):
    r = _rects(3)
    vision.responses = [_page(_word("alpha", r[0]), _word("beta", r[1]),
                              _word("gamma", r[2]))]

    got = ocr.ocr_cells(_pdf([3]), [(0, [rect]) for rect in r])

    assert got == ["alpha", "beta", "gamma"]


def test_vision_orders_the_words_within_a_cell_left_to_right(vision):
    x0, top, x1, bottom = _rects(1)[0]
    left = (x0, top, x0 + 40, bottom)
    right = (x0 + 60, top, x1, bottom)
    # deliberately handed back in the wrong order
    vision.responses = [_page(_word("দাস", right), _word("জগদীশ", left))]

    got = ocr.ocr_cells(_pdf([1]), [(0, [(x0, top, x1, bottom)])])

    assert got == ["জগদীশ দাস"]


def test_vision_joins_the_lines_of_a_wrapped_cell(vision):
    """A name too long for its column wraps, so one cell owns two rects."""
    r = _rects(3)
    vision.responses = [_page(_word("first", r[0]), _word("second", r[1]),
                              _word("third", r[2]))]

    got = ocr.ocr_cells(_pdf([3]), [(0, [r[0], r[1]]), (0, [r[2]])])

    assert got == ["first second", "third"]


def test_vision_folds_assamese_ra_into_bengali_ra(vision):
    """Vision, even hinted `bn`, spells Bengali ra as its Assamese neighbour in
    many conjuncts. They are separate code points for the same letter and the
    Assamese one never occurs here, so an unfolded name could never be
    matched by a query spelled the way the roll spells it."""
    r = _rects(1)[0]
    vision.responses = [_page(_word("কীৰ্ত্তনীয়া", r))]

    got = ocr.ocr_cells(_pdf([1]), [(0, [r])])

    assert got == ["কীর্ত্তনীয়া"]


def test_vision_ignores_words_outside_every_cell(vision):
    """Page furniture -- headers, the part number, column rules -- is on the
    same raster as the names and must not be swept into one."""
    r = _rects(1)[0]
    vision.responses = [_page(_word("ভাগ", (10, 2, 190, 12)), _word("দাস", r))]

    got = ocr.ocr_cells(_pdf([1]), [(0, [r])])

    assert got == ["দাস"]


def test_vision_gives_a_word_to_at_most_one_cell(vision):
    """Two cells whose rects overlap must not both claim the same word."""
    r = _rects(2)
    overlapping = (r[0][0], r[0][1], r[0][2], r[0][3] + 30)
    vision.responses = [_page(_word("এক", r[0]))]

    got = ocr.ocr_cells(_pdf([2]), [(0, [r[0]]), (0, [overlapping])])

    assert got == ["এক", ""]


def test_vision_leaves_a_cell_with_no_glyphs_empty(vision):
    r = _rects(1)[0]
    vision.responses = [_page(_word("only", r))]

    got = ocr.ocr_cells(_pdf([1]), [(0, []), (0, [r])])

    assert got == ["", "only"]


def test_vision_never_sends_a_page_that_has_no_cells_to_read(vision):
    """Vision bills per image, so a page with nothing to read costs nothing
    only if it is never submitted -- this is what keeps a Latin-typeset AC,
    and every blank tail page, off the bill."""
    r = _rects(1)[0]
    vision.responses = [_page(_word("এক", r))]

    ocr.ocr_cells(_pdf([1, 1, 1]), [(0, [r]), (1, []), (2, [])])

    assert [len(p["requests"]) for p in vision.sent] == [1]


def test_vision_batches_pages_to_keep_round_trips_down(vision):
    """Billing is per image either way, so batching only trades HTTP calls --
    but at ~16 pages a part that is still most of the round trips."""
    pages = 20
    r = _rects(1)[0]
    vision.responses = [_page(_word("এক", r)) for _ in range(pages)]

    got = ocr.ocr_cells(_pdf([1] * pages), [(p, [r]) for p in range(pages)])

    assert got == ["এক"] * pages
    assert [len(p["requests"]) for p in vision.sent] == [8, 8, 4]


def test_vision_refuses_a_response_count_it_cannot_map(vision):
    """Zipping mismatched lists would label voters with each other's names, so
    a short response set has to be an error, not a best guess."""
    vision.responses = []

    with pytest.raises(ocr.OcrUnavailableError, match="cannot map"):
        ocr.ocr_cells(_pdf([1]), [(0, [_rects(1)[0]])])


def test_vision_surfaces_a_per_page_error_rather_than_dropping_the_names(vision):
    """A per-image failure comes back inside a 200, so it has to be looked
    for -- otherwise a whole page of voters silently loses its names."""
    vision.responses = [{"error": {"message": "quota exceeded"}}]

    with pytest.raises(ocr.OcrUnavailableError, match="quota exceeded"):
        ocr.ocr_cells(_pdf([1]), [(0, [_rects(1)[0]])])


# --------------------------------------------------------------------------
# Tesseract (the free fallback)
# --------------------------------------------------------------------------

def _completed(stdout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")


@pytest.fixture
def tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "ENGINE", "tesseract")
    monkeypatch.setattr(ocr, "ensure_available", lambda: None)

    def stdout(text):
        monkeypatch.setattr(ocr.subprocess, "run", lambda *a, **k: _completed(text))

    return stdout


def test_tesseract_maps_each_crop_back_to_its_own_cell(tesseract):
    tesseract(b"alpha\n\x0cbeta\n\x0cgamma\n\x0c")

    got = ocr.ocr_cells(_pdf([3]), [(0, [r]) for r in _rects(3)])

    assert got == ["alpha", "beta", "gamma"]


def test_tesseract_joins_the_lines_of_a_wrapped_cell(tesseract):
    tesseract(b"first\n\x0csecond\n\x0cthird\n\x0c")
    r = _rects(3)

    got = ocr.ocr_cells(_pdf([3]), [(0, [r[0], r[1]]), (0, [r[2]])])

    assert got == ["first second", "third"]


def test_tesseract_refuses_a_result_count_it_cannot_map(tesseract):
    tesseract(b"a\n\x0cb\n")

    with pytest.raises(ocr.OcrUnavailableError, match="cannot map"):
        ocr.ocr_cells(_pdf([3]), [(0, [r]) for r in _rects(3)])


def test_tesseract_leaves_a_cell_with_no_glyphs_empty(tesseract):
    tesseract(b"only\n\x0c")

    got = ocr.ocr_cells(_pdf([1]), [(0, []), (0, [_rects(1)[0]])])

    assert got == ["", "only"]


def test_an_unknown_engine_is_rejected_by_name(monkeypatch):
    monkeypatch.setattr(ocr, "ENGINE", "paddleocr")

    with pytest.raises(ocr.OcrUnavailableError, match="not a known engine"):
        ocr.ocr_cells(_pdf([1]), [(0, [_rects(1)[0]])])


# --------------------------------------------------------------------------
# switches
# --------------------------------------------------------------------------

def test_ocr_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("WB_OCR", raising=False)
    assert WestBengalConnector().ocr is False
    monkeypatch.setenv("WB_OCR", "1")
    assert WestBengalConnector().ocr is True
    monkeypatch.setenv("WB_OCR", "0")
    assert WestBengalConnector().ocr is False
    assert WestBengalConnector(ocr=True).ocr is True


def test_worker_count_defaults_to_six_and_is_configurable(monkeypatch):
    monkeypatch.delenv("WB_OCR_WORKERS", raising=False)
    assert ocr.workers() == 6
    monkeypatch.setenv("WB_OCR_WORKERS", "12")
    assert ocr.workers() == 12
    monkeypatch.setenv("WB_OCR_WORKERS", "0")
    assert ocr.workers() == 1
    monkeypatch.setenv("WB_OCR_WORKERS", "not-a-number")
    assert ocr.workers() == 6


# --------------------------------------------------------------------------
# against real rolls
# --------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="AC001 raw data not downloaded")
def test_parts_parsed_concurrently_still_come_back_in_part_order(monkeypatch):
    """Parts finish out of order by design; the records must not be, since
    the part column is what a voter uses to find their own polling station."""
    monkeypatch.setenv("WB_OCR_WORKERS", "4")
    monkeypatch.setattr(ocr, "ocr_cells", lambda pdf, cells: ["নাম"] * len(cells))

    records = WestBengalConnector(ocr=True).parse_raw(
        _first_parts(BENGALI_ZIP, 6),
        Constituency(ac_code="AC001", ac_name="test", district="test"), 2002)

    parts = [r.part_no for r in records]
    assert len(set(parts)) == 6
    assert parts == sorted(parts)


@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="AC001 raw data not downloaded")
def test_worker_count_does_not_change_the_records(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_cells",
                        lambda pdf, cells: [f"n{i}" for i in range(len(cells))])
    raw = _first_parts(BENGALI_ZIP, 4)
    ac = Constituency(ac_code="AC001", ac_name="test", district="test")

    monkeypatch.setenv("WB_OCR_WORKERS", "1")
    serial = WestBengalConnector(ocr=True).parse_raw(raw, ac, 2002)
    monkeypatch.setenv("WB_OCR_WORKERS", "6")
    concurrent = WestBengalConnector(ocr=True).parse_raw(raw, ac, 2002)

    assert serial
    assert [(r.part_no, r.serial_no, r.full_name) for r in serial] == \
           [(r.part_no, r.serial_no, r.full_name) for r in concurrent]


@pytest.mark.skipif(not os.path.exists(LATIN_ZIP), reason="pilot raw data not downloaded")
def test_a_latin_ac_is_never_sent_to_ocr(monkeypatch):
    """Its names decode exactly from the text layer, so OCR could only be
    worse -- and with Vision it would also cost money for nothing."""
    def explode(*a, **k):
        raise AssertionError("OCR ran on an AC whose names already decoded")

    monkeypatch.setattr(ocr, "ocr_cells", explode)
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
@pytest.mark.skipif(not ocr.is_available(), reason="no OCR engine configured")
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
        assert "ৰ" not in r.full_name


@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="AC001 raw data not downloaded")
def test_name_cells_carry_the_box_the_glyphs_were_drawn_in():
    """The rects OCR relies on -- if these drift, OCR reads the wrong part of
    the page and there is no error to notice."""
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


def _first_parts(zip_path, count):
    """Repackage the first `count` parts, so a test stays quick on a big AC."""
    buf = io.BytesIO()
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(buf, "w") as dst:
        for member in sorted(n for n in src.namelist() if n.startswith("part"))[:count]:
            dst.writestr(member, src.read(member))
    return buf.getvalue()


def _parse_first_part(zip_path, ac_code, ocr):
    connector = WestBengalConnector(ocr=ocr)
    ac = Constituency(ac_code=ac_code, ac_name="test", district="test")
    with zipfile.ZipFile(zip_path) as zf:
        member = sorted(n for n in zf.namelist() if n.startswith("part"))[0]
        return connector._parse_part(zf.read(member), ac, 2002, 1, member)
