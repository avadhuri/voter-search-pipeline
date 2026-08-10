import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from states.base import Constituency
from states.haryana import (
    GENDER_NORMALIZE,
    RELATION_NORMALIZE,
    HaryanaConnector,
    UnparseableRollError,
    _normalize,
    _pad_part,
    _parse_int,
    _part_candidates,
)

PILOT_ZIP = os.path.join(
    os.path.dirname(__file__), "..", "data", "raw", "haryana", "HR47.zip"
)


def test_list_constituencies_reads_real_ac_meta():
    constituencies = HaryanaConnector().list_constituencies()
    assert len(constituencies) == 90
    by_code = {c.ac_code: c for c in constituencies}
    assert by_code["HR47"].ac_name == "Rajound"
    assert by_code["HR47"].district == "Jind"
    assert sum(1 for c in constituencies if c.extra["roll_format"] == "text") == 44


def test_pad_part_matches_the_sites_own_string_padding():
    # The portal left-pads to 4 characters as text, so a letter suffix is
    # preserved rather than being coerced through an integer.
    assert _pad_part("1") == "0001"
    assert _pad_part("232") == "0232"
    assert _pad_part("98A") == "098A"


def test_part_candidates_fills_gaps_the_portal_list_omits():
    # Real AC61 case: the portal lists "70" twice and never lists "71", but
    # CMB0610071.pdf exists. Trusting the list alone would drop a whole part.
    candidates = _part_candidates(["70", "70", "72"])
    assert "0071" in candidates
    assert candidates == [f"{i:04d}" for i in range(1, 73)]


def test_part_candidates_keeps_non_numeric_ids_the_portal_lists():
    candidates = _part_candidates(["1", "2", "98A"])
    assert "098A" in candidates
    assert "0001" in candidates and "0002" in candidates


def test_normalize_flags_unrecognized_values_instead_of_guessing():
    remarks = []
    assert _normalize("पि", RELATION_NORMALIZE, "relation_code", remarks) == "F"
    assert _normalize("पु.", GENDER_NORMALIZE, "gender", remarks) == "M"
    assert not remarks

    # An unknown value keeps its raw text AND earns a remark -- never silently
    # dropped, never silently mapped onto a code it might not mean.
    assert _normalize("ज़", RELATION_NORMALIZE, "relation_code", remarks) == "ज़"
    assert remarks == ["unrecognized relation_code: 'ज़'"]


def test_parse_int_treats_blank_as_normal_but_flags_garbage():
    remarks = []
    assert _parse_int("", "age", remarks) is None
    assert not remarks
    assert _parse_int("45", "age", remarks) == 45
    assert not remarks
    assert _parse_int("4x", "age", remarks) is None
    assert remarks == ["non-numeric age: '4x'"]


def test_scanned_acs_raise_rather_than_looking_like_an_empty_roll():
    connector = HaryanaConnector()
    scanned = next(
        c for c in connector.list_constituencies() if c.extra["roll_format"] == "scanned"
    )
    with pytest.raises(UnparseableRollError):
        connector.parse_raw(b"", scanned, roll_year=2002)


@pytest.mark.skipif(not os.path.exists(PILOT_ZIP), reason="pilot raw data not downloaded")
def test_parse_raw_decodes_a_real_part_into_devanagari_records():
    connector = HaryanaConnector()
    ac = next(c for c in connector.list_constituencies() if c.ac_code == "HR47")

    # Re-bundle a single part rather than parsing all 130 -- the whole AC takes
    # ~4 minutes, which is not a unit test.
    with zipfile.ZipFile(PILOT_ZIP) as src:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as one:
            one.writestr("part0001.pdf", src.read("part0001.pdf"))
    records = connector.parse_raw(buf.getvalue(), ac, roll_year=2002)

    # Verified by eye against the rendered page 2 of part 1 (see the module
    # docstring in states/haryana.py for the table layout).
    first = records[0]
    assert (first.part_no, first.serial_no) == (1, 1)
    assert first.full_name == "हैबुड़"
    assert first.full_relative_name == "एजिक"
    assert first.relation_code == "F"
    assert first.gender == "M"
    assert first.age == 74
    assert first.local_ref == "2/1"
    assert first.state == "haryana" and first.roll_year == 2002

    # Row 7 on that page is a wife listed against her husband.
    row7 = next(r for r in records if r.serial_no == 7)
    assert (row7.full_name, row7.relation_code, row7.gender) == ("दुलारी", "H", "F")

    assert {r.relation_code for r in records} <= {"F", "H", "M", "O", ""}
    assert {r.gender for r in records} <= {"M", "F", ""}
    flagged = sum(1 for r in records if r.remark)
    assert flagged / len(records) < 0.01
