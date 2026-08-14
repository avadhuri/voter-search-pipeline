"""
West Bengal connector: ceowestbengal.wb.gov.in's 2002 roll.

Data source
-----------
The CEO site publishes the digitized 2002 roll as one PDF per *part* (polling
booth), reachable at

    /RollPDF/GetDraft?acId={ac_id}&key={base64(filename)}    e.g. AC001PART001.pdf

The site's UI puts an image CAPTCHA in front of that link, but the CAPTCHA is
never checked server-side -- the endpoint returns the PDF to an anonymous
request with no cookie or session. Nothing here bypasses an access control;
these are public electoral rolls served by their own publisher.

The district -> AC -> part tree is scraped once into
states/meta/west_bengal_ac_meta.json (committed, mirroring Karnataka's
states/meta/ac_meta.json) rather than re-fetched on every run: it is 316 HTML
pages for a tree that has not changed since 2002, and committing it keeps a
build reproducible and lets fetch_raw() work offline-ish. An AC there also
carries "name_source": "ocr" once its name columns have actually been read
and spot-checked through the OCR path below; the field being absent means
nobody has run that AC yet, not that it is unsupported.

Raw bundle shape
----------------
states/base.py's interface is one raw blob per AC, but West Bengal has no
per-AC file -- an AC is 60-450 separate part PDFs. fetch_raw() therefore
downloads every part of the AC and returns them bundled as a single in-memory
ZIP, stored by the downloader as data/raw/west_bengal/{AC_CODE}.zip. Members
are named part{part_no:04d}.pdf so parse_raw() iterates them in part order.

Text extraction: the font problem
---------------------------------
These PDFs carry a real embedded text layer (not scans), but *no* font in them
has a ToUnicode CMap. Every font is a subset whose /Encoding /Differences maps
character codes to glyph names of the form /GXX, where XX is the hex TrueType
glyph id (gid) in the font the subset was cut from. So the text is recoverable
only if the gid -> character mapping of that font is known.

For the Latin fonts the mapping is the standard Macintosh glyph ordering, in
which gid 3 = space, gid 19..28 = '0'..'9', gid 36..61 = 'A'..'Z',
gid 68..93 = 'a'..'z' -- i.e. chr(gid + 29) over gid 3..97. That was confirmed
against rendered pages: e.g. the header font of AC146 part 1 encodes codes
1..7 as /G33 /G44 /G55 /G57 /G03 /G31 /G52 = "Part No".

The Bengali fonts reuse that same low gid range for Bengali glyphs, so the
+29 rule must be applied per font, never globally: a font is treated as Latin
only when every gid it uses lies in 3..97. Everything else decodes to
U+E000+gid, which keeps an undecoded glyph distinguishable from a real
character instead of silently becoming plausible-looking wrong text.

Which leaves the real limitation, see parse_raw(): only the ~19 Kolkata ACs
are typeset in English. The other ~275 are typeset in Bengali, and a Bengali
glyph id cannot be turned into Unicode without a gid->Unicode table for that
font (which nothing in the PDF provides). The numeric and closed-vocabulary
columns of those ACs are still recovered by reverse-engineering the finite
part of the glyph table; see BN_DIGIT_GID / BN_RELATION / BN_GENDER below.

For the open-vocabulary name columns there is no finite set to match against,
so they cannot be decoded this way at all. Those are instead read by OCR off
the rendered glyphs -- see states/west_bengal_ocr.py, which goes around the
broken mapping rather than trying to complete it. That is opt-in (WB_OCR=1,
or WestBengalConnector(ocr=True)) because it needs an OCR engine this package
does not otherwise require, and its default engine (Google Cloud Vision) bills
per page; with OCR off the names stay empty with a remark, as before, and are
never guessed at.

Note for the search side: OCR yields real Bengali-script names, which is a
strictly different problem from making them *findable*. Nothing here bridges
a Latin-script query to Bengali script -- scripts/transliteration.py is
Devanagari/ITRANS-only -- so these names are extracted and stored but are not
yet matchable from the existing Latin-query search UI.
"""
import io
import json
import os
import re
import zipfile
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor

import requests

from states import west_bengal_ocr
from states.base import Constituency, StateConnector, VoterRecord


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

_HERE = os.path.dirname(os.path.abspath(__file__))
AC_META_PATH = os.path.join(_HERE, "meta", "west_bengal_ac_meta.json")

PDF_URL = "https://ceowestbengal.wb.gov.in/RollPDF/GetDraft?acId={ac_id}&key={key}"
PART_MEMBER = "part{part_no:04d}.pdf"

RELATION_NORMALIZE = {
    "": "", "-": "",
    "FATHER": "F", "F": "F",
    "HUSBAND": "H", "H": "H",
    "MOTHER": "M", "M": "M",
    "OTHER": "O", "OTHERS": "O", "O": "O",
}

GENDER_NORMALIZE = {
    "": "", "-": "",
    "M": "M", "MALE": "M",
    "F": "F", "FEMALE": "F",
}


# --------------------------------------------------------------------------
# font decoding
# --------------------------------------------------------------------------

_GID_NAME = re.compile(r"^G([0-9A-Fa-f]{2,4})$")
PUA_BASE = 0xE000        # undecoded glyph gid N -> chr(PUA_BASE + N)
MAC_LATIN_MAX = 97       # last gid of the Latin run in Macintosh glyph order


def _patch_pdfminer_gid_encoding():
    """Teach pdfminer to read this site's /GXX glyph-id encodings.

    Applied once at import. It is strictly additive: it only intervenes when a
    font's whole Differences array is /GXX names, which is not a convention any
    normal PDF uses and which pdfminer otherwise decodes to garbage (its
    fallback pulls the digits out of the glyph name and chr()s them). Any other
    font falls through to pdfminer's own logic untouched, so importing this
    module cannot change how unrelated PDFs are parsed.
    """
    import pdfminer.encodingdb as edb

    if getattr(edb.EncodingDB, "_wb_gid_patched", False):
        return
    original = edb.EncodingDB.get_encoding

    @classmethod
    def get_encoding(cls, name, diff=None):
        pairs = _gid_pairs(diff) if diff else None
        if pairs:
            # Latin subsets stay inside the Macintosh Latin run; a Bengali
            # subset always reaches past it, and its low gids are Bengali
            # glyphs rather than ASCII -- so the rule is decided per font.
            latin = all(3 <= gid <= MAC_LATIN_MAX for _, gid in pairs)
            return {
                code: (chr(gid + 29) if latin else chr(PUA_BASE + gid))
                for code, gid in pairs
            }
        return original(name, diff)

    edb.EncodingDB.get_encoding = get_encoding
    edb.EncodingDB._wb_gid_patched = True


def _gid_pairs(diff):
    """[(char_code, gid)] if every name in a Differences array is /GXX, else None."""
    pairs, code = [], 0
    for item in diff:
        if isinstance(item, (int, float)):
            code = int(item)
            continue
        name = item.name if hasattr(item, "name") else str(item)
        m = _GID_NAME.match(name)
        if not m:
            return None
        pairs.append((code, int(m.group(1), 16)))
        code += 1
    return pairs or None


_patch_pdfminer_gid_encoding()

import pdfplumber  # noqa: E402  (import order matters: patch first)


def _is_undecoded(ch):
    return len(ch) == 1 and PUA_BASE <= ord(ch) < PUA_BASE + 0x1000


def _gid_of(ch):
    return ord(ch) - PUA_BASE


# Bengali glyph ids that *are* safely mappable, because they are closed sets
# whose members were read off rendered pages and cross-checked across ACs in
# different districts (the subset fonts are cut from the same base font, so a
# given gid is the same glyph in every part PDF of every AC).
#
# The Bengali digits sit at the Macintosh digit slots (gid 19..28 = 0..9), the
# same positions their ASCII counterparts occupy -- verified by decoding the
# serial-number and age columns of AC001 part 1 and comparing every one of the
# 45 rows on page 2 against the rendered page.
BN_DIGIT_GID = {19 + d: str(d) for d in range(10)}

# The Bengali fonts keep the standard-ordering space at gid 3 too, and draw it
# as a real glyph (serial numbers are right-aligned by padding with it), so it
# has to be decoded or the numeric columns come out with junk in front.
BN_ASCII_GID = {3: " ", **BN_DIGIT_GID}

# Relationship / sex columns are drawn from a fixed handful of words, so the
# glyph *sequence* of a whole cell can be matched even though the individual
# glyphs are not known. Keys are '-'-joined hex gids of the cell's glyphs.
#
# Only sequences with converging evidence are listed. For each one below the
# glyph decomposition is internally consistent (gid 7C is the same leading
# glyph in both "পিতা" and "পুং"; gid B2 leads both "স্বামী" and "স্ত্রী";
# "পিতা" and "মাতা" share the trailing 57-82-C1), the sequence is byte
# identical in AC001 / AC050 / AC232 (three districts far apart), and the
# relation-vs-sex cross-tabulation reproduces the English-language control
# roll's shape almost exactly -- Father/Male dominant, Husband/Female second,
# a small Father/Female tail, Mother rare:
#
#     Bengali (AC001+AC050+AC232, 2368 rows)   English (AC146, 711 rows)
#     C2-7C-57-82-C1 / 7C-C6-D2   1159         Father  / M   474
#     B2-8D-C1-94-C4 / B2-5F-C4   1058         Husband / F   187
#     C2-7C-57-82-C1 / B2-5F-C4     61         Father  / F    28
#     94-C1-57-82-C1 / either       25         Mother  / -     0
#
# Anything not listed keeps its raw private-use text and earns a remark.
BN_RELATION = {
    "C2-7C-57-82-C1": "F",   # পিতা  father
    "B2-8D-C1-94-C4": "H",   # স্বামী husband
    "94-C1-57-82-C1": "M",   # মাতা  mother
}
BN_GENDER = {
    "7C-C6-D2": "M",         # পুং   male
    "B2-5F-C4": "F",         # স্ত্রী female
}


def _bn_key(text):
    return "-".join(f"{_gid_of(c):02X}" for c in text if _is_undecoded(c))


# --------------------------------------------------------------------------
# table geometry
# --------------------------------------------------------------------------
#
# Every roll page repeats the same 8-column table:
#
#   Sl.No. | House No | Name of Elector | Relationship | Name Of Relation |
#   Sex | Age | EPIC No
#
# and, directly under the header, a row of the literal column numbers
# "1 2 3 4 5 6 7 8". That numbers row is the one piece of the layout that is
# ASCII in the Bengali pages too (they use the Latin digit glyphs there), so it
# makes a reliable, language-independent anchor for locating the columns --
# more robust than hard-coded x offsets, since column positions drift between
# ACs and even between parts.
#
# The numbers are only centred under their *headings*, though, while the data
# is left-aligned and much wider -- the midpoint between two anchors regularly
# falls inside a long name. So the anchors are used only to bracket the search:
# the actual boundary is the middle of the widest vertical whitespace gutter
# between one anchor and the next, measured over that page's own data rows.

N_COLS = 8
COL_SL, COL_HOUSE, COL_NAME, COL_REL, COL_RELNAME, COL_SEX, COL_AGE, COL_EPIC = range(8)

ROW_TOL = 3.0      # pt; serial numbers sit on a slightly different baseline
RUN_GAP = 2.0      # pt; wider than this between glyphs means a word break
CONT_LINES = 1.6   # a wrapped tail is one line down; the page footer is not


def _rows_of(chars, tol=ROW_TOL):
    """Cluster chars into visual rows by baseline."""
    out = []
    for ch in sorted(chars, key=lambda c: c["top"]):
        if out and ch["top"] - out[-1][0] <= tol:
            out[-1][1].append(ch)
        else:
            out.append((ch["top"], [ch]))
    return [_dedupe(sorted(g, key=lambda c: c["x0"])) for _, g in out]


def _dedupe(row, slop=0.5):
    """Drop fake-bold double strikes -- emboldened text is drawn twice a
    quarter of a point apart. Left in, it turns the column-number row into
    "11 22 33 ..." (defeating the anchor match) and doubles glyphs inside the
    closed-vocabulary cells. The duplicate is not always the adjacent char:
    zero-width combining marks make the two strikes interleave once sorted by
    x, so compare against every kept glyph still within slop."""
    out = []
    for ch in row:
        i = len(out) - 1
        while i >= 0 and ch["x0"] - out[i]["x0"] < slop:
            prev = out[i]
            if (
                ch["text"] == prev["text"]
                and abs(ch["x0"] - prev["x0"]) < slop
                and abs(ch["top"] - prev["top"]) < slop
            ):
                break
            i -= 1
        else:
            out.append(ch)
    return out


def _runs(chars, gap=RUN_GAP):
    """Split an x-sorted char list into whitespace-separated runs."""
    out, cur = [], [chars[0]]
    for ch in chars[1:]:
        if ch["x0"] - cur[-1]["x1"] > gap:
            out.append(cur)
            cur = [ch]
        else:
            cur.append(ch)
    out.append(cur)
    return out


def _cell_text(chars):
    """Join an x-sorted char list, inserting a space at each whitespace gap.
    Bengali digits are resolved here (see BN_DIGIT_GID); every other Bengali
    glyph stays a private-use code point for the caller to notice."""
    parts, prev = [], None
    for ch in chars:
        if prev is not None and ch["x0"] - prev > RUN_GAP:
            parts.append(" ")
        t = ch["text"]
        parts.append(BN_ASCII_GID.get(_gid_of(t), t) if _is_undecoded(t) else t)
        prev = ch["x1"]
    return "".join(parts).strip()


def _boundaries(centres, data_chars):
    """Column boundaries: the widest whitespace gutter between each pair of
    anchors, measured over the page's data rows only (title, section headings
    and the footer span several columns and would hide every gutter)."""
    bounds = [float("-inf")]
    for left, right in zip(centres, centres[1:]):
        lo, hi = int(left) + 1, int(right)
        occupied = [0] * (hi - lo + 1)
        for ch in data_chars:
            for x in range(max(int(ch["x0"]), lo), min(int(ch["x1"]) + 1, hi) + 1):
                occupied[x - lo] += 1
        bounds.append(lo + _widest_gap(occupied))
    bounds.append(float("inf"))
    return bounds


def _widest_gap(occupied):
    """Centre of the widest *interior* blank run -- one with ink on both sides.
    A trailing blank stretch is not a gutter: it is the slack before the next
    column starts, and cutting there swallows the neighbour. When a few long
    names run right into the next column no interior run exists at all; the
    fallback is then the emptiest single x, which cuts through those few names
    and leaves the row with a remark rather than corrupting the whole page --
    raising the ink threshold instead was tried and moves every boundary."""
    run = _widest_run(occupied)
    if run:
        return (run[0] + run[1] - 1) / 2
    return min(range(len(occupied)), key=lambda i: occupied[i])


def _widest_run(occupied):
    """Widest maximal blank run that has ink on both sides."""
    best = start = None
    for i, n in enumerate(occupied):
        if n:
            if start not in (None, 0):
                if best is None or i - start > best[1] - best[0]:
                    best = (start, i)
            start = None
        elif start is None:
            start = i
    return best


def _split_row(row, bounds):
    cols = [[] for _ in range(N_COLS)]
    for ch in row:
        centre = (ch["x0"] + ch["x1"]) / 2
        for i in range(N_COLS):
            if bounds[i] <= centre < bounds[i + 1]:
                cols[i].append(ch)
                break
    return cols


# --------------------------------------------------------------------------
# cover-page locality
# --------------------------------------------------------------------------
#
# Each part's first page is a cover sheet, not part of the roll table (see
# _page_rows below -- a page with no "1 2 3 ..." row and no carried-forward
# geometry contributes no rows at all). For the Latin-typeset Kolkata ACs it
# carries a "Village/Area/Road:" field, decodable through the same font
# patch as the roll table itself. Found the same way the column-number
# anchor is found above: split each visual row into whitespace-separated
# runs and look for the one matching the label text, then take the next run
# on that row as its value. Confirmed only against a Latin-typeset fixture
# (AC146) -- an unmatched label just leaves locality empty, the same
# don't-guess discipline as the rest of this module, rather than assuming
# Bengali-typeset cover pages share the layout.

COVER_LOCALITY_LABEL = re.compile(r"^village\s*/\s*area\s*/\s*road\s*:?$", re.IGNORECASE)


_LIST_MARKER = re.compile(r"^\d+\)$")


def _parse_cover_locality(page):
    """"Village/Area/Road" value off a part's cover page, or "" if this page
    doesn't carry a recognizable one.

    The value sits beside the label on the same row when it's short (e.g.
    "PARK STREET"), but wraps onto the next row, prefixed with a "1)" list
    marker, when it's long enough to need one (e.g. "1) BAGHBAZAR STREET
    (PREMISES NO.22/2A TO 30/2)") -- both confirmed against real fixtures
    (AC146, AC141 respectively).
    """
    rows = _rows_of(page.chars)
    row_texts = [[_cell_text(r) for r in _runs(row)] for row in rows]
    for i, texts in enumerate(row_texts):
        for j, text in enumerate(texts):
            if not COVER_LOCALITY_LABEL.match(text):
                continue
            # the trailing ":" sometimes sits far enough from the label to be
            # its own whitespace-run rather than part of the label token, and
            # the value itself is often more than one run ("PARK" "STREET")
            k = j + 1
            while k < len(texts) and texts[k] in ("", ":"):
                k += 1
            rest = texts[k:]
            if not rest and i + 1 < len(row_texts):
                rest = row_texts[i + 1]
            if rest and _LIST_MARKER.match(rest[0]):
                rest = rest[1:]
            if rest and all(t and not _has_undecoded(t) for t in rest):
                return " ".join(rest)
    return ""


# --------------------------------------------------------------------------
# record extraction
# --------------------------------------------------------------------------

def _page_rows(page, fallback=None):
    """Return ([(cell_text x 8, cell_boxes x 8)] per logical roll row, column
    geometry) for one page. Both come back empty if the page carries no roll
    table. See _segment_rows for what the per-cell boxes are.

    A logical row is not always one visual row: a long name wraps onto a
    following line that carries no serial number, and the Bengali layout puts
    the EPIC number on its own line under the row it belongs to. Rows with no
    serial number are therefore folded into the row above, but only when they
    sit on the very next line -- the page footer and the column legend also
    lack a serial number, and folding those in would corrupt the last row of
    every page. The "Section No..." sub-headings interleaved in the table start
    at the left margin with a word rather than a number, and are dropped.

    A page can carry more than one table -- a part's last page restarts the
    numbering for the supplement's additions, deletions and corrections lists
    -- so every column-number row starts a fresh segment, measured on its own.
    Segments whose column-number row is not eight wide are skipped whole: the
    deletions list is a three-column name-and-EPIC table, not an elector roll.

    A page can also carry no column-number row at all -- a long table simply
    continues onto the next page without repeating its header -- so `fallback`
    carries the last page's geometry forward, and the geometry actually used is
    returned for the next page in turn.
    """
    if not page.chars:
        return [], fallback
    rows = _rows_of(page.chars)
    marks = [(i, _column_numbers_of(row)) for i, row in enumerate(rows)]
    marks = [(i, c) for i, c in marks if c]
    if not marks:
        return (list(_segment_rows(fallback, rows)) if fallback else []), fallback

    out = []
    ends = [i for i, _ in marks[1:]] + [len(rows)]
    for (start, centres), end in zip(marks, ends):
        if len(centres) == N_COLS:
            out.extend(_segment_rows(centres, rows[start + 1:end]))
            fallback = centres
    return out, fallback


def _segment_rows(centres, body):
    """Yield (cells, boxes) per logical roll row.

    `boxes` parallels `cells`: one list of (x0, top, x1, bottom) rects per
    column, holding where that cell's glyphs were actually drawn. A wrapped
    cell gets one rect per visual line rather than their union, because the
    union of two lines' rects also covers the whitespace and neighbouring
    columns between them -- useless as an OCR crop. Only the name columns are
    ever cropped (see WestBengalConnector._parse_part), but every column is
    tracked because the cost is a tuple per cell and the alternative is a
    column-specific special case here.
    """
    # A data row starts with a number at the far left. Spotting them without
    # column boundaries first is what makes the gutters measurable.
    data = [r for r in body if _starts_with_serial(r, centres)]
    if not data:
        return
    bounds = _boundaries(centres, [ch for r in data for ch in r])
    reach = _line_height(body) * CONT_LINES

    out, prev_top = [], None
    for row in body:
        top = min(ch["top"] for ch in row)
        split = _split_row(row, bounds)
        cells = [_cell_text(c) for c in split]
        boxes = [[_bbox_of(c)] if c else [] for c in split]
        if cells[COL_SL].isdigit():
            out.append((cells, boxes))
        elif out and not cells[COL_SL] and top - prev_top <= reach:
            prev_cells, prev_boxes = out[-1]
            for i, extra in enumerate(cells):
                if extra:
                    prev_cells[i] = (prev_cells[i] + " " + extra).strip()
                    prev_boxes[i].extend(boxes[i])
        else:
            continue
        prev_top = top
    yield from out


def _bbox_of(chars):
    return (
        min(c["x0"] for c in chars),
        min(c["top"] for c in chars),
        max(c["x1"] for c in chars),
        max(c["bottom"] for c in chars),
    )


def _line_height(rows):
    """Median baseline-to-baseline distance, i.e. the page's line pitch."""
    tops = sorted(min(ch["top"] for ch in r) for r in rows)
    gaps = sorted(b - a for a, b in zip(tops, tops[1:]))
    return gaps[len(gaps) // 2] if gaps else 0.0


def _column_numbers_of(row):
    """x-centres of this row's cells if it is a "1 2 3 ..." column-number row.

    Every table on every page carries one, in both languages, and it is the
    only ASCII-legible element of a Bengali page -- which makes it the one
    reliable place to read the column geometry off.
    """
    runs = _runs(row)
    if len(runs) < 3:
        return None
    labels = ["".join(_digit_or_none(c["text"]) or "?" for c in r) for r in runs]
    if labels != [str(i) for i in range(1, len(runs) + 1)]:
        return None
    return [(r[0]["x0"] + r[-1]["x1"]) / 2 for r in runs]


def _starts_with_serial(row, centres):
    first = _runs(row)[0]
    return (first[0]["x0"] + first[-1]["x1"]) / 2 < centres[1] and _cell_text(
        first
    ).isdigit()


def _digit_or_none(ch):
    if ch.isdigit():
        return ch
    return BN_DIGIT_GID.get(_gid_of(ch)) if _is_undecoded(ch) else None


def _parse_int(raw, field_label, remarks):
    """Blank is a normal 'not recorded' state (no remark). Anything else that
    won't parse is a genuine source quirk -- kept null with a remark rather
    than guessed at or used to drop the row."""
    val = (raw or "").strip()
    if not val:
        return None
    if val.isdigit():
        return int(val)
    digits = re.sub(r"\D", "", val)
    if digits and not _has_undecoded(val):
        remarks.append(f"non-numeric {field_label}: {val!r}")
        return int(digits)
    remarks.append(f"unreadable {field_label}: {_describe(val)}")
    return None


def _has_undecoded(s):
    return any(_is_undecoded(c) for c in s)


def _describe(s):
    """Render a cell for a remark without spilling private-use code points."""
    return "".join("?" if _is_undecoded(c) else c for c in s)


def _part_no_of(member):
    m = re.search(r"(\d+)", os.path.basename(member))
    return int(m.group(1)) if m else None


def _normalize(raw, table, field_label, remarks):
    """Map a cell through a confident-only table; anything unrecognized keeps
    its raw value and earns a remark instead of being guessed at."""
    val = (raw or "").strip()
    key = val.upper()
    if key in table:
        return table[key]
    if val:
        remarks.append(f"unrecognized {field_label}: {val!r}")
    return val


class WestBengalConnector(StateConnector):
    state_id = "west_bengal"

    def __init__(self, session=None, ocr=None):
        self.session = session or requests.Session()
        # Off unless asked for: OCR needs a Tesseract install that the rest of
        # this package does not, and it is pure cost on the Latin-typeset ACs,
        # whose names already decode exactly. build_db.py constructs connectors
        # with no arguments, so the env var is how a build turns it on.
        self.ocr = _env_flag("WB_OCR") if ocr is None else ocr

    def list_constituencies(self) -> list:
        with open(AC_META_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return [
            Constituency(
                ac_code=row["ac_code"],
                ac_name=row["ac_name"],
                district=row["district"],
                total_parts=row.get("total_parts", 0),
                # the part list travels with the Constituency so fetch_raw()
                # needs no extra live request per AC, and a download run is
                # reproducible from the committed metadata alone
                extra={
                    "ac_id": row["ac_id"],
                    "ac_no": row["ac_no"],
                    "district_id": row["district_id"],
                    "parts": row["parts"],
                },
            )
            for row in raw
        ]

    def fetch_part(self, ac: Constituency, part: dict) -> bytes:
        url = PDF_URL.format(
            ac_id=ac.extra["ac_id"], key=b64encode(part["filename"].encode()).decode()
        )
        resp = self.session.get(url, timeout=120)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            raise ValueError(
                f"{part['filename']}: expected a PDF, got {resp.headers.get('Content-Type')!r} "
                f"({len(resp.content)} bytes)"
            )
        return resp.content

    def fetch_raw(self, ac: Constituency, roll_year: int, on_part=None) -> bytes:
        """Every part PDF of this AC, bundled into one ZIP (see module docstring)."""
        if roll_year != 2002:
            raise NotImplementedError(
                "West Bengal connector only implements the 2002 roll; the "
                "current roll lives behind a different, ECI-side pipeline."
            )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for part in ac.extra["parts"]:
                data = self.fetch_part(ac, part)
                zf.writestr(PART_MEMBER.format(part_no=part["part_no"]), data)
                if on_part:
                    on_part(part, len(data))
        return buf.getvalue()

    def parse_raw(self, raw: bytes, ac: Constituency, roll_year: int) -> list:
        """
        Every roll row is kept -- nothing is silently dropped, and nothing that
        could not be read is silently guessed at.

        district/ac_code/ac_name come from `ac` rather than from the PDF's own
        cover page, which is typeset in Bengali for all but the Kolkata ACs and
        so is not readable (see the module docstring). part_no comes from the
        ZIP member name, which the downloader took from the site's part index.

        For a Bengali-typeset AC the name columns come out as glyph ids with no
        Unicode mapping. Every other column of those rows -- serial no, house
        no, age, sex, relationship, EPIC number -- *is* recoverable and is
        emitted. The names themselves come from OCR when it is enabled (see the
        module docstring), and are otherwise left empty; either way the row
        carries a remark saying which of the two happened, so a consumer can
        tell an OCR'd name from an exactly-decoded one.
        """
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            jobs = [(name, _part_no_of(name)) for name in sorted(zf.namelist())]
            if not self.ocr or len(jobs) < 2:
                return [
                    rec
                    for name, part_no in jobs
                    for rec in self._parse_part(
                        zf.read(name), ac, roll_year, part_no, name
                    )
                ]
            return self._parse_parts_concurrently(zf, jobs, ac, roll_year)

    def _parse_parts_concurrently(self, zf, jobs, ac, roll_year):
        """Parse parts `workers` at a time, keeping the output in part order.

        Parts are independent and, with OCR on, dominated by waiting on an OCR
        engine -- a Cloud Vision round trip, or a tesseract subprocess -- so
        threads suffice and no PDF has to cross a process boundary. Measured on
        6 parts of AC001 via Vision: 110.9s at workers=1, 45.1s at 6 (2.5x),
        identical output. The gap from a clean 6x is accounted for: of the
        110.9s, ~7s is pdfplumber and ~7s is PNG encoding, both GIL-bound,
        against ~95s of network wait that does parallelize.

        Deliberately threads and not a process pool: build_db.build_per_ac
        already fans ACs out across a ProcessPoolExecutor, so this is the inner
        layer of an outer process pool, and nesting pools would multiply into
        dozens of processes rather than the intended concurrency.

        A wave at a time rather than submitting every part at once: ZipFile.read
        is not safe to call from several threads concurrently, so bytes are read
        here on one thread, and holding only `workers` parts at a time keeps
        peak memory flat instead of scaling with a 450-part AC.
        """
        size = min(west_bengal_ocr.workers(), len(jobs))
        records = []
        with ThreadPoolExecutor(max_workers=size) as pool:
            for start in range(0, len(jobs), size):
                wave = [
                    pool.submit(
                        self._parse_part, zf.read(name), ac, roll_year, part_no, name
                    )
                    for name, part_no in jobs[start:start + size]
                ]
                for future in wave:
                    records.extend(future.result())
        return records

    def _parse_part(self, pdf_bytes, ac, roll_year, part_no, member):
        rows, locality = [], None
        geometry = None
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_no, page in enumerate(pdf.pages):
                if locality is None and page.chars:
                    locality = _parse_cover_locality(page) or None
                page_rows, geometry = _page_rows(page, geometry)
                rows.extend((page_no, cells, boxes) for cells, boxes in page_rows)

        names = self._ocr_names(pdf_bytes, rows) if self.ocr else {}
        records = [
            self._record(cells, ac, roll_year, part_no, member, names.get(i))
            for i, (_, cells, _) in enumerate(rows)
        ]
        for rec in records:
            rec.locality = locality or ""
        return records

    def _ocr_names(self, pdf_bytes, rows):
        """{row_index: (name, relative_name)} for the rows whose name columns
        came out as undecoded Bengali glyphs.

        Batched across the whole part in one engine call -- see
        states/west_bengal_ocr.py on why that matters. Rows whose names already
        decoded (a Latin-typeset AC) are not sent: OCR would be strictly worse
        than the exact text the glyph mapping already gives.
        """
        wanted, cells_to_ocr = [], []
        for i, (page_no, cells, boxes) in enumerate(rows):
            if not (_has_undecoded(cells[COL_NAME]) or _has_undecoded(cells[COL_RELNAME])):
                continue
            wanted.append(i)
            cells_to_ocr.append((page_no, boxes[COL_NAME]))
            cells_to_ocr.append((page_no, boxes[COL_RELNAME]))
        if not wanted:
            return {}
        out = west_bengal_ocr.ocr_cells(pdf_bytes, cells_to_ocr)
        return {i: (out[2 * n], out[2 * n + 1]) for n, i in enumerate(wanted)}

    def _record(self, cells, ac, roll_year, part_no, member, ocr_names=None):
        remarks = []
        name = cells[COL_NAME]
        rel_name = cells[COL_RELNAME]

        if _has_undecoded(name) or _has_undecoded(rel_name):
            # Bengali-typeset AC: the glyphs are there but nothing in the PDF
            # says what they mean, so the text layer is a dead end. Either OCR
            # read the names off the rendered glyphs, or we refuse rather than
            # transliterate a guess.
            name, rel_name = (ocr_names or ("", ""))
            remarks.append(
                "name in Bengali script, read by OCR from the rendered glyphs: "
                "source font carries no ToUnicode map. Best-effort accuracy"
                if name or rel_name else
                "name in Bengali script: source font carries no ToUnicode map, "
                "so the name columns are not decodable yet"
            )

        relation = _normalize(
            _bn_lookup(cells[COL_REL], BN_RELATION, "relation_code", remarks),
            RELATION_NORMALIZE, "relation_code", remarks,
        )
        gender = _normalize(
            _bn_lookup(cells[COL_SEX], BN_GENDER, "gender", remarks),
            GENDER_NORMALIZE, "gender", remarks,
        )
        epic = cells[COL_EPIC]
        if _has_undecoded(epic):
            remarks.append(f"unreadable EPIC no: {_describe(epic)}")
            epic = ""

        return VoterRecord(
            state=self.state_id,
            district=ac.district,
            ac_code=ac.ac_code,
            ac_name=ac.ac_name,
            part_no=part_no,
            serial_no=_parse_int(cells[COL_SL], "serial_no", remarks),
            local_ref=epic,
            full_name=name,
            full_relative_name=rel_name,
            relation_code=relation,
            age=_parse_int(cells[COL_AGE], "age", remarks),
            gender=gender,
            roll_year=roll_year,
            remark="; ".join(remarks),
        )


def _bn_lookup(cell, table, field_label, remarks):
    """Resolve a Bengali closed-vocabulary cell to its English equivalent and
    leave an already-Latin cell alone. An unrecognized glyph sequence is
    reported verbatim in the remark -- it is the only durable handle on the
    cell, and it lets a later pass census what was missed."""
    if not _has_undecoded(cell):
        return cell
    key = _bn_key(cell)
    if key in table:
        return table[key]
    remarks.append(f"unrecognized {field_label} in Bengali script: gid {key}")
    return ""
