import os
import zipfile

import pytest

from states.base import Constituency
from states.west_bengal import WestBengalConnector

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
    assert {r.locality for r in records} == {"317, নিত্যাননদী (অংশ)"}


@pytest.mark.skipif(
    not os.path.exists(BENGALI_BLANK_ZIP), reason="raw data not downloaded"
)
def test_blank_bengali_locality_is_empty_not_the_next_label():
    records = _parse_first_part(BENGALI_BLANK_ZIP, "AC260")
    assert records
    assert {r.locality for r in records} == {""}
