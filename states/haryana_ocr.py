"""
OCR preprocessing for Haryana's 46 scanned ACs.

WHY THIS IS A SEPARATE PASS, NOT PART OF parse_raw()
----------------------------------------------------
states/haryana.py parses an already-clean text layer. The 46 ACs listed in
its SCANNED_ACS have no such layer -- they are page images. Rasterising and
OCRing them is minutes-to-hours of CPU per AC (~5s per page at 300 dpi, and
an AC runs to ~250 parts of ~20 pages), which is batch work, not something
to run implicitly inside a build.

So OCR is a distinct, re-runnable stage: scripts/ocr_haryana.py adds a
"partNNNN.ocr.json" artifact next to each "partNNNN.pdf" inside the AC's
existing raw ZIP. parse_raw() then reads those artifacts instead of the page
images, and still raises UnparseableRollError if the pass has not been run.
The PDFs stay in the ZIP so a better engine can be re-run later without
re-downloading ~900 MB per AC.

WHY THE EMBEDDED OCR LAYER IS NOT REUSED
----------------------------------------
41 of the 46 already carry a Tesseract "GlyphLessFont" text layer, 1 a
"HiddenHorzOCR" one, and AC 72 an "Untitled" one. All were probed directly:
AC 72's does decode to real Unicode Devanagari, but its pages carry a
full-page image, zero vector ruling lines, and broken "[1]".."[8]" header
markers, with conjuncts mangled ("क् रम ह स ा ं ं ख् स य ी ा" for "क्रम
संख्या"). It is a mediocre pre-baked OCR layer, not a decodable legacy font
like DK-RAJ, so it is re-rendered and re-OCRed like the other 45.

ENGINE
------
Tesseract's own `hin` LSTM traineddata from tessdata_best, driven through the
`tesseract` CLI. Two deliberate choices:

  * Not the Indic-OCR project's Devanagari models, despite them being the
    obvious candidate. Probed: that repo (github.com/indic-ocr/tessdata) was
    last touched in 2016, ships legacy Tesseract 3.x models with a mismatched
    normproto/unicharset that errors on exactly the matras these rolls are
    full of, and its own README says output needs a glyph-reordering
    post-process it never shipped. It is not a drop-in for Tesseract 5.
  * The CLI rather than a Python binding, so OCR adds no Python dependency
    at all -- only pymupdf, for rasterisation, which ships as a self-contained
    wheel (no poppler/OS package, unlike pdf2image).

Both `tesseract` and its `hin` traineddata are an OS-level prerequisite, the
same shape as the Playwright browser `make setup` already installs.

COLUMN RECOVERY
---------------
The text-layer path recovers the 8 columns from the PDF's own vertical
ruling lines, keyed to the printed "[1]".."[8]" markers. Neither survives
rasterisation: a scan has no vector edges, and OCR reads the markers as
"॥॥]", "जि]", "[है]". Pure x-position clustering is no good either -- the
full-width title and footer lines bridge every column gap.

So rows are anchored on content instead, which these rolls make unusually
easy: columns 4 and 6 are closed vocabularies documented in the legend
printed on every data page (पि/प/मा/अ and पु/म -- see haryana.py's
RELATION_NORMALIZE). A line is a voter row only if it starts with an integer
serial and contains both anchors, which also drops the title, header and
footer lines for free. Everything else is positional relative to those two
anchors. Only the house/name boundary needs geometry, and it is recovered
per page from the widest gap in the x-positions of the tokens sitting
between the serial and the relation anchor.
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile

# Chosen by eye against real pages: 300 dpi is the lowest that reads these
# scans' matras reliably, and doubling it roughly quadruples OCR time.
DEFAULT_DPI = 300
DEFAULT_LANG = "hin"

# --psm 6 ("a single uniform block of text") beats the default page
# segmentation here: these pages are one ruled table, and the automatic
# mode splits it into columns it then reads in the wrong order.
DEFAULT_PSM = "6"

ARTIFACT_SUFFIX = ".ocr.json"

# Column 4 and column 6 as printed in the legend at the foot of every data
# page. Kept separate from haryana.py's RELATION_NORMALIZE/GENDER_NORMALIZE
# (which map to VoterRecord's F/H/M/O scheme) because this set's job is only
# to recognise "this token is the relation cell", before any normalisation.
RELATION_ANCHORS = frozenset({"पि", "िप", "प", "मा", "अ"})
GENDER_ANCHORS = frozenset({"पु", "म"})

# OCR routinely tacks a trailing dot or a stray ruling-line fragment onto a
# one- or two-character cell ("पु...", "म.", "प|").
_ANCHOR_STRIP = ".,;:|]['\"`~_ ।"

# Two tokens are in different cells if they are further apart than this (in
# pixels at DEFAULT_DPI); the house and name columns sit ~180px apart at 300
# dpi, while words within one name sit ~30px apart.
CELL_GAP = 60

# How many rows must agree before a column's position is trusted. A data page
# carries 40-50 rows, so this only rules out a page OCR essentially failed on.
MIN_ROWS_PER_COLUMN = 3

# Column numbers as printed in the table's own [1]..[8] header markers, the
# same numbering states/haryana.py uses for the text-layer ACs.
COL_SERIAL, COL_HOUSE, COL_NAME = 1, 2, 3
COL_RELATION, COL_RELATIVE, COL_GENDER, COL_AGE, COL_EPIC = 4, 5, 6, 7, 8


class TesseractUnavailableError(RuntimeError):
    """Raised when the `tesseract` binary or its `hin` traineddata is missing."""


def require_tesseract(lang=DEFAULT_LANG):
    """Fail loudly and actionably up front rather than once per page."""
    if shutil.which("tesseract") is None:
        raise TesseractUnavailableError(
            "the `tesseract` binary is not on PATH; install it "
            "(macOS: `brew install tesseract`, Debian: `apt-get install "
            "tesseract-ocr`) to OCR Haryana's scanned ACs"
        )
    listed = subprocess.run(
        ["tesseract", "--list-langs"], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=True,
    ).stdout.decode("utf-8", "replace").split()
    if lang not in listed:
        raise TesseractUnavailableError(
            f"tesseract has no {lang!r} traineddata (found: {sorted(listed)}). "
            f"Install it, e.g. curl -L -o $(dirname $(dirname $(which tesseract)))"
            f"/share/tessdata/{lang}.traineddata https://raw.githubusercontent.com"
            f"/tesseract-ocr/tessdata_best/main/{lang}.traineddata"
        )


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _anchor(text):
    return _clean(text).strip(_ANCHOR_STRIP)


def ocr_image(png_bytes, lang=DEFAULT_LANG, psm=DEFAULT_PSM):
    """Run tesseract over one page image, returning its words with geometry.

    Tesseract's TSV output is used rather than plain text because the column
    layout is only recoverable from word positions -- see the module
    docstring. Words are returned in tesseract's own reading order.
    """
    with tempfile.NamedTemporaryFile(suffix=".png") as img:
        img.write(png_bytes)
        img.flush()
        proc = subprocess.run(
            ["tesseract", img.name, "stdout", "-l", lang, "--psm", psm, "tsv"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        )
    # QUOTE_NONE: a lone '"' is a perfectly ordinary OCR result and must not
    # be read as the start of a quoted field.
    reader = csv.DictReader(
        proc.stdout.decode("utf-8", "replace").splitlines(),
        delimiter="\t", quoting=csv.QUOTE_NONE,
    )
    words = []
    for row in reader:
        if row.get("level") != "5" or not _clean(row.get("text")):
            continue
        left, top = int(row["left"]), int(row["top"])
        words.append({
            "text": _clean(row["text"]),
            "x0": left,
            "x1": left + int(row["width"]),
            "top": top,
            "bottom": top + int(row["height"]),
            # Tesseract's per-word confidence, 0-100 (-1 for some tokens).
            # Recorded but not filtered on: dropping low-confidence words
            # would silently delete real names, and the anchor test below
            # already discards the lines that are pure noise.
            "conf": float(row["conf"]),
            "line": (row["block_num"], row["par_num"], row["line_num"]),
        })
    return words


def ocr_part(pdf_bytes, dpi=DEFAULT_DPI, lang=DEFAULT_LANG, psm=DEFAULT_PSM):
    """Rasterise every page of one part PDF and OCR it.

    Returns the artifact dict that gets written as partNNNN.ocr.json.
    """
    import pymupdf  # imported lazily: only the OCR pass needs it

    pages = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            png = page.get_pixmap(dpi=dpi).tobytes("png")
            pages.append({"words": ocr_image(png, lang=lang, psm=psm)})
    return {"engine": "tesseract", "lang": lang, "dpi": dpi, "psm": psm,
            "pages": pages}


# --------------------------------------------------------------------------
# turning OCR words back into table rows
# --------------------------------------------------------------------------


def _lines(words):
    """Group words into tesseract's own text lines, each sorted left to right."""
    grouped = {}
    for w in words:
        grouped.setdefault(tuple(w["line"]), []).append(w)
    return [
        sorted(line, key=lambda w: w["x0"])
        for _, line in sorted(grouped.items(), key=lambda kv: min(w["top"] for w in kv[1]))
    ]


def _serial_led(words):
    """Lines that could be voter rows: ones starting with an integer serial.
    Catches the title line too ("18 - सम्भालका ..."), which the anchor and
    geometry tests below then reject."""
    return [
        line for line in _lines(words)
        if re.fullmatch(r"\d{1,4}", _anchor(line[0]["text"]))
    ]


def _confident_rows(lines):
    """The rows whose relation and gender cells OCRed to exactly the values
    the legend prints. Their only job is to locate the columns for every other
    row -- see _column_edges()."""
    rows = []
    for line in lines:
        rel = next(
            (i for i, w in enumerate(line) if _anchor(w["text"]) in RELATION_ANCHORS),
            None,
        )
        if not rel:  # absent, or at index 0 where the serial must be
            continue
        gen = next(
            (i for i in range(rel + 1, len(line))
             if _anchor(line[i]["text"]) in GENDER_ANCHORS),
            None,
        )
        if gen is not None:
            rows.append((line, rel, gen))
    return rows


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _column_edges(rows):
    """The left edge of each of the 8 columns, medianed over `rows`.

    Every column in this table is left-aligned and its left edge barely moves
    down the page (measured across real pages: within a few pixels at 300
    dpi), which is what makes a handful of confidently-read rows enough to
    place the columns for all of them. Columns that never appear on the page
    -- house numbers are blank on most rows, EPIC numbers absent on some --
    are simply left out, and nothing is then assigned to them.
    """
    lefts = {}
    for line, rel, gen in rows:
        after = line[gen + 1:]
        age = next(
            (i for i, w in enumerate(after) if re.fullmatch(r"\d{1,3}", _anchor(w["text"]))),
            None,
        )
        cells = {
            COL_SERIAL: line[:1],
            COL_NAME: line[1:rel],
            COL_RELATION: [line[rel]],
            COL_RELATIVE: line[rel + 1:gen],
            COL_GENDER: [line[gen]],
        }
        if age is not None:
            cells[COL_AGE] = [after[age]]
            cells[COL_EPIC] = after[age + 1:]
        for col, words in cells.items():
            if words:
                lefts.setdefault(col, []).append(min(w["x0"] for w in words))

    edges = {col: _median(vals) for col, vals in lefts.items() if len(vals) >= MIN_ROWS_PER_COLUMN}
    _split_house_and_name(rows, edges)
    return edges


def _split_house_and_name(rows, edges):
    """Separate the house-number column from the name column.

    Both were measured above as one "everything between the serial and the
    relation cell" span, so COL_NAME's edge is wrong whenever house numbers
    are present. Split them on the widest gap among those tokens' left edges:
    house numbers sit ~180px left of the names at 300 dpi, while the words
    within a name sit ~30px apart. A page whose house column is blank
    throughout yields one tight cluster and no split, which is the right
    answer -- house numbers really are absent from most rows.
    """
    if COL_NAME not in edges:
        return
    lefts = sorted(w["x0"] for line, rel, _ in rows for w in line[1:rel])
    if len(lefts) < 2:
        return
    gap, below = max((b - a, a) for a, b in zip(lefts, lefts[1:]))
    if gap > CELL_GAP:
        edges[COL_HOUSE] = lefts[0]
        edges[COL_NAME] = below + gap


def _assign(line, edges):
    """Group one line's tokens into columns by which column's left edge they
    sit past, the raster equivalent of what _cells() does with the PDF's own
    ruling lines on the text-layer ACs."""
    ordered = sorted(edges.items(), key=lambda kv: kv[1])
    cells = {}
    for w in line:
        centre = (w["x0"] + w["x1"]) / 2
        col = next((c for c, edge in reversed(ordered) if centre >= edge), None)
        if col is not None:
            cells.setdefault(col, []).append(w)
    return cells


def _canonical(text, vocabulary):
    """Map an OCR'd closed-vocabulary cell onto the value the legend prints, by
    longest matching prefix.

    Both these columns hold one or two characters, which OCR routinely reads
    with a spurious extra matra or half-form: the male "पु" comes back as
    "पु..", the female "म" as "मे"/"मं"/"मम", the husband "प" as "प्"/"पं"/
    "पृ". A prefix match recovers all of those without an open-ended table of
    misspellings. It is only safe because the caller has already established
    by geometry that this token IS the relation or gender cell -- run against
    an arbitrary token it would happily read a name beginning with म as a
    gender code.
    """
    val = _anchor(text)
    for candidate in sorted(vocabulary, key=len, reverse=True):
        if val.startswith(candidate):
            return candidate
    return val


def _join(words):
    """Join a cell's tokens left to right, dropping the ones that are pure
    punctuation -- OCR litters cells with fragments of the table's own ruling
    lines ("|", "_.....") which would otherwise end up inside a name."""
    kept = [w["text"] for w in sorted(words, key=lambda w: w["x0"])
            if _anchor(w["text"])]
    return _clean(" ".join(kept)).strip(_ANCHOR_STRIP).strip()


def rows_from_page(words):
    """Recover voter rows from one OCR'd page's words.

    Two passes: find the rows whose relation and gender cells OCRed cleanly,
    use those to locate the 8 columns, then read every serial-led line against
    those columns. The second pass is what recovers the rows -- roughly a
    third of them -- whose relation or gender cell OCRed to something just
    outside the closed vocabulary.

    Each row comes back as a dict of the cells haryana.py's VoterRecord
    actually uses. Column 8 (the EPIC number) is deliberately dropped: it has
    nowhere to go in VoterRecord, and OCRing its Latin text under the `hin`
    model produces nothing worth keeping.
    """
    lines = _serial_led(words)
    confident = _confident_rows(lines)
    if len(confident) < MIN_ROWS_PER_COLUMN:
        return []  # not a data page (a cover page, or one OCR failed on)
    edges = _column_edges(confident)
    if not {COL_NAME, COL_RELATION, COL_GENDER} <= set(edges):
        return []

    parsed = []
    for line in lines:
        cells = _assign(line, edges)
        # A voter row has a serial and something in the relation column; the
        # title line, which also starts with a number, has neither.
        serial = _join(cells.get(COL_SERIAL, []))
        if not re.fullmatch(r"\d{1,4}", serial) or COL_RELATION not in cells:
            continue
        age = next(
            (w["text"] for w in cells.get(COL_AGE, [])
             if re.fullmatch(r"\d{1,3}", _anchor(w["text"]))),
            "",
        )
        parsed.append({
            "serial_no": serial,
            "local_ref": _join(cells.get(COL_HOUSE, [])),
            "full_name": _join(cells.get(COL_NAME, [])),
            "relation_code": _canonical(_join(cells[COL_RELATION]), RELATION_ANCHORS),
            "full_relative_name": _join(cells.get(COL_RELATIVE, [])),
            "gender": _canonical(_join(cells.get(COL_GENDER, [])), GENDER_ANCHORS),
            "age": _anchor(age),
        })
    return parsed


# --------------------------------------------------------------------------
# the ZIP-level pass
# --------------------------------------------------------------------------


def artifact_name(part_id):
    return f"part{part_id}{ARTIFACT_SUFFIX}"


def part_id_of(entry):
    """The part id an entry belongs to, for either a page image or an
    artifact, or None for anything else in the ZIP (manifest.json)."""
    m = re.fullmatch(r"part(\w+?)(?:\.pdf|" + re.escape(ARTIFACT_SUFFIX) + r")", entry)
    return m.group(1) if m else None


def ocr_zip(zip_path, parts=None, dpi=DEFAULT_DPI, lang=DEFAULT_LANG,
            psm=DEFAULT_PSM, force=False, progress=None):
    """OCR an AC's raw ZIP in place, adding a partNNNN.ocr.json per part PDF.

    Already-OCRed parts are skipped unless force is set, so an interrupted run
    is safe to just re-run. `parts` caps how many parts are processed, which
    is what makes a small validation sample possible without an AC-sized run.

    The ZIP is rebuilt into a temporary file and moved over the original only
    once it is complete, so an interrupted run can never leave a truncated
    archive where the downloaded PDFs used to be.
    """
    import zipfile

    require_tesseract(lang)

    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.namelist()
        existing = {part_id_of(e) for e in entries if e.endswith(ARTIFACT_SUFFIX)}
        pdfs = sorted(e for e in entries if e.endswith(".pdf"))
        todo = [e for e in pdfs if force or part_id_of(e) not in existing]
        if parts is not None:
            todo = todo[:parts]

        artifacts = {}
        for i, entry in enumerate(todo, 1):
            part_id = part_id_of(entry)
            if progress:
                progress(i, len(todo), part_id)
            artifacts[artifact_name(part_id)] = ocr_part(
                zf.read(entry), dpi=dpi, lang=lang, psm=psm
            )

        if not artifacts:
            return 0

        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(zip_path) or ".", suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
                for entry in entries:
                    if entry in artifacts:
                        continue  # superseded by this run's fresh artifact
                    out.writestr(entry, zf.read(entry))
                for name, artifact in artifacts.items():
                    out.writestr(name, json.dumps(artifact, ensure_ascii=False))
            os.replace(tmp, zip_path)
        except BaseException:
            os.path.exists(tmp) and os.unlink(tmp)
            raise
    return len(artifacts)


def read_artifacts(raw):
    """{part_id: artifact} for every OCR artifact in an AC's raw ZIP bytes.

    Bytes that are not a readable ZIP at all come back empty rather than
    raising, so the caller's "this AC has not been OCRed" path covers a
    missing or truncated download too."""
    import zipfile

    found = {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return found
    with zf:
        for entry in sorted(zf.namelist()):
            if entry.endswith(ARTIFACT_SUFFIX):
                found[part_id_of(entry)] = json.loads(zf.read(entry))
    return found
