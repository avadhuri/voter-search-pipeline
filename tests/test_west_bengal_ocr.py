"""Row reassembly from a Cloud Vision response for a scanned WB roll page.

The fixtures here are synthetic on purpose. What this module has to get right
is geometric -- which words share a row, which token in a row is the age --
and a synthetic page lets a test state the geometry it is exercising instead
of hoping a real scan happens to contain it. The real responses are 100 MB of
GCS output and aren't committed.
"""
import gzip
import json
import os

import pytest

from states.west_bengal_ocr import (
    BN_DIGITS,
    MAX_ELECTOR_AGE,
    MIN_ELECTOR_AGE,
    PAGES_MANIFEST,
    SERIAL_RUN_MIN,
    bengali_int,
    group_rows,
    ocr_gaps,
    page_failures,
    pages_covered,
    parse_page,
    parse_part,
    parse_row,
    window_paths,
)
from states.west_bengal_ocr import _upside_down


def _reads_inverted(response):
    """_upside_down()'s answer for a whole response, which is what the
    fixtures build. It takes the page node, the way _words() calls it."""
    return _upside_down(response["fullTextAnnotation"]["pages"][0])

PAGE_W, PAGE_H = 1000.0, 1400.0
ROW_H = 20.0


def _word(text, x, y):
    """One Vision word box, in the normalized form PDF input returns."""
    w = 8.0 * max(len(text), 1)
    verts = [(x, y), (x + w, y), (x + w, y + ROW_H), (x, y + ROW_H)]
    return {
        "symbols": [{"text": c} for c in text],
        "boundingBox": {
            "normalizedVertices": [
                {"x": vx / PAGE_W, "y": vy / PAGE_H} for vx, vy in verts
            ]
        },
    }


def _page(rows, column_order=True, skew=0.0):
    """A response whose words are emitted the way Vision actually emits them.

    Reading order on these pages runs down the columns, so by default the
    words are handed over column-major -- every row's first token, then every
    row's second token, and so on. A parser that trusted emission order would
    pass a row-major fixture and fail on real data.
    """
    placed = []
    for r, tokens in enumerate(rows):
        x = 40.0
        for c, token in enumerate(tokens):
            # `skew` drops each word by that fraction of its x -- the linear
            # baseline drift a scan on a tilted page actually has.
            placed.append((c, _word(token, x, 100.0 + r * 3 * ROW_H + skew * x)))
            x += 8.0 * len(token) + 12.0
    if column_order:
        placed.sort(key=lambda p: p[0])
    return {
        "fullTextAnnotation": {
            "pages": [{
                "width": PAGE_W,
                "height": PAGE_H,
                "blocks": [{"paragraphs": [{"words": [w for _, w in placed]}]}],
            }]
        }
    }


def test_bengali_int_accepts_only_whole_bengali_numerals():
    assert bengali_int("৪৬") == 46
    assert bengali_int("০") == 0
    assert bengali_int("46") is None      # Latin
    assert bengali_int("৪6") is None      # mixed
    assert bengali_int("") is None


def test_rows_survive_column_major_reading_order():
    rows = [
        ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBX1234567"],
        ["২", "সীতা", "মণ্ডল", "স্বামী", "রমেশ", "মণ্ডল", "স্ত্রী", "৩০", "WBX1234568"],
    ]
    parsed = parse_page(_page(rows, column_order=True))
    assert [r["full_name"] for r in parsed] == ["রমেশ মণ্ডল", "সীতা মণ্ডল"]
    assert [r["serial_no"] for r in parsed] == [1, 2]
    assert [r["age"] for r in parsed] == [35, 30]
    assert [r["gender"] for r in parsed] == ["M", "F"]
    assert [r["local_ref"] for r in parsed] == ["WBX1234567", "WBX1234568"]


def test_relation_words_map_to_the_connector_s_codes():
    codes = [
        parse_row(["১", "ক", "খ", rel, "গ", "ঘ", "পুং", "৩৫"])["relation_code"]
        for rel in ("পিতা", "স্বামী", "মাতা")
    ]
    assert codes == ["F", "H", "M"]


def test_a_line_with_no_relation_word_is_not_a_voter_row():
    # The cover page, column headings and the part footer all look like rows
    # geometrically. The relation word is what separates them from data.
    assert parse_row(["জেলা", ":", "বীরভূম"]) is None
    assert parse_row(["ক্রমিক", "নং", "নাম"]) is None


def test_a_line_with_two_relation_words_is_rejected_rather_than_guessed():
    # Two rows clustered into one. Splitting them would need to invent a
    # boundary; dropping one voter is better than emitting two wrong ones.
    assert parse_row(["১", "ক", "পিতা", "খ", "২", "গ", "স্বামী", "ঘ"]) is None


def test_the_roll_s_fourth_relation_code_is_read():
    # `অন্য` ("other") is the fourth code base.py has carried since
    # Karnataka. Not reading it refused 296 otherwise-perfect rows across the
    # corpus -- every column present, the elector simply related to their
    # guardian rather than to a parent or spouse.
    row = parse_row(["১", "ক", "খ", "অন্য", "গ", "ঘ", "পুং", "৩৫"])
    assert row["relation_code"] == "O"
    assert (row["full_name"], row["full_relative_name"]) == ("ক খ", "গ ঘ")


def test_the_fourth_code_never_outranks_a_kinship_term():
    """The discriminating half, and the reason `অন্য` is a fallback rather
    than a fourth entry in RELATION_WORDS.

    `অন্য` is an ordinary word meaning "other", so unlike পিতা/স্বামী/মাতা it
    turns up inside names. Promoted to a peer it would make this row carry
    two relation words and be refused outright -- 3 real rows in the corpus,
    against the 296 the fallback recovers. Here the row is related by
    marriage and the word is part of the elector's name; the split has to
    happen at স্বামী.
    """
    row = parse_row(["৬০", "অন্য", "মাল", "স্বামী", "জয়ধন", "মাল", "স্ত্রী", "২৬"])
    assert row["relation_code"] == "H"
    assert row["full_name"] == "অন্য মাল"
    assert row["full_relative_name"] == "জয়ধন মাল"


def test_a_relation_word_read_glued_to_the_next_name_is_split():
    # Vision reads the relation cell and the first word of the relative's
    # name as one token often enough to cost 389 electors across the corpus.
    # The row is otherwise whole, so the only thing standing between it and
    # the index is a missing space.
    row = parse_row(["১৬৭", "গুরুপদ", "মণ্ডল", "পিতানবেন", "মণ্ডল", "পুং", "৫৮"])
    assert row["relation_code"] == "F"
    assert row["full_name"] == "গুরুপদ মণ্ডল"
    assert row["full_relative_name"] == "নবেন মণ্ডল"
    assert "glued" in row["remark"] and "পিতানবেন" in row["remark"]


def test_a_glued_split_is_only_made_where_a_row_corroborates_it():
    """"Starts with these letters" is a weak signal, so it is never the only
    one. A heading that happens to begin with a relation word carries no EPIC
    and no sex word, and stays out -- which is what stops this from being a
    hole in the test that keeps the cover page from being parsed as voters.
    """
    assert parse_row(["পিতামাতার", "নাম"]) is None
    assert parse_row(["স্বামীর", "নামের", "তালিকা"]) is None

    # Discriminating: the same tokens are split the moment the group looks
    # like a row, so what refuses the two above is the corroboration rule and
    # not some other property of the words.
    assert parse_row(["পিতামাতার", "নাম", "পুং"])["full_relative_name"] == "মাতার নাম"


def test_the_roll_s_own_separators_are_stripped_but_a_name_is_not():
    # `পিতা-` and `পিতাঃ` are the printed separator surviving inside the
    # token; `পিতামহঃ` is not a separator at all -- `মহঃ` abbreviates
    # Mohammad and is the relative's first name. It is the same visarga in
    # the last two, which is why the strip only ever runs at the front of
    # the remainder.
    def relname(glued):
        return parse_row(["১", "ক", "খ", glued, "গ", "পুং", "৩৫"])["full_relative_name"]

    assert relname("পিতা-") == "গ"
    assert relname("পিতাঃ") == "গ"
    assert relname("পিতামহঃ") == "মহঃ গ"


def test_a_row_that_begins_at_the_relation_word_is_stored_not_dropped():
    """These used to be refused, which made 42 electors unreachable by any
    search. The elector name cell produced no tokens in this row's band --
    measured on 19 such rows with a readable name column, 18 have the ink
    standing there and chained onto a neighbouring row.

    What must not happen is the relative's name sliding into the elector's
    field: the relation word says whose name follows it, so filling the gap
    would publish a false statement about who this row is.
    """
    row = parse_row(["পিতা", "সোম", "টুডু", "৪৬"])
    assert row is not None
    assert row["full_name"] == ""
    assert row["full_relative_name"] == "সোম টুডু"
    assert row["relation_code"] == "F"
    assert row["age"] == 46
    assert "name not read: row begins at the relation word" in row["remark"]


def test_an_epic_with_no_elector_attached_is_still_refused():
    """The other half of storing damaged rows: a group that identifies
    nobody is not a row to keep.

    ~12,000 groups in the corpus look like this -- an age and an EPIC, the
    right-hand end of a row whose left half chained into a different group.
    Storing them would not recover an elector, it would invent one: a
    nameless row no search can reach, counted in every coverage figure the
    site publishes. The elector is already indexed from the left half.
    """
    assert parse_row(["৪১", "WB", "/", "42", "/", "287", "/", "003523"]) is None
    assert parse_row(["ex", "৬২", "MYB1200559"]) is None


def test_a_latin_age_is_dropped_and_said_so_rather_than_stored():
    # Arrival script is the whole test. A Latin-arriving digit is one Vision
    # read the ink of correctly and then filed under the wrong numeral
    # system -- 5.28% of Latin-arriving "8"s are really an 8 -- so the value
    # is a plausible number that is not this elector's age. Year-of-birth is
    # a *required* search field, so a decade-wrong age hides the elector from
    # the person looking for them; an absent one is spared by the query.
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "40"])
    assert row["age"] is None
    # The token itself goes into the remark, so what was thrown away stays
    # countable -- reading the rejected column's distribution is how the
    # 80s-versus-70s check was calibrated in the first place.
    assert "age not read: Latin digits '40'" in row["remark"]
    # Located either way, or it lands in a field the site searches.
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_a_bengali_age_outranks_a_stray_latin_fragment_beside_it():
    # Observed on the first part OCR'd: the column rule sheds a "2" to the
    # right of the age. Taking the rightmost number lost the real age.
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "৪৬", "2"])
    assert row["age"] == 46
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_a_trailing_fragment_after_the_age_stays_out_of_the_relative_s_name():
    row = parse_row(["১", "রমেশ", "মণ্ডল", "স্বামী", "হরি", "মণ্ডল", "স্ত্রী", "২২", "N"])
    assert row["age"] == 22
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_a_missing_serial_does_not_eat_the_first_word_of_the_name():
    # Position 0 is only the serial if it looks like one. Trusting position
    # cost the name's first word, which is the field the site searches.
    row = parse_row(["পরেশ", "কিসকু", "পিতা", "কেশর", "কিসকু", "৪৬"])
    assert row["serial_no"] is None
    assert row["full_name"] == "পরেশ কিসকু"
    assert "serial no not read" in row["remark"]


def test_a_missing_epic_leaves_the_row_searchable():
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫"])
    assert row["local_ref"] == ""
    assert row["full_name"] == "রমেশ মণ্ডল"
    assert row["age"] == 35
    # Stated without a cause on purpose: a quarter of this roll's electors
    # genuinely have no EPIC, and a Vision response cannot tell an empty
    # cell from one it failed to read.
    assert "EPIC no not read" in row["remark"]


def test_a_split_epic_is_rejoined():
    row = parse_row(
        ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫",
         "WB", "/", "42", "/", "287", "/", "000221"]
    )
    assert row["local_ref"] == "WB/42/287/000221"


def test_a_missing_gender_is_left_empty_not_inferred_from_the_relation():
    # স্বামী almost always means a woman, but "almost" is not a fact to
    # record about a named person.
    row = parse_row(["১", "সীতা", "মণ্ডল", "স্বামী", "রমেশ", "মণ্ডল", "৩০"])
    assert row["gender"] == ""
    assert row["age"] == 30
    assert "sex not read" in row["remark"]


def test_group_rows_keeps_words_left_to_right_within_a_row():
    words = [
        {"text": "খ", "x0": 200, "x1": 220, "y0": 10, "y1": 30, "cy": 20},
        {"text": "ক", "x0": 40, "x1": 60, "y0": 12, "y1": 32, "cy": 22},
    ]
    assert [w["text"] for w in group_rows(words)[0]] == ["ক", "খ"]


def test_an_empty_or_textless_response_yields_no_rows():
    assert parse_page({}) == []
    assert parse_page({"fullTextAnnotation": {"pages": []}}) == []


def test_every_unread_cell_names_itself_in_the_remark():
    # One row missing all four, so the remark has to carry four separate
    # causes rather than a single "row was damaged".
    row = parse_row(["রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল"])
    for cause in ("serial no not read", "sex not read",
                  "age not read", "EPIC no not read"):
        assert cause in row["remark"], cause


def test_a_row_that_read_cleanly_carries_no_remark():
    row = parse_row(
        ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫",
         "WBA1234567"]
    )
    assert row["remark"] == ""


def test_a_bengali_age_in_range_is_stored():
    """The policy this module carried until its evidence was rebuilt.

    `d8bbc0c` stored no age at all, on a confusion matrix over 73 digit pairs
    that put ৩->3 at 54% and a serial-monotonicity proxy that found Bengali-
    read tokens 92.5% increasing against Latin-read at 89.7% -- close enough
    to conclude the errors were visual and script was no evidence. The same
    matrix rebuilt over 79,844 digit comparisons puts ৩->3 at 93.4%, and,
    conditioned on arrival script, Bengali-arriving digits at 99.27-99.60%
    against 5.28% for a Latin-arriving "8". Every dangerous substitution is
    cross-script: true ৪ read as 8 happens 976 times in Latin and 0 times in
    Bengali. See the module docstring.
    """
    row = parse_row(
        ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"]
    )
    assert row["age"] == 35
    assert row["remark"] == ""
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_the_bounds_are_inclusive_at_both_ends():
    for token, value in (("১৮", MIN_ELECTOR_AGE), ("১২০", MAX_ELECTOR_AGE)):
        row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", token])
        assert row["age"] == value, token


def test_a_bengali_age_below_the_floor_is_refused_and_names_the_bound():
    """The floor is not decoration -- it catches the one substitution that
    survives within Bengali. ৯ reads as ১ at the tens position 30% of the
    time, so a true 9X arrives as 1X, and 10..17 falls outside the window:
    74% of observed decade errors are rejected here rather than stored."""
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "১৪"])
    assert row["age"] is None
    assert "age not read: 14 outside 18..120" in row["remark"]
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_a_token_above_the_ceiling_is_refused():
    # The ceiling does something different from the floor: it drops tokens
    # that are not ages at all -- a column-rule fragment, a part number --
    # 839 of 50,279 sampled rows.
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৪৫"])
    assert row["age"] is None
    assert "age not read: 345 outside 18..120" in row["remark"]


def test_an_age_never_found_reads_differently_from_one_found_and_refused():
    # Two different failures and both are worth counting. Nothing was
    # trimmed in the first, so the leftovers may be sitting in a searched
    # field; the second located its token and only rejected the value.
    missing = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং"])
    refused = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "40"])
    assert "age not read" in missing["remark"]
    assert "age not read: " not in missing["remark"]
    assert "age not read: " in refused["remark"]


SKEWED_ROW = ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"]


def test_a_skewed_row_stays_one_row():
    """The regression that made a column look dead.

    These pages are scans and they are tilted, so a row's baseline drifts as
    it crosses the page -- on AC294 far enough that the sex, age and EPIC
    cells sit a full row-height below the name. Grouping every word against
    one y for the whole row filed that half as a separate row, which then
    parsed as a non-row and was dropped, taking the elector's age and EPIC
    with it. It read as a dead column (both recovered on 3.5% of rows) and
    was a lost half-row: measured over five pages, chaining each row from the
    word it currently ends at took AC294's age from 37.9% to 73.6% and its
    EPIC from 39.5% to 78.1%, and AC291's sex from 73.4% to 86.7%.
    """
    total_drift = 0.11 * (40.0 + sum(8.0 * len(t) + 12.0 for t in SKEWED_ROW))
    assert total_drift > ROW_H, "fixture must actually drift a full row"

    rows = parse_page(_page([SKEWED_ROW], skew=0.11))
    assert len(rows) == 1
    row = rows[0]
    assert row["serial_no"] == 1
    assert row["full_name"] == "রমেশ মণ্ডল"
    assert row["gender"] == "M"
    assert row["local_ref"] == "WBA1234567"
    assert row["remark"] == ""


def test_an_unskewed_page_is_unaffected():
    """The fix must not be a trade: AC287, the least skewed of the three
    parts, is why nothing looked wrong for weeks."""
    rows = parse_page(_page([SKEWED_ROW, SKEWED_ROW]))
    assert len(rows) == 2
    assert all(r["local_ref"] == "WBA1234567" and r["remark"] == "" for r in rows)


def test_a_word_never_joins_a_row_that_already_covers_it():
    """Two rows whose baselines are close enough to attract each other still
    stay apart, because a row only ever extends rightwards. Without that
    guard a chained grouper merges a whole column into one row."""
    page = _page([["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং"],
                  ["২", "সীতা", "মণ্ডল", "স্বামী", "রমেশ", "মণ্ডল", "স্ত্রী"]])
    rows = parse_page(page)
    assert [r["serial_no"] for r in rows] == [1, 2]
    assert [r["full_name"] for r in rows] == ["রমেশ মণ্ডল", "সীতা মণ্ডল"]


def _inverted(response):
    """The same page as if the sheet had been fed in the wrong way up.

    Both halves of the rotation matter and a fixture that does only one of
    them tests nothing: the word *positions* reflect through the page centre,
    and each word's vertex list starts from a different corner, because Vision
    emits vertices from a word's top-left in reading order and on an inverted
    page that corner is the bottom-right of the page frame. The winding is the
    only signal the reader has -- the text itself comes back perfectly
    readable either way, which is exactly why this went unnoticed.
    """
    page = response["fullTextAnnotation"]["pages"][0]
    for block in page["blocks"]:
        for para in block["paragraphs"]:
            for word in para["words"]:
                verts = word["boundingBox"]["normalizedVertices"]
                word["boundingBox"]["normalizedVertices"] = [
                    {"x": 1.0 - v["x"], "y": 1.0 - v["y"]} for v in verts]
    return response


UPRIGHT_ROWS = [["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"],
                ["২", "সীতা", "মণ্ডল", "স্বামী", "রমেশ", "মণ্ডল", "স্ত্রী", "৩১", "WBA1234568"]]


def test_a_page_scanned_upside_down_parses_to_the_same_rows():
    """The whole point: an inverted page is a rotation, not damage. Vision
    reads its text correctly, so the failure is silent -- names come back at
    100% while the serial, the relative's name and the sex all come back
    wrong, because those are decided by position."""
    upright = parse_page(_page(UPRIGHT_ROWS))
    inverted = parse_page(_inverted(_page(UPRIGHT_ROWS)))
    assert upright == inverted
    assert [r["serial_no"] for r in inverted] == [1, 2]
    assert [r["full_relative_name"] for r in inverted] == ["হরি মণ্ডল", "রমেশ মণ্ডল"]
    assert [r["gender"] for r in inverted] == ["M", "F"]


def test_assamese_ra_is_folded_back_to_the_bengali_one():
    """Vision hinted `bn` still reaches for Assamese ra inside a conjunct.
    It is the same letter at a different code point, and ৰ does not occur in
    Bengali orthography, so this is unconditionally safe on this corpus --
    and it has to happen, because a Bengali query for the real spelling
    matches none of the rows that carry the wrong one."""
    rows = [["\u09e7", "\u09ae\u09b9\u09bf\u09a8\u09cd\u09a6\u09f0", "\u09aa\u09bf\u09a4\u09be",
             "\u09ac\u09bf\u09a8\u09cd\u09a6\u09f0", "\u09aa\u09c1\u0982", "\u09e9\u09eb", "WBA1234567"]]
    out = parse_page(_page(rows))
    assert len(out) == 1
    assert "\u09f0" not in out[0]["full_name"]
    assert "\u09f0" not in out[0]["full_relative_name"]
    assert out[0]["full_name"] == "\u09ae\u09b9\u09bf\u09a8\u09cd\u09a6\u09b0"
    assert out[0]["full_relative_name"] == "\u09ac\u09bf\u09a8\u09cd\u09a6\u09b0"


def test_assamese_wa_is_left_alone():
    """The other un-Bengali code point Vision emits is *not* folded. ৱ is a
    different letter rather than a variant form, and the real occurrences
    disagree about what was meant -- বেসৱা beside বেসবা argues ব, হজৱেতুন
    argues র, হোৱাই argues য়. Nine rows in a 48,482-row sample is not worth a
    guess that is wrong a third of the time, so they stay as read and stay
    visible."""
    rows = [["\u09e7", "\u09b9\u09cb\u09f1\u09be\u0987", "\u09aa\u09bf\u09a4\u09be",
             "\u0997\u09be\u099c\u09b2", "\u09aa\u09c1\u0982", "\u09e9\u09eb", "WBA1234567"]]
    out = parse_page(_page(rows))
    assert out[0]["full_name"] == "\u09b9\u09cb\u09f1\u09be\u0987"


def test_an_upright_page_is_not_flipped():
    """The guard against the correction firing on the 95% of pages that were
    always fine -- on those it has to be exactly a no-op."""
    page = _page(UPRIGHT_ROWS)
    assert not _reads_inverted(page)
    assert parse_page(page) == parse_page(_page(UPRIGHT_ROWS))


def test_orientation_is_read_from_the_geometry_not_the_text():
    """The signal is the winding, and it has to be read off the page rather
    than guessed from what the row says -- the text of an inverted page is
    identical to an upright one's, which is the whole reason this was
    invisible until the columns were counted."""
    assert not _reads_inverted(_page(UPRIGHT_ROWS))
    assert _reads_inverted(_inverted(_page(UPRIGHT_ROWS)))


# --------------------------------------------------------------------------
# the response tree on disk
# --------------------------------------------------------------------------

ROW = ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"]


def _write_window(part_dir, first, last, rows_per_page, gzipped=True):
    """One response file covering pages `first`..`last`, named the way
    scripts/ocr_vision.py names them."""
    part_dir.mkdir(parents=True, exist_ok=True)
    payload = {"responses": [_page(rows_per_page)
                             for _ in range(first, last + 1)]}
    name = f"p{first:04d}-{last:04d}.json" + (".gz" if gzipped else "")
    path = part_dir / name
    opener = gzip.open if gzipped else open
    with opener(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def _ac_tree(tmp_path, counts):
    """An AC's OCR directory with a pages.json manifest, nothing OCR'd yet."""
    ac_dir = tmp_path / "AC287"
    ac_dir.mkdir(parents=True, exist_ok=True)
    (ac_dir / PAGES_MANIFEST).write_text(json.dumps(counts), encoding="utf-8")
    return ac_dir


def test_both_response_suffixes_are_read_and_come_back_in_page_order(tmp_path):
    # The first parts OCR'd landed before the responses were gzipped, so a
    # part can hold either suffix -- and the second window must not sort
    # ahead of the first just because ".gz" is longer than ".json".
    part = tmp_path / "part0001"
    _write_window(part, 6, 10, [ROW], gzipped=False)
    _write_window(part, 1, 5, [ROW], gzipped=True)
    assert [os.path.basename(p) for p in window_paths(part)] == [
        "p0001-0005.json.gz", "p0006-0010.json"
    ]
    assert pages_covered(part) == set(range(1, 11))


def test_a_file_that_is_not_a_response_window_is_not_counted(tmp_path):
    # pages.json lives in the AC directory, but a stray note or a partial
    # download landing in a part directory must not read as coverage.
    part = tmp_path / "part0001"
    _write_window(part, 1, 5, [ROW])
    (part / "notes.txt").write_text("x", encoding="utf-8")
    (part / "p0006-0010.json.tmp").write_text("{}", encoding="utf-8")
    assert pages_covered(part) == set(range(1, 6))


def test_an_ac_is_gapless_only_when_every_page_of_every_part_is_present(tmp_path):
    ac_dir = _ac_tree(tmp_path, {"part0001": 7, "part0002": 5})
    _write_window(ac_dir / "part0001", 1, 5, [ROW])
    _write_window(ac_dir / "part0002", 1, 5, [ROW])
    # part0001 is two pages short: the run is still going, or was killed.
    gaps = ocr_gaps(ac_dir, ["part0001", "part0002"])
    assert len(gaps) == 1
    assert "part0001" in gaps[0] and "2 of 7" in gaps[0]

    _write_window(ac_dir / "part0001", 6, 7, [ROW])
    assert ocr_gaps(ac_dir, ["part0001", "part0002"]) == []


def test_a_part_the_manifest_never_listed_is_a_gap_too(tmp_path):
    # The expected part list comes from the raw zip's own members, so a
    # manifest that is itself short cannot make an AC look complete.
    ac_dir = _ac_tree(tmp_path, {"part0001": 5})
    _write_window(ac_dir / "part0001", 1, 5, [ROW])
    gaps = ocr_gaps(ac_dir, ["part0001", "part0002"])
    assert len(gaps) == 1 and "part0002" in gaps[0]


def test_a_missing_manifest_is_a_gap_rather_than_a_pass(tmp_path):
    ac_dir = tmp_path / "AC287"
    _write_window(ac_dir / "part0001", 1, 5, [ROW])
    assert ocr_gaps(ac_dir, ["part0001"]) == [f"no {PAGES_MANIFEST} in {ac_dir}"]


def _bn(n):
    """`n` in Bengali numerals, the way the serial column is actually printed."""
    return "".join(BN_DIGITS[int(c)] for c in str(n))


def _numbered_rows(first, count):
    """`count` consecutive rows of a part, numbered from `first`."""
    return [[_bn(n), "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫"]
            for n in range(first, first + count)]


def _write_pages(part_dir, first, pages):
    """One response file whose pages differ from each other -- `_write_window`
    repeats a single page, which cannot express a numbering."""
    part_dir.mkdir(parents=True, exist_ok=True)
    payload = {"responses": [_page(rows) for rows in pages]}
    path = part_dir / f"p{first:04d}-{first + len(pages) - 1:04d}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def test_a_serial_past_the_third_digit_is_trimmed_off_the_name():
    """The half of this that matters most, and the half a page can settle on
    its own. A part of this roll can run past a thousand electors, and while
    the serial column was capped at three digits the fourth-digit rows did
    not merely lose a serial -- the token stayed where an unconsumed leading
    token goes, at the front of the field the whole site searches."""
    row = parse_row(["১০০০", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫"])
    assert row["full_name"] == "রমেশ মণ্ডল"
    # Located, but not yet vouched for: one row cannot confirm a numbering.
    assert row["serial_no"] is None
    assert row["serial_wide"] == 1000


def test_a_part_numbered_past_a_thousand_keeps_every_serial(tmp_path):
    part = tmp_path / "part0001"
    _write_pages(part, 1, [_numbered_rows(995, 5), _numbered_rows(1000, 5),
                           _numbered_rows(1005, 5)])
    rows = parse_part(part)
    assert [r["serial_no"] for r in rows] == list(range(995, 1010))
    # parse_part is what the connector calls, so the unresolved field must
    # not survive it.
    assert all("serial_wide" not in r for r in rows)
    assert all(r["remark"] == "EPIC no not read" for r in rows)


def test_a_lone_year_shaped_token_is_refused_but_still_trimmed(tmp_path):
    """What the run test is for. 1965 in the serial cell of one row of a
    part numbered in the low hundreds is not that part's numbering, and no
    number of neighbours agree with it. The name is cleaned either way --
    refusing the value costs the serial and nothing else."""
    part = tmp_path / "part0001"
    stray = [_bn(1965), "পরেশ", "কিসকু", "পিতা", "কেশর", "কিসকু", "পুং", "৪৬"]
    _write_pages(part, 1, [_numbered_rows(1, 5), [stray] + _numbered_rows(6, 4)])
    rows = parse_part(part)
    assert rows[5]["full_name"] == "পরেশ কিসকু"
    assert rows[5]["serial_no"] is None
    assert "serial no not read: 1965 unconfirmed" in rows[5]["remark"]
    assert [r["serial_no"] for r in rows if r["serial_no"]] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_a_run_shorter_than_the_threshold_is_not_a_numbering(tmp_path):
    """Two tokens agreeing is arithmetic, not evidence: AC291's part 135
    holds a 1936 and a 1955 nineteen rows apart, in a part far too small to
    hold either as a serial. SERIAL_RUN_MIN is what separates that from a
    column."""
    part = tmp_path / "part0001"
    short = _numbered_rows(998, 2 + SERIAL_RUN_MIN - 1)   # only 1000.. counts
    _write_pages(part, 1, [short])
    rows = parse_part(part)
    confirmed = [r["serial_no"] for r in rows if r["serial_no"] and r["serial_no"] > 999]
    assert len(confirmed) == 0
    assert [r["serial_no"] for r in rows[:2]] == [998, 999]


def test_parse_page_leaves_a_wide_serial_for_the_part_to_settle():
    """The seam is deliberate and the docstring says so: a page holds about
    46 rows of a numbering that runs 1..N across the part, so the page is
    not the scope that can confirm one."""
    page = parse_page(_page([[_bn(1000), "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল",
                              "পুং", "৩৫"]]))
    assert page[0]["serial_no"] is None
    assert page[0]["serial_wide"] == 1000


def test_parse_part_returns_every_page_of_every_window(tmp_path):
    part = tmp_path / "part0001"
    _write_window(part, 1, 5, [ROW, ROW])
    _write_window(part, 6, 6, [ROW])
    rows = parse_part(part)
    assert len(rows) == 11              # 5 pages x 2 rows, then 1 page x 1
    assert {r["full_name"] for r in rows} == {"রমেশ মণ্ডল"}


def test_an_unread_name_names_itself_so_it_can_be_counted(tmp_path):
    # A row with no name is one no query can reach. Every other column can be
    # recovered from the source document later; this one has to be countable.
    row = parse_row(["১", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"])
    assert row["full_name"] == ""
    assert "name not read" in row["remark"]

    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "পুং", "৩৫", "WBA1234567"])
    assert row["full_relative_name"] == ""
    assert "relative's name not read" in row["remark"]


def _write_error_window(part_dir, first, last, message):
    """A window whose pages Vision answered for but could not read -- the
    shape a per-page failure actually lands in, an `error` object where the
    `fullTextAnnotation` would be."""
    part_dir.mkdir(parents=True, exist_ok=True)
    payload = {"responses": [{"error": {"code": 3, "message": message}}
                             for _ in range(first, last + 1)]}
    path = part_dir / f"p{first:04d}-{last:04d}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def test_a_page_vision_could_not_read_is_named_with_its_reason(tmp_path):
    part = tmp_path / "part0001"
    _write_window(part, 1, 5, [ROW])
    _write_error_window(part, 6, 10, "Bad image data.")
    assert page_failures(part) == [(p, "Bad image data.") for p in range(6, 11)]


def test_a_page_read_and_found_blank_is_not_a_failure(tmp_path):
    # Every part of these ACs ends on a blank sheet and most carry a blank
    # verso behind the cover. Vision answers for them with neither an error
    # nor a fullTextAnnotation, and counting those as damage would report
    # two unreadable pages in every part of every AC.
    part = tmp_path / "part0001"
    _write_window(part, 1, 5, [ROW])
    with gzip.open(part / "p0006-0010.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"responses": [{} for _ in range(5)]}, fh)
    assert page_failures(part) == []
    assert pages_covered(part) == set(range(1, 11))


def test_an_unreadable_page_still_counts_as_covered(tmp_path):
    # ocr_gaps() asks whether the *run* finished, and it did: Vision was
    # asked about these pages and answered. Re-running would not change the
    # answer, so this must not read as an unfinished run -- the two are
    # deliberately separate signals with separate remedies.
    ac_dir = _ac_tree(tmp_path, {"part0001": 5})
    _write_error_window(ac_dir / "part0001", 1, 5, "Bad image data.")
    assert ocr_gaps(ac_dir, ["part0001"]) == []
    assert len(page_failures(ac_dir / "part0001")) == 5
    assert parse_part(ac_dir / "part0001") == []
