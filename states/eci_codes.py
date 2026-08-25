"""
The one table mapping this repo's `state_id` onto the Election Commission's
own state/UT code (S01-S29, U01-U09).

Two separate build-time joins need it, and they used to keep private copies:
`states/roll_years.py` (which roll year to stamp, keyed by ECI code in
`state_roll_years.json`) and `states/source_urls.py` (which per-part source
workbook to read, whose filenames are `<code>_<Name>.xlsx`). A state present
in one copy and absent from the other is a silent half-wired state -- right
roll year, empty source links, or the reverse -- so there is one copy.

**The mapping is asserted per state after checking, never inferred from the
slug.** `states/meta/sir_source_urls/`'s filenames are close enough to a
`state_id` to make a `.replace("_", " ")` match look like it works, and it
does, right up to `U05_NCT_OF_Delhi` and the states whose CEO spelling
differs from ours. Adding a state means reading its code off the ECI source
and writing it here.

Adding a state's entry here is the *whole* wiring cost for both joins: the
roll year and the per-part source URLs both start resolving, with no further
per-state code. What it does not do is assert that a workbook exists (some
states genuinely have none -- see `source_urls._PER_AC_URL_STATES`) or that
the AC numbering lines up; `source_urls.py` documents that check per state,
and `tests/test_source_urls.py` enforces that every registered state has
made one decision or the other.
"""

# state_id -> ECI state/UT code. Entries exist for states this repo doesn't
# have a connector for yet: the code is a fact about the state, not about
# our support for it, and pre-declaring it is what lets a new connector be
# one registry entry rather than a hunt through three files. An unlisted
# state degrades (2002 roll year, empty source_url) rather than failing --
# see each consumer for its own fallback.
STATE_CODES = {
    # --- states with a connector today ---
    "karnataka": "S10",
    "west_bengal": "S25",
    "haryana": "S07",

    # --- the 19 CSV-extracted states, ahead of their connectors landing ---
    "arunachal_pradesh": "S02",
    "assam": "S03",
    "goa": "S05",
    "himachal_pradesh": "S08",
    "madhya_pradesh": "S12",
    "meghalaya": "S15",
    "mizoram": "S16",
    "nagaland": "S17",
    "punjab": "S19",
    "rajasthan": "S20",
    "sikkim": "S21",
    "tripura": "S23",
    "chhattisgarh": "S26",
    "uttarakhand": "S28",
    "telangana": "S29",
    "chandigarh": "U02",
    "delhi": "U05",
    "lakshadweep": "U06",
    "puducherry": "U07",
}


def eci_code(state_id):
    """The ECI code for a state_id, or None if it hasn't been declared."""
    return STATE_CODES.get(state_id)
