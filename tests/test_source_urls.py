from states.base import VoterRecord
from states.karnataka import CSV_URL_TEMPLATE
from states.source_urls import resolve_source_url


def _record(**overrides):
    fields = dict(
        state="karnataka", district="BANGALORE URBAN", ac_code="A085",
        ac_name="Shivajinagar", part_no=5, serial_no=201, local_ref="",
        full_name="Shivaram", full_relative_name="Ramaiah",
        relation_code="F", age=45, gender="M", roll_year=2002,
    )
    fields.update(overrides)
    return VoterRecord(**fields)


def test_karnataka_gets_per_ac_csv_url_unconditionally():
    # Karnataka has no per-part granularity to join against -- every row of
    # an AC gets the same CSV URL regardless of part_no/serial_no.
    rec = _record(state="karnataka", ac_code="A085", part_no=5, serial_no=201)
    assert resolve_source_url(rec) == CSV_URL_TEMPLATE.format(ac_code="A085")

    rec2 = _record(state="karnataka", ac_code="A085", part_no=99, serial_no=1)
    assert resolve_source_url(rec2) == resolve_source_url(rec)


def test_haryana_resolves_known_ac_part_to_eci_url():
    # HR47 (Rajound, per states/meta/haryana_ac_meta.json) is ac_id 47 --
    # dataset's "AC No" 47 -- confirmed by cross-referencing AC names
    # (see states/source_urls.py's module docstring).
    rec = _record(state="haryana", ac_code="HR47", part_no=1)
    url = resolve_source_url(rec)
    assert url == "https://www.eci.gov.in/sir/f1/S07/data/OLDSIRROLL/S07/47/S07_47_1.pdf"


def test_haryana_missing_part_falls_back_to_empty_string():
    rec = _record(state="haryana", ac_code="HR47", part_no=999999)
    assert resolve_source_url(rec) == ""


def test_west_bengal_resolves_known_ac_part():
    # AC001 (Mekhliganj) part 1 -- confirmed against
    # states/meta/west_bengal_ac_meta.json's ac_no field.
    rec = _record(state="west_bengal", ac_code="AC001", part_no=1)
    url = resolve_source_url(rec)
    assert url.startswith("https://ceowestbengal.wb.gov.in/RollPDF/GetDraft?acId=1&key=")


def test_unknown_state_falls_back_to_empty_string():
    rec = _record(state="some_future_state", ac_code="X001", part_no=1)
    assert resolve_source_url(rec) == ""
