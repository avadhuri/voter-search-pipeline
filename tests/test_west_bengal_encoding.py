"""Which script a font is in, and which glyphs of it can be shown as ASCII.

These are two questions and _patch_pdfminer_gid_encoding answered them with
one expression:

    latin = all(3 <= gid <= MAC_LATIN_MAX for _, gid in pairs)

Both bounds are the *mapping's* preconditions -- chr(gid + 29) is printable
for exactly gids 3..97 -- so putting them under all() promoted a per-glyph
render check into a per-font script vote. A subset font whose Differences
array includes gid 0 (.notdef, which carries no text and cannot be evidence
of anything) then failed the vote on behalf of every readable glyph beside
it. All of its ASCII went to the private-use area, _record saw undecoded
characters, and the row was blanked with the remark "name in an unrecognized
Bengali-script font" -- a confident, specific and false statement about the
source, rendered to a citizen checking whether they were on the 2002 roll.
79,638 rows are in that state on the live site.

The real gid profiles below are what makes the classification unambiguous
rather than a close call: on the affected parts no font reaches past gid 93,
while the fonts that genuinely are Bengali reach 220-224. Nothing sits near
the boundary. The tests therefore assert the classification of a Differences
array directly, not a row count -- a row count would also pass if the
recovery came from somewhere else, and it is the vote that was wrong.
"""
import os
import zipfile

import pytest

from states import west_bengal as wb
from states.west_bengal import MAC_LATIN_MAX, PUA_BASE, TEXTLESS_MAX_GID

# Real profiles, read off the fonts of the parts named. AC148 part 22 and
# AC143 part 5 are affected parts on live constituencies; AC003/AC010 are the
# Bengali font the Shree-Lipi table does decode; AC023 is Darjeeling, which is
# refused for a different and genuine reason.
LATIN_RUN = [3, 29, 55, 70, 93]          # AC148 p22 / AC143 p5: 3..93, all Latin
BENGALI_RUN = [3, 66, 140, 201, 224]     # AC003 p1 / AC010 p1: 3..224
DARJEELING_RUN = [7, 88, 150, 220]       # AC023 p1: 7..220


def _diff(gids):
    """A Differences array in the /GXX form _gid_pairs recognises."""
    out = [0]
    for gid in gids:
        out.append("G%02X" % gid)
    return out


def _encode(gids):
    """-> {gid: decoded character}, via the real Differences parsing."""
    import pdfminer.encodingdb as edb

    wb._patch_pdfminer_gid_encoding()
    diff = _diff(gids)
    enc = edb.EncodingDB.get_encoding("WinAnsiEncoding", diff)
    return {gid: enc[code] for code, gid in wb._gid_pairs(diff)}


def _is_pua(ch):
    return len(ch) == 1 and PUA_BASE <= ord(ch) < PUA_BASE + 0x1000


# -- the bug ---------------------------------------------------------------

def test_a_textless_glyph_does_not_make_a_latin_font_bengali():
    enc = _encode([0] + LATIN_RUN)
    for gid in LATIN_RUN:
        assert enc[gid] == chr(gid + 29), "gid %d should decode as ASCII" % gid


def test_the_textless_code_contributes_no_characters():
    """Not a control character, and not absent from the encoding either.

    chr(0 + 29) is '\\x1d', which lands in a name. Leaving the code out of the
    dict is worse: pdfminer's handle_undefined_char substitutes the literal
    string "(cid:0)", which renders. Measured on AC143 part 5 serial 193,
    where the three candidates give 'Mitali Mukherjee\\x1d\\x1d',
    'Mitali Mukherjee (cid:0)(cid:0)' and 'Mitali Mukherjee'.
    """
    enc = _encode([0, 1, 2] + LATIN_RUN)
    for gid in range(TEXTLESS_MAX_GID + 1):
        assert gid in enc, "the code must be mapped, or pdfminer writes (cid:N)"
        assert enc[gid] == "", "a textless glyph carries no text"


# -- the controls, which are the half that matters -------------------------

@pytest.mark.parametrize("gids", [BENGALI_RUN, DARJEELING_RUN])
def test_a_font_reaching_past_the_latin_run_stays_undecoded(gids):
    enc = _encode(gids)
    assert all(_is_pua(ch) for ch in enc.values()), (
        "a font with a gid above %d is not Latin and must reach _record as "
        "undecoded, whatever else is in it" % MAC_LATIN_MAX
    )


def test_a_textless_glyph_does_not_rescue_a_bengali_font_either():
    """The relaxation has to be neutral in both directions.

    Dropping textless gids from the vote must not make an otherwise
    out-of-range font classify as Latin -- that would silently turn real
    Bengali into ASCII mojibake, which is worse than the blank it replaces.
    """
    enc = _encode([0] + BENGALI_RUN)
    assert all(_is_pua(ch) for ch in enc.values())


def test_an_all_textless_font_is_not_latin_by_vacuous_truth():
    """all([]) is True. A font with nothing but .notdef has no evidence in it."""
    enc = _encode([0, 1, 2])
    assert all(_is_pua(ch) for ch in enc.values())


def test_a_latin_subset_with_no_textless_glyph_is_unchanged():
    enc = _encode(LATIN_RUN)
    for gid in LATIN_RUN:
        assert enc[gid] == chr(gid + 29)


# -- against the real PDFs -------------------------------------------------

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "west_bengal")


def _records(ac_code, part):
    from states.registry import STATE_CONNECTORS

    conn = STATE_CONNECTORS["west_bengal"]["connector_cls"]()
    ac = {a.ac_code: a for a in conn.list_constituencies()}[ac_code]
    with zipfile.ZipFile(os.path.join(RAW_DIR, ac_code + ".zip")) as zf:
        blob = zf.read("part%04d.pdf" % part)
    return conn._parse_part(blob, ac, 2002, part, "part%04d.pdf" % part)


def _names(ac_code, part):
    return [(r.serial_no, r.full_name or "") for r in _records(ac_code, part)]


def _pua_gid_range(ac_code, part):
    """(lowest, highest) glyph id still reaching the page as private-use.

    Part-level, not per-font, and the difference matters: pdfplumber reports
    fontname as "unknown" for every char in these PDFs, so a part that mixes a
    genuinely Bengali font with a misclassified Latin one would show the
    Bengali font's high gid here and hide the Latin one behind it. That mixing
    is real -- 1 of 38 fonts on AC148 part 22 carries the textless glyph. So
    this bounds the claim rather than proving it per row.
    """
    import io

    import pdfplumber

    with zipfile.ZipFile(os.path.join(RAW_DIR, ac_code + ".zip")) as zf:
        blob = zf.read("part%04d.pdf" % part)
    lo, hi = None, None
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for page in pdf.pages:
            for ch in page.chars:
                if len(ch["text"]) != 1 or not _is_pua(ch["text"]):
                    continue
                gid = ord(ch["text"]) - PUA_BASE
                lo = gid if lo is None else min(lo, gid)
                hi = gid if hi is None else max(hi, gid)
    return lo, hi


@pytest.mark.skipif(not os.path.exists(os.path.join(RAW_DIR, "AC143.zip")),
                    reason="raw data not downloaded")
def test_ac143_part_5_stops_blanking_the_names_it_could_always_read():
    names = _names("AC143", 5)
    assert names, "the part parses"
    blank = [s for s, n in names if not n.strip()]
    assert not blank, "360 of these 409 rows were blank before the fix"
    assert dict(names)[193] == "Mitali Mukherjee"


@pytest.mark.skipif(not os.path.exists(os.path.join(RAW_DIR, "AC143.zip")),
                    reason="raw data not downloaded")
def test_no_recovered_name_carries_a_non_printable_character():
    """The regression the rejected mappings would have caused, asserted.

    A widening of a decode classifier must never emit garbage where it
    previously blanked: a blank is honest failure, a confidently wrong name
    is the defect this whole change exists to fix.
    """
    for serial, name in _names("AC143", 5):
        bad = [ch for ch in name if not ch.isprintable() and ch != " "]
        assert not bad, "serial %s: %r" % (serial, name)
        assert "(cid:" not in name, "serial %s: %r" % (serial, name)


# Parts that still refuse after the fix, and must: their fonts genuinely reach
# past the Latin run, so the remark they carry is true of them.
STILL_REFUSED = [("AC146", 90), ("AC146", 92)]


@pytest.mark.skipif(not os.path.exists(os.path.join(RAW_DIR, "AC146.zip")),
                    reason="raw data not downloaded")
@pytest.mark.parametrize("ac_code,part", STILL_REFUSED)
def test_a_surviving_blank_sits_in_a_font_that_really_is_past_the_latin_run(
        ac_code, part):
    """The remark has to be true of exactly the rows still carrying it.

    "name in an unrecognized Bengali-script font" is a specific claim about
    the source, and before this change it was false on 162,642 rows whose
    text was ASCII. The fix is only complete if what remains blanked is
    blanked for the reason the remark gives -- otherwise the string would
    need a synchronised edit in the same release, and a claim that has to be
    kept true by hand goes stale the first time nobody remembers to.

    Asserted rather than observed. The difference between "we saw no
    exceptions" and "an exception fails the suite" is the whole point.
    """
    records = _records(ac_code, part)
    blank = [r for r in records if not (r.full_name or "").strip()]
    # Non-vacuous: if the fix ever recovered this part entirely, this test
    # would pass by having nothing to check, which is not the same as passing.
    assert blank, "%s part %d is the control -- it must still refuse" % (
        ac_code, part)

    _, highest = _pua_gid_range(ac_code, part)
    assert highest is not None, "a refused part still emits private-use glyphs"
    assert highest > MAC_LATIN_MAX, (
        "%s part %d blanks %d rows but no glyph reaches past the Latin run "
        "(highest gid %s) -- the classifier has widened again and the remark "
        "on those rows is false" % (ac_code, part, len(blank), highest))


@pytest.mark.skipif(not os.path.exists(os.path.join(RAW_DIR, "AC146.zip")),
                    reason="raw data not downloaded")
@pytest.mark.parametrize("ac_code,part", STILL_REFUSED + [("AC143", 5)])
def test_no_row_is_blanked_without_saying_why(ac_code, part):
    """A blank name always carries a remark naming its cause.

    An undeclared blank is the failure mode with no handle on it: nothing
    downstream can tell it from a genuinely nameless elector, and the row is
    unfindable either way. Cheap to assert here and it covers the recovered
    part too, where the answer is that there are no blanks left at all.
    """
    for r in _records(ac_code, part):
        if not (r.full_name or "").strip():
            assert (r.remark or "").strip(), (
                "%s part %d serial %s is blank and says nothing about why"
                % (ac_code, part, r.serial_no))


# -- recover-and-declare ---------------------------------------------------
#
# The twin of the rule above. Refusing a row and saying why is half of it;
# the other half is that a row we DO emit never carries a glyph we know we
# dropped without saying so. The mapping sends a textless gid to "", which is
# exact and is also, after the fact, indistinguishable from the character
# never having been there -- so "" is the one thing that cannot report on
# itself. pdfplumber keeps the char with empty text, which is where
# _consumed_textless_glyph reads the fact before it is gone.
#
# Detected, never enumerated. A list of the affected rows would be keyed by
# indices no reparse is obliged to reproduce -- the same hazard as an xfail
# list keyed by fixture position, in a worse place, because here the list
# would be the only thing standing between a citizen and a wrong EPIC.

DROPPED = "does not encode"


def _record_with(textless_cols, cells=None):
    """Drive _record directly with a chosen per-cell textless flag list."""
    from states.registry import STATE_CONNECTORS
    from states.west_bengal import (
        COL_SL, COL_NAME, COL_REL, COL_RELNAME, COL_SEX, COL_AGE, COL_EPIC,
        N_COLS,
    )

    conn = STATE_CONNECTORS["west_bengal"]["connector_cls"]()
    ac = conn.list_constituencies()[0]
    row = [""] * N_COLS
    row[COL_SL] = "12"
    row[COL_NAME] = "Mitali Mukherjee"
    row[COL_REL] = "Father"
    row[COL_RELNAME] = "Sailen Mukherjee"
    row[COL_SEX] = "F"
    row[COL_AGE] = "44"
    row[COL_EPIC] = "WB/21/143/0120"
    if cells:
        for col, val in cells.items():
            row[col] = val
    flags = [False] * N_COLS
    for col in textless_cols:
        flags[col] = True
    return conn._record(row, ac, 2002, 5, "part0005.pdf", False, flags)


def test_a_dropped_glyph_is_declared_and_names_its_field():
    from states.west_bengal import COL_EPIC

    rec = _record_with([COL_EPIC])
    assert DROPPED in rec.remark
    assert "EPIC no" in rec.remark
    # and only that field -- a remark that names every column is not a handle
    assert "name" not in rec.remark.split("dropped from:")[1]


def test_a_row_with_no_dropped_glyph_says_nothing():
    """The discriminator. An assertion that fires on every row is not one."""
    assert DROPPED not in (_record_with([]).remark or "")


def test_a_field_already_blanked_is_not_declared_twice():
    """A dropped glyph in a cell whose value we then refuse outright already
    has a remark saying the cell is unreadable. Two remarks for one cause is
    noise, and noise is how the real one stops being read."""
    from states.west_bengal import COL_EPIC, PUA_BASE

    rec = _record_with([COL_EPIC], {COL_EPIC: chr(PUA_BASE + 140)})
    assert "unreadable EPIC no" in rec.remark
    assert DROPPED not in rec.remark
    # Non-vacuous: the identical flags on a readable EPIC do declare, so this
    # is the blanking suppressing the remark and not the remark being off.
    assert DROPPED in _record_with([COL_EPIC]).remark


@pytest.mark.skipif(not os.path.exists(os.path.join(RAW_DIR, "AC151.zip")),
                    reason="raw data not downloaded")
def test_the_short_epic_declares_itself():
    """AC151 part 71 serial 11 is the sharpest real case in the census.

    Its EPIC recovers as WB/23/151/891 -- thirteen characters where every
    sibling row has sixteen -- because a glyph inside the number rendered
    nothing. Before the classification fix the whole row was blank, so this
    is not a good value turned bad; it is a row findable by nothing at all
    becoming findable by name while carrying a number that will not match.
    That is the case the remark exists for, and it is the one the house rule
    about not answering wrongly bears on most directly.
    """
    rec = [r for r in _records("AC151", 71) if r.serial_no == 11]
    assert rec, "AC151 part 71 serial 11 is the fixture; it must parse"
    rec = rec[0]
    assert rec.local_ref == "WB/23/151/891"
    assert DROPPED in rec.remark and "EPIC no" in rec.remark


@pytest.mark.skipif(not os.path.exists(os.path.join(RAW_DIR, "AC151.zip")),
                    reason="raw data not downloaded")
def test_the_declaration_is_rare_enough_to_mean_something():
    """1 of 875 rows on AC151 part 71, which is the real point of the census:
    the .notdef poisons a font's classification and then almost never appears
    in the rendered text. A remark on a large fraction of the part would mean
    the detector had stopped discriminating."""
    recs = _records("AC151", 71)
    flagged = [r for r in recs if DROPPED in (r.remark or "")]
    assert len(recs) > 500
    assert 0 < len(flagged) < len(recs) // 100
