"""
Build-time resolution of each row's original source-document URL.

Joins states/meta/sir_source_urls/*.xlsx (State|District|AC No|AC Name|Part
No|PDF Link, one workbook per state/UT -- see that directory's README for
provenance) against this repo's own ac_code format, so build_db.py can write
a source_url column per row with no runtime lookup logic needed in the
serving app at all (mirrors how part_no/serial_no already flow straight
through today).

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
  - Karnataka: not applicable -- this dataset carries no per-part table
    for Karnataka at all (its own source is one CSV per AC, no per-part
    granularity exists to describe -- see the sir_source_urls README).
    Every Karnataka row gets the same per-AC CSV URL instead, from
    states.karnataka.CSV_URL_TEMPLATE, unconditionally.

Haryana dual-host note: states/haryana.py's own PART_PDF_URL (state CEO
host) and this dataset's eci.gov.in/sir/f1 URL were spot-checked (two
different AC+part pairs, HR47 part 1 and HR01 part 10) and found
byte-identical (matching Content-Length and MD5) -- both serve the exact
same 2002 roll PDF. This module links to the dataset's eci.gov.in URL since
it's the one this dataset provides pre-resolved per part, not because the
state host was found lacking.
"""
import functools
import os

import openpyxl

from states.karnataka import CSV_URL_TEMPLATE

_HERE = os.path.dirname(os.path.abspath(__file__))
_META_DIR = os.path.join(_HERE, "meta", "sir_source_urls")

# state_id -> (xlsx filename, ac_code formatter). Only states whose
# ac_code<->AC-No mapping has been confirmed (see module docstring) are
# listed here; anything else falls back to "" in resolve_source_url().
_STATE_TABLES = {
    "haryana": ("S07_Haryana.xlsx", lambda ac_no: f"HR{ac_no:02d}"),
    "west_bengal": ("S25_West_Bengal.xlsx", lambda ac_no: f"AC{ac_no:03d}"),
}


@functools.lru_cache(maxsize=None)
def _load_state_table(state_id):
    """(ac_code, part_no) -> source URL for one state, loaded once per
    process (cached -- a build touching thousands of rows for the same
    state must not re-parse the xlsx per row). Returns {} for a state this
    dataset doesn't cover per-part (Karnataka) or doesn't know at all."""
    entry = _STATE_TABLES.get(state_id)
    if entry is None:
        return {}
    filename, ac_code_fmt = entry
    path = os.path.join(_META_DIR, filename)
    if not os.path.exists(path):
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
    if record.state == "karnataka":
        return CSV_URL_TEMPLATE.format(ac_code=record.ac_code)
    return _load_state_table(record.state).get((record.ac_code, record.part_no), "")
