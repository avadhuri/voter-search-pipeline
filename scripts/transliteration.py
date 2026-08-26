"""
Bridges a Latin-script query against non-Latin-script source data.

Two things produce the `*_latin` columns that matching actually scores
against, and they are not equals:

1. **A connector that already has a romanization** puts it on the
   VoterRecord (`full_name_latin` / `full_relative_name_latin`) and it is
   kept verbatim. A state whose extraction pipeline produced a Latin name
   alongside the native one knows more than this module does -- it saw the
   source, and its output is name-shaped rather than scheme-shaped.
2. **Everything else** is filled in here, rule-based, from the native
   string. `backfill_latin_columns()` only ever writes where the column is
   NULL or empty, so it can never overwrite (1).

The rule-based half uses indic_transliteration's Indic->ITRANS romanization
rather than a neural model (AI4Bharat's IndicXlit): IndicXlit's PyPI package
depends on `fairseq`, whose sdist has had a broken build (`fairseq/version.txt`
missing) for a long time -- three separate install strategies all failed in
this project's venv. `indic_transliteration` is pure Python and installs in
seconds.

**How good it is depends on the script, and for one script it is poor.**
Measured with the same scorer and processor the app uses
(`fuzz.WRatio(query, latin, processor=utils.default_process)`), against real
name/spelling pairs:

    Devanagari  ~95    सुभाष -> subhASha        vs subhash    93
    Telugu      ~93    కృష్ణా -> kRRiShNA         vs krishna    93
    Bengali     ~90    সুব্রত -> suvrata         vs subrata    86
    Malayalam   ~82    കൃഷ്ണൻ -> kRRiShNaൻ       vs krishnan   82
    Gurmukhi    ~78    ਗੁਰਪ੍ਰੀਤ -> guraprIta       vs gurpreet   71
    Tamil       ~73    செல்வம் -> jhèlvaM         vs selvam     62

WRatio absorbs the ITRANS scheme's ordinary artifacts (inherent vowels,
capitalized retroflex markers) as typos, which is why the top of that list
needs nothing further. The bottom of it has two real defects, not
artifacts:

- **Tamil maps several consonants to the wrong Latin letters entirely.**
  ச -> "jh" (Selvam becomes "jhelvam"), ட -> "Dh". These are not near-misses
  a fuzzy scorer recovers from; 4 of 6 sampled Tamil names scored below 80.
- **Some letters do not transliterate at all** and survive into the output
  as native codepoints -- Malayalam chillu forms (ൻ ൽ ൾ, i.e. most
  Malayalam names ending in -n/-l), Tamil ன. Every target scheme
  (ITRANS/IAST/ISO/HK/SLP1) leaves them, and so does routing via
  Devanagari.

So for Tamil and Malayalam states, a connector-supplied romanization is not
a nice-to-have: it is the only usable Latin key. `latin_residue()` and
`backfill_latin_columns()`'s `incomplete` count exist so a build says which
names came out this way instead of leaving it to be discovered by a user
who can't find themselves.

Precomputed once per distinct name string, not per query or per row: a
1.6M-row Haryana table has only ~183K distinct full_name/full_relative_name
values combined (names repeat heavily -- "सुनीता" alone appears 12K+ times),
and transliterating all of them takes about 3 seconds.
"""
import collections
import re

from indic_transliteration import sanscript

from states.registry import STATE_CONNECTORS

# Unicode block -> (regex class, sanscript scheme). Detection is by
# codepoint, not by the registry's `script` tag: the tag says what a state
# is mostly in, and a single roll can carry more than one script (Puducherry
# spans Tamil, Telugu and Malayalam), so the string itself is the authority
# on how to romanize it. Blocks are listed in codepoint order; the first
# Indic character found in a string decides the scheme for the whole string.
_SCRIPT_BLOCKS = (
    ("devanagari", "ऀ-ॿ", sanscript.DEVANAGARI),
    ("bengali",    "ঀ-৿", sanscript.BENGALI),
    ("gurmukhi",   "਀-੿", sanscript.GURMUKHI),
    ("gujarati",   "઀-૿", sanscript.GUJARATI),
    ("oriya",      "଀-୿", sanscript.ORIYA),
    ("tamil",      "஀-௿", sanscript.TAMIL),
    ("telugu",     "ఀ-౿", sanscript.TELUGU),
    ("kannada",    "ಀ-೿", sanscript.KANNADA),
    ("malayalam",  "ഀ-ൿ", sanscript.MALAYALAM),
)

# Any Indic codepoint at all, including scripts with no sanscript scheme
# (Meetei Mayek, Ol Chiki, Tibetan). Used by is_latin_query() and
# latin_residue(), which care about "is this Latin?", not "can we romanize
# it?" -- a state in an unsupported script must fail visibly at build time,
# not have its queries silently routed to columns that were never filled.
_INDIC_RANGE = "".join(rng for _name, rng, _scheme in _SCRIPT_BLOCKS)
INDIC_RE = re.compile(f"[{_INDIC_RANGE}඀-෿ༀ-࿿ꯀ-꯿᱐-᱿]")

_SCHEME_RES = [(re.compile(f"[{rng}]"), scheme) for _name, rng, scheme in _SCRIPT_BLOCKS]

# Retained under its old name: Devanagari is still the only script the three
# originally-shipped states use, and the serving app pinned to a pre-this-change
# commit imports it. non_latin_state_ids() is what new code should use.
DEVANAGARI_RE = _SCHEME_RES[0][0]

TRANSLIT_COLUMNS = ["full_name", "full_relative_name"]

BackfillResult = collections.namedtuple(
    "BackfillResult", ["transliterated", "incomplete", "sample"]
)


def detect_scheme(text):
    """The sanscript scheme for a string, from its first Indic character.
    None for an empty/Latin string, or one in an Indic script sanscript has
    no scheme for."""
    if not text:
        return None
    for pattern, scheme in _SCHEME_RES:
        if pattern.search(text):
            return scheme
    return None


def to_latin(text):
    """Native-script string -> romanized (ITRANS) string. Empty or
    already-Latin input passes through unchanged (a cheap no-op if this is
    ever called on a Latin-script state's rows).

    A string in an Indic script sanscript has no scheme for also passes
    through unchanged rather than raising -- see latin_residue(), which is
    how that case gets reported instead of silently producing a column that
    is a copy of the native one.
    """
    scheme = detect_scheme(text)
    if scheme is None:
        return text
    return sanscript.transliterate(text, scheme, sanscript.ITRANS)


def latin_residue(text):
    """The distinct native-script characters left in a romanized string.

    Empty for a clean transliteration. Non-empty means the scheme had no
    mapping for those characters and passed them through -- Malayalam
    chillu forms and Tamil ன are the known cases, and both are common name
    endings, so a name with residue is one a Latin-script query will score
    badly against. See the module docstring.
    """
    return sorted(set(INDIC_RE.findall(text or "")))


def needs_latin_bridge(text):
    """True if this string carries characters a Latin-script query cannot
    type -- i.e. this row is unsearchable from a Latin keyboard unless a
    romanized column is filled in for it.

    The per-*row* counterpart to the registry's per-state `script` field,
    and the one servability actually turns on. The two are not the same
    question and must not be substituted for each other: `script` answers
    "could any row in this state be non-Latin?", which is a routing hint for
    whether to run the backfill at all, while this answers "does *this* name
    need romanizing?". West Bengal is why the distinction is load-bearing --
    its Kolkata ACs are Latin-typeset and its other ACs are Bengali, in one
    state, permanently. Keying a per-row check off the state's script counts
    every Latin row in a non-Latin state as missing a column it has no use
    for.
    """
    return bool(INDIC_RE.search(text or ""))


def is_latin_query(text):
    """True if text has no Indic codepoints, i.e. it should be matched
    against a non-Latin-script state's *_latin columns rather than its raw
    ones. A native-script query still matches the raw columns directly, no
    transliteration involved."""
    return bool(text) and not needs_latin_bridge(text)


def non_latin_state_ids(state_ids=None):
    """Every registry state_id whose data is not in Latin script, optionally
    restricted to a subset -- i.e. every state whose rows need *_latin
    columns to be searchable by a Latin-script query.

    Keyed off the registry's `script` being anything other than "latin",
    rather than an allow-list of known scripts, so a state added with a
    script this module can't romanize still gets routed through the backfill
    (and reported as incomplete) instead of being silently treated as Latin
    and never matching anything.
    """
    return [
        sid for sid, info in STATE_CONNECTORS.items()
        if info.get("script", "latin") != "latin"
        and (state_ids is None or sid in state_ids)
    ]


def devanagari_state_ids(state_ids=None):
    """Every registry state_id flagged script=devanagari, optionally
    restricted to a subset.

    Kept exactly as it was -- it means what its name says. New code wants
    non_latin_state_ids(); this stays exported because the serving app
    pinned to an older commit of this repo imports it by name, and a pin
    bump shouldn't be able to break an app that hasn't been updated yet.
    """
    return [
        sid for sid, info in STATE_CONNECTORS.items()
        if info.get("script") == "devanagari" and (state_ids is None or sid in state_ids)
    ]


def ensure_latin_columns(conn):
    """ALTER TABLE ADD COLUMN for full_name_latin / full_relative_name_latin
    if they don't already exist -- needed for any DB built before this
    feature shipped. Idempotent; returns True if it changed the schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(voters)")}
    changed = False
    for column in TRANSLIT_COLUMNS:
        latin_column = f"{column}_latin"
        if latin_column not in existing:
            conn.execute(f"ALTER TABLE voters ADD COLUMN {latin_column} TEXT")
            changed = True
    return changed


def backfill_latin_columns(conn, state_ids=None, only_missing=True):
    """Populate *_latin columns for every non-Latin-script state's rows, one
    distinct string at a time via a temp-table join (not a per-row UPDATE --
    at 183K distinct strings that's the difference between ~3s and minutes).

    `only_missing` (the default) skips any row that already has a Latin
    value, empty string included. That is what makes a connector-supplied
    romanization authoritative: a connector that filled the column keeps its
    own value, and only rows it left blank are romanized here. Passing
    only_missing=False deliberately overwrites everything, connector output
    included -- for re-running after a change to this module, not for
    ordinary builds.

    Returns a BackfillResult: how many distinct strings were transliterated,
    how many of those came out still holding native-script characters (see
    latin_residue()), and up to five examples of the latter. Caller commits.
    """
    target_states = non_latin_state_ids(state_ids)
    if not target_states:
        return BackfillResult(0, 0, [])

    state_placeholders = ",".join("?" * len(target_states))
    transliterated = 0
    incomplete = 0
    sample = []

    for column in TRANSLIT_COLUMNS:
        latin_column = f"{column}_latin"
        missing_clause = (
            f"AND ({latin_column} IS NULL OR {latin_column} = '')"
            if only_missing else ""
        )
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM voters "
            f"WHERE state IN ({state_placeholders}) AND {column} IS NOT NULL "
            f"AND {column} != '' {missing_clause}",
            target_states,
        ).fetchall()
        distinct_values = [r[0] for r in rows]
        if not distinct_values:
            continue

        pairs = []
        for value in distinct_values:
            latin = to_latin(value)
            pairs.append((value, latin))
            if latin_residue(latin):
                incomplete += 1
                if len(sample) < 5:
                    sample.append((value, latin))

        conn.execute("DROP TABLE IF EXISTS _translit_map")
        conn.execute("CREATE TEMP TABLE _translit_map (name TEXT PRIMARY KEY, latin TEXT)")
        conn.executemany("INSERT INTO _translit_map (name, latin) VALUES (?, ?)", pairs)
        conn.execute(
            f"""
            UPDATE voters
            SET {latin_column} = (SELECT latin FROM _translit_map WHERE name = {column})
            WHERE state IN ({state_placeholders})
              AND {column} IN (SELECT name FROM _translit_map)
              {missing_clause}
            """,
            target_states,
        )
        conn.execute("DROP TABLE _translit_map")
        transliterated += len(distinct_values)

    return BackfillResult(transliterated, incomplete, sample)


def backfill_latin_for_rows(conn, rows, non_latin_states):
    """Lazy-eval fallback for rows fetched mid-search whose *_latin columns
    are still empty (a DB that hasn't had backfill_latin_columns() run
    against it yet, or a row added since the last run). Mutates `rows` (a
    list of dicts) in place and persists the computed values so the next
    search on the same names doesn't recompute them. Returns the number of
    rows touched.

    Treats "" the same as NULL, matching backfill_latin_columns() -- a
    connector-supplied value is never recomputed, a blank one always is.
    """
    updates = []
    for r in rows:
        if r["state"] not in non_latin_states:
            continue
        new_vals = {}
        for column in TRANSLIT_COLUMNS:
            latin_column = f"{column}_latin"
            if not r.get(latin_column) and r.get(column):
                new_vals[latin_column] = to_latin(r[column])
        if new_vals:
            r.update(new_vals)
            updates.append((
                new_vals.get("full_name_latin"),
                new_vals.get("full_relative_name_latin"),
                r["id"],
            ))
    if updates:
        conn.executemany(
            "UPDATE voters SET "
            "full_name_latin = COALESCE(?, full_name_latin), "
            "full_relative_name_latin = COALESCE(?, full_relative_name_latin) "
            "WHERE id = ?",
            updates,
        )
        conn.commit()
    return len(updates)
