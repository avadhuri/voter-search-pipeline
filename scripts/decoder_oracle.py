"""An independent check on the Shree-Lipi glyph table, from outside it.

Why this exists
---------------
`states/west_bengal_shreelipi.py` decodes a font that carries no ToUnicode
map, so every glyph id in it was assigned a meaning by hand. The metric used
while that table was built was the **unmapped** rate: how often decoding met
a glyph id with no entry. It fell from 9.5% to 0.15% and looked like
convergence.

It was not, and it structurally could not have been. Three later rounds of
fixes -- gid 224 read as a space, 203 as a duplicate matra, 61/94/120 as
plain consonants rather than half forms -- were all ids that **already had
entries**. They were mis-mapped, not unmapped, so the unmapped rate scored
every one of them as a success. The last round alone moved ~4.8% of name
occurrences. Nothing we had could have told us whether a fourth round was
waiting, and 265 constituencies were about to be published on it.

An oracle has to come from outside the table entirely. This is that: render
the PDF with its **own embedded font** and read the resulting pixels with an
OCR engine that has never heard of our glyph ids, then diff what it reads
against what `decode()` produces for the same cell. A mis-mapped id shows up
as a disagreement that concentrates on that id; OCR's own errors do not
concentrate anywhere, because they are independent of our table.

Why pixels, and why we rasterize them ourselves
-----------------------------------------------
Vision will accept a PDF directly, and for the *scanned* constituencies
(`scripts/ocr_vision.py`) that is the right input because those pages have no
text layer to confuse it. These pages do have one -- it is exactly the
private-use garbage this table exists to interpret -- so handing Vision the
PDF leaves it ambiguous whether the reply came from the pixels or from the
text layer, and a reply sourced from the text layer is not an oracle, it is
our own claim handed back to us.

Rasterizing locally removes the question: Ghostscript draws the glyphs using
the font embedded in the file, we send a PNG, and an image has no text layer
to read. It also keeps the whole run off GCS -- `images:annotate` takes the
bytes inline -- and sidesteps the five-page ceiling on the synchronous PDF
endpoint.

How a cell is aligned to what Vision saw
---------------------------------------
Not by reading order: Vision emits these pages column-major and interleaved,
so its flat `text` field cannot be walked alongside ours. Not by serial
number either, which would make the whole result depend on Bengali digit OCR
being right. Geometrically -- the PDF gives an exact rectangle for every
cell, Vision gives a box for every word, and the transform between them is
the render scale. A word belongs to the cell its centre lands in.

Reading the result
------------------
There is a floor. Vision misreads these glyphs on its own: on the first page
tried it read row 47's name as `ঝান্টু` where the page plainly shows
`ঝাল্টু`, then read the identical name correctly one row down. So the
deliverable is not a disagreement count, which is meaningless in isolation --
it is where the disagreement *sits*:

  - scattered across ids in proportion to how often they occur => the
    residual is OCR noise, and that is convergence evidence;
  - concentrated on one id => that id is round four, before we publish.

`--report` prints both the per-id table and the summary judgement. `lift` is
the ratio of an id's disagreement rate to the corpus base rate and `z` is how
many standard errors that is, so "concentrated" is a number rather than an
impression.

Cost and safety
---------------
Billed per page at $1.50/1000 with the first 1000 each month free, so a
300-page run is well under a dollar. `--dry-run` prints the page-exact bill
and submits nothing. Every response is cached on disk keyed by page, so a
re-run costs nothing and a kill loses only the page in flight. Nothing here
writes to a bucket, a database or any deployed environment; it reads the raw
zips and writes under `--out`.
"""
from __future__ import annotations

import argparse
import base64
import collections
import difflib
import gzip
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pdfplumber

from states.west_bengal import (
    CONT_LINES,
    COL_NAME,
    COL_RELNAME,
    COL_SL,
    N_COLS,
    _boundaries,
    _cell_text,
    _column_numbers_of,
    _gid_of,
    _has_undecoded,
    _is_undecoded,
    _line_height,
    _page_rows,
    _rows_of,
    _split_row,
    _starts_with_serial,
)
from states.west_bengal_shreelipi import (
    GID_MAP,
    HALF_GIDS,
    IGNORE_GIDS,
    JOINER_GIDS,
    KNOWN_GIDS,
    PREBASE_GIDS,
    REPHA_GIDS,
    STEM_GIDS,
    decode,
    looks_like_shreelipi,
)

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
PRICE_PER_1K = 1.50
FREE_PAGES_PER_MONTH = 1000

# 300 dpi is what the accuracy bake-off in scripts/ocr_vision.py measured
# Vision at its best on; these pages are vector, so rendering higher costs
# render time and request bytes without adding information the font does not
# already have.
RENDER_DPI = 300

# Concurrent pages in flight. Each is one synchronous request; Vision's
# default project quota is 1800/min, which this comes nowhere near.
WORKERS = 8

RETRIES = 5
TOKEN_TTL_SECONDS = 45 * 60

# The name columns are the only ones the glyph table touches -- every other
# column is a closed-vocabulary lookup or a digit run, decoded by tables that
# were never in doubt. Scoring them would dilute the signal with cells that
# cannot carry it.
DECODED_COLUMNS = (COL_NAME, COL_RELNAME)

# North Bengal earns extra weight because its rolls carry the Latin-origin
# and tribal names that pinned gids 114/38/182 -- the ids whose meaning was
# least constrained by the rest of the corpus are the ids most likely to
# still be wrong.
NORTH_BENGAL = (
    "Coochbehar", "Jalpaiguri", "Darjeeling",
    "Uttar Dinajpur", "Dakshin Dinajpur", "Malda",
)
NORTH_BENGAL_SHARE = 0.5

_token_lock = threading.Lock()
_token_cache: dict = {"value": None, "expires": 0.0}
_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ------------------------------------------------------------------ raster

# Ghostscript is looked up on PATH, but the fallbacks are here because an
# interactive shell on a dev machine can shadow `gs` with an alias (a `git
# status` alias is what hid it here for a while, since `gs --version` then
# prints a git status and reads as "not installed"). shutil.which searches
# PATH and is unaffected by shell aliases, so this only bites a human.
GS_CANDIDATES = ("gs", "/opt/homebrew/bin/gs", "/usr/local/bin/gs", "/usr/bin/gs")


def _ghostscript() -> str:
    for cand in GS_CANDIDATES:
        found = shutil.which(cand) if os.sep not in cand else (
            cand if os.access(cand, os.X_OK) else None
        )
        if found:
            return found
    raise RuntimeError(
        "Ghostscript not found. It is what renders the PDF with its own "
        "embedded font, which is the whole basis of this check. "
        "Install it (brew install ghostscript) and re-run."
    )


def render_pages(pdf_bytes: bytes, pages: list[int], dpi: int = RENDER_DPI) -> dict:
    """{page number: PNG bytes} for the requested 1-based pages.

    One Ghostscript invocation per contiguous request rather than per page:
    the interpreter has to parse the file's resources either way, and these
    parts run to hundreds of pages.
    """
    out: dict[int, bytes] = {}
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "in.pdf"
        src.write_bytes(pdf_bytes)
        for page in pages:
            dst = td / f"p{page}.png"
            proc = subprocess.run(
                [_ghostscript(), "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                 "-sDEVICE=png16m", f"-r{dpi}",
                 f"-dFirstPage={page}", f"-dLastPage={page}",
                 f"-sOutputFile={dst}", str(src)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0 or not dst.exists():
                raise RuntimeError(
                    f"ghostscript failed on page {page}: "
                    f"{(proc.stderr or proc.stdout)[:400]}"
                )
            out[page] = dst.read_bytes()
    return out


# ------------------------------------------------------------------- vision

def _token(force: bool = False) -> str:
    """The current access token, re-minted when it ages out or is rejected.

    Same shape as scripts/ocr_vision.py's, and for the same reason recorded
    there: gcloud can hand back a token Vision rejects outright, and caching
    it under a TTL that reads as valid makes every worker fail against the
    same bad value until the TTL runs out.
    """
    with _token_lock:
        now = time.monotonic()
        if force or _token_cache["value"] is None or now >= _token_cache["expires"]:
            out = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True, text=True, check=True,
            ).stdout
            # The token is picked out by line rather than taken as the whole
            # of stdout. gcloud runs its own interpreter and inherits the
            # environment, so anything that makes a Python start-up hook talk
            # (a PYTHONPATH sitecustomize, a deprecation notice) lands in the
            # middle of what looks like a token. Observed, not hypothetical.
            minted = next(
                (ln.strip() for ln in out.splitlines()
                 if ln.strip().startswith("ya29.")), out.strip())
            if not minted.startswith("ya29."):
                raise RuntimeError(
                    "gcloud returned something that is not an OAuth access "
                    f"token ({minted[:16]!r}...). Run: gcloud auth login"
                )
            _token_cache["value"] = minted
            _token_cache["expires"] = now + TOKEN_TTL_SECONDS
        return _token_cache["value"]


def annotate(png: bytes, project: str) -> dict:
    """DOCUMENT_TEXT_DETECTION on one rendered page.

    429/5xx are transient over a run this size and are backed off; a 401 is
    refreshable rather than permanent and is retried once against a forced
    new token; everything else fails the same way next attempt and is raised.
    """
    body = {"requests": [{
        "image": {"content": base64.b64encode(png).decode()},
        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
        "imageContext": {"languageHints": ["bn"]},
    }]}
    data = json.dumps(body).encode()
    forced = False
    for attempt in range(RETRIES):
        req = urllib.request.Request(VISION_ENDPOINT, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {_token()}")
        # Without this an inline Vision call 403s on a user-credential token,
        # quota project unset.
        req.add_header("x-goog-user-project", project)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())["responses"][0]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:600]
            if e.code == 401 and not forced:
                forced = True
                _token(force=True)
                continue
            if e.code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                raise RuntimeError(f"vision -> {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == RETRIES - 1:
                raise RuntimeError(f"vision -> {e}") from None
        time.sleep((2 ** attempt) + random.random())
    raise AssertionError("unreachable")


def vision_words(resp: dict) -> tuple[list[dict], float, float]:
    """Every word Vision read, with a pixel box, plus the page's pixel size.

    Reading order is deliberately not preserved -- on these pages Vision
    emits column-major and interleaved, so order carries no information. Only
    the boxes do.
    """
    fta = resp.get("fullTextAnnotation")
    if not fta or not fta.get("pages"):
        return [], 0.0, 0.0
    page = fta["pages"][0]
    pw, ph = float(page.get("width") or 0), float(page.get("height") or 0)
    words = []
    for block in page.get("blocks", []):
        for para in block.get("paragraphs", []):
            for word in para.get("words", []):
                text = "".join(s.get("text", "") for s in word.get("symbols", []))
                if not text:
                    continue
                box = word.get("boundingBox") or {}
                verts = box.get("vertices")
                if verts:
                    xs = [float(v.get("x", 0)) for v in verts]
                    ys = [float(v.get("y", 0)) for v in verts]
                else:
                    nv = box.get("normalizedVertices") or []
                    if not nv:
                        continue
                    xs = [float(v.get("x", 0)) * pw for v in nv]
                    ys = [float(v.get("y", 0)) * ph for v in nv]
                words.append({
                    "text": text,
                    "cx": (min(xs) + max(xs)) / 2,
                    "cy": (min(ys) + max(ys)) / 2,
                    "x0": min(xs),
                })
    return words, pw, ph


# ------------------------------------------------------------------- engines
#
# The oracle asks an OCR engine for exactly two things: turn a rendered page
# into a response worth caching, and turn that response into word boxes. That
# is the whole Google-specific surface -- alignment, attribution, the
# per-glyph statistics and the report all work on normalized boxes and never
# learn which engine produced them. A second engine is a class here.
#
# Nothing but Vision is implemented. This exists because the question "can a
# local engine stand in, so the oracle runs without a billing project" is a
# live one, and the answer has to be decided on evidence rather than on how
# much rewriting it would cost. The evidence is already available: gid 212 is
# the pre-fix decoder's largest real error, so an engine that is a valid
# oracle for this corpus must rank it at the top of the residual on the
# pre-fix table. An engine whose own errors concentrate on Bengali conjuncts
# would fail that and is not an oracle, however convenient it is to run.


class VisionEngine:
    """Google Cloud Vision DOCUMENT_TEXT_DETECTION, one page per request."""

    name = "vision"
    needs_project = True
    price_per_1k = PRICE_PER_1K
    free_per_month = FREE_PAGES_PER_MONTH

    def read(self, png: bytes, project: str) -> dict:
        return annotate(png, project)

    def words(self, resp: dict) -> tuple[list[dict], float, float]:
        return vision_words(resp)


ENGINES = {VisionEngine.name: VisionEngine}


def _engine_for(out: Path, name: str):
    """The engine to use, refusing to score one engine's cells against another's.

    A response cache is per engine -- the cached JSON is that engine's own
    wire format, and even where two engines agree on a page they disagree on
    enough of it to make a mixed cache a report about nothing. The engine is
    therefore recorded in the --out tree the first time it is used, the same
    way the sample is, and a later run under a different engine is refused
    rather than quietly appended to.
    """
    if name not in ENGINES:
        raise SystemExit(f"unknown --engine {name!r}; have: "
                         + ", ".join(sorted(ENGINES)))
    stamp = out / "engine.json"
    if stamp.exists():
        was = json.loads(stamp.read_text()).get("engine")
        if was != name:
            raise SystemExit(
                f"{out} holds {was!r} responses; --engine {name!r} would score "
                f"them as though {name!r} had produced them. Use a different "
                f"--out for {name!r}.")
    else:
        out.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"engine": name}))
    return ENGINES[name]()


# ----------------------------------------------------- our side of the diff
#
# `states/west_bengal._page_rows` returns cell *strings*, which is all the
# connector needs and one field short of what an oracle needs: to ask Vision
# what it saw in a cell, you have to know where on the page that cell is.
#
# The two functions below are that same walk with the chars kept alongside
# the text. They deliberately duplicate `_segment_rows`' row-folding rather
# than reimplementing it differently -- and because a duplicate drifts,
# `page_cells` asserts on every page that the text it produced is exactly
# what the connector's own path produced. If the two ever diverge the oracle
# stops rather than quietly scoring a page the pipeline would have read
# differently.


def _segment_cells(centres, body):
    data = [r for r in body if _starts_with_serial(r, centres)]
    if not data:
        return
    bounds = _boundaries(centres, [ch for r in data for ch in r])
    reach = _line_height(body) * CONT_LINES

    out, prev_top = [], None
    for row in body:
        top = min(ch["top"] for ch in row)
        cells = [{"text": _cell_text(c), "chars": list(c)}
                 for c in _split_row(row, bounds)]
        if cells[COL_SL]["text"].isdigit():
            out.append({"cells": cells, "bounds": bounds})
        elif out and not cells[COL_SL]["text"] and top - prev_top <= reach:
            for i, extra in enumerate(cells):
                if extra["text"]:
                    prev = out[-1]["cells"][i]
                    prev["text"] = (prev["text"] + " " + extra["text"]).strip()
                    prev["chars"] += extra["chars"]
        else:
            continue
        prev_top = top
    yield from out


def page_cells(page, fallback=None):
    """([row], geometry) for one page, each row's cells carrying their chars.

    Mirrors states/west_bengal._page_rows exactly; see the note above.
    """
    if not page.chars:
        return [], fallback
    rows = _rows_of(page.chars)
    marks = [(i, c) for i, c in
             ((i, _column_numbers_of(r)) for i, r in enumerate(rows)) if c]

    if not marks:
        out = list(_segment_cells(fallback, rows)) if fallback else []
    else:
        out = []
        ends = [i for i, _ in marks[1:]] + [len(rows)]
        for (start, centres), end in zip(marks, ends):
            if len(centres) == N_COLS:
                out.extend(_segment_cells(centres, rows[start + 1:end]))
                fallback = centres

    return out, fallback


def verify_against_connector(page, fallback, mine):
    """Fail loudly if the mirrored walk above ever stops matching the real one."""
    theirs, _ = _page_rows(page, fallback)
    ours = [[c["text"] for c in row["cells"]] for row in mine]
    if ours != theirs:
        raise RuntimeError(
            "decoder_oracle's row walk has drifted from "
            "states/west_bengal._page_rows -- refusing to score a page the "
            f"pipeline reads differently ({len(ours)} rows vs {len(theirs)})"
        )


# How far outside a row's glyph band a Vision word's centre may sit and still
# belong to that row, as a fraction of the line pitch. Vision's boxes run a
# little taller than the ink; anything past this is another line's text.
BAND_PAD = 0.15


def row_band(row):
    """(top, bottom) in PDF points across every char in the logical row."""
    chars = [ch for cell in row["cells"] for ch in cell["chars"]]
    return (min(ch["top"] for ch in chars), max(ch["bottom"] for ch in chars))


def attach_vision(rows, words, scale_x, scale_y):
    """Give every row's decoded columns the words Vision read in that cell.

    A word is assigned to the row whose band -- padded a little, because a
    Vision box sits a shade taller than the glyphs it covers -- actually
    CONTAINS its centre, then to the column its centre's x lands in. A word
    inside no padded band is dropped rather than given to the nearest row:
    nearest-within-half-a-pitch pulled a section heading into a name cell on
    AC034/part0020 p12, and a cell handed text from another line disagrees
    with our decode for a reason that has nothing to do with the glyph table.
    Dropping is the safe direction -- it costs recall in the oracle, which
    shows up as skipped cells, not as a manufactured mismatch.
    """
    bands = [row_band(r) for r in rows]
    if not bands:
        return
    centres = [(a + b) / 2 for a, b in bands]
    pitch = (max(centres) - min(centres)) / max(len(centres) - 1, 1) or 1.0

    for r in rows:
        for cell in r["cells"]:
            cell["vision"] = []

    pad = pitch * BAND_PAD
    for w in words:
        y = w["cy"] / scale_y
        x = w["cx"] / scale_x
        best = None
        for i, (top, bot) in enumerate(bands):
            if top - pad <= y <= bot + pad:
                best = i
                break
        if best is None:
            continue
        row = rows[best]
        bounds = row["bounds"]
        for i in range(N_COLS):
            if bounds[i] <= x < bounds[i + 1]:
                row["cells"][i]["vision"].append((w["x0"], w["text"]))
                break

    for r in rows:
        for cell in r["cells"]:
            cell["vision_text"] = " ".join(
                t for _, t in sorted(cell["vision"], key=lambda p: p[0])
            )


# -------------------------------------------------------------- attribution

_WS = re.compile(r"\s+")


def _nows(s: str) -> str:
    """Whitespace carries no claim here.

    Our spacing comes from measuring gaps between glyph runs and Vision's
    comes from its own word segmentation; the two disagree constantly on
    where a Bengali name breaks, and none of that is the glyph table being
    wrong. Comparing without spaces keeps the diff about letters.
    """
    return _WS.sub("", s)


def gid_spans(pua: str) -> list[tuple[int, int | None, int, int]]:
    """(position, gid, out_start, out_end) for every glyph in a cell.

    Found by decoding progressively longer prefixes and watching where the
    output grows, rather than by instrumenting `decode()` -- an oracle that
    modifies the thing it is testing is not measuring the thing that ships.

    It is an approximation and the approximation is one-directional: this
    font draws pre-base matras before their cluster and rephas after the
    glyph they clear, so `decode()` reorders, and a prefix's output is not
    always a prefix of the whole cell's. That smears a span by a character or
    two around a reordered cluster. It cannot invent a concentration -- a
    smear spreads blame to a glyph's *neighbours*, which are different
    glyphs in different names, so it adds noise to the ranking rather than a
    peak. The row-level enrichment below carries no such assumption and is
    the metric to trust if the two ever disagree.
    """
    gids = [_gid_of(ch) if _is_undecoded(ch) else None for ch in pua]
    lens = [0]
    for k in range(1, len(pua) + 1):
        text, _ = decode(pua[:k])
        lens.append(len(_nows(text)))
    for i in range(1, len(lens)):
        lens[i] = max(lens[i], lens[i - 1])
    return [(i, gids[i], lens[i], lens[i + 1]) for i in range(len(pua))]


def implicated(pua: str, ours: str, theirs: str) -> tuple[set[int], int]:
    """Glyph ids sitting under the characters Vision read differently.

    Returns the ids and how many of our characters fell in a differing run,
    so a cell that disagrees in one letter does not weigh the same as one
    that disagrees in six.
    """
    a, b = _nows(ours), _nows(theirs)
    spans = gid_spans(pua)
    hit: set[int] = set()
    chars = 0
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
        None, a, b, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        chars += max(i2 - i1, 1)
        for _pos, gid, s, e in spans:
            if gid is None:
                continue
            if i1 == i2:
                if s <= i1 <= e:          # an insertion sits between glyphs
                    hit.add(gid)
            elif s < i2 and e > i1:
                hit.add(gid)
    return hit, chars


def gids_of(pua: str) -> set[int]:
    return {_gid_of(ch) for ch in pua if _is_undecoded(ch)}


def describe_gid(gid: int) -> str:
    """What our table currently claims this id means."""
    roles = []
    if gid in HALF_GIDS:
        roles.append("half")
    if gid in PREBASE_GIDS:
        roles.append("prebase")
    if gid in REPHA_GIDS:
        roles.append("repha")
    if gid in STEM_GIDS:
        roles.append("stem")
    if gid in JOINER_GIDS:
        roles.append("joiner")
    if gid in IGNORE_GIDS:
        roles.append("ignored")
    mapped = GID_MAP.get(gid)
    label = repr(mapped) if mapped is not None else (
        "-" if gid in KNOWN_GIDS else "UNMAPPED"
    )
    return f"{label}{' [' + ','.join(roles) + ']' if roles else ''}"


# A cell whose two readings differ this wildly in length is not a glyph
# disagreement, it is an alignment failure -- Vision text from a neighbouring
# line or a section heading landing in this cell. Counted and reported rather
# than scored, so contamination stays visible instead of being blamed on ids.
LEN_RATIO = 2.0
LEN_SLACK = 4


class Tally:
    """Evidence collected from the corpus, and the counts derived from it.

    The evidence unit is a DISTINCT NAME STRING PER AC, not a row. Village
    rolls repeat surnames heavily, and a per-row unit lets one part's one
    surname carry a whole id: a nine-page smoke run flagged gid 151 at z=14.1
    purely because AC039/part0050 is dominated by কর্ম্মকার, which we decode
    correctly and Vision misreads the same way every single time. Deduping
    turns that from 14 sigma into one observation, which is what it is.

    `add` only RECORDS -- every count is derived in `finalize`, once, after
    all workers have been merged. Counting in the workers instead would
    double-count any name that appears in two parts of the same AC when
    those parts land on different workers, and no amount of adjusting the
    scalars at merge time recovers which of the two disagreed.
    """

    def __init__(self):
        self.records = {}                      # (ac, pua) -> (ours, theirs, where)
        self.skipped_unmapped = 0
        self.skipped_no_vision = 0
        self.skipped_alignment = 0
        self.skipped_duplicate = 0
        # derived by finalize()
        self.cells = 0
        self.disagree = 0
        self.diff_chars = 0
        self.in_cells = collections.Counter()
        self.in_disagreeing = collections.Counter()
        self.acs_disagreeing = collections.defaultdict(set)
        self.char_blame = collections.Counter()
        self.examples = collections.defaultdict(list)

    def add(self, pua, ours, theirs, missing, where):
        if missing:
            self.skipped_unmapped += 1
            return
        if not _nows(theirs):
            self.skipped_no_vision += 1
            return
        a, b = len(_nows(ours)), len(_nows(theirs))
        if max(a, b) > LEN_RATIO * min(a, b) + LEN_SLACK:
            self.skipped_alignment += 1
            return
        key = (where.split("/", 1)[0], pua)
        if key in self.records:
            self.skipped_duplicate += 1
            return
        self.records[key] = (ours, theirs, where)

    def merge(self, other):
        for key, rec in other.records.items():
            if key in self.records:
                self.skipped_duplicate += 1
            else:
                self.records[key] = rec
        self.skipped_unmapped += other.skipped_unmapped
        self.skipped_no_vision += other.skipped_no_vision
        self.skipped_alignment += other.skipped_alignment
        self.skipped_duplicate += other.skipped_duplicate

    def finalize(self):
        for (ac, pua), (ours, theirs, where) in self.records.items():
            self.cells += 1
            present = gids_of(pua)
            self.in_cells.update(present)
            if _nows(ours) == _nows(theirs):
                continue
            self.disagree += 1
            self.in_disagreeing.update(present)
            hit, chars = implicated(pua, ours, theirs)
            self.diff_chars += chars
            self.char_blame.update(hit)
            # Breadth is counted over the same population the rate is: every
            # gid PRESENT in a disagreeing cell, not only the ones the
            # character-level diff could pin.  Counting it over `hit` instead
            # made the two halves of a row answer different questions, and
            # it buried a true positive: gid 206 (a pre-base matra, which
            # `implicated` can never blame because it contributes no
            # character of its own at its own position) came back 128/128
            # cells disagreeing with `acs=0`, was filed under "confined to
            # one or two constituencies", and carried no examples to read.
            # It is in fact wrong in every AC it appears in.
            for gid in present:
                self.acs_disagreeing[gid].add(ac)
                if len(self.examples[gid]) < 6:
                    self.examples[gid].append((where, ours, theirs))
        return self


# ----------------------------------------------------------------- sampling

META = Path(__file__).resolve().parent.parent / "states" / "meta" / "west_bengal_ac_meta.json"


def _members(zip_path: Path) -> list[str]:
    """The part PDFs inside one AC's zip, in order.

    Read from the zip rather than from the AC metadata's `filename` field:
    the metadata records the name the CEO site serves a part under
    (`AC001PART001.pdf`) while the downloader stores it as `part0001.pdf`, and
    `WestBengalConnector.parse_raw` reads the member names too. Trusting the
    metadata here silently selected nothing at all.
    """
    with zipfile.ZipFile(zip_path) as z:
        return sorted(n for n in z.namelist() if n.lower().endswith(".pdf"))


def choose_parts(raw_dir: Path, n_parts: int, seed: int):
    """Parts to sample, spread over districts and weighted to North Bengal.

    Round-robin over districts rather than uniform over ACs: sampling ACs
    uniformly would hand a quarter of the corpus to the two 28-AC districts
    and leave Darjeeling and the Dinajpurs with almost nothing, which is the
    opposite of what a search for a rare mis-mapping wants.
    """
    meta = json.loads(META.read_text())
    avail = [a for a in meta if (raw_dir / f"{a['ac_code']}.zip").exists()]
    if not avail:
        raise RuntimeError(f"no West Bengal zips under {raw_dir}")

    rng = random.Random(seed)
    north = [a for a in avail if a["district"] in NORTH_BENGAL]
    rest = [a for a in avail if a["district"] not in NORTH_BENGAL]
    n_north = round(n_parts * NORTH_BENGAL_SHARE) if north else 0

    listing: dict[str, list[str]] = {}
    picks, seen = [], set()
    for pool, want in ((north, n_north), (rest, n_parts - n_north)):
        if not pool or want <= 0:
            continue
        by_district = collections.defaultdict(list)
        for ac in pool:
            by_district[ac["district"]].append(ac)
        districts = sorted(by_district)
        rng.shuffle(districts)
        got, tries = 0, 0
        while got < want and tries < want * 40:
            ac = rng.choice(by_district[districts[tries % len(districts)]])
            tries += 1
            code = ac["ac_code"]
            if code not in listing:
                listing[code] = _members(raw_dir / f"{code}.zip")
            if not listing[code]:
                continue
            member = rng.choice(listing[code])
            if (code, member) in seen:
                continue
            seen.add((code, member))
            picks.append((ac, member))
            got += 1
    return picks


# --------------------------------------------------------------------- run

def _sample(out: Path, raw_dir: Path, n_parts: int, seed: int):
    """The sample for a given --out, drawn once and then reused verbatim.

    `choose_parts` is a function of the seed AND of which zips are on disk, so
    a seed alone does not pin a sample: the corpus grew under a re-run here
    (another job was still downloading) and the "same" seed selected 77 ACs
    where the original run had 73, missing the response cache entirely and
    reporting zero cells scored. The seed's own help text promised the
    opposite -- that re-running it re-reads the same pages for free. Writing
    the draw down next to the cache it indexes is what actually makes that
    true, and it is the difference between a fixture and a fresh draw that
    happens to look like one.

    Nothing rewrites an existing sample.json: a run whose --out already has
    one gets that sample even if the seed on the command line differs, and
    says so, because silently redrawing is the failure this exists to stop.

    The SEED comes back with the picks, and the caller must use it in place of
    the one on the command line. The picks pin which parts are read; the seed
    pins which PAGES are read out of each of them (`do_part` shuffles a part's
    pages under it), so restoring one without the other reuses the cache for a
    different set of pages and produces a report that is not comparable to the
    one beside it. That is not hypothetical: an identical sample.json scored
    4,619 rows under the seed it was drawn with and 11,208 under the command
    line's default, and nothing in either report said they were different
    pages.
    """
    path = out / "sample.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if saved["seed"] != seed or saved["n_parts"] != n_parts:
            _say(f"  reusing the sample already drawn in {out} "
                 f"(seed {saved['seed']}, {saved['n_parts']} parts) -- "
                 f"--seed {seed}/--pages implying {n_parts} parts ignored")
        return ([(a, m) for a, m in (tuple(x) for x in saved["picks"])],
                saved["seed"])

    picks = choose_parts(raw_dir, n_parts, seed)
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"seed": seed, "n_parts": n_parts,
         "picks": [[a, m] for a, m in picks]}, ensure_ascii=False))
    return picks, seed


def _cache_path(out: Path, ac_code: str, part_stem: str, page: int) -> Path:
    return out / "responses" / ac_code / part_stem / f"p{page:04d}.json.gz"


def _load_cached(path: Path):
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _store(path: Path, resp: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(resp, fh)
    tmp.replace(path)


def do_part(ac, member, raw_dir, out, dpi, pages_per_part, project, dry_run,
            seed, report_only=False, engine=None):
    """Score up to `pages_per_part` pages of one part. Returns (Tally, stats)."""
    engine = engine or VisionEngine()
    tally = Tally()
    stats = {"pages": 0, "billed": 0, "skipped_font": 0, "rows": 0}
    zip_path = raw_dir / f"{ac['ac_code']}.zip"
    stem = Path(member).stem
    with zipfile.ZipFile(zip_path) as z:
        # No try/except: a member the sampler chose out of this zip's own
        # listing must be readable, and swallowing a KeyError here is how an
        # earlier cut reported "0 pages selected" as though the corpus were
        # empty.
        pdf_bytes = z.read(member)

    rng = random.Random(f"{seed}:{ac['ac_code']}:{stem}")
    wanted: dict[int, list] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = len(pdf.pages)
        if n < 2:
            return tally, stats

        # Which font this part is set in, decided exactly the way
        # WestBengalConnector._parse_part decides it. A Darjeeling part uses a
        # different glyph space whose ids overlap this one's, so scoring it
        # here would blame this table for a mismatch it never caused.
        shreelipi = None
        for page in pdf.pages:
            if not page.chars:
                continue
            text = page.extract_text() or ""
            if _has_undecoded(text):
                shreelipi = looks_like_shreelipi(text)
                break
        if not shreelipi:
            stats["skipped_font"] = 1
            return tally, stats

        # Geometry carries forward page to page, so the pages have to be
        # walked in order even though only a few of them are scored.
        order = list(range(2, n + 1))
        rng.shuffle(order)
        chosen = set(order[:pages_per_part * 3])   # over-pick: some carry no table

        geometry = None
        for i, page in enumerate(pdf.pages, start=1):
            incoming = geometry
            rows, geometry = page_cells(page, geometry)
            if i not in chosen or len(wanted) >= pages_per_part:
                continue
            verify_against_connector(page, incoming, rows)
            rows = [r for r in rows
                    if any(r["cells"][c]["text"] for c in DECODED_COLUMNS)]
            if rows:
                wanted[i] = (rows, float(page.width), float(page.height))

    if not wanted:
        return tally, stats

    pages = sorted(wanted)
    to_bill = [p for p in pages
               if not _cache_path(out, ac["ac_code"], stem, p).exists()]
    stats["pages"] = len(pages)
    stats["billed"] = len(to_bill)
    if dry_run:
        return tally, stats

    # --report-only rescores what is already cached and submits nothing. It
    # was originally folded in with --dry-run, which returns above -- so it
    # printed "no cells scored", wrote that empty result over report.json,
    # and cost the full report it claimed to be rebuilding. A flag whose help
    # text says "rebuild from cache" has to actually read the cache.
    if report_only:
        pages = [p for p in pages
                 if _cache_path(out, ac["ac_code"], stem, p).exists()]
        if not pages:
            return tally, stats
        to_bill = []

    rendered = render_pages(pdf_bytes, to_bill, dpi) if to_bill else {}
    for page_no in pages:
        cache = _cache_path(out, ac["ac_code"], stem, page_no)
        resp = _load_cached(cache)
        if resp is None:
            resp = engine.read(rendered[page_no], project)
            _store(cache, resp)
        words, pw, ph = engine.words(resp)
        rows, w_pt, h_pt = wanted[page_no]
        if not words or not pw or not ph:
            continue
        attach_vision(rows, words, pw / w_pt, ph / h_pt)
        stats["rows"] += len(rows)
        for r in rows:
            for col in DECODED_COLUMNS:
                cell = r["cells"][col]
                pua = cell["text"]
                if not pua or not _has_undecoded(pua):
                    continue
                ours, missing = decode(pua)
                tally.add(pua, ours, cell.get("vision_text", ""), missing,
                          f"{ac['ac_code']}/{stem}/p{page_no}")
    return tally, stats


# ------------------------------------------------------------------ report

# A glyph seen in fewer cells than this cannot separate a real mis-mapping
# from a run of bad luck, however extreme its rate looks.
MIN_CELLS = 50

# ~250 ids are tested at once, so an ordinary 2-sigma bar would flag a
# handful of them on noise alone every run. z >= 6 is p ~ 1e-9 -- survives
# that multiplicity with room to spare, and is the level at which "this id is
# concentrated" stops being a judgement call.
Z_FLAG = 6.0
LIFT_FLAG = 1.5

# ...and it must be wrong in more than one constituency. Per-AC deduping stops
# one repeated surname from carrying an id, but a name can repeat across the
# parts of a single AC's own district too. A real mis-mapping is a property of
# the glyph table, so it shows up wherever that glyph is used; a Vision
# weakness on one local surname does not travel.
MIN_ACS = 3


def _z(rate, base, n):
    if not n or base <= 0 or base >= 1:
        return 0.0
    return (rate - base) / math.sqrt(base * (1 - base) / n)


def build_report(tally: Tally, sample: dict) -> dict:
    tally.finalize()
    base = tally.disagree / tally.cells if tally.cells else 0.0
    rows = []
    for gid, n in tally.in_cells.items():
        d = tally.in_disagreeing.get(gid, 0)
        rate = d / n if n else 0.0
        rows.append({
            "gid": gid,
            "meaning": describe_gid(gid),
            "cells": n,
            "disagreeing": d,
            "rate": rate,
            "lift": (rate / base) if base else 0.0,
            "z": _z(rate, base, n),
            "acs": len(tally.acs_disagreeing.get(gid, ())),
            "char_blame": tally.char_blame.get(gid, 0),
            "char_share": (tally.char_blame.get(gid, 0) / tally.diff_chars)
                          if tally.diff_chars else 0.0,
        })
    rows.sort(key=lambda r: (-r["z"], -r["lift"]))
    flagged = [r for r in rows
               if r["cells"] >= MIN_CELLS and r["z"] >= Z_FLAG
               and r["lift"] >= LIFT_FLAG and r["acs"] >= MIN_ACS]
    # Kept separate so a narrow-but-extreme id is visible as a thing ruled out
    # rather than silently absent: that is exactly the shape gid 151 had.
    narrow = [r for r in rows
              if r["cells"] >= MIN_CELLS and r["z"] >= Z_FLAG
              and r["lift"] >= LIFT_FLAG and r["acs"] < MIN_ACS]
    return {
        "sample": sample,
        "cells": tally.cells,
        "disagree": tally.disagree,
        "base_rate": base,
        "skipped_unmapped": tally.skipped_unmapped,
        "skipped_no_vision": tally.skipped_no_vision,
        "skipped_alignment": tally.skipped_alignment,
        "skipped_duplicate": tally.skipped_duplicate,
        "diff_chars": tally.diff_chars,
        "gids": rows,
        "flagged": flagged,
        "narrow": narrow,
        "examples": {str(g): v for g, v in tally.examples.items()},
    }


def print_report(rep: dict) -> None:
    s = rep["sample"]
    print()
    print("=" * 78)
    print("decoder oracle -- West Bengal Shree-Lipi glyph table")
    print("=" * 78)
    # Seed and scored-cell count lead the report because they are what makes
    # two runs comparable, and nothing else in it says so. The seed selects
    # which PAGES of each part get scored, not just which parts are sampled,
    # so the same sample.json under two seeds is two different measurements:
    # 4,619 cells against 11,208 once, with both reports otherwise identical
    # in appearance. Print them where a reader cannot skip past them.
    print(f"run           : seed {s.get('seed', '?')} -- "
          f"{rep['cells']:,} name cells scored "
          f"(compare only against a run with both the same)")
    print(f"sample        : {s['parts']} parts, {s['pages']} pages, "
          f"{s['acs']} ACs, {s['districts']} districts "
          f"(North Bengal {int(NORTH_BENGAL_SHARE * 100)}% by design)")
    print(f"                {s['skipped_font']} parts skipped: not Shree-Lipi")
    print(f"                rendered locally at {s['dpi']} dpi with the "
          f"embedded font; the engine read pixels, never a text layer")
    print(f"read by       : {s.get('engine', 'vision')}")
    if not rep["cells"]:
        print("\nno cells scored -- nothing to report")
        return
    base = rep["base_rate"]
    print(f"\nname cells scored : {rep['cells']:,}")
    print(f"  agreeing        : {rep['cells'] - rep['disagree']:,} "
          f"({100 * (1 - base):.1f}%)")
    print(f"  disagreeing     : {rep['disagree']:,} ({100 * base:.1f}%)"
          "   <- our errors AND Vision's, mixed")
    print(f"  not scored      : {rep['skipped_unmapped']:,} unmapped glyph "
          f"(already known), {rep['skipped_no_vision']:,} Vision read "
          f"nothing, {rep['skipped_alignment']:,} lengths too far apart to "
          f"be the same cell, {rep['skipped_duplicate']:,} repeats of a name "
          f"already counted in that AC")
    print("  evidence unit   : one distinct name string per AC. A per-row "
          "unit lets one\n                    village's repeated surname "
          "manufacture a six-sigma id.")

    print(f"\nper-glyph residual, ranked by concentration (flag: "
          f"cells>={MIN_CELLS}, z>={Z_FLAG}, lift>={LIFT_FLAG}, ACs>={MIN_ACS})")
    print(f"{'gid':>5} {'our table says':<22} {'cells':>7} {'disag':>7} "
          f"{'rate':>7} {'lift':>6} {'z':>7} {'ACs':>4} {'blame':>7}")
    print("-" * 78)
    for r in rep["gids"][:25]:
        print(f"{r['gid']:>5} {r['meaning'][:22]:<22} {r['cells']:>7,} "
              f"{r['disagreeing']:>7,} {100 * r['rate']:>6.1f}% "
              f"{r['lift']:>6.2f} {r['z']:>7.1f} {r['acs']:>4} "
              f"{100 * r['char_share']:>6.1f}%")

    print()
    if rep["flagged"]:
        print(f"VERDICT: NOT CONVERGED -- {len(rep['flagged'])} glyph id(s) "
              f"own a concentration of the residual.")
        print("These are candidates for a fourth decoder round. Publishing "
              "before resolving them repeats the exact failure this check "
              "was built to catch.")
        for r in rep["flagged"]:
            print(f"\n  gid {r['gid']} -- table says {r['meaning']}")
            print(f"    {r['disagreeing']:,}/{r['cells']:,} cells disagree "
                  f"({100 * r['rate']:.1f}% vs {100 * base:.1f}% corpus), "
                  f"lift {r['lift']:.2f}, z {r['z']:.1f}")
            for where, ours, theirs in rep["examples"].get(str(r["gid"]), [])[:4]:
                print(f"      {where}\n        ours   {ours}\n        vision {theirs}")
    else:
        print("VERDICT: CONVERGED, on this evidence.")
        print(f"No glyph id clears the bar. The {100 * base:.1f}% "
              "disagreement is spread across ids in proportion to how often "
              "they occur, which is what OCR noise looks like and is not "
              "what a mis-mapped id looks like.")
        print(f"Bounded by the sample: {rep['cells']:,} distinct names. "
              "This test can only see a\nmis-mapping that changes enough "
              "characters for difflib to attribute it, and only\nin glyphs "
              "the sample actually exercised.")
        top = rep["gids"][0] if rep["gids"] else None
        if top:
            print(f"Strongest residual is gid {top['gid']} "
                  f"({top['meaning']}) at lift {top['lift']:.2f}, z "
                  f"{top['z']:.1f} over {top['cells']:,} cells -- below the "
                  "bar, but it is where a fourth round would start looking.")
    if rep["narrow"]:
        print(f"\nRuled out for lack of breadth ({MIN_ACS}+ ACs required) -- "
              "extreme, but confined to\none or two constituencies, which is "
              "what a Vision weakness on a local surname\nlooks like, not a "
              "glyph table defect:")
        for r in rep["narrow"]:
            print(f"  gid {r['gid']:>4} {r['meaning'][:22]:<22} z "
                  f"{r['z']:>6.1f} across {r['acs']} AC(s)")
            for where, ours, theirs in rep["examples"].get(str(r["gid"]), [])[:2]:
                print(f"       {where}  ours {ours}  |  vision {theirs}")

    print()
    print("This is evidence about the glyph table only. It says nothing "
          "about row segmentation, column assignment or any non-name column.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", default="data/raw/west_bengal", type=Path)
    ap.add_argument("--out", default="data/oracle/west_bengal", type=Path)
    ap.add_argument("--pages", type=int, default=300,
                    help="page budget; the bill is $1.50/1000 with 1000 free "
                         "per month (default: %(default)s)")
    ap.add_argument("--pages-per-part", type=int, default=3,
                    help="pages sampled from each part -- lower spreads the "
                         "sample wider for the same bill (default: %(default)s)")
    ap.add_argument("--dpi", type=int, default=RENDER_DPI)
    ap.add_argument("--seed", type=int, default=20260825,
                    help="the sample is a fixture, not a fresh draw: the same "
                         "seed re-reads the same pages out of cache for free")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--project", default=os.environ.get(
        "GOOGLE_CLOUD_PROJECT", "oldvoterlist-prod"))
    ap.add_argument("--engine", default=VisionEngine.name,
                    choices=sorted(ENGINES),
                    help="which OCR engine reads the rendered pages "
                         "(default: %(default)s). A response cache belongs to "
                         "one engine; use a separate --out per engine.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the page-exact bill and submit nothing")
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild the report from cached responses; bills nothing")
    args = ap.parse_args(argv)

    engine = _engine_for(args.out, args.engine)
    n_parts = max(1, -(-args.pages // args.pages_per_part))
    picks, args.seed = _sample(args.out, args.raw_dir, n_parts, args.seed)
    _say(f"sampling {len(picks)} parts across "
         f"{len({a['ac_code'] for a, _ in picks})} ACs, "
         f"{len({a['district'] for a, _ in picks})} districts")

    tally = Tally()
    agg = {"pages": 0, "billed": 0, "skipped_font": 0, "rows": 0}
    dry = args.dry_run
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(do_part, ac, member, args.raw_dir, args.out, args.dpi,
                        args.pages_per_part, args.project, dry, args.seed,
                        args.report_only, engine): ac
            for ac, member in picks
        }
        for fut in as_completed(futures):
            t, st = fut.result()
            tally.merge(t)
            for k in agg:
                agg[k] += st[k]
            done += 1
            if done % 10 == 0 or done == len(picks):
                _say(f"  {done}/{len(picks)} parts, {agg['pages']} pages, "
                     f"{len(tally.records):,} names collected")

    if args.dry_run:
        billable = max(0, agg["billed"])
        cost = billable / 1000 * PRICE_PER_1K
        _say(f"\n--dry-run: {agg['pages']} pages selected, {billable} not yet "
             f"cached => ${cost:.2f} at ${PRICE_PER_1K}/1000 "
             f"({FREE_PAGES_PER_MONTH} free/month). Nothing submitted.")
        return 0

    sample = {
        "parts": len(picks),
        "pages": agg["pages"],
        "acs": len({a["ac_code"] for a, _ in picks}),
        "districts": len({a["district"] for a, _ in picks}),
        "skipped_font": agg["skipped_font"],
        "dpi": args.dpi,
        "seed": args.seed,
        "rows": agg["rows"],
        "engine": engine.name,
    }
    rep = build_report(tally, sample)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1))
    print_report(rep)
    _say(f"full detail: {args.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
