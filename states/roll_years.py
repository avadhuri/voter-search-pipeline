"""
Build-time resolution of each state's electoral-roll year.

India's "2002 rolls" are not all from 2002. The intensive-revision cycle ran
per state across 2002-2006, so a build that assumes one year for everything
silently mis-stamps whole states -- and `roll_year` is not decoration: the
serving app derives an elector's year of birth as `roll_year - age` for its
(required) year-of-birth filter. A state stamped 2002 when its roll is
actually 2006 mis-targets every one of its electors by four years, which
reads to a user as "you were never on the roll" rather than as a bug.

The years come from `states/meta/sir_source_urls/state_roll_years.json`,
which ships alongside the per-part source-URL workbooks in the same
directory and covers all 36 states/UTs (see that directory's README for
provenance). That file has been in the repo, correct and unread by any code,
since the source-URL work landed -- this module is what wires it up.

Keyed by ECI state code (S01, S07, U05, ...), so mapping a registry
`state_id` onto it needs the same explicit table `source_urls.py` keeps for
its workbooks, and for the same reason: the mapping is asserted per state
after checking, never inferred from a slug. `STATE_CODES` below is that
table, and it is deliberately the single place a new state declares which
ECI code it is.

Fallback is `DEFAULT_ROLL_YEAR` (2002) for a state not listed, matching the
old hardcoded behaviour rather than raising -- a state whose code hasn't
been added yet builds exactly as it did before this module existed. But
`resolve_roll_year()` is the only path that decides, so an unlisted state is
one line away from being right instead of being a silently-wrong build.
"""
import functools
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROLL_YEARS_PATH = os.path.join(
    _HERE, "meta", "sir_source_urls", "state_roll_years.json"
)

DEFAULT_ROLL_YEAR = 2002

# registry state_id -> ECI state code, as used by state_roll_years.json and
# the sir_source_urls workbook filenames. Only states this repo actually
# has a connector for need an entry; anything else falls back to
# DEFAULT_ROLL_YEAR. Karnataka/West Bengal/Haryana are all genuinely 2002,
# so listing them changes nothing today -- they're here so the table is the
# complete statement of what we know, not just the exceptions.
STATE_CODES = {
    "karnataka": "S10",
    "west_bengal": "S25",
    "haryana": "S07",
}


@functools.lru_cache(maxsize=1)
def _load_roll_years():
    """ECI state code -> roll year, loaded once per process. Returns {} if
    the metadata file is missing rather than raising, so a partial checkout
    degrades to the old hardcoded-2002 behaviour instead of failing every
    build."""
    if not os.path.exists(_ROLL_YEARS_PATH):
        return {}
    with open(_ROLL_YEARS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    years = {}
    for state_code, entry in raw.items():
        year = entry.get("roll_year")
        if isinstance(year, int):
            years[state_code] = year
    return years


def resolve_roll_year(state_id, default=DEFAULT_ROLL_YEAR):
    """The roll year for one registry state_id.

    Falls back to `default` for a state with no STATE_CODES entry, or whose
    ECI code isn't in the metadata file -- see the module docstring for why
    that's a fallback rather than an error.
    """
    state_code = STATE_CODES.get(state_id)
    if state_code is None:
        return default
    return _load_roll_years().get(state_code, default)


def roll_year_for(info, state_id, override=None):
    """The roll year to build one state with, in precedence order:
    an explicit `override` (a CLI flag), then the registry entry's own
    `roll_year` if it carries one, then the metadata file, then the default.

    Exists so `build_db.py` has one call to make per state instead of
    repeating this precedence at each of its four build entry points.
    """
    if override is not None:
        return override
    declared = (info or {}).get("roll_year")
    if declared is not None:
        return declared
    return resolve_roll_year(state_id)
