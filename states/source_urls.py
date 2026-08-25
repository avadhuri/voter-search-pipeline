"""
Build-time resolution of each row's original source-document URL.

Joins states/meta/sir_source_urls/*.xlsx (State|District|AC No|AC Name|Part
No|PDF Link, one workbook per state/UT -- see that directory's README for
provenance) against this repo's own ac_code format, so build_db.py can write
a source_url column per row with no runtime lookup logic needed in the
serving app at all (mirrors how part_no/serial_no already flow straight
through today).

Which workbook a state reads is derived from its ECI code
(states/eci_codes.py) rather than written down again here, so a new state
declares its code once and both this join and the roll-year one start
resolving.

AC-number -> ac_code mapping (confirmed by cross-referencing every AC's
name across the two data sources, not assumed 1:1):

  - Haryana: ac_code == f"HR{ac_no:02d}". The dataset's bare "AC No" is
    exactly this connector's own ac_id (states/haryana.py derives
    ac_code the same way in refresh_meta()). Verified against every one
    of the 90 ACs in states/meta/haryana_ac_meta.json: AC *name* matches
    exactly for all 90; only a handful of *district* spellings differ
    between the two sources (Jhajjar/Jhajjer, Sonipat/Sonepat,
    Hisar/Hissar, Mahendergarh/Mahendragarh) -- a labeling difference,
    not a numbering mismatch.
  - West Bengal: ac_code == f"AC{ac_no:03d}", the same ac_no already
    recorded in states/meta/west_bengal_ac_meta.json. Verified against
    all 294 ACs in that file: AC name matches exactly for every one (only
    ~19 Kolkata ACs are actually loaded/searchable per registry.py, but
    the mapping was checked against the full list, not just those).
  - Every CSV-extracted state (states/csv_connector.py): ac_code is the
    bare AC number as a string, because that connector's
    list_constituencies() emits `str(ac["ac_no"])` straight off the
    state's meta JSON -- so the join needs no formatting at all.
    Verified against every one of those states' meta files, at *part*
    granularity rather than AC granularity: all 186,190 (ac_no, part_no)
    pairs across the 20 workbooks resolve to a URL, 0 unresolved. That
    check is what this claim rests on -- see
    test_every_declared_state_resolves_the_acs_its_meta_declares, which
    re-runs the AC half of it on every test run.
  - Karnataka: not applicable -- this dataset carries no per-part table
    for Karnataka at all (its own source is one CSV per AC, no per-part
    granularity exists to describe -- see the sir_source_urls README).
    Every Karnataka row gets the same per-AC CSV URL instead, from
    states.karnataka.CSV_URL_TEMPLATE, unconditionally. That is the one
    shape a state can take other than "per-part workbook", and it is
    declared in _PER_AC_URL_STATES rather than being a bare `if`, so
    "this state has no per-part source table" stays a decision someone
    wrote down -- see undeclared_states() and the test that uses it.

Haryana dual-host note: states/haryana.py's own PART_PDF_URL (state CEO
host) and this dataset's eci.gov.in/sir/f1 URL were spot-checked (two
different AC+part pairs, HR47 part 1 and HR01 part 10) and found
byte-identical (matching Content-Length and MD5) -- both serve the exact
same 2002 roll PDF. This module links to the dataset's eci.gov.in URL since
it's the one this dataset provides pre-resolved per part, not because the
state host was found lacking.
"""
import functools
import glob
import os

import openpyxl

from states.eci_codes import STATE_CODES
from states.karnataka import CSV_URL_TEMPLATE

_HERE = os.path.dirname(os.path.abspath(__file__))
_META_DIR = os.path.join(_HERE, "meta", "sir_source_urls")


def _bare_ac_no(ac_no):
    """ac_code for a CSV-extracted state: the AC number as a plain string,
    exactly as states/csv_connector.py's list_constituencies() emits it."""
    return str(ac_no)


# The 19 states whose per-AC CSVs come from the extraction pipeline. Listed
# by name rather than detected from the connector class, so this module has
# no import-time dependency on the registry (which imports the connectors,
# which would close a cycle) -- and so that adding a state is a deliberate
# entry here rather than something a base-class choice silently decides.
_BARE_AC_NO_STATES = frozenset({
    "arunachal_pradesh", "assam", "chandigarh", "chhattisgarh", "delhi",
    "goa", "himachal_pradesh", "lakshadweep", "madhya_pradesh", "meghalaya",
    "mizoram", "nagaland", "puducherry", "punjab", "rajasthan", "sikkim",
    "telangana", "tripura", "uttarakhand",
})

# state_id -> ac_code formatter, for states joined against a per-part
# workbook. Only states whose ac_code<->AC-No mapping has been confirmed
# (see module docstring) are listed; anything else falls back to "" in
# resolve_source_url(). The workbook itself is found by ECI code, not named
# here -- see _workbook_path().
_STATE_TABLES = dict(
    {
        "haryana": lambda ac_no: f"HR{ac_no:02d}",
        "west_bengal": lambda ac_no: f"AC{ac_no:03d}",
    },
    **{state_id: _bare_ac_no for state_id in _BARE_AC_NO_STATES}
)

# state_id -> URL template taking ac_code, for states the SIR dataset
# carries no per-part table for at all. Karnataka's own source is one CSV
# per AC, so there is no per-part granularity to describe -- every row of an
# AC gets that AC's CSV.
_PER_AC_URL_STATES = {
    "karnataka": CSV_URL_TEMPLATE,
}


def undeclared_states(state_ids):
    """The subset of `state_ids` this module would silently return "" for.

    A state must be declared one of two ways -- per-part workbook
    (_STATE_TABLES) or per-AC URL (_PER_AC_URL_STATES) -- and a state that
    is neither gets an empty source_url on every one of its rows, with
    nothing at build time saying so. That is precisely the failure the
    freshness guards exist for on the serving side: the rows parse, score
    and rank correctly, and the only symptom is a missing "view the
    original document" link that nobody notices until someone asks where a
    name came from. This exists so a test can fail instead.
    """
    return sorted(
        s for s in state_ids
        if s not in _STATE_TABLES and s not in _PER_AC_URL_STATES
    )


@functools.lru_cache(maxsize=None)
def _workbook_path(state_id):
    """The sir_source_urls workbook for a state, located by its ECI code
    prefix (`S07_Haryana.xlsx`, `U05_NCT_OF_Delhi.xlsx`) rather than by a
    filename written down per state -- the suffix is the ECI's own spelling
    of the state name and does not reliably match our state_id.

    None if the state has no declared ECI code, or no workbook ships for
    that code (Karnataka is the real instance of the latter).
    """
    code = STATE_CODES.get(state_id)
    if code is None:
        return None
    matches = sorted(glob.glob(os.path.join(_META_DIR, f"{code}_*.xlsx")))
    if len(matches) != 1:
        return None
    return matches[0]


@functools.lru_cache(maxsize=None)
def _load_state_table(state_id):
    """(ac_code, part_no) -> source URL for one state, loaded once per
    process (cached -- a build touching thousands of rows for the same
    state must not re-parse the xlsx per row). Returns {} for a state this
    dataset doesn't cover per-part (Karnataka) or doesn't know at all."""
    ac_code_fmt = _STATE_TABLES.get(state_id)
    if ac_code_fmt is None:
        return {}
    path = _workbook_path(state_id)
    if path is None:
        return {}

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        next(rows)  # header: State, District, AC No, AC Name, Part No, PDF Link
        table = {}
        for row in rows:
            _state, _district, ac_no, _ac_name, part_no, url = row[:6]
            if ac_no is None or part_no is None or not url:
                continue
            table[(ac_code_fmt(int(ac_no)), int(part_no))] = url
        return table
    finally:
        wb.close()


def resolve_source_url(record):
    """The origin-document URL for one VoterRecord.

    Karnataka: the per-AC CSV URL, unconditionally -- there is no
    per-part granularity to join against. Every other state: looked up
    by (ac_code, part_no) in its sir_source_urls table, falling back to
    "" if genuinely missing (e.g. a part this dataset doesn't carry)
    rather than raising -- a missing source link is an accepted
    low-severity gap for this feature, not a build-blocking error.
    """
    template = _PER_AC_URL_STATES.get(record.state)
    if template is not None:
        return template.format(ac_code=record.ac_code)
    return _load_state_table(record.state).get((record.ac_code, record.part_no), "")
