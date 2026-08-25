"""Turn a Cloud Vision response for a scanned roll page back into table rows.

`states/west_bengal.py` decodes the rolls that ship with a text layer. Three
ACs -- AC287, AC291, AC294 -- ship as page scans instead, and go through
`scripts/ocr_vision.py` to get a text layer at all. This module is the other
half of that: it reassembles Vision's output into the eight-column roll table.

The flat `fullTextAnnotation.text` is useless for this. Vision emits reading
order, and on these pages reading order runs down the columns, so the flat
text interleaves one voter's name with another's age. The geometry is what
carries the table: every word has a box, rows are words that share a
horizontal band, and columns are the order within that band. So this works
off `normalizedVertices` and ignores `text` entirely.

Rows are split on the relation word rather than on x-position. Column
x-positions drift between parts (these are scans -- the page is not always
square on the platen), but every data row contains exactly one of
পিতা/স্বামী/মাতা, and it sits between the two name fields, which are the two
fields the app actually searches. Splitting on it costs nothing when a scan
is skewed.

**A digit that came back in the wrong script is dropped, not converted.**
Vision transcribes some Bengali numerals as the Latin numerals they resemble,
and most of those are harmless because the glyphs genuinely correspond
(৬->6, ৯->9, ০->0). ৪ is not: it looks like an 8, so ৪০ comes back as "80"
and a 40-year-old becomes 80. Measured at 14% of rows on the first part
OCR'd. An age that is wrong but plausible is worse than no age at all here --
the search form's year-of-birth field is required, so a doubled age makes
that elector unfindable by the person looking for them, while `age IS NULL`
is already spared by the query (see CLAUDE.md, "An unusable age means
'unknown', never 'not your match'"). Hence: Bengali digits are trusted,
Latin digits in the age column become None plus a remark.
"""
from __future__ import annotations

import re

BN_DIGITS = "০১২৩৪৫৬৭৮৯"
_BN_TO_INT = {c: str(i) for i, c in enumerate(BN_DIGITS)}

RELATION_WORDS = {
    "পিতা": "F",    # father
    "স্বামী": "H",   # husband
    "মাতা": "M",    # mother
}
GENDER_WORDS = {
    "পুং": "M",     # male
    "স্ত্রী": "F",    # female
}

# WB/42/287/000221 arrives as seven tokens; MYB1201623 as one.
EPIC_SPLIT = re.compile(r"^[A-Z]{2,3}$|^\d{2,6}$|^/$")
EPIC_WHOLE = re.compile(r"^[A-Z]{3}\d{7}$")

# One to three digits in either script -- the age column.
AGE_SHAPE = re.compile(r"^(?:[০-৯]{1,3}|\d{1,3})$")

# How much of a word's own height two words may differ by and still count as
# the same row. Generous because a scan's baseline wanders.
ROW_TOLERANCE = 0.6


def _words(response: dict) -> list[dict]:
    """Every word on the page, with absolute pixel boxes.

    Vision returns normalized (0-1) vertices for PDF input and absolute ones
    for images; only the normalized form appears here, but both are handled
    so a rasterized page can be fed through the same code.
    """
    fa = response.get("fullTextAnnotation")
    if not fa or not fa.get("pages"):
        return []
    page = fa["pages"][0]
    w, h = page.get("width", 1), page.get("height", 1)
    out = []
    for block in page.get("blocks", []):
        for para in block.get("paragraphs", []):
            for word in para.get("words", []):
                text = "".join(s.get("text", "") for s in word.get("symbols", []))
                if not text:
                    continue
                box = word.get("boundingBox", {})
                verts = box.get("normalizedVertices")
                scale_x, scale_y = (w, h) if verts else (1, 1)
                verts = verts or box.get("vertices", [])
                if not verts:
                    continue
                xs = [v.get("x", 0) * scale_x for v in verts]
                ys = [v.get("y", 0) * scale_y for v in verts]
                out.append({
                    "text": text,
                    "x0": min(xs), "x1": max(xs),
                    "y0": min(ys), "y1": max(ys),
                    "cy": (min(ys) + max(ys)) / 2,
                })
    return out


def group_rows(words: list[dict]) -> list[list[dict]]:
    """Words clustered into visual rows, each left-to-right."""
    rows: list[dict] = []
    for word in sorted(words, key=lambda w: w["cy"]):
        height = max(word["y1"] - word["y0"], 1e-6)
        for row in reversed(rows):
            if abs(row["cy"] - word["cy"]) < ROW_TOLERANCE * height:
                row["words"].append(word)
                row["cy"] = sum(w["cy"] for w in row["words"]) / len(row["words"])
                break
        else:
            rows.append({"cy": word["cy"], "words": [word]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x0"])
    return [r["words"] for r in sorted(rows, key=lambda r: r["cy"])]


def bengali_int(token: str) -> int | None:
    """A Bengali-numeral token as an int, or None if it isn't purely one."""
    if not token or any(c not in _BN_TO_INT for c in token):
        return None
    return int("".join(_BN_TO_INT[c] for c in token))


def _epic(tokens: list[str]) -> tuple[str, int]:
    """The EPIC number and the index it starts at, or ("", len(tokens))."""
    for i, t in enumerate(tokens):
        if EPIC_WHOLE.match(t):
            return t, i
        if t == "WB" and i + 1 < len(tokens):
            return "".join(tokens[i:]), i
    return "", len(tokens)


def parse_row(tokens: list[str]) -> dict | None:
    """One data row, or None if this line isn't one.

    A data row is defined by carrying exactly one relation word. Headers,
    the page title, the column-number strip and the footer carry none, which
    is what keeps them out without hardcoding how many header lines a part
    happens to have.
    """
    rel_at = [i for i, t in enumerate(tokens) if t in RELATION_WORDS]
    if len(rel_at) != 1:
        return None
    r = rel_at[0]
    if r == 0:
        return None

    remarks: list[str] = []

    # Only consume a leading token as the serial if it actually looks like
    # one. A scan sometimes drops the serial entirely, and taking position 0
    # on faith then ate the first word of the name -- which is the field the
    # whole site searches on.
    name_from = 0
    serial = None
    while name_from < r and AGE_SHAPE.match(tokens[name_from]):
        if serial is None:
            serial = bengali_int(tokens[name_from])
            if serial is None:
                serial = int(tokens[name_from])
                remarks.append(f"serial no read as Latin digits {tokens[name_from]!r}")
        name_from += 1
    if serial is None:
        remarks.append("serial no not read")

    epic, epic_at = _epic(tokens)
    tail = tokens[r + 1:epic_at]

    gender = ""
    for i, t in enumerate(tail):
        if t in GENDER_WORDS:
            gender = GENDER_WORDS[t]
            tail = tail[:i] + tail[i + 1:]
            break
    else:
        # Printed on every row of this roll, so an absent one is a read
        # failure, not a blank cell -- unlike the EPIC below.
        remarks.append("sex not read")

    # The age is the rightmost number in the tail, not necessarily its last
    # token: a scan often picks up a fragment of the next column past it
    # ("... স্ত্রী ২২ N"). Anchoring on the last token instead put the
    # age inside the relative's name, which is a searched field.
    # Bengali first, and only then Latin: the stray fragments a scan picks up
    # off the column rule are Latin far more often than Bengali, so "rightmost
    # number" alone let a stray "2" outrank the real "৪৬" sitting beside it.
    age = None
    at = next((i for i in range(len(tail) - 1, -1, -1)
               if bengali_int(tail[i]) is not None), None)
    if at is None:
        at = next((i for i in range(len(tail) - 1, -1, -1)
                   if AGE_SHAPE.match(tail[i])), None)
    if at is not None:
        age = bengali_int(tail[at])
        if age is None:
            # See the module docstring: a Latin numeral here may be a
            # correctly-shaped 6/9/0 or a ৪ misread as 8, and nothing in the
            # response distinguishes them. Dropped rather than guessed.
            remarks.append(
                f"age read as Latin digits {tail[at]!r}, not stored -- "
                "\u09e9/\u09ea transcribe as 6/8, so the value may be wrong "
                "by decades"
            )
        tail = tail[:at]
    else:
        remarks.append("age not read")

    if not epic:
        # Left deliberately without a cause: column 8 is genuinely blank for
        # a quarter of the electors in this roll (measured against the
        # digitally-typeset ACs), and nothing in a Vision response
        # distinguishes an empty cell from one it failed to read.
        remarks.append("EPIC no not read")

    return {
        "serial_no": serial,
        "full_name": " ".join(tokens[name_from:r]),
        "relation_code": RELATION_WORDS[tokens[r]],
        "full_relative_name": " ".join(tail),
        "gender": gender,
        "age": age,
        "local_ref": epic,
        "remark": "; ".join(remarks),
    }


def parse_page(response: dict) -> list[dict]:
    """Every voter row on one OCR'd page."""
    rows = []
    for words in group_rows(_words(response)):
        row = parse_row([w["text"] for w in words])
        if row is not None:
            rows.append(row)
    return rows


def parse_response_file(payload: dict) -> list[dict]:
    """Every voter row in one Vision output shard (up to `batchSize` pages)."""
    out = []
    for response in payload.get("responses", []):
        out.extend(parse_page(response))
    return out
