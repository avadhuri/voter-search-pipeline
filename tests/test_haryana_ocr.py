import io
import json
import os
import zipfile

import pytest

from states.base import Constituency
from states.haryana import (
    OCR_VALIDATED_ACS,
    SCANNED_ACS,
    HaryanaConnector,
    UnparseableRollError,
)
from states.haryana_ocr import (
    GENDER_ANCHORS,
    RELATION_ANCHORS,
    _canonical,
    artifact_name,
    part_id_of,
    read_artifacts,
    rows_from_page,
)

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "haryana")
SCANNED_ZIP = os.path.join(RAW_DIR, "HR18.zip")


def _word(text, x0, top, width=60, line=0):
    return {"text": text, "x0": x0, "x1": x0 + width, "top": top,
            "bottom": top + 30, "conf": 90.0, "line": ["1", "1", str(line)]}


def _row(line, serial, house, name, relation, relative, gender, age):
    """One synthetic OCR'd table row, at the column positions really measured
    off a 300-dpi render of HR18 part 1 (see states/haryana_ocr.py)."""
    top = 100 + line * 50
    words = [_word(serial, 320, top, line=line)]
    if house:
        words.append(_word(house, 450, top, line=line))
    words.append(_word(name, 706, top, width=140, line=line))
    words.append(_word(relation, 1100, top, line=line))
    words.append(_word(relative, 1230, top, width=140, line=line))
    words.append(_word(gender, 1645, top, line=line))
    words.append(_word(age, 1762, top, line=line))
    return words


def _page(rows):
    return [w for r in rows for w in r]


def test_canonical_maps_ocr_noise_onto_the_printed_legend_values():
    # These are the real misreads counted off HR18 part 1: the male "पु" comes
    # back with trailing dots, the female "म" with a spurious matra, the
    # husband "प" with a half-form.
    assert _canonical("पु...", GENDER_ANCHORS) == "पु"
    assert _canonical("मे", GENDER_ANCHORS) == "म"
    assert _canonical("मम", GENDER_ANCHORS) == "म"
    assert _canonical("प्‌", RELATION_ANCHORS) == "प"
    assert _canonical("पं", RELATION_ANCHORS) == "प"
    # "पि" (father) must win over "प" (husband) -- longest prefix, not first.
    assert _canonical("पिं", RELATION_ANCHORS) == "पि"
    assert _canonical("मा.", RELATION_ANCHORS) == "मा"
    # Something genuinely unlike any legend value is left alone, to be flagged
    # downstream by haryana.py's _normalize() rather than forced onto a code.
    assert _canonical("सम", GENDER_ANCHORS) == "सम"


def test_rows_are_recovered_from_column_positions():
    rows = rows_from_page(_page([
        _row(0, "49", "7क", "सुलोचना", "प", "देविन्द्र", "म", "34"),
        _row(1, "50", "", "सुरेन्द्र", "पि", "जागे राम", "पु", "27"),
        _row(2, "53", "8", "राम किशन", "पि", "टेका", "पु", "62"),
    ]))
    assert [r["serial_no"] for r in rows] == ["49", "50", "53"]
    assert rows[0] == {
        "serial_no": "49", "local_ref": "7क", "full_name": "सुलोचना",
        "relation_code": "प", "full_relative_name": "देविन्द्र",
        "gender": "म", "age": "34",
    }
    # The house column is blank on row 2 without swallowing the name.
    assert (rows[1]["local_ref"], rows[1]["full_name"]) == ("", "सुरेन्द्र")


def test_a_row_whose_gender_ocred_badly_is_recovered_not_dropped():
    # The whole point of the two-pass design: rows 0-2 OCR cleanly and place
    # the columns, so row 3 is still read even though its gender cell came
    # back as "मे" and its relation cell as "प्", neither of which is a legend
    # value. Before this, a third of real rows were lost this way.
    rows = rows_from_page(_page([
        _row(0, "49", "", "सुलोचना", "प", "देविन्द्र", "म", "34"),
        _row(1, "50", "", "सुरेन्द्र", "पि", "जागे राम", "पु", "27"),
        _row(2, "53", "", "राम किशन", "पि", "टेका", "पु", "62"),
        _row(3, "54", "", "लिछमी", "प्‌", "राम किशन", "मे", "59"),
    ]))
    assert len(rows) == 4
    assert rows[3]["full_name"] == "लिछमी"
    assert rows[3]["relation_code"] == "प"
    assert rows[3]["gender"] == "म"


def test_page_furniture_is_not_mistaken_for_a_voter_row():
    # The title line also starts with an integer ("18 - सम्भालका ...") but has
    # nothing in the relation column, so it must not become a record.
    title = [_word("18", 300, 40), _word("-", 363, 40),
             _word("सम्भालका", 390, 40, width=200)]
    rows = rows_from_page(_page([
        title,
        _row(1, "49", "", "सुलोचना", "प", "देविन्द्र", "म", "34"),
        _row(2, "50", "", "सुरेन्द्र", "पि", "जागे राम", "पु", "27"),
        _row(3, "53", "", "राम किशन", "पि", "टेका", "पु", "62"),
    ]))
    assert [r["serial_no"] for r in rows] == ["49", "50", "53"]


def test_a_page_with_no_readable_table_yields_nothing():
    # A cover page, or one OCR failed on: too few confidently-read rows to
    # place the columns, so no rows are guessed at.
    assert rows_from_page(_page([
        _row(0, "49", "", "सुलोचना", "प", "देविन्द्र", "म", "34"),
    ])) == []


def test_artifact_naming_round_trips():
    assert artifact_name("0001") == "part0001.ocr.json"
    assert part_id_of("part0001.ocr.json") == "0001"
    assert part_id_of("part0001.pdf") == "0001"
    assert part_id_of("part098A.pdf") == "098A"
    assert part_id_of("manifest.json") is None


def test_read_artifacts_treats_unreadable_bytes_as_not_yet_ocred():
    # So parse_raw()'s "run the OCR pass first" path also covers a missing or
    # truncated download, rather than surfacing a BadZipFile.
    assert read_artifacts(b"") == {}


def test_ocr_validated_acs_are_a_subset_of_the_scanned_ones():
    # The flag means "the OCR path has actually been run and checked on this
    # AC", so it is only meaningful on an AC that routes into that path at
    # all -- and it must never be read as a claim about the other scans.
    assert OCR_VALIDATED_ACS < SCANNED_ACS
    validated = {c.extra["ac_id"] for c in HaryanaConnector().list_constituencies()
                 if c.extra["ocr_validated"]}
    assert validated == set(OCR_VALIDATED_ACS)


def _scanned_ac():
    return next(
        c for c in HaryanaConnector().list_constituencies()
        if c.extra["roll_format"] == "scanned"
    )


def test_scanned_ac_without_artifacts_still_refuses_to_guess():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("part0001.pdf", b"%PDF-1.4 not really")
    with pytest.raises(UnparseableRollError, match="OCR"):
        HaryanaConnector().parse_raw(buf.getvalue(), _scanned_ac(), roll_year=2002)


def test_scanned_ac_records_are_built_from_ocr_artifacts():
    artifact = {"engine": "tesseract", "lang": "hin", "dpi": 300, "psm": "6",
                "pages": [{"words": _page([
                    _row(0, "49", "7क", "सुलोचना", "प", "देविन्द्र", "म", "34"),
                    _row(1, "50", "", "सुरेन्द्र", "पि", "जागे राम", "पु", "27"),
                    _row(2, "53", "8", "राम किशन", "पि", "टेका", "पु", "62"),
                ])}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("part0007.pdf", b"%PDF-1.4 the page images stay put")
        zf.writestr("part0007.ocr.json", json.dumps(artifact, ensure_ascii=False))

    ac = Constituency(ac_code="HR18", ac_name="Samalkha", district="Panipat",
                      extra={"ac_id": 18, "roll_format": "scanned"})
    records = HaryanaConnector().parse_raw(buf.getvalue(), ac, roll_year=2002)

    assert len(records) == 3
    first = records[0]
    assert (first.part_no, first.serial_no) == (7, 49)
    assert first.full_name == "सुलोचना"
    assert first.full_relative_name == "देविन्द्र"
    assert (first.relation_code, first.gender, first.age) == ("H", "F", 34)
    assert first.local_ref == "7क"
    assert (first.state, first.roll_year) == ("haryana", 2002)
    # A scanned row is never silently indistinguishable from a decoded
    # text-layer one.
    assert all("OCR" in r.remark for r in records)
    # Nothing on a scanned cover page OCRs reliably enough to read a locality.
    assert {r.locality for r in records} == {""}


@pytest.mark.skipif(not os.path.exists(SCANNED_ZIP),
                    reason="scanned raw data not downloaded/OCR'd")
def test_real_scanned_ac_parses_into_devanagari_records():
    connector = HaryanaConnector()
    ac = next(c for c in connector.list_constituencies() if c.ac_code == "HR18")
    with open(SCANNED_ZIP, "rb") as f:
        raw = f.read()
    if not read_artifacts(raw):
        pytest.skip("HR18.zip has not been through `make ocr-haryana` yet")

    records = connector.parse_raw(raw, ac, roll_year=2002)
    assert len(records) > 100

    # Best-effort OCR, so this asserts the shape of the result rather than any
    # exact string: real Devanagari names, and the two closed-vocabulary
    # columns landing on their documented codes for the large majority of rows.
    named = [r for r in records if r.full_name]
    assert len(named) / len(records) > 0.9
    assert any("ऀ" <= ch <= "ॿ" for r in named for ch in r.full_name)
    coded = sum(1 for r in records if r.relation_code in {"F", "H", "M", "O"})
    assert coded / len(records) > 0.9
    gendered = sum(1 for r in records if r.gender in {"M", "F"})
    assert gendered / len(records) > 0.85
