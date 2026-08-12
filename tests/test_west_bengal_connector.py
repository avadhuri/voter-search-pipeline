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
