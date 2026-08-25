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
build reproducible and lets fetch_raw() work offline-ish.

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

What parse_raw() dispatches on
------------------------------
Three kinds of part PDF reach this connector, and which one a file is can be
read off the file itself rather than off a list of AC codes:

- **a Latin text layer** -- the ~19 Kolkata ACs, typeset in English. The text
  comes straight out of the PDF.
- **a PUA text layer** -- ~265 ACs typeset in Bengali, whose glyph ids carry
  no Unicode meaning of their own. A gid->Unicode table has been derived for
  the Shree-Lipi font they use (states/west_bengal_shreelipi.py), so their
  names decode too, with any glyph the table does not know reported in the
  row's remark rather than guessed at. Still undecodable is Darjeeling's own
  font: AC022-AC024 entirely, and a minority of the parts of AC025/AC026,
  whose glyph ids overlap Shree-Lipi's numerically while meaning something
  else. Rows from those parts keep their numeric and closed-vocabulary
  columns (see BN_DIGIT_GID / BN_RELATION / BN_GENDER below) and carry empty
  names with a remark saying so.
- **no text layer at all** -- AC287, AC291, AC294 are page images, so there
  are no glyphs for any of the font work above to decode. They go through
  Cloud Vision (scripts/ocr_vision.py) and come back as word geometry, which
  states/west_bengal_ocr.py reassembles into roll rows. They stay West Bengal
  ACs with their own ac_codes -- see _parse_scanned() for why that is a
  branch here rather than a connector of its own.

The three cases are disjoint and each is decided *before* any of them runs,
never by one path failing into the next. That matters more here than the
tidiness of it: a Shree-Lipi part misrouted to OCR, or a scan misrouted to
the glyph decoder, does not raise -- it produces plausible wrong names, which
is the one failure this connector has no way to notice downstream.
"""
import io
import json
import os
import re
import zipfile
from base64 import b64encode

import requests

from states.base import (
    Constituency,
    StateConnector,
    UnparseableRollError,
    VoterRecord,
)
from states.west_bengal_ocr import OCR_SUBDIR, ocr_gaps, page_failures, parse_part

_HERE = os.path.dirname(os.path.abspath(__file__))
AC_META_PATH = os.path.join(_HERE, "meta", "west_bengal_ac_meta.json")

PDF_URL = "https://ceowestbengal.wb.gov.in/RollPDF/GetDraft?acId={ac_id}&key={key}"
PART_MEMBER = "part{part_no:04d}.pdf"

# Where scripts/ocr_vision.py leaves its responses -- one directory per AC
# under this state's raw dir. Spelled out here rather than read from
# states/registry.py, which imports this module; a test asserts the two
# agree instead.
RAW_DIR = os.path.join("data", "raw", "west_bengal")
OCR_DIR = os.path.join(RAW_DIR, OCR_SUBDIR)

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

from states.west_bengal_shreelipi import (  # noqa: E402
    decode as _shreelipi_decode,
    looks_like_shreelipi,
)


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
# on that row as its value. An unmatched label just leaves locality empty,
# the same don't-guess discipline as the rest of this module.
#
# The label itself is typeset in the same legacy font as everything else on a
# Bengali cover page, so it is only matchable after decoding -- decoding just
# the *value* finds nothing to decode, because the label never matched. That
# is why COVER_LOCALITY_LABEL_BN exists and why _parse_cover_locality decodes
# every run on the page rather than only the ones after the label.

COVER_LOCALITY_LABEL = re.compile(r"^village\s*/\s*area\s*/\s*road\s*:?$", re.IGNORECASE)

# "গ্রাম/মহল্লা/রাস্তা" -- the same field on a Shree-Lipi cover page. The
# source punctuates its labels with "ঃ" (U+0983) rather than a colon.
COVER_LOCALITY_LABEL_BN = re.compile(r"^গ্রাম\s*/\s*মহল্লা\s*/\s*রাস্তা\s*[ঃ:]?$")

# Punctuation that can sit between the label and its value as its own run.
_LABEL_PUNCTUATION = ("", ":", "ঃ")

_LIST_MARKER = re.compile(r"^\d+\)$")


def _parse_cover_locality(page, shreelipi=False):
    """"Village/Area/Road" value off a part's cover page, or "" if this page
    doesn't carry a recognizable one.

    `shreelipi` says the page is set in the Bengali legacy font this repo has
    a table for, in which case the whole page is transcoded before matching --
    label included, since the label is set in that font too.

    The value sits beside the label on the same row when it's short (e.g.
    "PARK STREET"), but wraps onto the next row, prefixed with a "1)" list
    marker, when it's long enough to need one (e.g. "1) BAGHBAZAR STREET
    (PREMISES NO.22/2A TO 30/2)") -- both confirmed against real fixtures
    (AC146, AC141 respectively).

    The next-row fallback requires that list marker rather than taking whatever
    follows: a part whose locality is genuinely blank leaves the label alone on
    its row, and the row under it is the *next* label ("Name of Gram Panchayat
    / Ward No"), which would otherwise be published as this part's locality.
    Measured over 435 Latin parts across all 19 Kolkata ACs, every real wrap
    carries the marker, so requiring it costs nothing there.
    """
    rows = _rows_of(page.chars)
    row_texts = [[_cell_text(r) for r in _runs(row)] for row in rows]
    if shreelipi:
        row_texts = [[_shreelipi_decode(t)[0] for t in texts] for texts in row_texts]
        label = COVER_LOCALITY_LABEL_BN
    else:
        label = COVER_LOCALITY_LABEL
    for i, texts in enumerate(row_texts):
        for j, text in enumerate(texts):
            if not label.match(text):
                continue
            # the trailing ":" sometimes sits far enough from the label to be
            # its own whitespace-run rather than part of the label token, and
            # the value itself is often more than one run ("PARK" "STREET")
            k = j + 1
            while k < len(texts) and texts[k] in _LABEL_PUNCTUATION:
                k += 1
            rest = texts[k:]
            if not rest and i + 1 < len(row_texts):
                nxt = row_texts[i + 1]
                rest = nxt[1:] if nxt and _LIST_MARKER.match(nxt[0]) else []
            elif rest and _LIST_MARKER.match(rest[0]):
                rest = rest[1:]
            if rest and all(t and not _has_undecoded(t) for t in rest):
                return " ".join(rest)
    return ""


# --------------------------------------------------------------------------
# record extraction
# --------------------------------------------------------------------------

def _report_unread_pages(ac, stems, unread):
    """Say, on the build log, which pages Vision answered but could not read.

    Deliberately not an UnparseableRollError. The gap check above refuses an
    AC whose OCR *run* is unfinished, because re-running closes it; this is
    the other thing -- Vision answered for every page, and for some of them
    the answer was that it could not read the image. Refusing the AC over
    that publishes "AC287 is not digitized", which is 0.5% true, in place of
    "AC287 is digitized" with a named hole in it, which is 99.5% true; and
    UnparseableRollError's contract is an AC that cannot be parsed *at all*,
    which this is not.

    So it is carried and named. Whoever reads the build output learns which
    parts are short and by how much, which is what a re-OCR pass needs. A
    page that came back unreadable every time is a source-side defect (see
    AC287 part0103, whose PDF declares a 1859x2630pt page box where every
    other part is A4 -- Vision rejects the render with "Bad image data.")
    and wants fixing at the upload stage, not here.
    """
    if not unread:
        return
    by_part = {}
    for stem, page, why in unread:
        by_part.setdefault(stem, []).append((page, why))
    worst = sorted(by_part.items(), key=lambda kv: -len(kv[1]))
    shown = "; ".join(f"{stem} p{pages[0][0]} +{len(pages) - 1} more" if len(pages) > 1
                      else f"{stem} p{pages[0][0]}" for stem, pages in worst[:5])
    reasons = sorted({why for _, _, why in unread})
    print(
        f"  {ac.ac_code} ({ac.ac_name}): {len(unread)} page(s) across "
        f"{len(by_part)} of {len(stems)} part(s) came back unreadable and hold "
        f"no electors here -- {shown}"
        + (f" ... (+{len(by_part) - 5} more part(s))" if len(by_part) > 5 else "")
        + f" [Vision said: {', '.join(reasons)}]"
    )


def _part_no_of(member):
    """The part number a zip member name carries, or None.

    The downloader names members part{part_no:04d}.pdf from the CEO site's
    own part index, and scripts/ocr_vision.py names its per-part directories
    after those stems -- so this is the one place the two halves agree on
    what a part number is, and it is what makes source_url resolvable per
    part (states/source_urls.py joins on (ac_code, part_no)).
    """
    m = re.search(r"(\d+)", os.path.basename(member))
    return int(m.group(1)) if m else None


# The three kinds of part PDF this state ships, as parse_raw() dispatches on
# them. Named rather than inferred so that no path is ever reached by another
# one failing: a scan run through the glyph decoder, or a Shree-Lipi part run
# through the OCR responses, does not raise -- it yields confident wrong
# names, which is the one failure this connector cannot notice downstream.
LAYER_SCANNED = "scanned"      # no text layer at all -- pixels (AC287/291/294)
LAYER_PUA = "pua"              # glyph ids with no Unicode meaning of their own
LAYER_LATIN = "latin"          # text that is already text (the Kolkata ACs)


def _text_layer(pdf_bytes):
    """Which of the three a part PDF is, read off the file itself.

    Asked of an AC's first part. Cheap on the two typeset cases: the loop
    stops at the first page carrying characters, which for a typeset roll is
    page one. A scan costs a full pass over its 12-28 pages, once per AC.

    LATIN vs PUA is decided here for the dispatch only. The *authority* on
    which of the two a given part is stays in _parse_part(), per part, because
    AC025 and AC026 are mixed -- most of their parts are Shree-Lipi Bengali
    and a minority are not (see looks_like_shreelipi()). An AC-wide answer
    would be wrong for those two; what is safely AC-wide is only whether
    there is a text layer at all, and that is what the routing turns on.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            if page.chars:
                text = page.extract_text() or ""
                return LAYER_PUA if _has_undecoded(text) else LAYER_LATIN
    return LAYER_SCANNED



def _page_rows(page, fallback=None):
    """Return ([cell_text x 8] per logical roll row, column geometry) for one
    page. Both come back empty if the page carries no roll table.

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
        cells = [_cell_text(c) for c in _split_row(row, bounds)]
        if cells[COL_SL].isdigit():
            out.append(cells)
        elif out and not cells[COL_SL] and top - prev_top <= reach:
            for i, extra in enumerate(cells):
                if extra:
                    out[-1][i] = (out[-1][i] + " " + extra).strip()
        else:
            continue
        prev_top = top
    yield from out


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

    def __init__(self, session=None, ocr_dir=OCR_DIR):
        self.session = session or requests.Session()
        # Overridable so a test can point at a fixture tree; build_db
        # instantiates the connector with no arguments, so the default is
        # what every real build uses.
        self.ocr_dir = ocr_dir

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
        Unicode mapping. Where the font is the Shree-Lipi one this repo has a
        table for, they are decoded (see states/west_bengal_shreelipi.py). Where
        it is not -- Darjeeling -- the rows are still emitted, with their serial
        no, house no, age, sex, relationship and EPIC number, all of which *are*
        recoverable, but full_name/full_relative_name are left empty and the row
        carries a remark saying so, so that a later pass with that font's glyph
        table can fill them in from the same archived ZIP.

        An AC with no text layer at all is a page scan and none of the above
        applies to it: its rows come from a Cloud Vision response fetched
        earlier and stored on disk, reassembled from word geometry rather than
        extracted (see _parse_scanned and states/west_bengal_ocr.py). Which of
        the three a download is gets decided once, up front, by _text_layer --
        never by one path failing into the next.
        """
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = sorted(zf.namelist())
            if not members:
                raise UnparseableRollError(
                    f"{ac.ac_code}: the downloaded ZIP holds no part PDFs"
                )
            layer = _text_layer(zf.read(members[0]))
            if layer == LAYER_SCANNED:
                return self._parse_scanned(ac, roll_year, members)
            if layer in (LAYER_PUA, LAYER_LATIN):
                return self._parse_typeset(zf, members, ac, roll_year)
            raise UnparseableRollError(
                f"{ac.ac_code}: part PDFs are none of the three kinds this "
                f"connector reads (got {layer!r})"
            )

    def _parse_typeset(self, zf, members, ac, roll_year):
        """Every row of an AC whose PDFs have a text layer, Latin or PUA.

        Both cases run the same extraction; which one a *part* is is decided
        inside _parse_part(), not here -- see _text_layer().
        """
        records = []
        for member in members:
            records.extend(
                self._parse_part(
                    zf.read(member), ac, roll_year, _part_no_of(member), member
                )
            )
        return records

    def _parse_scanned(self, ac, roll_year, members):
        """Rows for an AC whose part PDFs are page scans, read back out of
        the Cloud Vision responses scripts/ocr_vision.py left beside the zip.

        Routed to by *what the PDF is*, not by an AC list: the first part is
        opened and asked whether it has a text layer at all. An AC list would
        be a second place to keep in sync with the source, and a state that
        re-scans an AC (or digitizes one properly) would have to be
        remembered about rather than just working.

        This is a fallback inside the West Bengal connector rather than a
        connector of its own because the app selects by `(state, ac_code)`
        and groups the picker by state -- AC287 has to be a West Bengal
        constituency, not a member of some "west_bengal_ocr" state. Nothing
        downstream of here can tell that these three ACs came through Vision:
        same VoterRecord shape, same ac_code, same per-part source_url.
        """
        ac_dir = os.path.join(self.ocr_dir, ac.ac_code)
        if not os.path.isdir(ac_dir):
            raise UnparseableRollError(
                f"{ac.ac_code} ({ac.ac_name}): the part PDFs are page scans with no "
                f"text layer, and no OCR output is present at {ac_dir} -- run "
                f"`make ocr-vision ACS={ac.ac_code}` first. Refusing to guess at pixels."
            )

        stems = [os.path.splitext(os.path.basename(m))[0] for m in members]
        gaps = ocr_gaps(ac_dir, stems)
        if gaps:
            shown = "; ".join(gaps[:5]) + (f" ... (+{len(gaps) - 5} more)" if len(gaps) > 5 else "")
            raise UnparseableRollError(
                f"{ac.ac_code} ({ac.ac_name}): OCR output is incomplete for "
                f"{len(gaps)} of {len(stems)} part(s) -- {shown}. An AC built from "
                f"the parts that happen to be finished is short by exactly the "
                f"electors nobody would notice missing, so it is absent instead."
            )

        records = []
        unread = []
        for member, stem in zip(members, stems):
            part_no = _part_no_of(member)
            part_dir = os.path.join(ac_dir, stem)
            unread.extend((stem, page, why) for page, why in page_failures(part_dir))
            for row in parse_part(part_dir):
                records.append(self._ocr_record(row, ac, roll_year, part_no))
        _report_unread_pages(ac, stems, unread)
        return records

    def _ocr_record(self, row, ac, roll_year, part_no):
        """One OCR'd row as a VoterRecord.

        district/ac_code/ac_name come from `ac` and part_no from the part
        directory's name -- the cover page of a scanned part is an image like
        every other page, so nothing here is read off the document itself.
        That also leaves `locality` empty: this roll prints the village/town
        on the cover, and an unread cell is left empty rather than inferred.

        `age` is always None, and that is the parser's decision, not a
        parse failure -- see states/west_bengal_ocr.py's module docstring.
        """
        return VoterRecord(
            state=self.state_id,
            district=ac.district,
            ac_code=ac.ac_code,
            ac_name=ac.ac_name,
            part_no=part_no,
            serial_no=row["serial_no"],
            local_ref=row["local_ref"],
            full_name=row["full_name"],
            full_relative_name=row["full_relative_name"],
            relation_code=row["relation_code"],
            age=row["age"],
            gender=row["gender"],
            roll_year=roll_year,
            remark=row["remark"],
        )

    def _parse_part(self, pdf_bytes, ac, roll_year, part_no, member):
        records, geometry = [], None
        locality = None
        # Which legacy font this part is set in, decided once from the first
        # page that carries any undecoded glyph. Darjeeling uses a different
        # glyph space whose ids overlap this one's, so the wrong table would
        # produce confident-looking nonsense rather than an error -- and it is
        # decided per *part*, not per AC, because AC025 and AC026 are mixed:
        # most of their parts are Shree-Lipi Bengali and a minority are not.
        shreelipi = None
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                if shreelipi is None and page.chars:
                    text = page.extract_text() or ""
                    if _has_undecoded(text):
                        shreelipi = looks_like_shreelipi(text)
                if locality is None and page.chars:
                    locality = _parse_cover_locality(page, bool(shreelipi)) or None
                rows, geometry = _page_rows(page, geometry)
                for cells in rows:
                    records.append(
                        self._record(
                            cells, ac, roll_year, part_no, member, bool(shreelipi)
                        )
                    )
        for rec in records:
            rec.locality = locality or ""
        return records

    def _record(self, cells, ac, roll_year, part_no, member, shreelipi=False):
        remarks = []
        name = cells[COL_NAME]
        rel_name = cells[COL_RELNAME]

        if _has_undecoded(name) or _has_undecoded(rel_name):
            if shreelipi:
                # The font has no ToUnicode map, but its glyph ids are known --
                # see states/west_bengal_shreelipi.py for how that table was
                # derived and validated.
                name, missing = _shreelipi_decode(name)
                rel_name, missing_rel = _shreelipi_decode(rel_name)
                missing = sorted(set(missing) | set(missing_rel))
                if missing:
                    # The row is kept with the damage recorded, matching
                    # states/karnataka.py -- a name short one letter still
                    # searches, and the remark is what lets a later pass
                    # census exactly which glyphs are still unmapped.
                    remarks.append(
                        "unmapped Bengali glyph id(s) in name columns: "
                        + ", ".join(str(g) for g in missing)
                    )
            else:
                # Some other legacy font -- the glyphs are there but nothing
                # says what they mean. Refuse rather than guess.
                remarks.append(
                    "name in an unrecognized Bengali-script font: no glyph "
                    "table for it, so the name columns are not decodable"
                )
                name = rel_name = ""

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
