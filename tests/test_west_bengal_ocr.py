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


def _page(rows, column_order=True):
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
            placed.append((c, _word(token, x, 100.0 + r * 3 * ROW_H)))
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


def test_a_latin_age_is_dropped_and_said_so_rather_than_stored():
    # Vision transcribes ৪ as 8, so a Latin age may be off by decades. The
    # year-of-birth filter is required, so a wrong age hides the elector from
    # the person searching for them; an absent one does not.
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "40"])
    assert row["age"] is None
    assert "Latin digits" in row["remark"]
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


def test_a_missing_epic_leaves_the_row_searchable():
    row = parse_row(["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫"])
    assert row["local_ref"] == ""
    assert row["full_name"] == "রমেশ মণ্ডল"


def test_a_split_epic_is_rejoined():
    row = parse_row(
        ["১", "রমেশ", "মণ্ডল", "পিতা", "হরি", "মণ্ডল", "পুং", "৩৫",
         "WB", "/", "42", "/", "287", "/", "000221"]
    )
    assert row["local_ref"] == "WB/42/287/000221"
    assert row["age"] == 35


def test_a_missing_gender_is_left_empty_not_inferred_from_the_relation():
    # স্বামী almost always means a woman, but "almost" is not a fact to
    # record about a named person.
    row = parse_row(["১", "সীতা", "মণ্ডল", "স্বামী", "রমেশ", "মণ্ডল", "৩০"])
    assert row["gender"] == ""
    assert row["age"] == 30


def test_group_rows_keeps_words_left_to_right_within_a_row():
    words = [
        {"text": "খ", "x0": 200, "x1": 220, "y0": 10, "y1": 30, "cy": 20},
        {"text": "ক", "x0": 40, "x1": 60, "y0": 12, "y1": 32, "cy": 22},
    ]
    assert [w["text"] for w in group_rows(words)[0]] == ["ক", "খ"]


def test_an_empty_or_textless_response_yields_no_rows():
    assert parse_page({}) == []
    assert parse_page({"fullTextAnnotation": {"pages": []}}) == []
