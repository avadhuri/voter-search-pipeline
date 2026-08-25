"""
"Is this state actually servable?" -- run against a built artifact, before
it goes anywhere near a bucket.

Every check in here exists because the thing it looks for produces a build
that is *completely normal downstream*. The rows parse, the counts are
right, the FTS index is fine, fuzzy matching scores exactly as it should,
and every search-quality assertion passes -- while the state is wrong or
unreachable in the serving app in a way that reads to a user as an answer
rather than as a bug:

  roll_year         the app derives year of birth as roll_year - age, and
                    its year-of-birth field is required. A state stamped
                    2002 whose roll is 2005 mis-targets every elector by
                    three years, which reads as "you were never on the roll".
  district/ac_name  district is the picker's primary tier. A blank one makes
                    an AC unreachable; a blank name makes it unrecognizable
                    once reached.
  source_url        the "view the original page" link. Blank means a result
                    a user can't verify against the source, in a tool whose
                    whole purpose is verification.
  *_latin           for a non-Latin-script state, the only thing a
                    Latin-script query scores against. Blank means the rows
                    exist and match nothing anybody types.

None of that is visible from a row count or a spot-check of names, which is
what a contributor naturally does. And none of it needs the closed serving
app to check -- which is the point of this script living here. Contributors
building a new state can answer "is this servable?" themselves.

Usage:
    python -m check_servable data/db/multi_state_2002.sqlite
    python -m check_servable data/db/ac            # per-AC output directory
    python -m check_servable data/db/ac --state telangana
    python -m check_servable data/db/ac --state telangana --sample-names

Exits non-zero if anything is a BLOCKER. WARNINGs are printed and don't
fail: they're degradations a maintainer may knowingly ship (a source with no
locality data, a script whose romanization is imperfect), not breakage.
"""
import argparse
import collections
import glob
import hashlib
import os
import random
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from states.registry import STATE_CONNECTORS
from states.roll_years import resolve_roll_year
from transliteration import latin_residue

# The intensive-revision cycle that produced these rolls ran 2002-2006.
# Anything outside it is a stamping bug, not a state we haven't met.
PLAUSIBLE_ROLL_YEARS = range(2002, 2007)

# How many offending rows to name in a finding. Enough to see the pattern
# (one part? one AC? scattered?) without printing a million rows.
EXAMPLES = 3


# Rows to show per state for --sample-names. Enough that a systematically
# broken romanization is obvious and a human will actually read them all.
SAMPLE_DEFAULT = 100


def _seeded(state_id):
    """A per-state RNG that is the same on every machine and every run.

    Python's own hash() is salted per process, so seeding from it would make
    "the sample" mean a different set of rows to each person looking at it --
    which is the whole point of the sample. Same discipline as the serving
    repo's seeded query-distortion.
    """
    digest = hashlib.sha256(state_id.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def sample_names(conn, state_id, k, rng):
    """Up to `k` (native, romanized) pairs for one state, spread across the
    file rather than taken from the front.

    Rows are reached by seeking to random rowids, not by scanning: a state
    can be tens of millions of rows and this runs on a contributor's laptop
    before a push. `rowid >= ?` then scans forward to the next matching row,
    so a rowid landing in a gap or on another state still yields something
    instead of nothing.
    """
    cols = _columns(conn)
    if "full_name_latin" not in cols:
        return []
    bounds = conn.execute(
        "SELECT MIN(rowid), MAX(rowid) FROM voters WHERE state = ?", (state_id,)
    ).fetchone()
    if not bounds or bounds[0] is None:
        return []
    low, high = bounds
    seen, pairs = set(), []
    # Over-draw: duplicates are ordinary (a common name, or two seeks landing
    # on the same row), so drawing exactly k would usually return fewer.
    for _ in range(k * 4):
        if len(pairs) >= k:
            break
        row = conn.execute(
            "SELECT full_name, full_name_latin FROM voters "
            "WHERE rowid >= ? AND state = ? AND full_name <> '' LIMIT 1",
            (rng.randint(low, high), state_id),
        ).fetchone()
        if not row or row[0] in seen:
            continue
        seen.add(row[0])
        pairs.append((row[0], row[1] or ""))
    return pairs


def print_samples(path, state_ids, k):
    """Print the sample for every state in `path`, one block per state.

    Deliberately prints rather than judging: whether a romanization is
    *name-shaped* is the one thing in this whole script a human has to decide
    and no assertion can. Everything else here is a check; this is a view.
    """
    per_state = collections.defaultdict(list)
    for _label, conn in _connections(path):
        for state_id in [r[0] for r in conn.execute("SELECT DISTINCT state FROM voters")]:
            if state_ids and state_id not in state_ids:
                continue
            if len(per_state[state_id]) >= k:
                continue
            rng = _seeded(state_id + _label)
            per_state[state_id].extend(sample_names(conn, state_id, k, rng))
    for state_id in sorted(per_state):
        pairs = per_state[state_id][:k]
        script = (STATE_CONNECTORS.get(state_id) or {}).get("script", "latin")
        print(f"\n=== {state_id} (script={script!r}), {len(pairs)} sampled ===")
        width = max([len(n) for n, _ in pairs] + [4])
        for native, latin in pairs:
            residue = latin_residue(latin)
            flag = "  <-- residue: " + "".join(residue) if residue else ""
            if not latin:
                flag = "  <-- NO full_name_latin"
            print(f"  {native:<{width}}  |  {latin}{flag}")
    if not per_state:
        print("no rows found to sample")


class Finding:
    def __init__(self, level, state, message):
        self.level = level
        self.state = state
        self.message = message

    def __str__(self):
        return f"  {self.level:<8} [{self.state}] {self.message}"


def _examples(conn, where, params=()):
    rows = conn.execute(
        f"SELECT ac_code, part_no, serial_no, full_name FROM voters "
        f"WHERE {where} LIMIT {EXAMPLES}", params,
    ).fetchall()
    return [f"{ac}/p{p}/s{s} {name!r}" for ac, p, s, name in rows]


def _count(conn, where, params=()):
    return conn.execute(f"SELECT COUNT(*) FROM voters WHERE {where}", params).fetchone()[0]


def _columns(conn):
    """The voters table's actual columns. A file built by an older pipeline
    is simply missing the newer ones -- source_url and the *_latin pair have
    both been added since data was first pushed -- and querying one that
    isn't there raises rather than returning zero. That's the stale-per-AC-
    file case that reached production once already, so it's a finding, not a
    crash."""
    return {row[1] for row in conn.execute("PRAGMA table_info(voters)")}


def _blank(column):
    """SQL for "this column carries nothing". Empty string and NULL both
    count -- a connector that writes "" and one that writes nothing produce
    the same unusable row, and only one of them is caught by IS NULL."""
    return f"({column} IS NULL OR {column} = '')"


class Tally:
    """Everything the checks need, accumulated across however many files
    hold one state's rows.

    Per-file findings were the obvious first shape and the wrong one: a
    schema-level problem is identical in all 44 of a state's per-AC files,
    and a row-level one differs only in its count, so the output buried
    every real finding under 44 near-duplicates. Counting first and phrasing
    once means one line per problem per state, with the true total.
    """

    def __init__(self):
        self.files = 0
        self.total = 0
        self.ac_codes = set()
        self.roll_years = set()
        self.roll_year_null = 0
        self.missing_columns = set()
        self.blank = collections.Counter()
        self.examples = {}
        self.residue = {}
        self.unusable_age = 0
        self.latin_on_latin_state = 0

    def note_examples(self, key, rows):
        """First few offending rows for `key`, kept across files so the
        examples come from wherever the problem actually starts."""
        kept = self.examples.setdefault(key, [])
        kept.extend(rows[: max(0, EXAMPLES - len(kept))])


def tally_state(conn, state_id, tally):
    """Fold one open connection's rows for one state into `tally`. `conn`
    may hold one AC or a whole multi-state DB; every query is scoped by
    state, and the per-AC caller passes one file at a time."""
    total = _count(conn, "state = ?", (state_id,))
    if not total:
        return
    tally.files += 1
    tally.total += total
    tally.ac_codes.update(
        r[0] for r in conn.execute(
            "SELECT DISTINCT ac_code FROM voters WHERE state = ?", (state_id,)))

    columns = _columns(conn)
    tally.missing_columns.update({"source_url", "full_name_latin"} - columns)

    tally.roll_years.update(
        r[0] for r in conn.execute(
            "SELECT DISTINCT roll_year FROM voters WHERE state = ? "
            "AND roll_year IS NOT NULL", (state_id,)))
    tally.roll_year_null += _count(conn, "state = ? AND roll_year IS NULL", (state_id,))

    for column in ("district", "ac_name", "full_name"):
        where = f"state = ? AND {_blank(column)}"
        blank = _count(conn, where, (state_id,))
        if blank:
            tally.blank[column] += blank
            tally.note_examples(column, _examples(conn, where, (state_id,)))

    if "source_url" in columns:
        tally.blank["source_url"] += _count(
            conn, f"state = ? AND {_blank('source_url')}", (state_id,))

    named = "state = ? AND full_name IS NOT NULL AND full_name != ''"
    if "full_name_latin" in columns:
        script = STATE_CONNECTORS.get(state_id, {}).get("script", "latin")
        if script != "latin":
            tally.blank["full_name_latin"] += _count(
                conn, f"{named} AND {_blank('full_name_latin')}", (state_id,))
            for native, latin in conn.execute(
                f"SELECT DISTINCT full_name, full_name_latin FROM voters "
                f"WHERE {named} AND full_name_latin IS NOT NULL", (state_id,)
            ):
                if latin_residue(latin):
                    tally.residue[native] = latin
        else:
            tally.latin_on_latin_state += _count(
                conn, f"{named} AND NOT {_blank('full_name_latin')}", (state_id,))

    tally.unusable_age += _count(
        conn, "state = ? AND (age IS NULL OR age < 18)", (state_id,))


def findings_for(state_id, tally):
    """Turn one state's tally into findings. BLOCKER = the state cannot be
    served correctly as built. WARNING = a degradation a maintainer may
    knowingly ship."""
    findings = []
    add = lambda level, msg: findings.append(Finding(level, state_id, msg))
    total = tally.total
    script = STATE_CONNECTORS.get(state_id, {}).get("script", "latin")

    # --- a file older than the schema it's read against --------------------
    for column, level, consequence in (
        ("source_url", "BLOCKER", "every result links to nothing"),
        ("full_name_latin", "BLOCKER" if script != "latin" else "OK",
         "a Latin-script query has nothing to match this state's names against"),
    ):
        if column in tally.missing_columns and level != "OK":
            add(level, f"built by a pipeline predating the {column} column -- "
                       f"{consequence}. Rebuild this state "
                       f"(make build-db-ac STATES={state_id})")

    # --- roll year ---------------------------------------------------------
    expected = resolve_roll_year(state_id)
    if tally.roll_year_null:
        add("BLOCKER", f"{tally.roll_year_null} rows carry no roll_year")
    if len(tally.roll_years) > 1:
        add("BLOCKER", f"rows carry more than one roll_year: "
                       f"{sorted(tally.roll_years)} -- the serving app renders one "
                       f"year-of-birth ceiling per state")
    for year in sorted(tally.roll_years):
        if year not in PLAUSIBLE_ROLL_YEARS:
            add("BLOCKER", f"roll_year {year} is outside the 2002-2006 revision cycle")
        elif year != expected:
            # Not a blocker on its own: --roll-year exists, and a state may
            # genuinely be re-rolled. But it's the single most silent field
            # in the schema, so it never passes unremarked.
            add("WARNING", f"roll_year {year} but states/roll_years.py resolves "
                           f"{expected} for this state -- deliberate?")

    # --- the picker's two required labels ----------------------------------
    for column, why in (
        ("district", "district is the picker's primary tier -- a blank one is "
                     "an AC no user can navigate to"),
        ("ac_name", "a blank name is an AC nobody recognizes once reached"),
    ):
        if tally.blank[column]:
            add("BLOCKER", f"{tally.blank[column]}/{total} rows have no {column} -- "
                           f"{why}. e.g. {'; '.join(tally.examples.get(column, []))}")

    # --- verifiability -----------------------------------------------------
    if tally.blank["source_url"]:
        add("BLOCKER", f"{tally.blank['source_url']}/{total} rows have no source_url "
                       f"-- a result a user can't check against the original page, in "
                       f"a tool built for exactly that. Declare the state's ECI code "
                       f"in states/eci_codes.py and its AC-number format in "
                       f"states/source_urls.py")

    # --- the name itself ---------------------------------------------------
    if tally.blank["full_name"]:
        add("WARNING", f"{tally.blank['full_name']}/{total} rows have an empty "
                       f"full_name -- unfindable by any query. Usually an extraction "
                       f"bug confined to a part or two; check whether it clusters. "
                       f"e.g. {'; '.join(tally.examples.get('full_name', []))}")

    # --- the Latin bridge --------------------------------------------------
    if tally.blank["full_name_latin"]:
        add("BLOCKER", f"{tally.blank['full_name_latin']} named rows have no "
                       f"full_name_latin, and this state's script is {script!r} -- "
                       f"those rows match nothing a Latin-script query can type. Run "
                       f"the build (it backfills) or have the connector supply it")
    if tally.residue:
        examples = "; ".join(
            f"{n} -> {l}" for n, l in list(tally.residue.items())[:EXAMPLES])
        add("WARNING", f"{len(tally.residue)} distinct names romanize to a string "
                       f"that still holds native-script characters -- the scheme has "
                       f"no mapping for them, so a Latin query scores badly. The fix "
                       f"is a connector-supplied full_name_latin. e.g. {examples}")
    if tally.latin_on_latin_state:
        add("WARNING", f"{tally.latin_on_latin_state} rows carry a full_name_latin "
                       f"but the state is registered script='latin' -- if the rolls "
                       f"aren't actually Latin, fix the registry: script='latin' is "
                       f"what stops the backfill running at all")

    # --- age, which the required year-of-birth filter reads ----------------
    if tally.unusable_age:
        share = 100.0 * tally.unusable_age / total
        add("WARNING", f"{tally.unusable_age}/{total} rows ({share:.1f}%) have no "
                       f"usable age. The serving app spares these from its "
                       f"year-of-birth filter rather than hiding them, so this is "
                       f"informational -- but a large share means an extraction problem")

    add("OK", f"{total} rows, roll year "
              f"{sorted(tally.roll_years)[0] if tally.roll_years else '?'}, "
              f"{len(tally.ac_codes)} AC(s) in {tally.files} file(s)")
    return findings


def _connections(path):
    """(label, connection) for each file to check. A combined DB is one
    connection holding every state; a per-AC directory is one per AC file,
    which is also how the serving app reads it."""
    if os.path.isdir(path):
        # <state>/<ac>-<contract>.p<n>.sqlite, skipping catalog/, which holds
        # the per-state index rather than rows -- the same layout the serving
        # app fetches, so this checks what actually gets served.
        files = sorted(
            f for f in glob.glob(os.path.join(path, "*", "*.sqlite"))
            if os.path.basename(os.path.dirname(f)) != "catalog"
        )
        if not files:
            raise SystemExit(f"no per-AC .sqlite files under {path}")
        for f in files:
            conn = sqlite3.connect(f)
            try:
                yield os.path.relpath(f, path), conn
            finally:
                conn.close()
    else:
        conn = sqlite3.connect(path)
        try:
            yield os.path.basename(path), conn
        finally:
            conn.close()


def check(path, state_ids=None):
    """Findings for every state present in `path`, one line per problem per
    state rather than per file -- see Tally."""
    tallies = collections.defaultdict(Tally)
    for _label, conn in _connections(path):
        for state_id in [r[0] for r in conn.execute("SELECT DISTINCT state FROM voters")]:
            if state_ids and state_id not in state_ids:
                continue
            tally_state(conn, state_id, tallies[state_id])
    findings = []
    for state_id in sorted(tallies):
        findings.extend(findings_for(state_id, tallies[state_id]))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", help="a built .sqlite, or a per-AC output directory")
    parser.add_argument("--state", help="comma-separated state_ids to check (default: all present)")
    parser.add_argument(
        "--sample-names", nargs="?", type=int, const=SAMPLE_DEFAULT, default=None,
        metavar="N",
        help=f"instead of checking, print N (default {SAMPLE_DEFAULT}) native names "
             f"beside their romanization, per state, for a human to eyeball",
    )
    args = parser.parse_args(argv)

    state_ids = set(args.state.split(",")) if args.state else None

    if args.sample_names is not None:
        print_samples(args.path, state_ids, args.sample_names)
        return 0

    findings = check(args.path, state_ids)

    for level in ("BLOCKER", "WARNING", "OK"):
        for f in findings:
            if f.level == level:
                print(f)

    blockers = [f for f in findings if f.level == "BLOCKER"]
    if blockers:
        print(f"\n{len(blockers)} blocker(s) -- this data is not servable as built.")
        return 1
    print(f"\nServable. {len([f for f in findings if f.level == 'WARNING'])} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
