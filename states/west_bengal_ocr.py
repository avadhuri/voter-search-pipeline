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

**An age is stored only when its token arrived entirely in Bengali
numerals and lands in 18..120.** That rule replaces a blanket "store no age",
which this module carried from `d8bbc0c` until the evidence under it was
rebuilt and did not survive.

What `d8bbc0c` concluded, and why it was wrong: it read a confusion matrix
built over **73 digit pairs** and found ৩ coming back as 3 only 54% of the
time and ৪ as 4 only 39%, then reasoned from a monotonicity proxy -- serials
read in Bengali run in increasing order down a page 92.5% of the time
against 89.7% for serials read in Latin -- that the errors were *visual*
rather than script-dependent, so that Bengali-read ages were no safer than
Latin-read ones and all of them had to go.

The same matrix rebuilt over **38,896 serial tokens / 79,844 digit
comparisons** says the opposite, and misses the old figures by 30-40 points:
৩->3 is right 93.4% of the time, not 54%, and ৪->4 86.8%, not 39%.

**Arrival script is the load-bearing variable, and it is the one thing the
old reading ruled out.** Conditioned on it:

    Bengali-arriving digits   99.27-99.60% correct, at every value
    Latin-arriving "8"         5.28% correct
    Latin-arriving "9"        43.90%
    Latin-arriving "6"        54.48%
    Latin-arriving "0"        83.09%

and every dangerous substitution is cross-script, not within-script:

    true ৪ read as 8    976 Latin,   0 Bengali
    true ৩ read as 6    403 Latin,   2 Bengali
    true ৭ read as 9    333 Latin,   3 Bengali
    true ৫ read as 0    180 Latin,   0 Bengali

Which is not a coincidence: ৪ and 8 are near-identical shapes, so Vision is
reading the ink correctly and choosing the wrong numeral *system* for it.
A token that came back in Bengali is one where that choice went the right
way, for every digit in it.

*Hypothesis* for why the monotonicity proxy could not see this (the
refutation above does not rest on it, and it is untested): monotonicity
compares orderings, not values. A substitution that preserves order --
every ৪ on a page becoming 8 -- leaves the page perfectly monotone while
every value on it is wrong.

**The check that does not depend on the instrument under dispute.** A
confusion matrix is itself a thing this module built, and a wrong one is
what produced `d8bbc0c`. So: read every age unfiltered and bin it by decade.
The result puts **more electors in their 80s (5.89%) than in their 70s
(1.85%)**, which cannot happen on a real roll -- past middle age a roll's
age histogram falls, and mortality is why. Restricting to Bengali-arriving
tokens takes 80-89 from 5.89% to 0.31%, in line with the digitally-typeset
ACs of the same roll. 53.23% of Latin-containing age tokens land in 80-89,
the same finding from the other side. `scripts/check_servable.py` now
enforces that falling tail on every state built, so this stays a check
rather than a measurement someone once ran.

**Vision's own confidence does not separate the two populations** -- a
negative result, recorded so nobody spends a day on it. Accuracy is 92.53%
at 100% coverage and 93.18% over the most-confident 22%: flat, and not even
monotone. The physical reason is the same one as above. The engine is
confident because it read the pixels correctly; its error is about which
numeral system those pixels belong to, and nothing in a per-symbol
confidence score is measuring that.

**What the surviving error looks like.** Over two-digit all-Bengali tokens
(an age's shape), n=2,408: 93.85% exactly right, 4.94% wrong in the tens
position. Per tens digit, tens 1/2/3/4/6/7/8 recorded **0 errors in 1,700
samples between them** -- which is not 100%; with no errors observed the
95% upper bound is roughly 3/n, and it is quoted that way below rather than
as a perfect score. Tens 5 is 99.08% and tens 9 is 69.55%.

Weighting each tens digit by how often a real elector's age starts with it
gives a **0.14% decade-error rate on stored ages**, with a pooled 95% upper
bound near 0.29% (about 1.6% if the tens digits are not pooled at all).
Tens 9 is bad but nearly weightless: 90-99 is 0.11% of this roll.

The 18 floor does most of the remaining work, for a reason worth stating.
The one substitution that survives within Bengali is ৯->১, so a true 9X
reads as 1X -- and 10..17 is outside the bound, so **74% of observed decade
errors are rejected rather than stored**. Only true 98/99 -> 18/19 gets
through. The ceiling is doing something different: it drops tokens that are
not ages at all (a column-rule fragment, a part number), 839 of 50,279
sampled rows.

An age that is wrong but plausible is worse than no age at all here -- the
search form's year-of-birth field is *required*, so a decade-sized error
moves an elector out of reach of the person looking for them, while
`age IS NULL` is already spared by the query (CLAUDE.md, "An unusable age
means 'unknown', never 'not your match'"). That is why the rule is
conservative in one direction only: everything it is not sure of becomes
NULL. Coverage is **78.79%** of rows overall and varies by AC -- 87.37%
AC287, 72.20% AC291, 77.17% AC294 -- against 0% before. Those are counted
by replaying every cached Vision response for all 583 parts through this
module and asking how many of the 407,386 rows come back with a non-NULL
age; the method is written down because the figure it replaces (78.54%,
87.55/71.05/76.85) is one whose own method can no longer be reconstructed,
which is the only reason it went unnoticed that the corpus had moved
underneath it.

A token that could not be *found* still gets an "age not read" remark,
because then nothing was trimmed and the relative's name may carry the
leftovers. A token that was found and then rejected gets a remark naming
which rule rejected it, so the two are countable apart.

**18 is also voter_search_engine's `MIN_ELECTOR_AGE`** (`scripts/app.py`),
which its year-of-birth filter uses to decide that an age is unusable and
the row must be spared rather than hidden. Two repos, one number, and
nothing mechanical keeping them in step across the open-source split.
Changing either one means looking for the other. The precedent is the
2002-roll birth-year incident: the bug was never the constant, it was that
nothing tied the constant to the data it described.
"""
from __future__ import annotations

import gzip
import json
import os
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

# The window an age token has to land in to be stored. Below the floor the
# elector could not have been enrolled; above the ceiling the token is not an
# age at all (a column-rule fragment, a part number). Both bounds are only
# reached for tokens that already arrived in Bengali numerals -- see the
# module docstring for why arrival script is the load-bearing test and these
# two are the backstop. MIN_ELECTOR_AGE is also a constant in
# voter_search_engine (`scripts/app.py`), where the year-of-birth filter uses
# it to spare a row rather than hide it; the two are not shared across the
# open-source split and nothing keeps them in step, so changing one means
# going to look for the other.
MIN_ELECTOR_AGE = 18
MAX_ELECTOR_AGE = 120

# Fraction of a word's own height that its baseline may sit away from the
# word the row currently ends at. Swept against three real parts: 0.6 leaves
# a skewed row's right-hand cells behind, and past ~1.2 rows start swallowing
# their neighbours (the recovered row count falls). 1.0 is the knee.
ROW_TOLERANCE = 1.0


def _upside_down(page: dict) -> bool:
    """Whether this page was fed into the scanner the wrong way up.

    Vision lists a word's four vertices starting from its top-left *in
    reading order*, so on a page it read at 180 degrees the first vertex is
    the one furthest down and right in the page's own frame. That is the
    rotation stated by the geometry rather than inferred from the text, and
    it is unambiguous, and the margin was measured rather than assumed --
    over all 10,763 pages of AC287/291/294 carrying 50 or more words, the
    most upright-looking inverted page still wound 'up' on 0.3% of its
    words and the most inverted-looking upright page on 1.0% of its. The
    two classes are about 98 points apart, so the bare `down > up` below is
    nowhere near a coin flip on scan skew. The minority of words that are
    neither (a skewed word whose corners do not land on the extremes) are
    simply not counted.

    Only 180 degrees is looked for, because only 180 degrees occurs: the
    same scan found no page fed in sideways, the largest sideways-wound
    fraction anywhere being 11% of one page's words against the ~100% a
    real rotation would produce. If a sideways sheet ever does turn up it
    will not be silently mishandled -- vertex 0 lands on neither extreme
    corner, so its words are uncounted here and the page is left alone.

    Vision itself is unaffected -- it rotates each word and reads it
    correctly, which is why the *names* on these pages have always been at
    100%. What breaks is everything positional: group_rows() walks left to
    right, so an inverted page comes back with every row's columns in
    reverse, and parse_row() then reads the EPIC as the serial, swallows the
    relation word's left side as the name, and finds no gender at all.
    Whole pages of AC294 read that way -- name 100%, relative's name 4%.
    """
    up = down = 0
    for block in page.get("blocks", []):
        for para in block.get("paragraphs", []):
            for word in para.get("words", []):
                verts = word.get("boundingBox", {}).get("normalizedVertices") or []
                if len(verts) != 4:
                    continue
                xs = [v.get("x", 0) for v in verts]
                ys = [v.get("y", 0) for v in verts]
                x0, y0 = verts[0].get("x", 0), verts[0].get("y", 0)
                if x0 == min(xs) and y0 == min(ys):
                    up += 1
                elif x0 == max(xs) and y0 == max(ys):
                    down += 1
    return down > up


# Vision is asked for Bengali (`languageHints: ["bn"]`) and mostly answers in
# it, but inside a conjunct it reaches for the Assamese form of ra --
# কীৰ্ত্তনীয়া where the roll prints কীর্ত্তনীয়া. Same letter, two code points;
# ৰ (U+09F0) is simply not part of Bengali orthography, so an occurrence is a
# substitution and needs no ground truth to call wrong. Measured on this
# corpus: 1,172 occurrences across a 48,482-row sample, 2.19% of rows, and
# the give-away is that the same surname arrives spelled both ways on one
# page (মুৰ্ম্ম beside মুর্মু). Left alone it is invisible damage of the worst
# kind -- the name looks right to a reader and a Bengali query for the real
# spelling matches none of those rows.
#
# ৱ (U+09F1) is NOT folded, though it is equally un-Bengali. It is a
# different letter rather than a variant form, and the nine occurrences in
# the same sample disagree about what was meant: বেসৱা beside বেসবা on one
# row argues ব, হজৱেতুন argues র, হোৱাই argues য়. Nine rows in 48,482 is not
# worth a guess that is wrong a third of the time, so they are left as read.
ASSAMESE_RA = "\u09f0"
BENGALI_RA = "\u09b0"


def _fold_script_neighbours(text: str) -> str:
    """The Assamese ra Vision emits mid-conjunct, back to the Bengali one."""
    return text.replace(ASSAMESE_RA, BENGALI_RA)


def _words(response: dict) -> list[dict]:
    """Every word on the page, with absolute pixel boxes, right way up.

    Vision returns normalized (0-1) vertices for PDF input and absolute ones
    for images; only the normalized form appears here, but both are handled
    so a rasterized page can be fed through the same code.
    """
    fa = response.get("fullTextAnnotation")
    if not fa or not fa.get("pages"):
        return []
    page = fa["pages"][0]
    flip = _upside_down(page)
    w, h = page.get("width", 1), page.get("height", 1)
    out = []
    for block in page.get("blocks", []):
        for para in block.get("paragraphs", []):
            for word in para.get("words", []):
                text = _fold_script_neighbours(
                    "".join(s.get("text", "") for s in word.get("symbols", []))
                )
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
    if flip and out:
        # Rotate the page, not the words: each box keeps its size and its
        # text, and only its position is reflected through the page centre.
        # Measured from the recorded extent rather than the page's declared
        # width/height, which for an image response is the raster and for a
        # PDF one is the point box -- reflecting through either would shift
        # every row by the margin.
        right = max(w["x1"] for w in out)
        bottom = max(w["y1"] for w in out)
        left = min(w["x0"] for w in out)
        top = min(w["y0"] for w in out)
        for w in out:
            w["x0"], w["x1"] = left + right - w["x1"], left + right - w["x0"]
            w["y0"], w["y1"] = top + bottom - w["y1"], top + bottom - w["y0"]
            w["cy"] = (w["y0"] + w["y1"]) / 2
    return out


def group_rows(words: list[dict]) -> list[list[dict]]:
    """Words clustered into visual rows, each left-to-right.

    Rows are built by walking left to right and extending each one from the
    word it currently ends at, rather than by clustering every word against a
    single y for the whole row. These pages are *scans* and they are skewed:
    a row's baseline drifts steadily as it crosses the page, so on AC294 the
    age and EPIC columns sit far enough below the name column that a
    fixed-y test files them as a separate row -- which then parses as a
    non-row and is dropped, taking that elector's age and EPIC with it. It
    read as a dead column and was in fact a lost half-row. Chaining from the
    last word absorbs the drift, because between two horizontally adjacent
    words it is small even when it is large across the whole page.

    Measured over five pages per part, against the same cached Vision
    responses (percent of rows on which the cell was recovered, sex/age/EPIC):

        AC287   96.1 / 97.4 / 82.5  ->  96.1 / 96.5 / 81.6
        AC291   73.4 / 64.2 / 61.3  ->  86.7 / 87.8 / 79.4
        AC294   52.0 / 37.9 / 39.5  ->  55.1 / 73.6 / 78.1

    AC287 is the least skewed of the three, which is why nothing looked
    wrong until AC294 was OCR'd; it is flat within noise either way. Its
    sex column is *not* evidence about grouping at all -- the 96 vs the 67
    an earlier pass reported there was the Vision endpoint (files:annotate
    rasterizes these PDFs materially better than asyncBatchAnnotate does on
    identical bytes), a separate finding that had to be held fixed before
    any of the above could be attributed to the grouper.
    """
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["x0"], w["cy"])):
        height = max(word["y1"] - word["y0"], 1e-6)
        best: list[dict] | None = None
        best_gap = None
        for row in rows:
            # Only ever extend a row rightwards: a word that starts back
            # inside what a row already covers belongs to a different one,
            # however close its baseline sits.
            if row[-1]["x1"] > word["x1"]:
                continue
            gap = abs(row[-1]["cy"] - word["cy"])
            if gap < ROW_TOLERANCE * height and (best_gap is None or gap < best_gap):
                best, best_gap = row, gap
        if best is None:
            rows.append([word])
        else:
            best.append(word)
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    # Ordered by where each row *starts*, which on a skewed page is the only
    # y that hasn't drifted yet.
    return sorted(rows, key=lambda r: r[0]["cy"])


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
    bengali = at is not None
    if at is None:
        at = next((i for i in range(len(tail) - 1, -1, -1)
                   if AGE_SHAPE.match(tail[i])), None)
    if at is None:
        # Nothing was trimmed, so the leftovers may be sitting in the
        # relative's name. That is a different failure from finding a token
        # and refusing it, and the two are worth counting apart.
        remarks.append("age not read")
    else:
        token = tail[at]
        # The token is trimmed off whether or not its value survives -- it
        # has to be *located* either way, or it lands in the relative's name,
        # which is a searched field.
        tail = tail[:at]
        value = bengali_int(token) if bengali else None
        if not bengali:
            # A Latin-arriving age token is the population the confusion
            # matrix in the module docstring finds unusable: Vision read the
            # ink correctly and picked the wrong numeral system for it, so
            # the value is a plausible number that is not this elector's age.
            remarks.append("age not read: Latin digits %r" % (token,))
        elif not MIN_ELECTOR_AGE <= value <= MAX_ELECTOR_AGE:
            remarks.append(
                "age not read: %d outside %d..%d"
                % (value, MIN_ELECTOR_AGE, MAX_ELECTOR_AGE))
        else:
            age = value

    if not epic:
        # Left deliberately without a cause: column 8 is genuinely blank for
        # a quarter of the electors in this roll (measured against the
        # digitally-typeset ACs), and nothing in a Vision response
        # distinguishes an empty cell from one it failed to read.
        remarks.append("EPIC no not read")

    # The two searched fields say so when they came back empty. Every other
    # column can be reconstructed from the source document later; a row with
    # no name is one no query can ever reach, and the remark is the only
    # thing that makes those countable instead of merely absent.
    full_name = " ".join(tokens[name_from:r])
    full_relative_name = " ".join(tail)
    if not full_name:
        remarks.append("name not read")
    if not full_relative_name:
        remarks.append("relative's name not read")

    return {
        "serial_no": serial,
        "full_name": full_name,
        "relation_code": RELATION_WORDS[tokens[r]],
        "full_relative_name": full_relative_name,
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


# --------------------------------------------------------------------------
# the response tree on disk
# --------------------------------------------------------------------------
#
# scripts/ocr_vision.py lands one directory per AC beside that state's raw
# zips, one sub-directory per part, and one file per five-page request
# window:
#
#     data/raw/west_bengal/ocr/AC287/pages.json
#     data/raw/west_bengal/ocr/AC287/part0001/p0001-0005.json.gz
#     data/raw/west_bengal/ocr/AC287/part0001/p0006-0010.json.gz
#
# The part directory names are the zip's own PDF stems, so `part0001` here is
# `part0001.pdf` in AC287.zip, which the downloader named from the CEO site's
# part index -- that is what makes part_no recoverable from a directory name
# and, through it, source_url resolvable per part.

OCR_SUBDIR = "ocr"
PAGES_MANIFEST = "pages.json"

# Both suffixes: the first parts OCR'd landed before the responses were
# gzipped, and re-OCRing them to change a filename would be paying twice for
# bytes already on disk.
WINDOW_NAME = re.compile(r"^p(\d{4})-(\d{4})\.json(?:\.gz)?$")


def window_paths(part_dir) -> list[str]:
    """Every response window for one part, in page order."""
    if not os.path.isdir(part_dir):
        return []
    return sorted(
        (os.path.join(part_dir, n) for n in os.listdir(part_dir)
         if WINDOW_NAME.match(n)),
        key=os.path.basename,
    )


def load_window(path) -> dict:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def pages_covered(part_dir) -> set:
    """The page numbers this part has a response for.

    Read off the window filenames rather than by opening them: a completeness
    check that costs a directory listing can run over 583 parts on every
    build, and the name is written by the same code that writes the file.
    """
    covered = set()
    for path in window_paths(part_dir):
        m = WINDOW_NAME.match(os.path.basename(path))
        covered.update(range(int(m.group(1)), int(m.group(2)) + 1))
    return covered


def load_page_counts(ac_dir):
    """{part stem: page count} for one AC, or None if the manifest is absent.

    scripts/ocr_vision.py counts every part's pages up front and writes this
    file in one go, so it is complete or missing -- never half-written. That
    is what makes it usable as the denominator below.
    """
    path = os.path.join(ac_dir, PAGES_MANIFEST)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def ocr_gaps(ac_dir, part_stems) -> list[str]:
    """Every expected part whose OCR output is missing or short, described.

    Empty means this AC is completely OCR'd and safe to build. The caller
    (states/west_bengal.py) refuses to build an AC with any gap rather than
    publishing the parts it happens to have, because a half-OCR'd AC is
    invisible downstream: every row in it parses, scores and ranks correctly,
    the build log is clean, and the only symptom is electors who are simply
    not there. That is the same failure class as the catalog-shrink guard in
    build_db.py, and it wants the same answer -- stop, and name what is
    missing.

    `part_stems` comes from the raw zip's own members, not from the manifest,
    so a manifest that is itself short is caught too.
    """
    counts = load_page_counts(ac_dir)
    if counts is None:
        return [f"no {PAGES_MANIFEST} in {ac_dir}"]

    gaps = []
    for stem in part_stems:
        total = counts.get(stem)
        if total is None:
            gaps.append(f"{stem}: not listed in {PAGES_MANIFEST}")
            continue
        missing = sorted(set(range(1, total + 1)) - pages_covered(os.path.join(ac_dir, stem)))
        if missing:
            gaps.append(
                f"{stem}: {len(missing)} of {total} page(s) have no OCR response "
                f"(first missing: p{missing[0]})"
            )
    return gaps


# Vision answers per page, so one unreadable page inside an otherwise fine
# window comes back as a response object carrying `error` instead of
# `fullTextAnnotation`. Two other shapes look similar and are not this:
# a page it read and found no text on (a blank verso, or a part's blank
# last sheet -- checked by eye on AC291 part0005 pages 2 and 16, which are
# bare paper with bleed-through from the reverse), and a page with no
# response file at all, which is ocr_gaps()' business above.
def page_failures(part_dir) -> list:
    """[(page number, Vision's message)] for every page it could not read."""
    failures = []
    for path in window_paths(part_dir):
        first = int(WINDOW_NAME.match(os.path.basename(path)).group(1))
        for offset, response in enumerate(load_window(path).get("responses", [])):
            error = response.get("error")
            if error:
                failures.append((first + offset, error.get("message", "unknown")))
    return failures


def parse_part(part_dir) -> list[dict]:
    """Every voter row in one part, in page order."""
    rows = []
    for path in window_paths(part_dir):
        rows.extend(parse_response_file(load_window(path)))
    return rows
