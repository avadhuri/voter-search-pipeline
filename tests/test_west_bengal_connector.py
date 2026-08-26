import gzip
import io
import json
import os
import zipfile

import pytest

from states.base import Constituency, UnparseableRollError
from states.source_urls import resolve_source_url
from states.west_bengal import OCR_DIR, WestBengalConnector
from states.west_bengal_ocr import OCR_SUBDIR

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "west_bengal")
SAME_ROW_ZIP = os.path.join(RAW_DIR, "AC146.zip")   # value beside the label, same row
WRAPPED_ZIP = os.path.join(RAW_DIR, "AC141.zip")    # value wraps onto the next row


def _parse_first_part(zip_path, ac_code):
    connector = WestBengalConnector()
    ac = Constituency(ac_code=ac_code, ac_name="test", district="Kolkata")
    with zipfile.ZipFile(zip_path) as zf:
        name = sorted(n for n in zf.namelist() if n.startswith("part"))[0]
        return connector._parse_part(zf.read(name), ac, 2002, 1, name)


@pytest.mark.skipif(not os.path.exists(SAME_ROW_ZIP), reason="pilot raw data not downloaded")
def test_locality_extracted_when_value_sits_beside_the_label():
    records = _parse_first_part(SAME_ROW_ZIP, "AC146")
    assert records
    assert {r.locality for r in records} == {"PARK STREET"}


@pytest.mark.skipif(not os.path.exists(WRAPPED_ZIP), reason="pilot raw data not downloaded")
def test_locality_extracted_when_value_wraps_onto_the_next_row():
    records = _parse_first_part(WRAPPED_ZIP, "AC141")
    assert records
    assert {r.locality for r in records} == {
        "BAGHBAZAR STREET (PREMISES NO.22/2A TO 30/2)"
    }


# The Bengali-typeset covers below are the same field, in the same layout, set
# in the legacy font -- so the label only matches after decoding. AC001 part 1
# carries a value; AC260 part 1 genuinely carries none, with the *next* cover
# label ("Name of Gram Panchayat / Ward No") sitting on the row below the
# empty one, which is what a bare take-the-next-row fallback would publish.
BENGALI_ZIP = os.path.join(RAW_DIR, "AC001.zip")        # value beside the label
BENGALI_BLANK_ZIP = os.path.join(RAW_DIR, "AC260.zip")  # no value at all


@pytest.mark.skipif(not os.path.exists(BENGALI_ZIP), reason="raw data not downloaded")
def test_locality_extracted_from_a_bengali_typeset_cover():
    records = _parse_first_part(BENGALI_ZIP, "AC001")
    assert records
    assert {r.locality for r in records} == {"317, নিত্যানন্দী (অংশ)"}


@pytest.mark.skipif(
    not os.path.exists(BENGALI_BLANK_ZIP), reason="raw data not downloaded"
)
def test_blank_bengali_locality_is_empty_not_the_next_label():
    records = _parse_first_part(BENGALI_BLANK_ZIP, "AC260")
    assert records
    assert {r.locality for r in records} == {""}


# --------------------------------------------------------------------------
# page-scan ACs: the Cloud Vision fallback
# --------------------------------------------------------------------------

SCANNED_ROW = ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"]


def _blank_pdf(n_pages):
    """A PDF of `n_pages` pages carrying no text layer at all.

    A real part of AC287 is that plus one full-page scanned image per page,
    and the image is exactly what this module cannot read -- so for deciding
    "is this a scan?" the image is the part that does not matter, and leaving
    it out keeps the fixture readable.
    """
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        f"<</Type/Pages/Kids[{' '.join(f'{3 + i} 0 R' for i in range(n_pages))}]"
        f"/Count {n_pages}>>".encode(),
    ] + [b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"] * n_pages

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\n"
            f"startxref\n{start}\n%%EOF\n").encode()
    return bytes(out)


def _scan_zip(parts):
    """{part stem: page count} bundled the way the downloader bundles an AC."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for stem, pages in parts.items():
            zf.writestr(f"{stem}.pdf", _blank_pdf(pages))
    return buf.getvalue()


def _vision_page(rows):
    """One Vision response for a page, words emitted column-major as Vision
    actually emits them on these scans."""
    placed = []
    for r, tokens in enumerate(rows):
        x = 40.0
        for c, token in enumerate(tokens):
            w = 8.0 * len(token)
            verts = [(x, 100.0 + r * 60), (x + w, 100.0 + r * 60),
                     (x + w, 120.0 + r * 60), (x, 120.0 + r * 60)]
            placed.append((c, {
                "symbols": [{"text": ch} for ch in token],
                "boundingBox": {"normalizedVertices": [
                    {"x": vx / 1000.0, "y": vy / 1400.0} for vx, vy in verts
                ]},
            }))
            x += w + 12.0
    placed.sort(key=lambda p: p[0])
    return {"fullTextAnnotation": {"pages": [{
        "width": 1000.0, "height": 1400.0,
        "blocks": [{"paragraphs": [{"words": [w for _, w in placed]}]}],
    }]}}


def _ocr_tree(root, ac_code, parts, ocr=None, row=None):
    """The response tree scripts/ocr_vision.py writes. `ocr` names the parts
    to actually write responses for; the rest stay un-OCR'd. `row` overrides
    the voter row every page carries."""
    ac_dir = os.path.join(root, ac_code)
    os.makedirs(ac_dir, exist_ok=True)
    with open(os.path.join(ac_dir, "pages.json"), "w", encoding="utf-8") as fh:
        json.dump(parts, fh)
    for stem in (parts if ocr is None else ocr):
        part_dir = os.path.join(ac_dir, stem)
        os.makedirs(part_dir, exist_ok=True)
        pages = parts[stem]
        for first in range(1, pages + 1, 5):
            last = min(first + 4, pages)
            payload = {"responses": [_vision_page([row or SCANNED_ROW])
                                     for _ in range(first, last + 1)]}
            with gzip.open(os.path.join(part_dir, f"p{first:04d}-{last:04d}.json.gz"),
                           "wt", encoding="utf-8") as fh:
                json.dump(payload, fh)
    return ac_dir


SCANNED_AC = Constituency(ac_code="AC287", ac_name="Nanoor", district="Birbhum")


def test_a_scanned_ac_is_read_from_its_vision_responses(tmp_path):
    """The whole point of the wiring: an AC with no text layer still builds,
    and builds as a West Bengal AC rather than as something else. The app
    selects by (state, ac_code) and groups the picker by state, so AC287
    reaching the DB under any other state id is the same as not reaching it.
    """
    root = str(tmp_path / "ocr")
    _ocr_tree(root, "AC287", {"part0001": 6, "part0002": 3})
    raw = _scan_zip({"part0001": 6, "part0002": 3})

    records = WestBengalConnector(ocr_dir=root).parse_raw(raw, SCANNED_AC, 2002)

    assert len(records) == 9                       # one row on each of 9 pages
    assert {r.state for r in records} == {"west_bengal"}
    assert {r.ac_code for r in records} == {"AC287"}
    assert {r.ac_name for r in records} == {"Nanoor"}
    assert {r.district for r in records} == {"Birbhum"}
    assert {r.full_name for r in records} == {"রমেশ মণ্ডল"}
    assert {r.full_relative_name for r in records} == {"হরি মণ্ডল"}
    assert {r.relation_code for r in records} == {"F"}
    assert {r.gender for r in records} == {"M"}
    assert {r.local_ref for r in records} == {"WBA1234567"}
    assert {r.roll_year for r in records} == {2002}


def test_part_numbers_come_from_the_part_directory_names(tmp_path):
    """Not decoration: states/source_urls.py joins (ac_code, part_no) against
    the SIR workbook to fill source_url, and a row with no part number gets
    no link back to the document it came from -- a known production defect
    class here, and one the freshness guards check for on the serving side.
    """
    root = str(tmp_path / "ocr")
    _ocr_tree(root, "AC287", {"part0001": 1, "part0002": 1, "part0017": 1})
    raw = _scan_zip({"part0001": 1, "part0002": 1, "part0017": 1})

    records = WestBengalConnector(ocr_dir=root).parse_raw(raw, SCANNED_AC, 2002)
    assert sorted(r.part_no for r in records) == [1, 2, 17]
    assert all(resolve_source_url(r).startswith("https://") for r in records)
    assert len({resolve_source_url(r) for r in records}) == 3


def test_a_scanned_ac_with_no_ocr_output_is_declared_absent_not_empty(tmp_path):
    """Before this wiring existed the scan path was not an error -- a page
    with no characters yields no rows, so the AC built, catalogued and
    published with zero electors and a clean log. Declaring it unparseable is
    what keeps it out of the catalog instead.
    """
    raw = _scan_zip({"part0001": 3})
    connector = WestBengalConnector(ocr_dir=str(tmp_path / "ocr"))
    with pytest.raises(UnparseableRollError) as exc:
        connector.parse_raw(raw, SCANNED_AC, 2002)
    assert "AC287" in str(exc.value)


def test_a_half_ocrd_ac_is_refused_rather_than_built_short(tmp_path):
    """A Vision run is hours long and resumable, so "some parts done" is its
    normal intermediate state. Building it would publish an AC missing
    exactly the electors nobody could notice were missing: every row present
    parses, scores and ranks correctly, and no downstream check -- not the
    search-quality suite, which drives explicit (state, ac_code) pairs, not
    the freshness guards, which see nothing stale -- can tell.
    """
    root = str(tmp_path / "ocr")
    parts = {"part0001": 3, "part0002": 3, "part0003": 3}
    _ocr_tree(root, "AC287", parts, ocr=["part0001"])
    raw = _scan_zip(parts)

    with pytest.raises(UnparseableRollError) as exc:
        WestBengalConnector(ocr_dir=root).parse_raw(raw, SCANNED_AC, 2002)
    message = str(exc.value)
    assert "part0002" in message and "part0003" in message
    assert "2 of 3" in message


def test_a_scan_carries_an_age_it_can_vouch_for_and_no_locality(tmp_path):
    """The age reaches the record; the locality is a deliberate blank.

    A scanned AC stored no age at all until the confusion matrix behind that
    decision was rebuilt at corpus scale -- see states/west_bengal_ocr.py's
    docstring. It now stores one when the token arrived entirely in Bengali
    numerals and lands in range, and leaves it NULL otherwise, which the
    serving app's required year-of-birth filter spares rather than hides.
    This is the wiring check: the value survives the connector, not just
    parse_row. The locality is a separate matter -- it is printed on a cover
    page that is itself an image, and an unread cell is left empty here
    rather than inferred.
    """
    root = str(tmp_path / "ocr")
    _ocr_tree(root, "AC287", {"part0001": 2})
    raw = _scan_zip({"part0001": 2})

    records = WestBengalConnector(ocr_dir=root).parse_raw(raw, SCANNED_AC, 2002)
    assert records
    assert all(r.age == 35 for r in records)
    assert all(r.locality == "" for r in records)
    # The age is trimmed off whether or not its value survives, or it sits
    # in the searched relative-name field.
    assert all(r.full_relative_name == "হরি মণ্ডল" for r in records)


def test_a_scan_leaves_an_age_it_cannot_vouch_for_null(tmp_path):
    """Same wiring, the other direction. A Latin-arriving age token is one
    Vision read the ink of correctly and filed under the wrong numeral
    system, so the value is a plausible number that is not this elector's
    age -- and a decade-wrong age in a required filter is an elector nobody
    can find, where an absent one is spared."""
    root = str(tmp_path / "ocr")
    latin_age = ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "35",
                 "WBA1234567"]
    _ocr_tree(root, "AC287", {"part0001": 2}, row=latin_age)
    raw = _scan_zip({"part0001": 2})

    records = WestBengalConnector(ocr_dir=root).parse_raw(raw, SCANNED_AC, 2002)
    assert records
    assert all(r.age is None for r in records)
    assert all("age not read: Latin digits" in r.remark for r in records)
    assert all(r.full_relative_name == "হরি মণ্ডল" for r in records)


def test_the_ocr_directory_sits_under_this_state_s_registry_raw_dir():
    """states/registry.py is the single source of truth for where a state's
    raw files live, but it imports this connector, so the connector cannot
    import it back to read raw_dir. This is the check that keeps the two
    spellings from drifting apart instead.
    """
    from states.registry import STATE_CONNECTORS
    raw_dir = STATE_CONNECTORS["west_bengal"]["raw_dir"]
    assert os.path.normpath(OCR_DIR) == os.path.join(
        os.path.normpath(raw_dir), OCR_SUBDIR
    )


@pytest.mark.skipif(not os.path.exists(SAME_ROW_ZIP), reason="pilot raw data not downloaded")
def test_a_typeset_ac_is_decoded_and_never_routed_to_ocr(tmp_path):
    """Precedence, stated as a test: the fallback is chosen by asking the PDF
    whether it has a text layer, so an AC that does keeps its own glyphs even
    when an OCR tree happens to exist beside it. Decoded text beats a
    machine reading of a picture of the same text, always.
    """
    root = str(tmp_path / "ocr")
    _ocr_tree(root, "AC146", {"part0001": 1})
    # One real part, re-bundled: the routing decision is made on the first
    # member of the zip, and parsing all 51 parts of AC146 to re-check it
    # would cost a minute of pdfplumber for nothing.
    buf = io.BytesIO()
    with zipfile.ZipFile(SAME_ROW_ZIP) as src, zipfile.ZipFile(buf, "w") as dst:
        name = sorted(n for n in src.namelist() if n.startswith("part"))[0]
        dst.writestr(name, src.read(name))
    ac = Constituency(ac_code="AC146", ac_name="test", district="Kolkata")

    records = WestBengalConnector(ocr_dir=root).parse_raw(buf.getvalue(), ac, 2002)
    assert records
    assert {r.locality for r in records} == {"PARK STREET"}
    assert any(r.age for r in records), "a typeset AC keeps its ages"


def test_a_part_vision_could_not_read_is_carried_and_named_not_refused(tmp_path, capsys):
    """The other half of the half-OCR'd rule above, and deliberately the
    opposite answer.

    An unfinished run is refused because re-running closes it. A page Vision
    answered for and could not read is not closable that way -- AC287's
    part0103 is a 15.9MB PDF declaring a 1859x2630pt page box where every
    other part is A4, and Vision rejects every page of it as "Bad image
    data." however many times it is asked. Refusing the AC over that
    publishes "AC287 is not digitized" in place of "AC287 is digitized with
    23 pages missing", and the second claim is the true one. So it builds,
    and the build log names the part, the page and Vision's own words.
    """
    root = tmp_path / "ocr"
    ac_dir = _ocr_tree(str(root), "AC287", {"part0001": 5, "part0103": 5})
    with gzip.open(os.path.join(ac_dir, "part0103", "p0001-0005.json.gz"),
                   "wt", encoding="utf-8") as fh:
        json.dump({"responses": [{"error": {"code": 3, "message": "Bad image data."}}
                                 for _ in range(5)]}, fh)

    conn = WestBengalConnector(ocr_dir=str(root))
    records = conn.parse_raw(_scan_zip({"part0001": 5, "part0103": 5}), SCANNED_AC, 2002)

    assert records, "the readable part must still build"
    assert {r.part_no for r in records} == {1}
    out = capsys.readouterr().out
    assert "part0103" in out and "Bad image data." in out and "5 page(s)" in out


def test_a_scanned_ac_with_nothing_unreadable_says_nothing(tmp_path, capsys):
    root = tmp_path / "ocr"
    _ocr_tree(str(root), "AC287", {"part0001": 5})
    conn = WestBengalConnector(ocr_dir=str(root))
    conn.parse_raw(_scan_zip({"part0001": 5}), SCANNED_AC, 2002)
    assert capsys.readouterr().out == ""
