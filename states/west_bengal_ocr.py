"""
Bengali name-field OCR for West Bengal's 2002 electoral-roll PDFs.

WHY THIS EXISTS
---------------
See states/west_bengal.py's module docstring for the underlying problem: the
part PDFs carry a real text layer, but no font in them has a /ToUnicode CMap,
so character codes map to arbitrary TrueType glyph ids. For the ~19 Latin-
typeset Kolkata ACs the glyph ids happen to follow standard Macintosh order
and decode directly. For the other ~275 Bengali-typeset ACs only the closed
sets could be recovered by reverse-engineering (digits at the Macintosh digit
slots, and the relation/gender columns matched by whole-cell glyph sequence).

The open-vocabulary columns -- elector name and relative name -- have no such
closed set to match against, so they stayed undecoded, emitted as private-use
code points with a remark. That is what this module fills in, by going around
the broken glyph->Unicode mapping entirely: render the glyphs to pixels and
read them back with a Bengali OCR model.

THE GEOMETRY IS ALREADY KNOWN -- OCR NEVER HAS TO REDISCOVER IT
---------------------------------------------------------------
Nothing here infers the table layout from pixels. The text layer is present
and correctly *positioned*; it is only the character mapping that is broken.
So west_bengal.py's parser already knows every name cell's bounding box to a
fraction of a point, straight from the PDF's own text-drawing coordinates, and
hands those rects here. Whatever the engine returns is mapped back onto those
rects, which makes the result-to-row mapping exact by construction instead of
inferred from OCR's own reading order.

That is why this is not "whole-page OCR with layout correlation" even in the
Vision path, which does submit a whole page: the page is only the *unit of
recognition*: attribution is still per-cell, by rect.

Coordinates are pdfplumber's (points, y from the page top). These PDFs are
unrotated with the mediabox at the origin, so they are also PyMuPDF's --
asserted at render time rather than assumed, since a rotated page would
silently read the wrong region.

ENGINE CHOICE (measured, not assumed)
-------------------------------------
Two engines are implemented; `vision` is the default. All numbers below are on
the same 45-name ground truth (AC001 part 1 page 2, hand-transcribed by eye
from the rendered page):

    engine / model                    exact    CER
    ---------------------------------------------
    Cloud Vision (bn hint)            43/45    0.011     <- default
    tesseract, tessdata_best ben      39/45    0.023
    tesseract, tessdata_fast ben      24/45    0.071
    tesseract, indic-ocr ben          10/45    0.338

Indic-OCR's Bengali model (https://indic-ocr.github.io/tessdata/) is *not*
broken -- it installs and runs -- it is simply much worse on this typeface,
plausibly because its models are trained on Noto and Sakal Bharati while these
2002 rolls are set in a different Bengali face.

Cloud Vision costs about $1.50 per 1000 images and everything else here is
free, so the choice is a real tradeoff rather than a free lunch; see COST
below before running a full state.

WHY VISION SUBMITS A PAGE AND TESSERACT SUBMITS A CROP
------------------------------------------------------
The two engines are billed and bottlenecked completely differently, so the
same batching strategy would be wrong for both.

Vision bills per *image*, so a crop per cell would be ~90 billable images per
page instead of 1 -- around a 60x cost multiplier for the identical text, since
a page carries ~45 rows x 2 name columns. It therefore renders the page once,
asks for DOCUMENT_TEXT_DETECTION, and assigns each returned word to a cell by
testing whether the word's centre falls inside that cell's rect. Pages with no
cells to read are never sent at all, so a Latin-typeset Kolkata AC costs
nothing.

Tesseract is free but pays a fixed ~250ms model load per invocation, so there
the win is the opposite one: crop each cell, and batch the crops into a single
invocation per part via tesseract's file-list input mode (~15ms amortized per
cell). Each input file still gets its own form-feed-separated output block, so
the 1:1 crop->result mapping survives batching.

TESSERACT'S PADDING IS ASYMMETRIC, AND THAT IS LOAD-BEARING
-----------------------------------------------------------
pdfplumber's per-character bbox tracks the font's declared ascent/descent,
which for this typeface sits above the below-baseline vowel signs -- the
u-kar/ru-kar hooks (Bengali "u" in সুন্দরী, কুলবালা, ডাকুয়া) render *below*
the reported box. Cropping to the reported box shears them off, and the OCR
then reads সুন্দরী as সন্দরী -- a silent, plausible-looking wrong name rather
than an obvious failure. Symmetric 1.0pt padding scored 26/45 exact, CER
0.060; extending only the bottom to 3.0pt scored 39/45, CER 0.023. Hence
PAD_BOTTOM >> PAD_TOP. The Vision path submits whole pages and so never has
this problem.

ASSAMESE RA
-----------
Vision, even hinted `bn`, spells Bengali ra (র U+09B0) as Assamese ra
(ৰ U+09F0) in a good fraction of conjuncts -- কীৰ্ত্তনীয়া for কীর্ত্তনীয়া,
বৰ্ম্মন for বর্ম্মন. It is a script-neighbour confusion, not a reading error:
the two letters are separate code points for the same sound, and ৰ/ৱ simply do
not occur in Bengali orthography. Folding them back is worth 4 of the 45 names
on its own (39/45 -> 43/45, CER 0.021 -> 0.011). It is applied to Vision
output only, since Tesseract's `ben` model never emits them.

RENDER SCALE
------------
Vision was measured at 1.5x/2.0x/3.0x render zoom: 42, 43 and 43 of 45 exact.
2.0 is the knee -- 3.0 buys nothing and sends 60% more bytes -- so that is the
default.

COST, FOR WHOEVER RUNS THE FULL STATE
-------------------------------------
Vision's DOCUMENT_TEXT_DETECTION is ~$1.50 per 1000 images, billed per image
regardless of how many are batched into one HTTP request. One image here is
one *page that has names on it*. A part PDF averages ~16 such pages, an AC
averages ~150 parts, and there are ~275 Bengali-typeset ACs, so a full
statewide run is on the order of 660K images -- roughly $1,000, and many hours
of wall clock even at WB_OCR_WORKERS=6. Budget for it deliberately. Setting
WB_OCR_ENGINE=tesseract makes the same run free and somewhat less accurate.

CONFIGURATION
-------------
    WB_OCR=1                  turn OCR on at all (see west_bengal.py)
    WB_OCR_ENGINE=vision      `vision` (default) or `tesseract`
    WB_OCR_WORKERS=6          parts OCR'd concurrently (default 6)
    WB_OCR_ZOOM=2.0           render scale
    GCP_PROJECT=...           billing/quota project for Vision
    WB_OCR_LANG=ben           tesseract model name, tesseract engine only
"""
import base64
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ENGINE = os.environ.get("WB_OCR_ENGINE", "vision").strip().lower()
DEFAULT_WORKERS = 6

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
GCP_PROJECT = os.environ.get("GCP_PROJECT", "oldvoterlist-prod")
# Vision accepts up to 16 images per call; billing is per image either way, so
# this only trades HTTP round trips. 8 keeps a request comfortably small.
VISION_BATCH = 8
VISION_ATTEMPTS = 5

ZOOM = float(os.environ.get("WB_OCR_ZOOM", "2.0"))

LANG = os.environ.get("WB_OCR_LANG", "ben")
TESSERACT = os.environ.get("WB_OCR_TESSERACT", "tesseract")
TESS_ZOOM = 5.0   # crops are small; the LSTM wants ~60px of line height
PSM = "7"         # treat each crop as one text line
PAD_X = 2.0
PAD_TOP = 0.5
PAD_BOTTOM = 3.0  # see module docstring -- below-baseline vowel signs

# how far outside its own rect a word's centre may sit and still be counted as
# that cell's. Small: the column gutters in this table are several points wide,
# so this cannot reach a neighbouring column.
SNAP = 1.0

_PAGE_SEP = "\f"   # tesseract's per-input-file output separator

# ৰ/ৱ (Assamese ra/wa) do not occur in Bengali orthography -- see module docstring
_ASSAMESE_FOLD = {0x09F0: "র", 0x09F1: "ব"}


class OcrUnavailableError(RuntimeError):
    """Raised when OCR was asked for but the engine isn't usable."""


def workers():
    """How many parts to OCR concurrently."""
    try:
        n = int(os.environ.get("WB_OCR_WORKERS", DEFAULT_WORKERS))
    except ValueError:
        return DEFAULT_WORKERS
    return max(1, n)


def ensure_available():
    """Raise with an actionable message unless the selected engine is usable."""
    if ENGINE == "vision":
        _access_token()
    elif ENGINE == "tesseract":
        _ensure_tesseract()
    else:
        raise OcrUnavailableError(
            f"WB_OCR_ENGINE={ENGINE!r} is not a known engine (expected "
            "'vision' or 'tesseract')"
        )


def is_available():
    try:
        ensure_available()
    except OcrUnavailableError:
        return False
    return True


def ocr_cells(pdf_bytes, cells):
    """OCR a batch of name cells out of one part PDF.

    `cells` is a list of (page_index, [(x0, top, x1, bottom), ...]) -- a list of
    rects per cell rather than one, because a long name wraps onto a second
    line and each line is its own run of glyphs. Returns one string per cell,
    in the order given, with a wrapped name's lines joined by a space.
    """
    if not cells:
        return []
    ensure_available()
    if ENGINE == "vision":
        return _ocr_cells_vision(pdf_bytes, cells)
    return _ocr_cells_tesseract(pdf_bytes, cells)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _open(pdf_bytes):
    import pymupdf
    return pymupdf.open(stream=pdf_bytes, filetype="pdf")


def _check_page(page, page_no):
    if page.rotation or page.rect.x0 or page.rect.y0:
        raise ValueError(
            f"page {page_no} is rotated/offset (rotation={page.rotation}, "
            f"rect={page.rect}); the rects from pdfplumber would not line up"
        )


def _render_page(doc, page_no, zoom, clip=None):
    import pymupdf
    page = doc[page_no]
    _check_page(page, page_no)
    matrix = pymupdf.Matrix(zoom, zoom)
    if clip is not None:
        return page.get_pixmap(matrix=matrix, clip=pymupdf.Rect(*clip))
    return page.get_pixmap(matrix=matrix)


# --------------------------------------------------------------------------
# Cloud Vision
# --------------------------------------------------------------------------

_token_lock = threading.Lock()
_token = None


def _access_token(refresh=False):
    """The gcloud CLI's current access token, fetched once and reused.

    Deliberately shells out to `gcloud auth print-access-token` rather than
    using Application Default Credentials: the accounts that run this are
    logged in interactively, and ADC would need a separate setup step.
    """
    global _token
    with _token_lock:
        if _token and not refresh:
            return _token
        if shutil.which("gcloud") is None:
            raise OcrUnavailableError(
                "`gcloud` not found on PATH, and the Cloud Vision engine "
                "authenticates with it. Install the Google Cloud CLI and run "
                "`gcloud auth login`, or set WB_OCR_ENGINE=tesseract."
            )
        proc = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise OcrUnavailableError(
                "`gcloud auth print-access-token` failed -- run `gcloud auth "
                f"login` (or set WB_OCR_ENGINE=tesseract). {proc.stderr.strip()[:300]}"
            )
        _token = proc.stdout.strip()
        return _token


def _vision_post(payload):
    """POST to Vision, retrying the failures that are worth retrying.

    A user-credential token needs an explicit quota project (x-goog-user-project),
    otherwise Vision answers 403 SERVICE_DISABLED against gcloud's own shared
    project rather than the caller's.
    """
    body = json.dumps(payload).encode()
    for attempt in range(VISION_ATTEMPTS):
        req = urllib.request.Request(
            VISION_URL, data=body,
            headers={
                "Authorization": f"Bearer {_access_token()}",
                "x-goog-user-project": GCP_PROJECT,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 401 and attempt == 0:
                _access_token(refresh=True)
                continue
            # 429/5xx are Vision's own rate limiting and transient faults
            if e.code in (429, 500, 502, 503, 504) and attempt < VISION_ATTEMPTS - 1:
                time.sleep(2 ** attempt + random.random())
                continue
            raise OcrUnavailableError(
                f"Cloud Vision returned {e.code}: {detail}"
            ) from e
        except urllib.error.URLError as e:
            if attempt < VISION_ATTEMPTS - 1:
                time.sleep(2 ** attempt + random.random())
                continue
            raise OcrUnavailableError(f"Cloud Vision unreachable: {e}") from e
    raise OcrUnavailableError("Cloud Vision retries exhausted")


def _ocr_cells_vision(pdf_bytes, cells):
    by_page = {}
    for i, (page_no, rects) in enumerate(cells):
        if rects:
            by_page.setdefault(page_no, []).append(i)

    results = [""] * len(cells)
    page_nos = sorted(by_page)
    doc = _open(pdf_bytes)
    try:
        rendered = [
            (p, _render_page(doc, p, ZOOM).tobytes("png")) for p in page_nos
        ]
    finally:
        doc.close()

    for start in range(0, len(rendered), VISION_BATCH):
        chunk = rendered[start:start + VISION_BATCH]
        payload = {"requests": [
            {
                "image": {"content": base64.b64encode(png).decode()},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["bn"]},
            }
            for _, png in chunk
        ]}
        responses = _vision_post(payload).get("responses", [])
        if len(responses) != len(chunk):
            raise OcrUnavailableError(
                f"Cloud Vision returned {len(responses)} responses for "
                f"{len(chunk)} pages; cannot map results back to cells"
            )
        for (page_no, _), resp in zip(chunk, responses):
            if "error" in resp:
                raise OcrUnavailableError(
                    f"Cloud Vision error on page {page_no}: "
                    f"{resp['error'].get('message', resp['error'])}"
                )
            words = _vision_words(resp)
            for i, text in _assign(words, [cells[i] for i in by_page[page_no]],
                                   by_page[page_no]):
                results[i] = text
    return results


def _vision_words(resp):
    """[(text, x0, top, x1, bottom)] in PDF points, from one page's response."""
    fta = resp.get("fullTextAnnotation")
    if not fta:
        return []
    out = []
    for page in fta.get("pages", []):
        for block in page.get("blocks", []):
            for para in block.get("paragraphs", []):
                for word in para.get("words", []):
                    text = "".join(s.get("text", "") for s in word.get("symbols", []))
                    if not text.strip():
                        continue
                    verts = word.get("boundingBox", {}).get("vertices", [])
                    if not verts:
                        continue
                    xs = [v.get("x", 0) for v in verts]
                    ys = [v.get("y", 0) for v in verts]
                    out.append((
                        text.translate(_ASSAMESE_FOLD),
                        min(xs) / ZOOM, min(ys) / ZOOM,
                        max(xs) / ZOOM, max(ys) / ZOOM,
                    ))
    return out


def _assign(words, page_cells, indices):
    """Attach each word to the one cell rect its centre falls in.

    A word belongs to at most one cell -- first rect that claims it wins -- so
    an overlapping pair of rects can never duplicate a word into two voters'
    names. Words are then ordered by (which line of a wrapped cell, then x),
    which is the reading order the row was drawn in.
    """
    picked = [[] for _ in page_cells]
    for text, x0, top, x1, bottom in words:
        cx, cy = (x0 + x1) / 2, (top + bottom) / 2
        for slot, (_, rects) in enumerate(page_cells):
            hit = next(
                (
                    line
                    for line, (rx0, rtop, rx1, rbottom) in enumerate(rects)
                    if rx0 - SNAP <= cx <= rx1 + SNAP
                    and rtop - SNAP <= cy <= rbottom + SNAP
                ),
                None,
            )
            if hit is not None:
                picked[slot].append((hit, x0, text))
                break
    for slot, got in enumerate(picked):
        got.sort()
        yield indices[slot], " ".join(t for _, _, t in got)


# --------------------------------------------------------------------------
# Tesseract
# --------------------------------------------------------------------------

def _ensure_tesseract():
    if shutil.which(TESSERACT) is None:
        raise OcrUnavailableError(
            f"{TESSERACT!r} not found on PATH. The tesseract engine needs it "
            "(macOS: `brew install tesseract`, Debian: "
            "`apt-get install tesseract-ocr`)."
        )
    langs = subprocess.run(
        [TESSERACT, "--list-langs"], capture_output=True, text=True
    ).stdout.split()
    if LANG not in langs:
        raise OcrUnavailableError(
            f"Tesseract has no {LANG!r} model installed (found: "
            f"{', '.join(sorted(l for l in langs if l != 'List'))}). Install the "
            "Bengali data (macOS: `brew install tesseract-lang`, Debian: "
            "`apt-get install tesseract-ocr-ben`), or drop tessdata_best's "
            "ben.traineddata into your TESSDATA_PREFIX directory."
        )


def _ocr_cells_tesseract(pdf_bytes, cells):
    flat = [(i, rect) for i, (_, rects) in enumerate(cells) for rect in rects]
    results = [[] for _ in cells]

    with tempfile.TemporaryDirectory(prefix="wb_ocr_") as tmp:
        doc = _open(pdf_bytes)
        try:
            paths = []
            for n, (cell_i, rect) in enumerate(flat):
                x0, top, x1, bottom = rect
                clip = (x0 - PAD_X, top - PAD_TOP, x1 + PAD_X, bottom + PAD_BOTTOM)
                path = os.path.join(tmp, f"{n:06d}.png")
                _render_page(doc, cells[cell_i][0], TESS_ZOOM, clip).save(path)
                paths.append(path)
        finally:
            doc.close()

        if not paths:
            return [""] * len(cells)

        listing = os.path.join(tmp, "cells.txt")
        with open(listing, "w", encoding="utf-8") as f:
            f.write("\n".join(paths) + "\n")
        proc = subprocess.run(
            [TESSERACT, listing, "stdout", "-l", LANG, "--psm", PSM],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise OcrUnavailableError(
                f"tesseract failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:400]}"
            )
        blocks = proc.stdout.decode("utf-8", "replace").split(_PAGE_SEP)

    # A trailing separator leaves an empty final block; anything else means the
    # 1:1 mapping this whole approach relies on has broken, so refuse rather
    # than attach OCR'd text to the wrong voter.
    if len(blocks) > len(paths):
        blocks = blocks[: len(paths)]
    if len(blocks) != len(paths):
        raise OcrUnavailableError(
            f"tesseract returned {len(blocks)} blocks for {len(paths)} crops; "
            "cannot map results back to cells"
        )

    for (cell_i, _), block in zip(flat, blocks):
        text = " ".join(block.split())
        if text:
            results[cell_i].append(text)
    return [" ".join(parts) for parts in results]
