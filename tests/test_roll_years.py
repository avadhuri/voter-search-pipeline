"""
Per-state roll year: resolution, precedence, and the fact that it actually
reaches the built artifact.

Why this file exists at all: `roll_year` is the one column in `voters` that
is wrong *silently*. A mis-stamped state still parses, still scores, still
ranks, and still passes every search-quality assertion -- it just answers
the serving app's required year-of-birth filter (`roll_year - age`) with a
year that is off by one to four, which reads to a user as "you were never on
the roll" rather than as a bug. Nothing downstream can catch it, so it gets
caught here.
"""
import json
import sqlite3

import pytest

import build_db
from states import roll_years
from states.karnataka import KarnatakaConnector

A085_ROWS = (
    "BANGALORE URBAN,A085,Shivajinagar,5,201,,Shivaram,,Ramaiah,,F,45,M\r\n"
    "BANGALORE URBAN,A085,Shivajinagar,5,202,,Lakshmi,,Shivaram,,H,38,F\r\n"
)


# --- resolution ------------------------------------------------------------

def test_metadata_file_is_present_and_covers_every_state_code():
    """state_roll_years.json shipped with the source-URL work and sat unread
    by any code until roll_years.py. Assert it's actually loadable and
    non-trivial, so a packaging change that drops it from package-data fails
    here rather than silently reverting every state to the 2002 default."""
    years = roll_years._load_roll_years()
    assert len(years) >= 30, f"only {len(years)} state codes loaded"
    assert all(isinstance(y, int) for y in years.values())
    # The three live states are genuinely 2002 -- this is what makes wiring
    # roll_years.py a no-op for them, which is the regression-safety property.
    assert years["S10"] == 2002  # Karnataka
    assert years["S25"] == 2002  # West Bengal
    assert years["S07"] == 2002  # Haryana


def test_the_rolls_are_not_all_from_2002():
    """The premise of the whole module. If this ever passes trivially,
    per-state resolution stopped being necessary and this can all go."""
    years = set(roll_years._load_roll_years().values())
    assert years - {2002}, "every state is 2002 -- roll_years.py is dead code"


def test_every_live_state_resolves_to_2002():
    for state_id in ("karnataka", "west_bengal", "haryana"):
        assert roll_years.resolve_roll_year(state_id) == 2002


def test_unknown_state_falls_back_to_the_default_rather_than_raising():
    """A state whose ECI code hasn't been added to STATE_CODES yet must build
    exactly as it did before this module existed -- one line away from being
    right, not a hard failure mid-build."""
    assert roll_years.resolve_roll_year("atlantis") == roll_years.DEFAULT_ROLL_YEAR


@pytest.mark.parametrize("info,state_id,override,expected", [
    ({}, "karnataka", None, 2002),                      # metadata file
    ({}, "atlantis", None, 2002),                       # default
    ({"roll_year": 2003}, "karnataka", None, 2003),     # registry declaration
    ({"roll_year": 2003}, "karnataka", 1999, 1999),     # explicit override
    ({}, "karnataka", 1999, 1999),
])
def test_precedence_is_override_then_registry_then_file_then_default(
    info, state_id, override, expected
):
    assert roll_years.roll_year_for(info, state_id, override=override) == expected


# --- it reaches the artifact ----------------------------------------------

def _karnataka_at(raw_dir, **extra):
    entry = {
        "connector_cls": KarnatakaConnector,
        "label": "Karnataka",
        "raw_dir": str(raw_dir),
        "raw_glob": "*.csv",
        "script": "latin",
    }
    entry.update(extra)
    return entry


def test_state_coverage_carries_the_roll_year(tmp_path, monkeypatch):
    """The serving app renders its year-of-birth ceiling per state at form
    time, when (in AC_DB_DIR mode) it holds only the catalog. So the year has
    to be on state_coverage, not just on the voter rows."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    monkeypatch.setitem(build_db.STATE_CONNECTORS, "karnataka", _karnataka_at(raw_dir))

    build_db.build_multi_state(["karnataka"], str(tmp_path / "multi.sqlite"))

    conn = sqlite3.connect(tmp_path / "multi.sqlite")
    assert conn.execute(
        "SELECT roll_year FROM state_coverage WHERE state_id = 'karnataka'"
    ).fetchone()[0] == 2002
    assert {r[0] for r in conn.execute("SELECT DISTINCT roll_year FROM voters")} == {2002}
    conn.close()


def test_a_registry_declared_roll_year_reaches_both_rows_and_coverage(tmp_path, monkeypatch):
    """The path a genuinely non-2002 state takes. Uses a declared roll_year
    rather than a real non-2002 state because none is registered yet -- when
    one is, this stops being a synthetic case and starts being a real one."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    monkeypatch.setitem(
        build_db.STATE_CONNECTORS, "karnataka", _karnataka_at(raw_dir, roll_year=2005)
    )

    build_db.build_multi_state(["karnataka"], str(tmp_path / "multi.sqlite"))

    conn = sqlite3.connect(tmp_path / "multi.sqlite")
    assert conn.execute(
        "SELECT roll_year FROM state_coverage WHERE state_id = 'karnataka'"
    ).fetchone()[0] == 2005
    assert {r[0] for r in conn.execute("SELECT DISTINCT roll_year FROM voters")} == {2005}
    conn.close()


def test_per_ac_catalog_carries_the_roll_year(tmp_path, monkeypatch):
    """--per-ac is the production path; it used to never pass roll_year at
    all (the __main__ branch dropped it), so every state built as 2002
    regardless. This is the regression test for exactly that."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    monkeypatch.setitem(
        build_db.STATE_CONNECTORS, "karnataka", _karnataka_at(raw_dir, roll_year=2006)
    )
    out_dir = tmp_path / "ac"

    build_db.build_per_ac(["karnataka"], str(out_dir), workers=1)

    cat = sqlite3.connect(out_dir / "catalog" / "karnataka.sqlite")
    assert cat.execute("SELECT roll_year FROM state_coverage").fetchone()[0] == 2006
    cat.close()

    ac = sqlite3.connect(out_dir / "karnataka" / "A085-c1.p0.sqlite")
    assert {r[0] for r in ac.execute("SELECT DISTINCT roll_year FROM voters")} == {2006}
    ac.close()


def test_an_explicit_override_wins_over_every_states_own_year(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "A085.csv").write_text(A085_ROWS)
    monkeypatch.setitem(
        build_db.STATE_CONNECTORS, "karnataka", _karnataka_at(raw_dir, roll_year=2005)
    )
    out_dir = tmp_path / "ac"

    build_db.build_per_ac(["karnataka"], str(out_dir), roll_year=1999, workers=1)

    cat = sqlite3.connect(out_dir / "catalog" / "karnataka.sqlite")
    assert cat.execute("SELECT roll_year FROM state_coverage").fetchone()[0] == 1999
    cat.close()
