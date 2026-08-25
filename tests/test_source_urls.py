from states import source_urls
from states.base import VoterRecord
from states.karnataka import CSV_URL_TEMPLATE
from states.registry import STATE_CONNECTORS
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


# --- coverage of every state, not just the ones with a hand-written case ---

def test_every_registered_state_has_declared_how_its_source_url_resolves():
    """The tripwire for the silent failure this module can have.

    A state that is in neither `_STATE_TABLES` nor `_PER_AC_URL_STATES` gets
    `source_url = ""` on every row, and nothing anywhere says so -- the
    build succeeds, the rows score and rank correctly, and the only symptom
    is a missing "view the original document" link. Adding a state must be
    a decision about which of the two shapes it is, taken here rather than
    discovered later.
    """
    assert source_urls.undeclared_states(STATE_CONNECTORS) == []


def test_every_declared_state_resolves_the_acs_its_meta_declares():
    """The AC-code formatter has to agree with what the connector actually
    emits as `ac_code` -- an off-by-one in zero-padding (`HR7` vs `HR07`)
    resolves nothing and reports as an empty string, not as an error.

    Only checks states with a connector; the rest of `eci_codes.STATE_CODES`
    is declared ahead of its connectors landing and has no meta to check
    against yet.
    """
    for state_id in STATE_CONNECTORS:
        if state_id in source_urls._PER_AC_URL_STATES:
            continue
        table = source_urls._load_state_table(state_id)
        assert table, f"{state_id}: no per-part table loaded at all"
        known = {ac_code for ac_code, _part in table}
        declared = {
            ac.ac_code for ac in STATE_CONNECTORS[state_id]["connector_cls"]().list_constituencies()
        }
        unresolvable = sorted(declared - known)
        assert not unresolvable, (
            f"{state_id}: {len(unresolvable)} of {len(declared)} ACs resolve to "
            f"no source URL at all, e.g. {unresolvable[:5]} -- the ac_code "
            f"formatter and the connector disagree"
        )


def test_a_workbook_is_found_by_eci_code_not_by_filename():
    # U05_NCT_OF_Delhi.xlsx is the case that breaks any scheme deriving the
    # filename from the state_id: nothing about "delhi" produces "NCT_OF_Delhi".
    assert source_urls._workbook_path("delhi").endswith("U05_NCT_OF_Delhi.xlsx")
    assert source_urls._workbook_path("haryana").endswith("S07_Haryana.xlsx")


def test_a_state_with_no_workbook_resolves_to_none_rather_than_raising():
    # Karnataka has a declared ECI code (S10) but no workbook ships for it --
    # this dataset carries no per-part table for Karnataka at all.
    assert source_urls._workbook_path("karnataka") is None
    assert source_urls._workbook_path("atlantis") is None


def test_a_csv_extracted_state_joins_on_the_bare_ac_number():
    # Telangana's 2002 roll uses undivided AP's numbering (188-294, not
    # 1-107), so this doubles as a check that nothing renumbers on the way
    # through.
    table = source_urls._load_state_table("telangana")
    assert ("188", 1) in table
    assert table[("188", 1)].startswith("https://www.eci.gov.in/sir/")
    assert ("1", 1) not in table
