"""
The Latin-script bridge: script dispatch, what it can't romanize, and the
rule that a connector's own romanization always wins.

Why this file exists: for a non-Latin-script state, the `*_latin` columns
are the *only* thing a Latin-script query scores against. Get them wrong and
every row still builds, parses, ranks and passes every search-quality check
-- it is simply unfindable by the queries real users type. That is the same
invisible-downstream failure class as roll_year, and it gets caught here.
"""
import sqlite3

import pytest

import build_db
import transliteration as tl
from states.base import Constituency, StateConnector, VoterRecord


# --- script dispatch -------------------------------------------------------

@pytest.mark.parametrize("native,expected_start", [
    ("सुभाष", "subh"),      # Devanagari -- Haryana, the state this shipped for
    # Bengali. Was "suvr" when this case was written -- ITRANS gives ব the
    # Sanskrit reading of व. The roll's own English settles it: across the 19
    # Latin-typeset Kolkata ACs the CEO prints Subrata/Subrato/Subroto 7,961
    # times and Suvrata/Suvrat 5, a 1,592:1 ratio in favour of b.
    ("সুব্রত", "subr"),       # Bengali
    ("ਗੁਰਪ੍ਰੀਤ", "gura"),      # Gurmukhi
    ("કૃષ્ણ", "kRRi"),       # Gujarati
    ("କୃଷ୍ଣ", "kRRi"),       # Oriya
    ("కృష్ణా", "kRRi"),      # Telugu
    ("ಕೃಷ್ಣ", "kRRi"),       # Kannada
    ("കൃഷ്ണ", "kRRi"),      # Malayalam
])
def test_each_supported_script_romanizes_rather_than_passing_through(native, expected_start):
    """Before this change to_latin() hardcoded Devanagari, so a Telugu or
    Malayalam string came back byte-identical -- i.e. the latin column was a
    copy of the native one and a Latin query matched nothing at all."""
    latin = tl.to_latin(native)
    assert latin != native
    assert latin.startswith(expected_start), latin


def test_a_latin_string_passes_through_untouched():
    """Karnataka and West Bengal's Latin-typeset rolls go through the same
    code path in a mixed build; romanizing them would corrupt them."""
    assert tl.to_latin("Ramesh Kumar") == "Ramesh Kumar"
    assert tl.to_latin("") == ""
    assert tl.to_latin(None) is None


def test_the_first_indic_character_picks_the_scheme_for_the_whole_string():
    """Detection is per-string by codepoint, not per-state by registry tag:
    Puducherry's roll spans Tamil, Telugu and Malayalam, so the state's tag
    can't decide. Documented as first-wins because a genuinely mixed-script
    single name has no right answer -- it just needs a deterministic one."""
    from indic_transliteration import sanscript
    assert tl.detect_scheme("கண்ணன்") == sanscript.TAMIL
    assert tl.detect_scheme("కృష్ణా") == sanscript.TELUGU
    assert tl.detect_scheme("Ramesh") is None
    assert tl.detect_scheme("") is None


# --- what it can't do, stated as a test rather than discovered by a user ----

def test_malayalam_chillus_survive_transliteration_and_are_reported():
    """ൻ/ൽ/ൾ have no mapping in any target scheme (checked across
    ITRANS/IAST/ISO/HK/SLP1 and via-Devanagari), and they end a large share
    of Malayalam names. The point of latin_residue() is that this is
    *reported* at build time rather than showing up as a user who can't find
    themselves."""
    latin = tl.to_latin("കൃഷ്ണൻ")
    assert "ൻ" in latin
    assert tl.latin_residue(latin) == ["ൻ"]


def test_a_clean_transliteration_reports_no_residue():
    assert tl.latin_residue(tl.to_latin("सुभाष")) == []
    assert tl.latin_residue("") == []
    assert tl.latin_residue(None) == []


# --- query-side ------------------------------------------------------------

def test_is_latin_query_covers_every_indic_script_not_just_devanagari():
    assert tl.is_latin_query("ramesh")
    assert not tl.is_latin_query("सुभाष")
    assert not tl.is_latin_query("கண்ணன்")
    assert not tl.is_latin_query("കൃഷ്ണൻ")
    assert not tl.is_latin_query("Ramesh கண்ணன்")


def test_non_latin_state_ids_is_script_not_equal_latin_not_an_allow_list():
    """A state registered with a script this module has no scheme for must
    still be routed through the backfill -- so its rows are reported as
    incomplete rather than silently treated as Latin, which would leave a
    Latin query matching nothing with nothing said about it."""
    ids = tl.non_latin_state_ids()
    assert "haryana" in ids
    assert "karnataka" not in ids
    assert tl.non_latin_state_ids(["karnataka"]) == []


def test_west_bengal_is_non_latin():
    """It stopped being a Latin-only state when the Shree-Lipi decoder landed
    and the page-scan ACs started coming back from OCR -- both produce real
    Bengali text. Without this flag the backfill never runs for West Bengal,
    so every Bengali row's *_latin columns stay empty and a Latin-script
    query matches none of them, with nothing said about it.

    The 19 Latin-typeset Kolkata ACs are not harmed by being swept in: the
    scheme is chosen per string, not per state, so a name holding no Indic
    character is written through unchanged."""
    assert "west_bengal" in tl.non_latin_state_ids()
    assert tl.non_latin_state_ids(["west_bengal"]) == ["west_bengal"]
    assert tl.to_latin("Sourav Ganguly") == "Sourav Ganguly"
    assert tl.detect_scheme("\u09b0\u09ae\u09c7\u09b6") is not None


def test_devanagari_state_ids_still_means_exactly_what_it_did():
    """Kept under its old name and old meaning: the serving app pinned to a
    pre-this-change commit imports it, and a pin bump shouldn't be able to
    break an app that hasn't been updated yet."""
    assert tl.devanagari_state_ids() == ["haryana"]


# --- precedence: a connector's own romanization wins ------------------------

class _TeluguConnector(StateConnector):
    """Two rows: one whose connector supplied a proper Latin name, one that
    left it blank. Mirrors the real incoming shape -- a CSV carrying both
    `elector_name` and `elector_name_en`, where `_en` is sometimes empty."""

    state_id = "faketelugu"

    def list_constituencies(self):
        return [Constituency(ac_code="188", ac_name="Fake AC", district="Fake District")]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        common = dict(
            state=self.state_id, district=ac.district, ac_code=ac.ac_code,
            ac_name=ac.ac_name, part_no=1, local_ref="", relation_code="F",
            age=30, gender="M", roll_year=roll_year,
        )
        return [
            VoterRecord(
                serial_no=1, full_name="కృష్ణా", full_relative_name="రామయ్య",
                full_name_latin="Krishna", full_relative_name_latin="Ramaiah",
                **common,
            ),
            VoterRecord(
                serial_no=2, full_name="కృష్ణా", full_relative_name="రామయ్య",
                **common,
            ),
        ]


def _register(monkeypatch, state_id, cls, raw_dir):
    monkeypatch.setitem(build_db.STATE_CONNECTORS, state_id, {
        "connector_cls": cls,
        "label": state_id.title(),
        "raw_dir": str(raw_dir),
        "raw_glob": "*.csv",
        "script": "telugu",
    })


def _build(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "188.csv").write_text("placeholder\n")
    _register(monkeypatch, "faketelugu", _TeluguConnector, raw_dir)
    db_path = tmp_path / "out.sqlite"
    build_db.build_multi_state(["faketelugu"], str(db_path))
    return sqlite3.connect(db_path)


def test_a_connector_supplied_romanization_is_kept_and_a_blank_one_is_filled(tmp_path, monkeypatch):
    """The whole reason VoterRecord grew these fields. Rule-based ITRANS
    emits kRRiShNA -- fine as a fuzzy key, unusable as a displayed name, and
    for Tamil actively wrong. A connector that saw the source beats it, so
    the backfill must fill only where the connector left a blank."""
    conn = _build(tmp_path, monkeypatch)
    rows = dict(conn.execute(
        "SELECT serial_no, full_name_latin FROM voters ORDER BY serial_no"
    ).fetchall())
    assert rows[1] == "Krishna"           # untouched
    assert rows[2].startswith("kRRi")     # backfilled
    assert rows[2] != "Krishna"


def test_the_native_name_is_stored_native_not_replaced_by_its_romanization(tmp_path, monkeypatch):
    """The other half of the same fix: before it, a non-Latin state had to
    choose between a searchable roll and a displayable one. It now gets
    both -- native in full_name, Latin in full_name_latin."""
    conn = _build(tmp_path, monkeypatch)
    names = {r[0] for r in conn.execute("SELECT full_name FROM voters")}
    assert names == {"కృష్ణా"}


def test_only_missing_false_deliberately_overwrites_connector_values(tmp_path, monkeypatch):
    """The escape hatch, for re-running after a change to this module. Not
    what any ordinary build does."""
    conn = _build(tmp_path, monkeypatch)
    tl.backfill_latin_columns(conn, state_ids=["faketelugu"], only_missing=False)
    latins = {r[0] for r in conn.execute("SELECT full_name_latin FROM voters")}
    assert latins == {tl.to_latin("కృష్ణా")}


def test_the_backfill_reports_how_many_names_it_could_not_fully_romanize():
    """Counted per distinct string, and carries examples, so a build's
    warning says which state needs a connector-supplied full_name_latin
    rather than just that something is wrong somewhere."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(build_db.VOTERS_SCHEMA)
    rows = [("haryana", "കൃഷ്ണൻ"), ("haryana", "सुभाष"), ("karnataka", "Ramesh")]
    for state, name in rows:
        conn.execute(
            "INSERT INTO voters (state, roll_year, district, ac_code, ac_name, "
            "part_no, serial_no, full_name, full_relative_name, age, gender) "
            "VALUES (?,2002,'D','A1','AC',1,1,?,?,30,'M')",
            (state, name, name),
        )
    result = tl.backfill_latin_columns(conn)
    # Two distinct Haryana strings x two name columns; Karnataka is Latin
    # script and isn't touched at all.
    assert result.transliterated == 4
    assert result.incomplete == 2
    assert result.sample and all("ൻ" in latin for _native, latin in result.sample)


def test_lazy_row_backfill_treats_an_empty_string_the_same_as_null(tmp_path, monkeypatch):
    """app.py's per-search fallback and the build-time backfill have to agree
    on what 'already has a value' means, or a row the build deliberately left
    to the connector gets recomputed on every single search."""
    conn = _build(tmp_path, monkeypatch)
    rows = [
        {"id": 1, "state": "faketelugu", "full_name": "కృష్ణా",
         "full_relative_name": "రామయ్య", "full_name_latin": "Krishna",
         "full_relative_name_latin": "Ramaiah"},
        {"id": 2, "state": "faketelugu", "full_name": "కృష్ణా",
         "full_relative_name": "రామయ్య", "full_name_latin": "",
         "full_relative_name_latin": ""},
    ]
    touched = tl.backfill_latin_for_rows(conn, rows, {"faketelugu"})
    assert touched == 1
    assert rows[0]["full_name_latin"] == "Krishna"
    assert rows[1]["full_name_latin"].startswith("kRRi")


def test_needs_latin_bridge_is_a_fact_about_the_string_not_the_state():
    """The per-row counterpart to the registry's per-state `script`. West
    Bengal is registered script='bengali' and holds Latin-typeset ACs; a
    Latin name in it needs no romanized column, and counting it as missing
    one turned the servability gate red for 2M perfectly servable rows."""
    assert tl.needs_latin_bridge("সন্তোষ")
    assert tl.needs_latin_bridge("सुभाष")
    assert not tl.needs_latin_bridge("RAMESH KUMAR")
    assert not tl.needs_latin_bridge("A. K. GHOSH")
    assert not tl.needs_latin_bridge("")
    assert not tl.needs_latin_bridge(None)


def test_is_latin_query_and_needs_latin_bridge_cannot_drift():
    """They are the same predicate asked from two sides, so one is defined
    in terms of the other rather than repeating the regex."""
    for text in ("সন্তোষ", "सुभाष", "RAMESH KUMAR", "কলকাতা 12", "12345"):
        assert tl.is_latin_query(text) is (
            not tl.needs_latin_bridge(text))


class _MixedScriptConnector(StateConnector):
    """West Bengal's real shape, minimised: one state, two ACs, two scripts.

    AC141 is Kolkata -- Latin-typeset, 2,054,521 rows of it in production
    today. AC001 is Bengali-typeset. They are the same state and always will
    be, so any rule keyed on the state's registered `script` is answering a
    question about AC001 and applying it to AC141.
    """

    state_id = "fakemixed"

    def list_constituencies(self):
        return [
            Constituency(ac_code="141", ac_name="Kolkata Fake", district="D"),
            Constituency(ac_code="001", ac_name="Bengali Fake", district="D"),
        ]

    def fetch_raw(self, ac, roll_year):
        raise NotImplementedError

    def parse_raw(self, raw, ac, roll_year):
        latin = ac.ac_code == "141"
        common = dict(
            state=self.state_id, district=ac.district, ac_code=ac.ac_code,
            ac_name=ac.ac_name, part_no=1, local_ref="", relation_code="F",
            age=30, gender="M", roll_year=roll_year,
        )
        return [VoterRecord(
            serial_no=1,
            full_name="RAMESH KUMAR DAS" if latin else "রমেশ মণ্ডল",
            full_relative_name="SUNIL DAS" if latin else "সুনীল মণ্ডল",
            **common,
        )]


def _build_mixed(tmp_path, monkeypatch):
    raw_dir = tmp_path / "rawmixed"
    raw_dir.mkdir()
    (raw_dir / "141.csv").write_text("placeholder\n")
    (raw_dir / "001.csv").write_text("placeholder\n")
    monkeypatch.setitem(build_db.STATE_CONNECTORS, "fakemixed", {
        "connector_cls": _MixedScriptConnector,
        "label": "Fake Mixed",
        "raw_dir": str(raw_dir),
        "raw_glob": "*.csv",
        "script": "bengali",
    })
    db_path = tmp_path / "mixed.sqlite"
    build_db.build_multi_state(["fakemixed"], str(db_path))
    return sqlite3.connect(db_path)


def test_the_build_backfill_skips_a_latin_row_in_a_non_latin_state(tmp_path, monkeypatch):
    """The discriminating test. Before this, the backfill selected on
    `state IN (...)` alone, so registering West Bengal as script='bengali'
    romanized all 2,054,521 of its live Latin Kolkata names -- a no-op
    to_latin() whose only effect was writing a column those rows have no
    use for.

    That is not merely wasteful: check_servable's `latin_on_latin_row`,
    added in the same round, WARNs on precisely a Latin row carrying a
    populated latin column. The two halves of one change disagreed, and
    this asserts they now don't.
    """
    conn = _build_mixed(tmp_path, monkeypatch)
    got = dict(conn.execute(
        "SELECT ac_code, full_name_latin FROM voters ORDER BY ac_code").fetchall())
    assert got["001"] and got["001"].startswith("ramesha"), got
    assert not got["141"], (
        "a Latin name in a non-Latin state was romanized: " + repr(got["141"]))


def test_the_lazy_row_backfill_skips_a_latin_row_too(tmp_path, monkeypatch):
    """Same rule at serve time, where it also costs a persisted UPDATE per
    row on every Cloud Run instance that touches them -- and the app calls
    this on the rows a search already fetched, so a single Latin query
    against a Kolkata AC would have written the whole AC."""
    conn = _build_mixed(tmp_path, monkeypatch)
    rows = [
        {"id": 1, "state": "fakemixed", "full_name": "RAMESH KUMAR DAS",
         "full_relative_name": "SUNIL DAS",
         "full_name_latin": "", "full_relative_name_latin": ""},
        {"id": 2, "state": "fakemixed", "full_name": "রমেশ মণ্ডল",
         "full_relative_name": "সুনীল মণ্ডল",
         "full_name_latin": "", "full_relative_name_latin": ""},
    ]
    touched = tl.backfill_latin_for_rows(conn, rows, {"fakemixed"})
    assert touched == 1, "the Latin row should not have been touched"
    assert rows[0]["full_name_latin"] == ""
    assert rows[1]["full_name_latin"].startswith("ramesha")


def test_a_latin_row_left_blank_is_what_the_servability_gate_wants(tmp_path, monkeypatch):
    """Ties the two halves together rather than asserting them separately:
    run the build, then run check_servable's own per-row counter over the
    result. A disagreement here is the bug this pair of tests exists for,
    and asserting it through the real counter means a future change to
    either half cannot quietly re-open it."""
    conn = _build_mixed(tmp_path, monkeypatch)
    conn.create_function(
        "needs_latin_bridge", 1, lambda t: 1 if tl.needs_latin_bridge(t) else 0)
    latin_on_latin = conn.execute(
        "SELECT COUNT(*) FROM voters WHERE NOT needs_latin_bridge(full_name) "
        "AND full_name_latin IS NOT NULL AND full_name_latin != ''"
    ).fetchone()[0]
    assert latin_on_latin == 0
    # ...and the half that must still happen: the Bengali row is bridged.
    bridged = conn.execute(
        "SELECT COUNT(*) FROM voters WHERE needs_latin_bridge(full_name) "
        "AND full_name_latin IS NOT NULL AND full_name_latin != ''"
    ).fetchone()[0]
    assert bridged == 1


# --- Bengali: the two corrections the real WB build forced --------------
#
# Both are Bengali-only on purpose, and both are measured rather than
# argued. The corpus is a real built AC001 (151,358 rows / 65,749 distinct
# name strings); the target spellings are not invented, they are the
# spellings the CEO's own 19 Latin-typeset Kolkata ACs print for the same
# names.

def test_a_decomposed_nukta_does_not_survive_into_the_latin_column():
    """The romanized column exists to be reachable from a Latin keyboard,
    so a Bengali codepoint left inside it is the column failing at its one
    job. ITRANS passes a lone U+09BC straight through; 29.08% of AC001's
    distinct name strings came out holding one.

    Written with escapes, not pasted literals, deliberately: the first
    version of the measurement script that found this bug had a map whose
    two sides round-tripped to the *same* decomposed form, so every
    replace() was a silent no-op and the fix looked score-neutral when it
    had simply never run.
    """
    for base, composed in (("ড", "ড়"), ("ঢ", "ঢ়"),
                           ("য", "য়")):
        decomposed = base + "়"
        assert "়" not in tl.to_latin(decomposed), base
        assert tl.to_latin(decomposed) == \
            tl.to_latin(composed)
    # The real row this was found on.
    assert "়" not in tl.to_latin("কামিনী কীর্ত্তনীয়া")


def test_nfc_is_not_the_fix():
    """U+09DC/09DD/09DF are Unicode composition exclusions, so normalize()
    leaves the sequence decomposed. This is why the map is explicit -- if
    this ever starts failing, the map can go."""
    import unicodedata
    assert unicodedata.normalize("NFC", "ড়") == "ড়"


def test_bengali_ba_romanizes_to_b_not_v():
    """ITRANS gives ব the Sanskrit reading of व. Bengali ব is /b/ and the
    Latin-typeset ACs spell it that way."""
    for bengali, expect in (("বীরেন", "bIrena"), ("চক্রবর্তী", "chakrabartI"),
                            ("বিবি", "bibi"), ("বিশ্বাস", "bishbAsa")):
        assert tl.to_latin(bengali) == expect


def test_devanagari_keeps_its_v():
    """The discriminating half. Hindi व genuinely is v/w, so Haryana must
    not get the Bengali flattening -- and sanscript's Devanagari scheme
    already handles a bare nukta, so it must not get the map either."""
    assert tl.to_latin("वर्मा") == "varmA"
    assert "़" not in tl.to_latin("ड़")
