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

WHY A CROP PER CELL, NOT A PAGE RASTER
--------------------------------------
Whole-page OCR would have to rediscover the table layout from pixels, and then
correlate each recognized line back to the row it belongs to -- reintroducing
exactly the column/row geometry problem that west_bengal.py already solves
exactly, from the PDF's own text-drawing coordinates.

Nothing here needs to be rediscovered: the text layer is present and correctly
*positioned*, it is only the character mapping that is broken. So the parser
already knows each name cell's bounding box to a fraction of a point. Each
cell is rendered on its own and OCR'd as a single text line (--psm 7), which
keeps the OCR target small and unambiguous and makes the result-to-row mapping
exact by construction rather than inferred from layout.

Crops are batched into one tesseract invocation per part via its file-list
input mode. That matters: loading the model dominates the cost of a single
small crop (~250ms per invocation vs ~15ms amortized), and a part is ~800
cells. Each input file still gets its own page in the output, so the 1:1
crop->result mapping survives batching.

PADDING IS ASYMMETRIC, AND THAT IS LOAD-BEARING
-----------------------------------------------
pdfplumber's per-character bbox tracks the font's declared ascent/descent,
which for this typeface sits above the below-baseline vowel signs -- the
u-kar/ru-kar hooks (Bengali "u" in সুন্দরী, কুলবালা, ডাকুয়া) render *below*
the reported box. Cropping to the reported box shears them off, and the OCR
then reads সুন্দরী as সন্দরী -- a silent, plausible-looking wrong name rather
than an obvious failure. Measured on a 45-name ground truth (AC001 part 1
page 2, transcribed by eye from the rendered page): symmetric 1.0pt padding
scored 26/45 exact, CER 0.060; extending only the bottom to 3.0pt scored
39/45 exact, CER 0.023. Hence PAD_BOTTOM >> PAD_TOP.

ENGINE CHOICE (measured, not assumed)
-------------------------------------
Indic-OCR's Bengali model (https://indic-ocr.github.io/tessdata/) was tried
first and is *not* what this uses. On the same 45-name ground truth, against
stock Tesseract traineddata:

    model                       exact    CER     rel. time
    indic-ocr ben (43MB)        10/45    0.338      2.6x
    tessdata_best ben (11MB)    39/45    0.023      1.0x
    tessdata_fast ben (0.9MB)   24/45    0.071      0.4x

Indic-OCR is not broken -- it installs and runs -- it is simply much worse on
this particular typeface, plausibly because its models are trained on Noto and
Sakal Bharati while these 2002 rolls are set in a different Bengali face. The
default is therefore tessdata_best's `ben`, overridable with WB_OCR_LANG for
anyone who wants to re-test that call on a different AC's typesetting.
"""
import os
import shutil
import subprocess
import tempfile

LANG = os.environ.get("WB_OCR_LANG", "ben")
TESSERACT = os.environ.get("WB_OCR_TESSERACT", "tesseract")

ZOOM = 5.0        # render scale; 5x puts a 12pt line at ~60px, enough for LSTM
PSM = "7"         # treat each crop as one text line
PAD_X = 2.0
PAD_TOP = 0.5
PAD_BOTTOM = 3.0  # see module docstring -- below-baseline vowel signs

# tesseract emits one form-feed-separated block per input file
_PAGE_SEP = "\f"


class OcrUnavailableError(RuntimeError):
    """Raised when OCR was asked for but the engine or model isn't installed."""


def ensure_available():
    """Raise with an actionable message unless tesseract + the model are usable."""
    if shutil.which(TESSERACT) is None:
        raise OcrUnavailableError(
            f"{TESSERACT!r} not found on PATH. West Bengal Bengali-name OCR needs "
            "Tesseract (macOS: `brew install tesseract`, Debian: "
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
    line and each line is its own text line to OCR. Returns one string per
    cell, in the order given, with a wrapped name's lines joined by a space.

    Coordinates are pdfplumber's (points, y from the page top). These PDFs are
    unrotated with the mediabox at the origin, so they are also PyMuPDF's --
    asserted below rather than assumed, since a rotated page would silently
    crop the wrong region.
    """
    import pymupdf

    if not cells:
        return []
    ensure_available()

    flat = [(i, rect) for i, (_, rects) in enumerate(cells) for rect in rects]
    results = [[] for _ in cells]

    with tempfile.TemporaryDirectory(prefix="wb_ocr_") as tmp:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            paths = []
            for n, (cell_i, rect) in enumerate(flat):
                page = doc[cells[cell_i][0]]
                if page.rotation or page.rect.x0 or page.rect.y0:
                    raise ValueError(
                        f"page {cells[cell_i][0]} is rotated/offset "
                        f"(rotation={page.rotation}, rect={page.rect}); the crop "
                        "coordinates from pdfplumber would not line up"
                    )
                x0, top, x1, bottom = rect
                clip = pymupdf.Rect(
                    x0 - PAD_X, top - PAD_TOP, x1 + PAD_X, bottom + PAD_BOTTOM
                )
                path = os.path.join(tmp, f"{n:06d}.png")
                page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=clip).save(path)
                paths.append(path)
        finally:
            doc.close()

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
