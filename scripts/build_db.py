"""
Parse raw state roll files into normalized SQLite database(s). Supports a
single AC (the original POC path), one state's ACs combined (the original
--combine path), multiple states combined into one DB
(--states), and one small file per (state, ac_code) plus a per-state
catalog (--states ... --per-ac) -- the native artifact set for per-AC-file
serving (see voter_search_engine's per-AC-file-serving plan; no combined DB
is built or needed for that path).

Column/row parsing lives in each states/<state>.py connector's parse_raw()
so this script and the download pipeline share one source of truth for each
state's raw format. states/registry.py maps a state_id to its connector
class and where its raw files live, so this script doesn't hardcode that
per state.

Usage:
    build_db.py <raw_path> <sqlite_db_path> [--state <state_id>]
        Build/overwrite a DB from a single AC's raw file, ac_code inferred
        from the filename (matches the original POC's naming, e.g.
        data/raw/A085.csv, and equally data/raw/haryana/HR02.zip). --state
        defaults to karnataka; see DEFAULT_STATE_ID.

    build_db.py --combine <raw_dir> <sqlite_db_path> [--state <state_id>]
        Build/overwrite one combined DB from every raw AC file in <raw_dir>
        matching that state's registry raw_glob. Kept as the original
        POC-regression path; --states below does the same thing without a
        raw_dir override, and also populates state_coverage.

    build_db.py --states karnataka,west_bengal <sqlite_db_path> [--roll-year YYYY] [--acs AC1,AC2]
        Build/overwrite one combined DB across every listed state, each
        read from its states/registry.py raw_dir/raw_glob. Still useful for
        local CLI/dev use (scripts/search.py, ad hoc queries) even though
        production serving has moved to --per-ac below.

    build_db.py --states karnataka,west_bengal --per-ac <out_dir> [--contract c1] [--patch 0] [--workers N] [--roll-year YYYY] [--acs AC1,AC2] [--allow-catalog-shrink]
        Build one <out_dir>/<state>/<ac_code>-<contract>.p<patch>.sqlite per
        AC, plus one <out_dir>/catalog/<state>.sqlite per state (a small
        state_coverage + ac_index summary, no voter rows). contract is the
        schema/shape version consumed by the serving app; patch is a content
        revision within a fixed contract, bumped explicitly (not
        auto-detected) whenever an AC's data is rebuilt/republished. Each
        AC's parse+write is independent (own raw file in, own sqlite file
        out) so this fans out across a process pool -- --workers caps it
        (default: cpu_count - 1). An AC whose output file already exists
        and was fully finalized by a prior run is skipped, not rebuilt --
        makes an interrupted run resumable by just re-running the same
        command, and lets a bigger --workers value be applied retroactively
        without redoing already-finished ACs.

        Each state is stamped with its own roll year (states/roll_years.py,
        from states/meta/sir_source_urls/state_roll_years.json) -- the
        "2002 rolls" are not all from 2002, and the serving app derives an
        elector's year of birth as `roll_year - age`. --roll-year overrides
        that for every state in the build; omit it unless you specifically
        mean to.

        --acs restricts the build to a named list of AC codes, and fails if
        any of them has no raw file. Without it the scope is every raw file
        present in the state's raw_dir, which makes the same command mean
        different things as downloads accumulate -- see _scope_paths.

        --acs also scopes the *catalog*, which is what makes it sharper than
        it looks: each state's catalog is dropped and rewritten from that
        run's results, never merged, and the serving app shows exactly what
        the catalog names. So listing only the ACs being added takes every
        other AC off the site. To extend a published state, pass every AC it
        should serve at the patch it is already published at -- the built
        ones are skipped rather than reparsed, so the full list re-indexes
        cheaply. --allow-catalog-shrink is the opt-in for meaning it; see
        _guard_catalog_shrink.
"""
import collections
import datetime
import glob
import os
import sqlite3
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.base import (NAME_ABSENT_IN_SOURCE, NAME_UNREAD,
                         UnparseableRollError)
from states.registry import STATE_CONNECTORS
from states.roll_years import resolve_roll_year, roll_year_for
from states.source_urls import resolve_source_url
from transliteration import BackfillResult, backfill_latin_columns

VOTERS_SCHEMA = """
DROP TABLE IF EXISTS voters;
CREATE TABLE voters (
    id INTEGER PRIMARY KEY,
    state TEXT,
    roll_year INTEGER,
    district TEXT,
    ac_code TEXT,
    ac_name TEXT,
    part_no INTEGER,
    serial_no INTEGER,
    local_ref TEXT,
    full_name TEXT,
    full_name_latin TEXT,
    full_relative_name TEXT,
    full_relative_name_latin TEXT,
    relation_code TEXT,
    relation_label TEXT,
    age INTEGER,
    gender TEXT,
    remark TEXT,
    locality TEXT,
    source_url TEXT
);
"""

SCHEMA = VOTERS_SCHEMA + """
DROP TABLE IF EXISTS state_coverage;
CREATE TABLE state_coverage (
    state_id TEXT PRIMARY KEY,
    label TEXT,
    acs_total INTEGER,
    acs_digitized INTEGER,
    locality_coverage TEXT,
    built_at TEXT,
    -- The electoral-roll year this state's rows carry. Denormalized out of
    -- voters.roll_year so the serving app can render its year-of-birth
    -- ceiling per state at form time, without opening a per-AC file (in
    -- AC_DB_DIR mode it holds only the catalog at that point). One value per
    -- state because a state's rolls are revised as a unit -- if that ever
    -- stops being true, this becomes a per-AC column, not a per-state one.
    roll_year INTEGER
);
"""

# Dropping every catalog table and rewriting it from one run's results is what
# made a scoped build silently take published ACs off the site (the failure
# _guard_catalog_shrink exists to refuse). The tables are now created only if
# absent and merged into per AC, so "build these three" means exactly that.
# Replacing a catalog wholesale is still possible -- it is just no longer what
# happens by accident. See CATALOG_RESET_SQL and --replace-catalog.
CATALOG_RESET_SQL = """
DROP TABLE IF EXISTS state_coverage;
DROP TABLE IF EXISTS ac_index;
DROP TABLE IF EXISTS catalog_locality;
"""

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_coverage (
    state_id TEXT PRIMARY KEY,
    label TEXT,
    acs_total INTEGER,
    acs_digitized INTEGER,
    locality_coverage TEXT,
    built_at TEXT,
    -- The electoral-roll year this state's rows carry. Denormalized out of
    -- voters.roll_year so the serving app can render its year-of-birth
    -- ceiling per state at form time, without opening a per-AC file (in
    -- AC_DB_DIR mode it holds only the catalog at that point). One value per
    -- state because a state's rolls are revised as a unit -- if that ever
    -- stops being true, this becomes a per-AC column, not a per-state one.
    roll_year INTEGER
);

CREATE TABLE IF NOT EXISTS ac_index (
    state TEXT,
    ac_code TEXT,
    ac_name TEXT,
    district TEXT,
    contract TEXT,
    patch INTEGER,
    row_count INTEGER,
    file_size_bytes INTEGER,
    has_locality INTEGER,
    PRIMARY KEY (state, ac_code, contract)
);

-- Distinct village/locality strings per AC, needed by the serving app's
-- picker (free-text locality search over every AC's label, rendered before
-- any AC/per-AC-file is fetched -- see VoterRecord.locality). Small (a few
-- hundred distinct strings per AC at most), so it stays in the eagerly-
-- downloaded per-state catalog rather than requiring every state's per-AC
-- files to be present just to power AC discovery.
CREATE TABLE IF NOT EXISTS catalog_locality (
    state TEXT,
    ac_code TEXT,
    locality TEXT,
    PRIMARY KEY (state, ac_code, locality)
);
"""

INSERT_SQL = """
INSERT INTO voters (
    state, roll_year, district, ac_code, ac_name, part_no, serial_no,
    local_ref, full_name, full_relative_name, relation_code,
    relation_label, age, gender, remark, locality, source_url,
    full_name_latin, full_relative_name_latin
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

STATE_COVERAGE_INSERT_SQL = """
INSERT INTO state_coverage (
    state_id, label, acs_total, acs_digitized, locality_coverage, built_at,
    roll_year
) VALUES (?,?,?,?,?,?,?)
ON CONFLICT(state_id) DO UPDATE SET
    label = excluded.label,
    acs_total = excluded.acs_total,
    acs_digitized = excluded.acs_digitized,
    locality_coverage = excluded.locality_coverage,
    built_at = excluded.built_at,
    roll_year = excluded.roll_year
"""

AC_INDEX_INSERT_SQL = """
INSERT INTO ac_index (
    state, ac_code, ac_name, district, contract, patch,
    row_count, file_size_bytes, has_locality
) VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(state, ac_code, contract) DO UPDATE SET
    ac_name = excluded.ac_name,
    district = excluded.district,
    patch = excluded.patch,
    row_count = excluded.row_count,
    file_size_bytes = excluded.file_size_bytes,
    has_locality = excluded.has_locality
"""

CATALOG_LOCALITY_INSERT_SQL = """
INSERT OR IGNORE INTO catalog_locality (state, ac_code, locality) VALUES (?,?,?)
"""

RELATION_LABELS = {"F": "Father", "H": "Husband", "M": "Mother", "O": "Other/Guardian"}


# build_single() and build_combined() predate the state registry: they take a
# raw *path* rather than a state, so nothing in their arguments says whose
# connector, meta, roll year and ECI code to use, and both have always
# resolved to Karnataka. That was never a property of the code -- neither
# function does anything Karnataka-specific -- only of the argument they
# don't take, so it is a default here rather than a law: pass `state_id` (or
# `--state` on the CLI, `STATE=` in the Makefile) and both build any
# registered state whose raw is one file per AC, which is all three today.
#
# Kept as a default rather than made required because `make build-db AC=A085`
# is the shape people already type and Karnataka is the only state whose raw
# lands directly in data/raw/. New code should still prefer
# build_multi_state/build_per_ac: they take state_ids, read raw_dir/raw_glob
# from the registry, and populate state_coverage, which these two don't.
DEFAULT_STATE_ID = "karnataka"


def _state_connector(state_id):
    """A state's connector via the registry rather than a direct import, so
    the caller's state_id is the single thing that decides."""
    if state_id not in STATE_CONNECTORS:
        raise SystemExit(
            f"Unknown state: {state_id}. Known: {', '.join(STATE_CONNECTORS)}"
        )
    return STATE_CONNECTORS[state_id]["connector_cls"]()


def _load_ac_lookup(state_id=DEFAULT_STATE_ID):
    """ac_code -> Constituency, from that state's committed meta (via the
    connector's own loader, so this and list_constituencies() can't drift)."""
    return _ac_lookup(_state_connector(state_id), state_id)


class UnknownConstituencyError(ValueError):
    """A raw file names an ac_code the state's meta doesn't declare."""


class DuplicateConstituencyError(ValueError):
    """A state's meta declares the same ac_code more than once."""


def _ac_lookup(connector, state_id):
    """ac_code -> Constituency for one state, refusing a meta file that
    declares the same ac_code twice.

    Building the dict alone would silently keep the last entry per code and
    drop the rest, and the loss is close to invisible: the AC still builds
    (its raw file is keyed by code, and some entry matched), it just gets
    another AC's name and district, and `acs_total` under-counts by however
    many were swallowed. Nothing downstream can tell -- the rows parse,
    score and rank exactly as they should, and the picker just shows a
    wrong label. Real instance: an incoming state's generated meta had 44
    entries for 32 distinct ac_no values, one of them repeated four times.
    """
    acs = list(connector.list_constituencies())
    lookup = {ac.ac_code: ac for ac in acs}
    if len(lookup) != len(acs):
        counts = collections.Counter(ac.ac_code for ac in acs)
        dupes = sorted(f"{code} x{n}" for code, n in counts.items() if n > 1)
        raise DuplicateConstituencyError(
            f"{state_id}: list_constituencies() returned {len(acs)} entries "
            f"for {len(lookup)} distinct ac_code values -- {', '.join(dupes)}. "
            f"Fix states/meta/{state_id}_ac_meta.json rather than letting the "
            f"duplicates be silently dropped."
        )
    return lookup


def _resolve_ac(ac_code, ac_lookup, state_id=None):
    """Known AC -> its real metadata. An unknown code raises.

    It used to fall back to blank ac_name/district, which reads as harmless
    -- the filenames come from the meta in the first place, so it "shouldn't
    happen". But a blank district is not a cosmetic gap on the serving side:
    the picker's whole primary tier is district, so an AC with no district
    is one a user cannot navigate to at all, and an AC with no name is one
    they cannot recognize once there. Both are invisible to every
    search-quality check, which drives searches by explicit (state,
    ac_code). A build is offline, re-runnable, and watched by whoever
    started it -- the right failure direction there is to stop, not to
    publish an unreachable AC.
    """
    ac = ac_lookup.get(ac_code)
    if ac is None:
        where = f"{state_id}: " if state_id else ""
        raise UnknownConstituencyError(
            f"{where}raw file names ac_code {ac_code!r}, which "
            f"list_constituencies() doesn't declare. Either the meta is "
            f"stale (regenerate it) or the file doesn't belong in this "
            f"state's raw_dir."
        )
    return ac


def _records_to_rows(records):
    return [
        (
            r.state, r.roll_year, r.district, r.ac_code, r.ac_name,
            r.part_no, r.serial_no, r.local_ref, r.full_name,
            r.full_relative_name, r.relation_code,
            RELATION_LABELS.get(r.relation_code, r.relation_code),
            r.age, r.gender, r.remark, r.locality, resolve_source_url(r),
            # Written straight through, blank included. A blank is what
            # backfill_latin_columns() below looks for; a connector-supplied
            # value is what it leaves alone. See VoterRecord's own comment.
            getattr(r, "full_name_latin", "") or "",
            getattr(r, "full_relative_name_latin", "") or "",
        )
        for r in records
    ]


def _report_backfill(result, prefix=""):
    """Print what the rule-based transliteration managed, and what it didn't.

    The `incomplete` half is the part worth printing: those are names whose
    romanization still holds native-script characters, because the scheme has
    no mapping for them (Malayalam chillu forms, Tamil ன -- both common name
    endings). Such a row is fully built and passes every downstream check
    while being effectively unfindable by a Latin-script query, so a build
    that stayed silent about it would hand the discovery to a user who can't
    find themselves. Not an error: the fix is a connector that supplies
    full_name_latin itself, which is a change to that state, not to this run.
    """
    if not result.transliterated:
        return
    print(
        f"{prefix}Transliterated {result.transliterated} distinct "
        f"non-Latin name strings to Latin script."
    )
    if result.incomplete:
        examples = ", ".join(f"{native} -> {latin}" for native, latin in result.sample)
        print(
            f"{prefix}  WARNING: {result.incomplete} of them still contain "
            f"native-script characters the scheme has no mapping for, so a "
            f"Latin-script query will score badly against them. Supply "
            f"full_name_latin from the connector for this state. e.g. {examples}"
        )


# --- The per-part nameless-name alarm ------------------------------------
#
# A part of a roll whose name column is mostly missing is an extraction
# failure, not a property of the roll, and nothing else in this pipeline can
# see it. Row counts stay right, the catalog stays consistent, the serving
# app's freshness guards see nothing stale, and voter_search_engine's
# search-quality suite drives explicit (state, ac_code) pairs so it never
# asks a question a nameless part could answer wrongly. Meanwhile the rows
# are counted in ac_index.row_count and therefore in the coverage figure the
# site publishes -- the same class as the acs_digitized incident, where the
# data was fine and the claim about how much of it there was, was not.
#
# Two real cases. Haryana HR22 parts 52 and 153, where 936 of part 52's
# 1,115 rows have no name -- found after the fact, by nothing. And West
# Bengal's Darjeeling constituencies, where AC025's first 37 parts and
# AC026's first 8 are Devanagari rather than Shree-Lipi Bengali and the
# connector correctly refuses to run them through the wrong glyph table
# (see looks_like_shreelipi's docstring, which names those exact counts).
# That second one is the more instructive of the two: the connector is
# behaving correctly and the gap is written down, and 34,347 rows are
# still nameless, still served, and still counted as digitized. An alarm
# is worth having for a known gap and not only for a bug -- "we decided
# not to guess here" and "we quietly lost these" are the same shape from
# the outside, and neither should have to be remembered.

# A part must be at least this fraction nameless, AND hold at least this
# many nameless rows, before it is called out.
#
# Honest statement of what calibrates these: nothing in the band does. Across
# the 106 built West Bengal Shree-Lipi AC files -- 22,941 parts, 17,254,095
# rows -- every part that trips is at exactly 100.0% and every part that does
# not is under 0.02%; 5% and 50% select the identical 45 parts. Haryana HR22
# part 52 sits at 84%, and the worst part of the West Bengal OCR corpus that
# is *not* a defect sits at 1.56%. So the measured claim is "the band is
# empty in every corpus we have looked at", which is a reason to believe a
# threshold anywhere in it behaves the same, and is not evidence that 10% is
# the right number. _report_nameless prints the rate of every tripping part
# for that reason: if the band ever fills, the report is where it shows up
# first, rather than in a threshold quietly moved to make a build quiet.
#
# A second corpus extends that bracket rather than establishing one: the
# Shree-Lipi figures above are what set it, and what follows corroborates them
# from a disjoint population without narrowing them. Scanning the 172 finalized
# West Bengal p3 per-AC files -- 26.9M rows, 665,521 carrying no name -- leaves
# a residual of 88 nameless rows across 43 ACs once five identified populations
# are set aside. Two limits on what that buys, both of which matter more than
# the number does:
#
# It is measured per AC, and both thresholds here are per part. 88 rows across
# 43 ACs cannot be turned into a per-part rate without knowing how they
# concentrate, and NAMELESS_PART_MIN_COUNT is exactly what decides whether a
# concentration matters. So this corroborates the picture at AC granularity;
# it is not a second measurement of the quantity these thresholds test.
#
# And 665,521 is not a count of damaged source data. The largest population in
# it was labelled "name in an unrecognized Bengali-script font" -- on the row,
# in production -- until it turned out to be readable ASCII we discarded over
# a .notdef glyph: our defect, not the source's. Two of those ACs measure
# 36.3% and 57.5% recoverable by that one classification fix. So the honest
# reading is "residual after five identified populations", not "healthy
# background rate" -- the latter asserts the remainder is unfixable, which is
# not established. The bracket survives either way: a population that later
# proves recoverable was still ~100% nameless *before* the fix, so it cannot
# move the gap between the worst healthy part and the lightest damaged one.
NAMELESS_PART_RATE = 0.10
# The minimum count is what stops a small part from tripping on noise: at 449
# rows, 7 nameless is 1.6%, but the same 7 in a 60-row part is 12%. On the
# Shree-Lipi corpus above it excludes exactly one part -- AC047/part0109, 1
# nameless row of 1 -- which is precisely what it is for.
NAMELESS_PART_MIN_COUNT = 20


def _is_usable_name(value):
    """True if `value` holds at least one letter, in any script.

    Blankness is the obvious way a name can be missing and not the only one.
    West Bengal has 30 rows across 13 ACs whose name is the literal
    'ঃঃ' -- punctuation that occupies the cell, passes a non-empty
    test, passes a length test, and is findable by nobody (vsp #35). Testing
    for a letter rather than for emptiness costs nothing and catches both,
    and stays script-general instead of encoding a rule about Bengali.
    """
    return any(unicodedata.category(ch).startswith("L") for ch in value or "")


def _classify_name(record):
    """Return "" for a record with a usable name, else why it has none.

    The alarm's whole discriminator lives in this function, so it is worth
    stating what it is *not*. It is not "did the connector explain itself":
    both known cases explain themselves accurately and are still defects.
    Haryana appends "no voter name in row"; West Bengal AC025 appends "name
    in an unrecognized Bengali-script font: no glyph table for it". Those are
    true, specific, and exactly the rows that need chasing -- a remark that
    restates the symptom is not a licence, and no test on remark text can
    tell the two apart.

    What separates them is whether the name exists to be recovered:
    NAME_ABSENT_IN_SOURCE is a fact about the roll, NAME_UNREAD is a fact
    about us. An undeclared blank is NAME_UNREAD, so a connector saying
    nothing cannot buy silence.
    """
    if _is_usable_name(record.full_name):
        return ""
    declared = getattr(record, "name_absence", "") or ""
    if declared in (NAME_ABSENT_IN_SOURCE, NAME_UNREAD):
        return declared
    return NAME_UNREAD


def _nameless_census(records):
    """Count rows, unread names and source-blank names per part.

    Returns {part_no: {"rows": n, NAME_UNREAD: n, NAME_ABSENT_IN_SOURCE: n}},
    plus an "unrecognized" Counter of any name_absence value that was
    neither constant -- counted as unread and reported rather than dropped,
    since a typo in a connector would otherwise silently disarm the alarm for
    exactly the rows it was aimed at.
    """
    parts = {}
    undeclared = collections.Counter()
    for rec in records:
        part = parts.setdefault(
            rec.part_no,
            {"rows": 0, NAME_UNREAD: 0, NAME_ABSENT_IN_SOURCE: 0},
        )
        part["rows"] += 1
        why = _classify_name(rec)
        if why:
            part[why] += 1
            declared = getattr(rec, "name_absence", "") or ""
            if declared and declared not in (NAME_ABSENT_IN_SOURCE, NAME_UNREAD):
                undeclared[declared] += 1
    return {"parts": parts, "unrecognized": undeclared, "reparsed": True}


def _nameless_census_from_db(conn):
    """The same census read back from an already-built per-AC file.

    Needed because a --per-ac run skips every AC a previous run finished, so
    without this the alarm would report on a full build and go quiet on every
    re-run after it -- which is when anyone actually reads the output. The
    file does not carry name_absence (adding a column would be a `contract`
    bump and a rebuild of every published AC, which this is not worth), so
    every nameless row here is counted as unread. That over-counts once a
    connector starts declaring NAME_ABSENT_IN_SOURCE, which is the safe
    direction for an alarm and is stated in the report rather than left for a
    reader to infer from a number that quietly means something else.
    """
    parts = {}
    for part_no, name in conn.execute("SELECT part_no, full_name FROM voters"):
        part = parts.setdefault(
            part_no, {"rows": 0, NAME_UNREAD: 0, NAME_ABSENT_IN_SOURCE: 0})
        part["rows"] += 1
        if not _is_usable_name(name):
            part[NAME_UNREAD] += 1
    return {"parts": parts, "unrecognized": collections.Counter(),
            "reparsed": False}


def _nameless_alarms(census):
    """Every part of one AC's census that is over both bars, worst first."""
    alarms = []
    for part_no, counts in census["parts"].items():
        unread = counts[NAME_UNREAD]
        rows = counts["rows"]
        if not rows or unread < NAMELESS_PART_MIN_COUNT:
            continue
        rate = unread / rows
        if rate >= NAMELESS_PART_RATE:
            alarms.append((part_no, unread, rows, rate))
    return sorted(alarms, key=lambda a: (-a[3], -a[1], a[0]))


def _report_nameless(results, prefix=""):
    """Print the state's nameless-name picture: the alarm, then the totals.

    Prints nothing at all when there is nothing to say -- no alarm, no
    source-blank rows, no unrecognized declaration -- so a clean state stays
    a clean line of output. It does *not* print a reassuring "0 parts" line,
    because a build that skipped every AC would otherwise print one while
    having checked nothing.
    """
    alarms = []
    unread = blank_in_source = rows = 0
    unrecognized = collections.Counter()
    inferred_acs = []
    for result in results:
        census = result.get("nameless")
        if census is None:
            continue
        if not census["reparsed"]:
            inferred_acs.append(result["ac_code"])
        unrecognized.update(census["unrecognized"])
        for counts in census["parts"].values():
            rows += counts["rows"]
            unread += counts[NAME_UNREAD]
            blank_in_source += counts[NAME_ABSENT_IN_SOURCE]
        for part_no, n, part_rows, rate in _nameless_alarms(census):
            alarms.append((rate, n, part_rows, result["ac_code"], part_no))

    if not (alarms or blank_in_source or unrecognized):
        return

    if alarms:
        alarms.sort(key=lambda a: (-a[0], -a[1]))
        affected = sum(a[1] for a in alarms)
        print(f"{prefix}WARNING: {len(alarms)} part(s) are at least "
              f"{NAMELESS_PART_RATE:.0%} unread names, {affected:,} row(s) in "
              f"total. These rows are counted in row_count and in the coverage "
              f"figure the site publishes, and are findable by nobody.")
        for rate, n, part_rows, ac_code, part_no in alarms[:10]:
            print(f"{prefix}    {ac_code} part {part_no}: {n:,}/{part_rows:,} "
                  f"= {rate:.1%} unread")
        if len(alarms) > 10:
            print(f"{prefix}    ... and {len(alarms) - 10} more part(s)")
    if blank_in_source:
        print(f"{prefix}{blank_in_source:,} row(s) of {rows:,} declare no name "
              f"in the source itself; not counted toward the alarm.")
    for value, n in sorted(unrecognized.items()):
        print(f"{prefix}WARNING: {n:,} row(s) set name_absence={value!r}, which "
              f"is neither {NAME_ABSENT_IN_SOURCE!r} nor {NAME_UNREAD!r}. "
              f"Counted as unread. Fix the connector or add the value.")
    if inferred_acs:
        print(f"{prefix}Note: {len(inferred_acs)} AC(s) were skipped as already "
              f"built, so their counts were read back from the built file, "
              f"which does not carry name_absence -- every nameless row in "
              f"them is counted as unread. Rebuild them to tell the two "
              f"apart: {', '.join(sorted(inferred_acs)[:8])}"
              + (" ..." if len(inferred_acs) > 8 else ""))


def _finalize(conn):
    conn.executescript(
        """
        CREATE INDEX idx_voters_ac_part ON voters(ac_code, part_no, serial_no);
        CREATE INDEX idx_voters_district ON voters(district);

        DROP TABLE IF EXISTS voters_fts;
        CREATE VIRTUAL TABLE voters_fts USING fts5(
            full_name, full_relative_name, content='voters', content_rowid='id'
        );
        INSERT INTO voters_fts(rowid, full_name, full_relative_name)
            SELECT id, full_name, full_relative_name FROM voters;
        """
    )
    conn.commit()


def _finalize_voters_only(conn):
    """Same as _finalize but for a per-AC file: indexes only, no voters_fts --
    confirmed dead weight for this shape (app.py/search.py/cross_reference.py
    never query it)."""
    conn.executescript(
        """
        CREATE INDEX idx_voters_ac_part ON voters(ac_code, part_no, serial_no);
        CREATE INDEX idx_voters_district ON voters(district);
        """
    )
    conn.commit()


def build_single(raw_path, db_path, roll_year=None, state_id=DEFAULT_STATE_ID):
    """Build a DB from one AC's raw file, inferring ac_code from the filename.

    Works for any state whose raw is one file per AC -- Karnataka's `A085.csv`
    and Haryana's `HR02.zip` alike, since parse_raw() takes bytes and the
    connector decides what they are. `state_id` defaults rather than being
    required; see DEFAULT_STATE_ID for why. The roll year is resolved from
    the state rather than written down again, so there stays exactly one
    place in the repo that says what any given state's year is.
    """
    roll_year = (
        roll_year if roll_year is not None else resolve_roll_year(state_id)
    )
    ac_code = os.path.splitext(os.path.basename(raw_path))[0]
    connector = _state_connector(state_id)
    ac = _resolve_ac(ac_code, _load_ac_lookup(state_id), state_id)

    with open(raw_path, "rb") as f:
        raw = f.read()
    records = connector.parse_raw(raw, ac, roll_year)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(INSERT_SQL, _records_to_rows(records))
    backfill_latin_columns(conn)
    _finalize(conn)

    _report_nameless([{"ac_code": ac_code, "nameless": _nameless_census(records)}])
    total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"Loaded {total} records from {ac_code} into {db_path}.")
    conn.close()


def build_combined(raw_dir, db_path, roll_year=None, state_id=DEFAULT_STATE_ID):
    """Build one DB from every raw AC file in raw_dir, for one state.

    This is `build_multi_state([state_id], db_path)` with the raw directory
    overridden and state_coverage not populated -- the override is the only
    thing it can still do that build_multi_state can't, since that one takes
    raw_dir from the registry. Reachable only as `python -m build_db
    --combine`; no Makefile target uses it. See build_single's docstring for
    the roll year and DEFAULT_STATE_ID for the state.
    """
    roll_year = (
        roll_year if roll_year is not None else resolve_roll_year(state_id)
    )
    connector = _state_connector(state_id)
    ac_lookup = _load_ac_lookup(state_id)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # From the registry, not a hardcoded *.csv -- Karnataka's raw is CSV and
    # both other live states' is ZIP, and the old literal would have silently
    # matched nothing for them rather than saying so.
    raw_glob = STATE_CONNECTORS[state_id]["raw_glob"]
    csv_paths = sorted(glob.glob(os.path.join(raw_dir, raw_glob)))
    total = 0
    nameless_results = []
    for path in csv_paths:
        ac_code = os.path.splitext(os.path.basename(path))[0]
        ac = _resolve_ac(ac_code, ac_lookup, state_id)
        with open(path, "rb") as f:
            raw = f.read()
        records = connector.parse_raw(raw, ac, roll_year)
        conn.executemany(INSERT_SQL, _records_to_rows(records))
        nameless_results.append(
            {"ac_code": ac_code, "nameless": _nameless_census(records)})
        total += len(records)
        print(f"  {ac_code}: {len(records)} records")

    backfill_latin_columns(conn)
    _finalize(conn)
    _report_nameless(nameless_results)
    grand_total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"Loaded {grand_total} records from {len(csv_paths)} ACs into {db_path}.")
    conn.close()


def _scope_paths(paths, ac_codes, state_id, info):
    """Restrict a state's raw files to an explicitly-named set of ACs.

    A build's scope has to be *declared*, not inherited from whatever raw
    files happen to be sitting in the raw dir. West Bengal's live 19 ACs
    were only ever 19 because that is all that had been downloaded when
    they were built; once the full 245-file download landed, the identical
    command silently meant something else. 224 of those ACs are typeset in
    a script this connector cannot decode, so they parse into rows with
    empty names -- which score 0 against any query and are dropped by the
    serving app's SCORE_THRESHOLD, making them unfindable by any search.
    The build was three million nameless rows in before anyone noticed.

    Missing ACs are fatal rather than skipped, because a typo'd or stale AC
    list quietly building fewer ACs than asked for is indistinguishable,
    downstream, from a state that genuinely is that small.
    """
    if not ac_codes:
        return paths
    wanted = {c.strip().upper() for c in ac_codes if c.strip()}
    kept = [p for p in paths
            if os.path.splitext(os.path.basename(p))[0].upper() in wanted]
    missing = sorted(wanted - {os.path.splitext(os.path.basename(p))[0].upper()
                               for p in kept})
    if missing:
        raise SystemExit(
            f"{state_id}: no raw file for {', '.join(missing)} "
            f"(looked in {info['raw_dir']} for {info['raw_glob']})"
        )
    return kept


def build_multi_state(state_ids, db_path, roll_year=None, ac_codes=None):
    """Build one DB combining every listed state's raw files, per
    states/registry.py's connector class + raw_dir/raw_glob for each.

    roll_year is resolved **per state, inside the loop** (see
    states/roll_years.py) rather than taken as one number for the whole
    build -- the "2002 rolls" are not all from 2002, and roll_year is not
    decoration: the serving app derives an elector's year of birth as
    `roll_year - age` for its required year-of-birth filter. The parameter
    survives only as an explicit override for a caller that genuinely knows
    better; passing it forces every state in the build to that year.

    Also populates state_coverage -- one row per state summarizing how much
    of it is digitized (acs_digitized/acs_total, from raw files present vs.
    list_constituencies()) and whether locality data (village/town, see
    VoterRecord.locality) was actually extracted for it. This is what lets
    the serving app show roadmap/coverage info straight from the DB, with
    no compile-time knowledge of which states exist.
    """
    unknown = [s for s in state_ids if s not in STATE_CONNECTORS]
    if unknown:
        raise SystemExit(f"Unknown state(s): {', '.join(unknown)}. Known: {', '.join(STATE_CONNECTORS)}")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    built_at = datetime.datetime.utcnow().isoformat()

    grand_total = 0
    for state_id in state_ids:
        info = STATE_CONNECTORS[state_id]
        connector = info["connector_cls"]()
        state_roll_year = roll_year_for(info, state_id, override=roll_year)
        ac_lookup = _ac_lookup(connector, state_id)
        paths = sorted(glob.glob(os.path.join(info["raw_dir"], info["raw_glob"])))
        paths = _scope_paths(paths, ac_codes, state_id, info)
        state_total = 0
        acs_with_locality = set()
        unparseable = []
        nameless_results = []
        for path in paths:
            ac_code = os.path.splitext(os.path.basename(path))[0]
            ac = _resolve_ac(ac_code, ac_lookup, state_id)
            with open(path, "rb") as f:
                raw = f.read()
            try:
                records = connector.parse_raw(raw, ac, state_roll_year)
            except UnparseableRollError as exc:
                unparseable.append(ac_code)
                print(f"  [{state_id}] {ac_code} UNPARSEABLE, skipped: {exc}")
                continue
            conn.executemany(INSERT_SQL, _records_to_rows(records))
            nameless_results.append(
                {"ac_code": ac_code, "nameless": _nameless_census(records)})
            state_total += len(records)
            if any(r.locality for r in records):
                acs_with_locality.add(ac_code)
            print(f"  [{state_id}] {ac_code}: {len(records)} records")
        _report_nameless(nameless_results, prefix=f"  [{state_id}] ")
        print(f"{state_id}: {state_total} records from {len(paths) - len(unparseable)} files (roll year {state_roll_year})")
        if unparseable:
            print(f"  [{state_id}] {len(unparseable)} of {len(paths)} ACs were "
                  f"unparseable and are absent from this build: {', '.join(sorted(unparseable))}")
        if paths and len(unparseable) == len(paths):
            raise UnparseableRollError(
                f"{state_id}: all {len(paths)} ACs were unparseable -- refusing to "
                f"record the state as built. Check the raw files and the connector."
            )
        grand_total += state_total

        # An unparseable AC is absent from this build, so it is neither
        # digitized nor a candidate for locality coverage. Counting it as
        # either would publish a state that looks more complete than it is
        # -- and "full" locality coverage is exactly the kind of claim the
        # serving app's freshness panel reports without re-deriving.
        built_count = len(paths) - len(unparseable)
        if not built_count:
            locality_coverage = "none"
        elif len(acs_with_locality) == built_count:
            locality_coverage = "full"
        elif acs_with_locality:
            locality_coverage = "partial"
        else:
            locality_coverage = "none"
        conn.execute(STATE_COVERAGE_INSERT_SQL, (
            state_id, info["label"], len(ac_lookup), built_count,
            locality_coverage, built_at, state_roll_year,
        ))

    _report_backfill(backfill_latin_columns(conn, state_ids=state_ids))
    _finalize(conn)
    check_total = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    print(f"\nLoaded {check_total} records across {len(state_ids)} state(s) into {db_path}.")
    conn.close()


def _ac_output_is_complete(ac_db_path):
    """True if ac_db_path exists and was fully finalized by a prior run --
    detected by the presence of the index _finalize_voters_only() creates
    last, not just file existence (a process killed mid-write, e.g. Ctrl-C
    or an OOM, leaves a file on disk that opens fine but was never
    finalized). Used to make a --per-ac run resumable: re-running the same
    command after an interruption skips every AC a prior run actually
    finished instead of redoing it."""
    if not os.path.exists(ac_db_path):
        return False
    try:
        conn = sqlite3.connect(ac_db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_voters_ac_part'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _build_one_ac(task):
    """Runs in a worker process (see build_per_ac's ProcessPoolExecutor) --
    parses one AC's raw file and writes its own small sqlite file. Takes a
    single tuple arg (not **kwargs) since ProcessPoolExecutor.submit pickles
    whatever it's given, and a plain tuple keeps that unambiguous. Every
    input is passed explicitly (connector_cls, ac metadata, roll_year)
    rather than re-derived from STATE_CONNECTORS/globals inside the worker
    -- a spawned process gets a fresh import of this module, so it would
    only see the *unpatched* registry, silently breaking callers (e.g.
    tests) that monkeypatch STATE_CONNECTORS in the parent process.

    Independent of every other AC's work -- own output file, own sqlite
    connection, no shared state -- which is what makes this safe to fan
    out across processes at all. build_per_ac() aggregates the per-AC
    results (state_total, ac_index_rows, acs_with_locality) after every
    future resolves, back in the parent."""
    state_id, connector_cls, path, ac_db_path, contract, patch, ac, roll_year = task
    ac_code = ac.ac_code

    if _ac_output_is_complete(ac_db_path):
        conn = sqlite3.connect(ac_db_path)
        try:
            row_count = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
            localities = sorted(
                loc for (loc,) in conn.execute(
                    "SELECT DISTINCT locality FROM voters WHERE locality IS NOT NULL AND locality != ''"
                ).fetchall()
            )
            # Read back rather than skipped: an alarm that only fires on a
            # from-scratch build is silent on every re-run, which is when the
            # output is actually read.
            nameless = _nameless_census_from_db(conn)
        finally:
            conn.close()
        return {
            "ac_code": ac_code, "ac_name": ac.ac_name, "district": ac.district,
            "row_count": row_count, "file_size_bytes": os.path.getsize(ac_db_path),
            "has_locality": bool(localities), "localities": localities, "skipped": True,
            "unparseable": None,
            "translit": BackfillResult(0, 0, []),
            "nameless": nameless,
        }

    connector = connector_cls()
    with open(path, "rb") as f:
        raw = f.read()
    try:
        records = connector.parse_raw(raw, ac, roll_year)
    except UnparseableRollError as exc:
        # Returned rather than raised: this runs in a pool worker, and an
        # exception here reaches the parent through fut.result(), which
        # aborts the whole build -- every other state included. A connector
        # declaring one AC unparseable is not a reason to lose the other
        # 43. Anything else still propagates and still stops the build.
        return {
            "ac_code": ac_code, "ac_name": ac.ac_name, "district": ac.district,
            "row_count": 0, "file_size_bytes": 0, "has_locality": False,
            "localities": [], "skipped": False, "unparseable": str(exc),
            "translit": BackfillResult(0, 0, []),
            # An AC that never parsed has no parts to have lost names from.
            # None rather than an empty census: nothing was checked here, and
            # a zero would read as a clean result.
            "nameless": None,
        }

    ac_conn = sqlite3.connect(ac_db_path)
    ac_conn.executescript(VOTERS_SCHEMA)
    ac_conn.executemany(INSERT_SQL, _records_to_rows(records))
    translit = backfill_latin_columns(ac_conn, state_ids=[state_id])
    _finalize_voters_only(ac_conn)
    ac_conn.close()

    localities = sorted({r.locality for r in records if r.locality})
    return {
        "ac_code": ac_code, "ac_name": ac.ac_name, "district": ac.district,
        "row_count": len(records), "file_size_bytes": os.path.getsize(ac_db_path),
        "has_locality": bool(localities), "localities": localities, "skipped": False,
        "unparseable": None,
        # Carried up rather than printed here: this runs in a pool worker, so
        # its stdout interleaves with every other AC's. build_per_ac() sums
        # them and reports once per state.
        "translit": translit,
        "nameless": _nameless_census(records),
    }


# Columns each catalog table must have for a merge to be safe. A catalog is
# now reused rather than rewritten, so an older file with a drifted schema
# would otherwise be silently merged into and quietly serve wrong columns --
# CREATE TABLE IF NOT EXISTS is a no-op against it, not a migration. Checked
# explicitly and loudly, because a guard that passes on a shape it does not
# understand is not a guard. The fix is --replace-catalog, which is exactly
# the old behaviour and is named in the error.
_CATALOG_COLUMNS = {
    "state_coverage": {"state_id", "label", "acs_total", "acs_digitized",
                       "locality_coverage", "built_at", "roll_year"},
    "ac_index": {"state", "ac_code", "ac_name", "district", "contract", "patch",
                 "row_count", "file_size_bytes", "has_locality"},
    "catalog_locality": {"state", "ac_code", "locality"},
}


def _assert_catalog_schema(conn, catalog_path):
    for table, expected in _CATALOG_COLUMNS.items():
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue                      # freshly created below; nothing to check
        missing = expected - present
        if missing:
            raise SystemExit(
                f"{catalog_path}: table {table} is missing column(s) "
                f"{', '.join(sorted(missing))}. This catalog predates the current "
                f"schema and cannot be merged into.\n"
                f"  Rebuild it with --replace-catalog (make build-db-ac "
                f"REPLACE_CATALOG=1), passing every AC the state should serve."
            )


def _write_catalog(catalog_path, state_id, label, acs_total, roll_year, built_at,
                   ac_index_rows, locality_rows, scoped_ac_codes, replace):
    """Merge one run's results into a state's catalog, or replace it outright.

    The default is a merge, per AC: a build of three ACs adds or updates those
    three rows and leaves every other AC the catalog serves exactly as it was,
    at the patch it was published at. That is the whole point -- the app reads
    the catalog as the sole authority for which ACs exist and at which patch,
    with no fallback to a lower one, so a rewrite-from-this-run-alone turns
    "build these three" into "serve only these three".

    Two things are deliberately not a plain union:

    * catalog_locality is cleared for the ACs in *this* run before inserting.
      A union would keep a locality string that a re-parse no longer produces,
      so a corrected AC would serve a picker entry that matches nothing.
    * acs_digitized and locality_coverage are recomputed from the merged
      ac_index, never from this run's results. acs_digitized is a public
      coverage claim -- app.py renders it per state, sums it for the site-wide
      constituency figure, and gates a state's liveness on it -- so under
      merging, "how many ACs this run built" is simply the wrong number. This
      is the same defect class as the len(paths) count it replaced.
    """
    conn = sqlite3.connect(catalog_path)
    try:
        if replace:
            conn.executescript(CATALOG_RESET_SQL)
        _assert_catalog_schema(conn, catalog_path)
        conn.executescript(CATALOG_SCHEMA)

        conn.executemany(AC_INDEX_INSERT_SQL, ac_index_rows)
        conn.executemany(
            "DELETE FROM catalog_locality WHERE state = ? AND ac_code = ?",
            [(state_id, code) for code in scoped_ac_codes],
        )
        conn.executemany(CATALOG_LOCALITY_INSERT_SQL, locality_rows)

        built, with_locality = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(has_locality), 0) "
            "FROM ac_index WHERE state = ?", (state_id,)
        ).fetchone()
        if not built:
            coverage = "none"
        elif with_locality == built:
            coverage = "full"
        elif with_locality:
            coverage = "partial"
        else:
            coverage = "none"

        conn.execute(STATE_COVERAGE_INSERT_SQL, (
            state_id, label, acs_total, built, coverage, built_at, roll_year,
        ))
        conn.commit()
    finally:
        conn.close()
    return built, coverage


def _guard_catalog_shrink(catalog_path, state_id, scoped_ac_codes, allow):
    """Refuse a --replace-catalog build whose scope drops ACs the catalog serves.

    A replace rewrites the state's catalog from that run's results alone. So
    a scoped replace (--acs) is not only "build these"; it is also "the
    catalog will name only these", and app.py serves exactly what the catalog
    names, with no fallback to a lower patch. Replacing a published state's
    catalog with only the ACs being added therefore takes every other AC off
    the site, silently, with a build that looks entirely normal.

    Merging -- the default since union support landed -- cannot do this at
    all, which is why this only guards the replace path. The flag stays
    because replacing is still the only way to *retire* an AC.

    Stopping is the safe direction here, against the house preference for
    degrading: the failure this prevents is invisible in the build output,
    in the search-quality suite (which drives explicit (state, ac_code)
    pairs) and in the freshness guards (the remaining data is perfectly
    fresh). The escape hatch is explicit, and it names what it is dropping.
    """
    if not os.path.exists(catalog_path):
        return
    conn = sqlite3.connect(catalog_path)
    try:
        # Probed rather than queried blind, and the probe is deliberately the
        # only thing swallowing an error. A catalog predating ac_index (or a
        # file that is not a database at all) means there is nothing to
        # protect; anything else -- a renamed column, a schema drift -- must
        # surface, because a guard that quietly returns on a bad query is a
        # guard that is not running. That is not hypothetical: the first cut
        # of this asked for a state_id column on a table whose column is
        # named `state`, and a blanket `except sqlite3.DatabaseError` turned
        # the resulting OperationalError into a silent pass.
        try:
            has_index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ac_index'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return
        if not has_index:
            return
        served = {
            code for (code,) in conn.execute(
                "SELECT ac_code FROM ac_index WHERE state = ?", (state_id,)
            ).fetchall()
        }
    finally:
        conn.close()

    dropped = sorted(served - set(scoped_ac_codes))
    if not dropped:
        return

    shown = ", ".join(dropped[:12]) + (f" ... (+{len(dropped) - 12} more)" if len(dropped) > 12 else "")
    message = (
        f"{state_id}: this build's scope covers {len(scoped_ac_codes)} AC(s), but the existing "
        f"catalog at {catalog_path} serves {len(served)}. Rewriting it would drop "
        f"{len(dropped)}: {shown}\n"
        f"  The catalog is written from this run's results alone -- it is not merged -- and the "
        f"app serves exactly what it names, so those ACs would go off the air.\n"
        f"  To extend a published state, pass EVERY AC it should serve, not just the new ones: "
        f"already-built ACs at this patch are skipped (a COUNT(*), not a reparse), so the full "
        f"list re-indexes rather than rebuilds.\n"
        f"  Pass --allow-catalog-shrink if narrowing the catalog is what you actually mean."
    )
    if allow:
        print(f"WARNING: {message}")
        return
    raise SystemExit(f"REFUSING: {message}")


def build_per_ac(state_ids, out_dir, contract="c1", patch=0, roll_year=None, workers=None,
                 ac_codes=None, allow_catalog_shrink=False, replace_catalog=False):
    """Build one small <ac_code>-<contract>.p<patch>.sqlite per (state,
    ac_code), plus one small catalog.sqlite per state -- the native per-AC
    serving artifact set (no combined DB is built or needed for this path;
    see voter_search_engine's per-AC-file-serving plan).

    Each AC's parse+write is independent (own raw file in, own sqlite file
    out, no shared connection) -- fanned out across a ProcessPoolExecutor
    rather than run one at a time, since this is CPU-bound (legacy-font
    PDF decoding for Devanagari states, transliteration backfill for all)
    and a single-threaded loop leaves every other core idle for the whole
    build. contract/patch are explicit inputs, not auto-detected from
    content -- republishing an AC's data (bug fix, added locality,
    re-digitized) is a deliberate act of bumping --patch, not something
    this script infers on its own.

    roll_year is resolved per state (states/roll_years.py), not taken as one
    number for the whole build -- see build_multi_state's docstring for why
    that matters. The parameter remains as an all-states override.

    ac_codes, when given, restricts the build to those ACs and fails if any
    of them has no raw file. Without it the scope is every raw file present,
    which is only ever right by accident -- see the comment at the filter.

    The catalog is *merged* into, per AC: this run's ACs are added or updated
    and every other AC the catalog serves is left alone, at the patch it was
    published at. So a scoped build now means only what it says, and adding
    ACs to a published state no longer requires re-listing (and re-indexing)
    every AC that state already serves. replace_catalog restores the old
    rewrite-from-this-run-alone behaviour, and only then does the shrink
    guard apply -- with merging, there is nothing to shrink.
    """
    unknown = [s for s in state_ids if s not in STATE_CONNECTORS]
    if unknown:
        raise SystemExit(f"Unknown state(s): {', '.join(unknown)}. Known: {', '.join(STATE_CONNECTORS)}")

    if workers is None:
        workers = max(1, (os.cpu_count() or 2) - 1)

    built_at = datetime.datetime.utcnow().isoformat()
    catalog_dir = os.path.join(out_dir, "catalog")
    os.makedirs(catalog_dir, exist_ok=True)

    grand_total = 0
    for state_id in state_ids:
        info = STATE_CONNECTORS[state_id]
        connector_cls = info["connector_cls"]
        connector = connector_cls()
        state_roll_year = roll_year_for(info, state_id, override=roll_year)
        ac_lookup = _ac_lookup(connector, state_id)
        paths = _scope_paths(
            sorted(glob.glob(os.path.join(info["raw_dir"], info["raw_glob"]))),
            ac_codes, state_id, info,
        )
        if replace_catalog:
            # Only a replace can drop a published AC now; a merge cannot.
            _guard_catalog_shrink(
                os.path.join(catalog_dir, f"{state_id}.sqlite"),
                state_id,
                [os.path.splitext(os.path.basename(p))[0] for p in paths],
                allow_catalog_shrink,
            )

        state_dir = os.path.join(out_dir, state_id)
        os.makedirs(state_dir, exist_ok=True)

        tasks = []
        for path in paths:
            ac_code = os.path.splitext(os.path.basename(path))[0]
            ac = _resolve_ac(ac_code, ac_lookup, state_id)
            ac_db_path = os.path.join(state_dir, f"{ac_code}-{contract}.p{patch}.sqlite")
            tasks.append((state_id, connector_cls, path, ac_db_path, contract, patch, ac, state_roll_year))

        results = []
        unparseable = []
        pool_size = max(1, min(workers, len(tasks) or 1))
        with ProcessPoolExecutor(max_workers=pool_size) as pool:
            futures = [pool.submit(_build_one_ac, t) for t in tasks]
            for fut in as_completed(futures):
                result = fut.result()
                if result["unparseable"]:
                    unparseable.append(result)
                    print(f"  [{state_id}] {result['ac_code']} UNPARSEABLE, skipped: "
                          f"{result['unparseable']}")
                    continue
                results.append(result)
                tag = "skip (already built)" if result["skipped"] else "built"
                print(f"  [{state_id}] {result['ac_code']} ({tag}): {result['row_count']} records")

        if unparseable:
            # Restated after the per-AC firehose, because the individual line
            # scrolls past in a build of hundreds of ACs and a silently
            # smaller state is exactly what the catalog then publishes.
            print(f"  [{state_id}] {len(unparseable)} of {len(tasks)} ACs were "
                  f"unparseable and are absent from this build: "
                  f"{', '.join(r['ac_code'] for r in sorted(unparseable, key=lambda r: r['ac_code']))}")
        if tasks and not results:
            # Skipping a minority is the designed behaviour; skipping all of
            # them means the connector or the raw files are broken, and
            # publishing an empty catalog would take the state off the site
            # while every check downstream still reads green.
            raise UnparseableRollError(
                f"{state_id}: all {len(tasks)} ACs were unparseable -- refusing to "
                f"write an empty catalog. Check the raw files and the connector."
            )

        results.sort(key=lambda r: r["ac_code"])
        state_total = sum(r["row_count"] for r in results)
        _report_backfill(
            BackfillResult(
                sum(r["translit"].transliterated for r in results),
                sum(r["translit"].incomplete for r in results),
                [s for r in results for s in r["translit"].sample][:5],
            ),
            prefix=f"  [{state_id}] ",
        )
        _report_nameless(results, prefix=f"  [{state_id}] ")
        ac_index_rows = [
            (
                state_id, r["ac_code"], r["ac_name"], r["district"], contract, patch,
                r["row_count"], r["file_size_bytes"], int(r["has_locality"]),
            )
            for r in results
        ]
        locality_rows = [
            (state_id, r["ac_code"], loc)
            for r in results
            for loc in r["localities"]
        ]

        print(f"{state_id}: {state_total} records across {len(results)} AC file(s) "
              f"({pool_size} worker(s), roll year {state_roll_year})")
        grand_total += state_total

        # Both coverage figures are computed inside _write_catalog, off the
        # *merged* ac_index -- this run's results are the wrong denominator
        # once a build can be a subset of what the state serves.
        catalog_path = os.path.join(catalog_dir, f"{state_id}.sqlite")
        served, coverage = _write_catalog(
            catalog_path, state_id, info["label"], len(ac_lookup),
            state_roll_year, built_at, ac_index_rows, locality_rows,
            [r["ac_code"] for r in results], replace_catalog,
        )
        verb = "replaced" if replace_catalog else "merged into"
        print(f"  {verb} catalog {catalog_path}: {len(results)} AC(s) this run, "
              f"{served} served, locality {coverage}")

    print(f"\nLoaded {grand_total} records across {len(state_ids)} state(s) into {out_dir} (per-AC).")


if __name__ == "__main__":
    # Pulled out of argv before dispatch, because the branches below match on
    # argument *count* -- a positional shape this predates argparse and isn't
    # worth rewriting wholesale for one flag. Applies only to the two legacy
    # single-state paths; --states carries its own state list.
    legacy_state_id = DEFAULT_STATE_ID
    if "--state" in sys.argv:
        _i = sys.argv.index("--state")
        legacy_state_id = sys.argv[_i + 1]
        del sys.argv[_i:_i + 2]

    # Same reason: a bare flag left in argv would shift the count-matched
    # branches below. Only --per-ac reads it (it is the only path that
    # rewrites a catalog), but stripping it here keeps every other branch's
    # arity honest rather than making the flag order-sensitive.
    allow_catalog_shrink = "--allow-catalog-shrink" in sys.argv
    if allow_catalog_shrink:
        sys.argv.remove("--allow-catalog-shrink")

    # Same reason again: a bare flag would shift the count-matched branches.
    replace_catalog = "--replace-catalog" in sys.argv
    if replace_catalog:
        sys.argv.remove("--replace-catalog")

    if len(sys.argv) == 4 and sys.argv[1] == "--combine":
        build_combined(sys.argv[2], sys.argv[3], state_id=legacy_state_id)
    elif "--per-ac" in sys.argv and "--states" in sys.argv:
        state_ids = sys.argv[sys.argv.index("--states") + 1].split(",")
        out_dir = sys.argv[sys.argv.index("--per-ac") + 1]
        contract = sys.argv[sys.argv.index("--contract") + 1] if "--contract" in sys.argv else "c1"
        patch = int(sys.argv[sys.argv.index("--patch") + 1]) if "--patch" in sys.argv else 0
        workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else None
        # --roll-year forces every state in this build to one year. Omit it
        # (the normal case) and each state gets its own, per roll_years.py.
        roll_year = int(sys.argv[sys.argv.index("--roll-year") + 1]) if "--roll-year" in sys.argv else None
        acs = sys.argv[sys.argv.index("--acs") + 1].split(",") if "--acs" in sys.argv else None
        build_per_ac(state_ids, out_dir, contract=contract, patch=patch,
                     roll_year=roll_year, workers=workers, ac_codes=acs,
                     allow_catalog_shrink=allow_catalog_shrink,
                     replace_catalog=replace_catalog)
    elif sys.argv[1:2] == ["--states"] and len(sys.argv) in (4, 6, 8):
        roll_year = int(sys.argv[sys.argv.index("--roll-year") + 1]) if "--roll-year" in sys.argv else None
        acs = sys.argv[sys.argv.index("--acs") + 1].split(",") if "--acs" in sys.argv else None
        build_multi_state(sys.argv[2].split(","), sys.argv[3], roll_year=roll_year, ac_codes=acs)
    elif len(sys.argv) == 3:
        build_single(sys.argv[1], sys.argv[2], state_id=legacy_state_id)
    else:
        print(__doc__)
        sys.exit(1)
