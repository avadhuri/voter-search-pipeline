"""Row reassembly from a Cloud Vision response for a scanned WB roll page.

The fixtures here are synthetic on purpose. What this module has to get right
is geometric -- which words share a row, which token in a row is the age --
and a synthetic page lets a test state the geometry it is exercising instead
of hoping a real scan happens to contain it. The real responses are 100 MB of
GCS output and aren't committed.
"""
import pytest

from states.west_bengal_ocr import (
    bengali_int,
    group_rows,
    parse_page,
    parse_row,
)

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
    assert [r["age"] for r in parsed] == [None, None]
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


def test_a_latin_age_is_dropped_and_said_so_rather_than_stored():
    # Vision transcribes ৪ as 8, so a Latin age may be off by decades. The
    # year-of-birth filter is required, so a wrong age hides the elector from
    # the person searching for them; an absent one does not.
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "40"])
    assert row["age"] is None
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_a_bengali_age_outranks_a_stray_latin_fragment_beside_it():
    # Observed on the first part OCR'd: the column rule sheds a "2" to the
    # right of the age. Taking the rightmost number lost the real age.
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "৪৬", "2"])
    assert row["age"] is None
    assert row["full_relative_name"] == "হরি মণ্ডল"


def test_a_trailing_fragment_after_the_age_stays_out_of_the_relative_s_name():
    row = parse_row(["১", "রমেশ", "মণ্ডল", "স্বামী", "হরি", "মণ্ডল", "স্ত্রী", "২২", "N"])
    assert row["age"] is None
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
    assert row["age"] is None
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
    assert row["age"] is None
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


def test_no_age_is_stored_from_a_scanned_roll_however_clean_it_reads():
    # Not squeamishness about a hard column: the digit errors measured on
    # this roll are visual (৩ read as 6, ৪ as 8), so they sit in the
    # Bengali-read ages exactly as much as in the Latin-read ones that make
    # them visible -- a serial-monotonicity check put Bengali-read tokens at
    # 92.5% increasing and Latin-read at 89.7%, close enough that script is
    # no evidence of correctness. Year-of-birth is a *required* search field,
    # so a decade-wrong age is an elector nobody can find, while an absent
    # one is spared by the query. Unknown, never "not your match".
    row = parse_row(
        ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫", "WBA1234567"]
    )
    assert row["age"] is None
    # A blank age is uniform here, so it earns no remark of its own -- only
    # an age token that could not be *found* does, since nothing got trimmed
    # and the relative's name may carry the leftovers.
    assert row["remark"] == ""
    # The token is still located, or it would land in the relative's name.
    assert row["full_relative_name"] == "হরি মণ্ডল"


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
