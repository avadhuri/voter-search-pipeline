import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from states.base import Constituency
from states.karnataka import KarnatakaConnector

SAMPLE_ROW = (
    "BANGALORE URBAN,A085,Shivajinagar,5,201,,Shivaram,,Ramaiah,,F,45,M\r\n"
    "BANGALORE URBAN,A085,Shivajinagar,5,202,,Lakshmi,,Shivaram,,H,38,F\r\n"
    "bad,row,too,few,cols\r\n"
)


def test_parse_raw_normalizes_rows_and_skips_malformed():
    connector = KarnatakaConnector()
    ac = Constituency(ac_code="A085", ac_name="Shivajinagar", district="BANGALORE URBAN")
    records = connector.parse_raw(SAMPLE_ROW.encode("utf-8"), ac, roll_year=2002)

    assert len(records) == 2
    assert records[0].full_name == "Shivaram"
    assert records[0].relation_code == "F"
    assert records[0].age == 45
    assert records[0].gender == "M"
    assert records[0].roll_year == 2002
    assert records[0].state == "karnataka"
    assert records[1].full_relative_name == "Shivaram"


def test_list_constituencies_reads_real_ac_meta():
    connector = KarnatakaConnector()
    constituencies = connector.list_constituencies()
    assert len(constituencies) == 224
    codes = {c.ac_code for c in constituencies}
    assert "A085" in codes
