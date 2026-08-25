from states.base import Constituency
from states.karnataka import KarnatakaConnector

SAMPLE_ROW = (
    "BANGALORE URBAN,A085,Shivajinagar,5,201,,Shivaram,,Ramaiah,,F,45,M\r\n"
    "BANGALORE URBAN,A085,Shivajinagar,5,202,,Lakshmi,,Shivaram,,H,38,F\r\n"
    "bad,row,too,few,cols\r\n"
    "\r\n"
)


def test_parse_raw_normalizes_clean_rows():
    connector = KarnatakaConnector()
    ac = Constituency(ac_code="A085", ac_name="Shivajinagar", district="BANGALORE URBAN")
    records = connector.parse_raw(SAMPLE_ROW.encode("utf-8"), ac, roll_year=2002)

    clean = [r for r in records if not r.remark]
    assert len(clean) == 2
    assert clean[0].full_name == "Shivaram"
    assert clean[0].relation_code == "F"
    assert clean[0].age == 45
    assert clean[0].gender == "M"
    assert clean[0].roll_year == 2002
    assert clean[0].state == "karnataka"
    assert clean[1].full_relative_name == "Shivaram"


def test_parse_raw_keeps_a_malformed_row_and_says_what_was_wrong_with_it():
    """The connector's contract is that every data row survives -- a row it
    cannot fully understand is kept with the damage recorded in `remark`,
    not discarded.

    This test used to assert the opposite (it was named
    ...skips_malformed and asserted 2 records out of 3 rows), and had been
    failing ever since the connector changed. It was carried as a "known
    pre-existing failure" rather than reconciled, which is the wrong end to
    leave it at: a red test says nothing about which of the two behaviours
    is intended, so it stopped defending either one.

    Keeping the row is the intended behaviour, and the reason is in
    parse_raw's docstring. A dropped row is invisible: the AC's total simply
    comes out lower, and nothing downstream can tell a roll that genuinely
    has fewer electors from a parser that quietly discarded some. The remark
    is what makes the damage auditable per row.
    """
    connector = KarnatakaConnector()
    ac = Constituency(ac_code="A085", ac_name="Shivajinagar", district="BANGALORE URBAN")
    records = connector.parse_raw(SAMPLE_ROW.encode("utf-8"), ac, roll_year=2002)

    # Three data rows in, three records out. The trailing blank line is not a
    # data row and is the one thing that is dropped.
    assert len(records) == 3

    bad = records[2]
    assert bad.remark, "the malformed row was kept, but with nothing recorded about it"

    # Each distinct problem in the row is named separately, so a reader of
    # the remark can tell a short row from a bad ac_code from a non-numeric
    # field rather than getting one undifferentiated "malformed".
    assert "only 5 columns (expected 13)" in bad.remark
    assert "non-numeric part_no: 'few'" in bad.remark
    assert "non-numeric serial_no: 'cols'" in bad.remark
    assert "source row listed ac_code 'row'" in bad.remark

    # Fields that could not be parsed are null rather than guessed at, and
    # the AC identity still comes from the filename, not the junk row.
    assert bad.part_no is None
    assert bad.serial_no is None
    assert bad.ac_code == "A085"
    assert bad.district == "BANGALORE URBAN"


def test_list_constituencies_reads_real_ac_meta():
    connector = KarnatakaConnector()
    constituencies = connector.list_constituencies()
    assert len(constituencies) == 224
    codes = {c.ac_code for c in constituencies}
    assert "A085" in codes
