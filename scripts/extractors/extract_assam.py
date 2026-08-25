"""
Extract Assam old SIR (2005) voter roll PDFs to CSV.

PDF uses MSTT legacy Bengali/Assamese font with WinAnsiEncoding.  The font
re-uses Latin code points to display Assamese glyphs — there are no usable
ToUnicode CMaps.  A custom transcoder (_mstt_decode) maps the raw
pdfplumber-extracted text to Unicode Bengali/Assamese (U+0980-U+09FF).

Consonants are stored in half-form (with trailing virama ্).  The stem
marker ¡ (U+00A1) "completes" them by dropping the virama so the inherent
vowel sounds.  Pre-base matras ি ([ U+005B) and ে (ë U+00EB / ì U+00EC)
are typed before their consonant and must be reordered after decoding.

Usage:
    python scripts/extractors/extract_assam.py                    # all downloaded ACs
    python scripts/extractors/extract_assam.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_assam.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_assam.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import unicodedata
import zipfile

import pdfplumber
from indic_transliteration import sanscript
from indic_transliteration.sanscript import transliterate

STATE_ID = "assam"
ROLL_YEAR = 2005
N_COLS = 8
NARROW_COLS = {4, 6, 7}
ROW_TOL = 5.0
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "sex", "age", "epic_no",
    "roll_year",
]

# Column indices inside an 8-cell row (0-based)
_SERIAL = 0
_HOUSE = 1
_NAME = 2
_REL = 3


# ── MSTT → Unicode Bengali/Assamese transcoder ────────────────────────
# The MSTT subset-embedded fonts in Assam 2005 SIR PDFs repurpose
# WinAnsiEncoding code points for Bengali/Assamese glyphs.  pdfplumber
# decodes these bytes via WinAnsi, so we see Latin-looking chars (à, Î,
# ¡, etc.) that must be mapped back to the correct Indic characters.
#
# Consonants are in "half form" (trailing ্).  The stem marker ¡ (0xA1)
# drops the virama — consonant + ¡ = consonant with inherent vowel.
# Pre-base matras [ (ি) and ë/ì (ে) are typed before their consonant
# cluster and need reordering.  ো and ৌ are decomposed: ে + া = ো,
# ে + ৗ = ৌ.

# Sentinel inserted by ¡ (stem marker) to signal "drop the preceding virama"
_STEM = '\ue000'

_MAP = {
    # Whitespace / punctuation — pass through
    ' ': ' ', '(': '(', ')': ')', ',': ',', '-': '-', '.': '.', '/': '/',
    ':': ':', ';': ';', '?': '?',
    # Digits — pass through
    **{c: c for c in '0123456789'},

    # ── Independent vowels ──
    '"': 'অ',        # a
    '&': 'এ',        # e
    '*': 'ও',        # o
    '+': 'অ',        # a (alternate)

    # ── Consonants ──
    # Mapped WITHOUT virama.  Conjuncts are formed explicitly by
    # sub-join forms (¸ ø û «) and dedicated conjunct glyphs (Û g ‰ etc.).
    # The stem marker ¡ is now used only as a no-op separator to prevent
    # two adjacent consonant glyphs from visually colliding.
    'A': 'ক',  'H': 'ক',  'y': 'ক',    # ka (y = variant before ্ৰ)
    'J': 'খ',                            # kha
    'K': 'গ',  'N': 'গ',                 # ga (N = variant in some subsets)
    'Q': 'ঘ',                            # gha
    'W': 'চ',                            # cha
    'c': 'ঝ',                            # jha
    'i': 'ট',                            # Ta (retroflex)
    'b': 'ড',                            # Da (retroflex)
    'k': 'ঠ',                            # Tha (retroflex)
    'e': 'ঢ',                            # Dha (retroflex) — tentative
    't': 'ত',  'z': 'ত',                 # ta (z = variant in conjuncts)
    '=': 'থ',                            # tha
    'ƒ': 'দ',  '\u201e': 'দ',            # da (U+201E „ = variant)
    '‹': 'ধ',                            # dha
    '>': 'ন',  'o': 'ন',                 # na  (o also ণ — ambiguous in font)
    'š': 'প',                            # pa
    'ó': 'ফ',                            # pha
    '¤': 'ব',                            # ba
    '®': 'ভ',                            # bha
    '³': 'ম',                            # ma
    '™': 'য',                            # ya
    '¹': 'ৰ',                            # ra (Assamese ৰ)
    'º': 'ল',                            # la
    'Å': 'শ',  'Ç': 'শ',                 # sha (Ç = alternate)
    'È': 'ষ',                            # Sha (retroflex)
    'Î': 'স',  'Ñ': 'স্',                 # sa  (Ñ = half-form for conjuncts like স্ব, স্থ)
    'Ò': 'হ',                            # ha
    '\\': 'জ',                            # ja

    # ── Conjuncts (pre-formed clusters) ──
    'Û': 'ক্ষ',                           # ksha
    'g': 'ঞ্জ',                           # nja
    '‰': 'দ্ৰ',                           # dra
    'v': 'ক্ত',                           # kta
    'Ë': 'ষ্ঠ',                           # shTha
    'Ì': 'ষ্ণ',                           # shNa (Krishna)
    '–': 'ন্',                            # na-virama (conjunct linker, keeps ্)

    # ── Vowel signs / matras ──
    'à': 'া',    # aa
    'å': 'ু',    # u
    'ç': 'ু',    # u (variant glyph for র, হ, etc.)
    'è': 'ূ',    # uu (long u)
    'ã': 'ী',    # ii (long i)
    'õ': 'ৃ',    # ri (vocalic r)
    '[': 'ি',    # i  (pre-base — reorder after next consonant cluster)
    'ë': 'ে',    # e  (pre-base — reorder after next consonant cluster)
    'ì': 'ে',    # e  (pre-base, alternate; also ্ in some conjuncts)
    'í': 'ৈ',    # ai (pre-base)
    'ï': 'ৗ',    # au-length mark (second part of ৌ)

    # ── Sub-joins / phala forms ──
    '¸': '্য',   # ya-phala  (য-ফলা)
    'ø': '্ৰ',   # ra-phala  (ৰ-ফলা)
    'û': '্ৰ',   # ra-phala  (alternate glyph)
    'Ã': '্ত',   # ta subscript (conjunct ক্ত etc.)
    '«': '্ব',   # ba-phala  (ব-ফলা)

    # ── Specials ──
    '¡': _STEM,   # stem marker — completes consonant, drops virama
    '}': 'ং',    # anusvara
    '¢': 'ৰ্',   # repha (ৰ + ্, placed above following consonant)
    'Ø': 'ৰ',    # ra (alternate glyph)
    '¬': 'ব',    # ba (alternate glyph, used in স্ব etc.)
    '´': 'ম্',   # ma + virama (half-form for conjuncts like স্ম)
    '@': 'ঃ',    # visarga
    'ò': 'ঁ',    # chandrabindu
    'ü': 'ই',    # independent i vowel (also দ্ in some conjuncts — ambiguous)

    # ── Assamese-specific ──
    'U': 'ৱ',    # Assamese wa (U+09F1)
    'l': 'উ',    # independent u vowel

    # ── Less certain / rare ──
    'I': 'ঈ',    # independent ii vowel
    'B': 'ক্',   # ka variant (rare)
    'S': 'S',    # English S (session codes like S03)
    'D': 'D',    # English D (Doubtful)
    'E': 'E',    # English E (Elector)
    'r': '',      # visual connector (silent)
    'f': 'ঢ্',   # Dha retroflex — tentative
    'u': 'u',    # appears only in English 'Doubtful'
    'j': 'ত্ৰ্', # tra conjunct
    'P': 'প্',   # pa variant (rare)
    'Z': 'Z',    # rare, likely English
    'a': 'a',    # English
    'd': 'd',    # English
    'R': 'R',    # English (rare)
    'T': 'T',    # English (rare)
    's': 's',    # English
    'q': 'q',    # English (rare)
    '|': '|',
    '~': '~',
    '^': '^',
    '`': '`',
    'À': 'ল্ল',  # lla conjunct — tentative
    '¿': '্ষ',   # sha subscript — tentative
    'Þ': 'ঞ',    # nya — tentative
    '×': 'ণ',    # Na — tentative
    'Í': 'স্ত',  # sta conjunct — tentative
    'Ó': 'Ó',    # rare
    'ê': 'ক',    # ka variant in conjuncts — tentative
    'î': 'ি',    # i-matra variant — tentative
    'ð': 'ড',    # Da variant — tentative
    'ñ': 'ণ',    # Na variant — tentative
    'ô': 'ô',    # rare
    'ú': 'ু',    # u-matra variant
    'ý': 'র',    # ra (Bengali র, not Assamese ৰ) — tentative
    'þ': 'þ',    # rare
    'ÿ': '্ব',   # ba-phala variant — tentative
    'œ': 'ঝ',    # jha standalone — tentative
    '¦': 'ব্দ',  # bda conjunct
    '¶': '¶',    # rare
    'µ': 'µ',    # rare
    'ª': 'ª',    # rare
    '°': '°',    # rare
    '±': '±',    # rare
    'Â': 'Â',    # rare
    'Õ': 'Õ',    # rare
    'Ü': 'Ü',    # rare
    'â': 'â',    # rare
    'Ê': 'Ê',    # rare
    '\x9d': '',   # control char — ignore
    '#': '#', '$': '$',
    '—': '—',    # em-dash
    '‚': 'থ',    # tha standalone (conjunct variant)
    '‡': 'দ্ধ',  # ddha conjunct — tentative
    '\u201c': '"', '\u201d': '"',
    '•': '•',
    '◊': '◊',
    'Ë': 'ষ্ঠ্',  # shTha (already above, included for clarity)
}

# Bengali/Assamese consonant range
_CONS = '[\u0995-\u09B9\u09DC-\u09DF\u09F0\u09F1]'
# A "cluster" is one or more consonants joined by virama (্).
# With the new mapping, explicit conjuncts already contain viramas,
# so a cluster can be a single consonant or a conjunct sequence.
_CLUSTER = '(?:' + _CONS + '্)*' + _CONS
_VIRAMA = '্'
_AA = 'া'


def _mstt_decode(s):
    """Transcode an MSTT-encoded string to Unicode Bengali/Assamese.

    The MSTT font stores consonants in half-form (with trailing virama ্).
    The stem marker ¡ (U+00A1) signals that the preceding consonant
    should drop its virama and stand on its own with inherent vowel.
    Without ¡, consecutive consonants form conjuncts via virama.
    """
    out = []
    for ch in s:
        if ch in _MAP:
            out.append(_MAP[ch])
        elif ch.isascii() and (ch.isdigit() or ch in '()[]{}/.,:;-'):
            out.append(ch)
        else:
            out.append('')  # drop unmapped
    t = ''.join(out)

    # --- Step 1: remove stem markers ---
    # With consonants now mapped WITHOUT virama, the stem marker ¡
    # is just a no-op separator (prevents visual collision in the
    # original font).  Remove all sentinels.
    t = t.replace(_STEM, '')

    # --- Step 3: pre-base ি reorder ---
    # ি typed before consonant cluster: move it after the cluster
    t = re.sub('ি(' + _CLUSTER + ')', r'\1ি', t)

    # --- Step 4: pre-base ে reorder ---
    # ে typed before consonant(s)+া → consonant(s)+ো
    # ে typed before consonant(s)+ৗ → consonant(s)+ৌ
    # ে typed before consonant(s) alone → consonant(s)+ে
    t = re.sub('ে(' + _CLUSTER + ')া', r'\1ো', t)
    t = re.sub('ে(' + _CLUSTER + ')ৗ', r'\1ৌ', t)
    t = re.sub('ে(' + _CLUSTER + ')', r'\1ে', t)

    # --- Step 5: pre-base ৈ reorder ---
    t = re.sub('ৈ(' + _CLUSTER + ')', r'\1ৈ', t)

    # --- Step 6: compose independent vowel combinations ---
    t = t.replace('অা', 'আ')
    t = t.replace('অো', 'ও')
    t = t.replace('অৌ', 'ঔ')
    t = t.replace('অে', 'এ')
    t = t.replace('অৈ', 'ঐ')

    # --- Step 6b: collapse double virama in repha + phala sequences ---
    # ৰ্্য → ৰ্য,  ৰ্্ৰ → ৰ্ৰ, etc.
    t = re.sub('্্', '্', t)

    # --- Step 6c: fix repha+ya-phala: যৰ্য → ৰ্য ---
    # In the font, ™(য) + ¢(ৰ্) + ¸(্য) renders as ৰ্য.  The য from
    # ™ is a visual connector and should be absorbed when followed by
    # repha + ya-phala.
    t = t.replace('যৰ্য', 'ৰ্য')

    # --- Step 7: compose ো / ৌ from decomposed pieces ---
    t = t.replace('াে', 'ো')
    t = t.replace('াৈ', 'ৌ')

    # --- Step 8: clean up stray viramas at word boundaries ---
    t = re.sub(_VIRAMA + r'(\s|$)', r'\1', t)
    t = re.sub(_VIRAMA + r'([^\u0980-\u09FF])', r'\1', t)

    return unicodedata.normalize('NFC', t)


# -- PDF extraction helpers ---------------------------------------------------


def _group_into_rows(words):
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    current_row = [words_sorted[0]]
    current_top = words_sorted[0]["top"]
    for w in words_sorted[1:]:
        if w["top"] - current_top <= ROW_TOL:
            current_row.append(w)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [w]
            current_top = w["top"]
    rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


def _find_column_row(words, min_cols=6):
    if not words:
        return None
    parens = [w for w in words if re.fullmatch(r"\(\d+\)", w["text"])]
    if len(parens) >= min_cols:
        parens.sort(key=lambda w: w["top"])
        for i in range(len(parens)):
            group = [parens[i]]
            for j in range(i + 1, len(parens)):
                if abs(parens[j]["top"] - parens[i]["top"]) <= ROW_TOL:
                    group.append(parens[j])
            if len(group) >= min_cols:
                labels = sorted(group, key=lambda w: w["x0"])
                nums = [int(re.search(r"\d+", w["text"]).group()) for w in labels]
                if nums[0] == 1 and nums[1] == 2:
                    return {n: (w["x0"] + w["x1"]) / 2 for n, w in zip(nums, labels)}
    singles = [w for w in words if re.fullmatch(r"\d", w["text"])]
    if len(singles) >= min_cols:
        singles.sort(key=lambda w: w["top"])
        for i in range(len(singles)):
            group = [singles[i]]
            for j in range(i + 1, len(singles)):
                if abs(singles[j]["top"] - singles[i]["top"]) <= ROW_TOL:
                    group.append(singles[j])
            labels = sorted(group, key=lambda w: w["x0"])
            nums = [w["text"] for w in labels]
            if len(nums) >= min_cols and nums[0] == "1" and nums[1] == "2":
                return {int(w["text"]): (w["x0"] + w["x1"]) / 2 for w in labels}
    return None


def _make_boundaries(col_centres):
    cols = sorted(col_centres.keys())
    boundaries = {}
    for i, col in enumerate(cols):
        if i == 0:
            left = 0
        else:
            gap = col_centres[col] - col_centres[cols[i - 1]]
            if col in NARROW_COLS:
                left = col_centres[col] - gap * 0.2
            elif cols[i - 1] in NARROW_COLS:
                left = col_centres[cols[i - 1]] + gap * 0.2
            else:
                left = col_centres[cols[i - 1]] + gap * 0.5
        if i == len(cols) - 1:
            right = 9999
        else:
            gap = col_centres[cols[i + 1]] - col_centres[col]
            if col in NARROW_COLS:
                right = col_centres[col] + gap * 0.2
            elif cols[i + 1] in NARROW_COLS:
                right = col_centres[cols[i + 1]] - gap * 0.2
            else:
                right = col_centres[col] + gap * 0.5
        boundaries[col] = (left, right)
    return boundaries


def _assign_column(x_centre, col_centres, boundaries):
    for col, (left, right) in boundaries.items():
        if left <= x_centre < right:
            return col
    return min(col_centres, key=lambda c: abs(col_centres[c] - x_centre))


def _row_to_cells(row_words, col_centres, boundaries):
    buckets = {c: [] for c in col_centres}
    for w in row_words:
        cx = (w["x0"] + w["x1"]) / 2
        col = _assign_column(cx, col_centres, boundaries)
        buckets[col].append(w)
    cells = []
    for c in sorted(col_centres.keys()):
        parts = sorted(buckets.get(c, []), key=lambda w: w["x0"])
        cells.append(" ".join(w["text"] for w in parts).strip())
    return cells


def _find_col_row_top(words, col_centres):
    """Find y of the column-number header row to skip.

    Requires 3+ column markers at the same y so a lone data '1' isn't
    mistaken for a header.
    """
    candidates = []
    for w in words:
        txt = w["text"]
        if re.fullmatch(r"\(?\d\)?", txt):
            cx = (w["x0"] + w["x1"]) / 2
            digit = int(re.search(r"\d", txt).group())
            if digit in col_centres and abs(cx - col_centres[digit]) < 15:
                candidates.append(w)
    if not candidates:
        return 0
    candidates.sort(key=lambda w: w["top"])
    col_row_top = 0
    group = [candidates[0]]
    for w in candidates[1:]:
        if w["top"] - group[0]["top"] <= ROW_TOL:
            group.append(w)
        else:
            if len(group) >= 3:
                col_row_top = max(col_row_top, max(g["top"] for g in group))
            group = [w]
    if len(group) >= 3:
        col_row_top = max(col_row_top, max(g["top"] for g in group))
    return col_row_top


def _fix_row(row):
    """Post-process an 8-cell row to fix common mis-assignments.

    MSTT text is non-ASCII after decoding, so "alphabetic" checks must
    look for absence of digits rather than ASCII letters.
    """
    house = row[_HOUSE].strip()
    name = row[_NAME].strip()
    rel = row[_REL].strip()

    # House_no with NO digits at all -> it's a name fragment, not a house number
    if house and not re.search(r"\d", house):
        name = f"{house} {name}".strip()
        house = ""

    # Trailing non-digit text after a digit-based house number -> name fragment
    if house:
        m = re.match(r"^(.*?\d[\d/\-]*)\s+(\S.*)$", house)
        if m:
            house = m.group(1).strip()
            name = f"{m.group(2).strip()} {name}".strip()

    row[_HOUSE] = house
    row[_NAME] = name
    row[_REL] = rel
    return row


def _extract_page(page, fallback_centres=None):
    words = page.extract_words()
    if not words:
        return [], fallback_centres
    col_centres = _find_column_row(words) or fallback_centres
    if col_centres is None:
        return [], None
    boundaries = _make_boundaries(col_centres)
    col_row_top = _find_col_row_top(words, col_centres)
    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]
    rows = _group_into_rows(data_words)
    records = []
    for rw in rows:
        cells = _row_to_cells(rw, col_centres, boundaries)
        serial = cells[0].strip() if cells else ""
        if serial and re.fullmatch(r"\d+", serial):
            cells = (cells + [""] * N_COLS)[:N_COLS]
            _fix_row(cells)
            # Decode MSTT text to Unicode Bengali/Assamese (skip serial, age, epic)
            for idx in (_HOUSE, _NAME, _REL, 4, 5):
                cells[idx] = _mstt_decode(cells[idx])
            records.append(cells)
    return records, col_centres


def _extract_pdf(pdf_bytes):
    all_rows = []
    col_centres = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows, col_centres = _extract_page(page, col_centres)
            all_rows.extend(rows)
    return all_rows


def _extract_ac_zip(zip_path):
    all_rows = []
    manifest = {}
    with zipfile.ZipFile(zip_path) as zf:
        if "manifest.json" in zf.namelist():
            manifest = json.loads(zf.read("manifest.json"))
        part_files = sorted([n for n in zf.namelist() if n.endswith(".pdf")])
        for pf in part_files:
            m = re.search(r"(\d+)", os.path.basename(pf))
            if not m:
                continue
            part_no = int(m.group(1))
            rows = _extract_pdf(zf.read(pf))
            for cells in rows:
                all_rows.append([part_no] + cells)
    return all_rows, manifest


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


# -- Main ----------------------------------------------------------------------


# ── Transliteration (Bengali/Assamese → English) ────────────────────────

_DIACRITIC_MAP = {
    'ā': 'a', 'ī': 'i', 'ū': 'u', 'ṛ': 'ri',
    'ś': 'sh', 'ṣ': 'sh', 'ṭ': 't', 'ḍ': 'd', 'ṇ': 'n',
    'ñ': 'n', 'ṅ': 'ng', 'ḥ': 'h', 'ṃ': 'm',
}

_RELATION_MAP = {
    'পি': 'Father', 'পিতা': 'Father',
    'স্বা': 'Husband', 'স্বামী': 'Husband',
    'মা': 'Mother', 'মাতা': 'Mother',
    'অ': 'Other',
    'ক': 'Other',
}

_SEX_MAP = {
    'পু': 'M',
    'ম': 'F',
}


def _transliterate_bengali(text):
    """Transliterate Bengali/Assamese text to approximate English."""
    if not text or not text.strip():
        return ''
    # Pre-process: replace Assamese-specific chars the library doesn't handle
    t = text.strip().replace('ৱ', 'ব')  # Assamese wa → Bengali ba (sounds like 'w/v')
    iast = transliterate(t, sanscript.BENGALI, sanscript.IAST)
    for k, v in _DIACRITIC_MAP.items():
        iast = iast.replace(k, v)
    # Bengali schwa deletion: drop final 'a' from words
    words = iast.split()
    result = []
    for w in words:
        if len(w) > 1 and w.endswith('a') and not w.endswith('aa'):
            w = w[:-1]
        result.append(w)
    return ' '.join(result).title()


def _transliterate_csv(csv_path):
    """Add English transliteration columns to an existing CSV."""
    import tempfile, shutil

    stats = {"total": 0, "name_en": 0, "rel_name_en": 0}

    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path),
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            out_fields = list(reader.fieldnames)
            # Remove existing _en columns if re-running
            for col in ["elector_name_en", "relation_name_en", "relation_type_en", "sex_en"]:
                if col in out_fields:
                    out_fields.remove(col)
            # Insert _en columns after roll_year (at end)
            out_fields += ["elector_name_en", "relation_name_en", "relation_type_en", "sex_en"]

            writer = csv.DictWriter(tmp, fieldnames=out_fields)
            writer.writeheader()
            for row in reader:
                stats["total"] += 1
                name = row.get("elector_name", "").strip()
                rel_name = row.get("relation_name", "").strip()
                rel = row.get("relation", "").strip()
                sex = row.get("sex", "").strip()

                name_en = _transliterate_bengali(name)
                rel_name_en = _transliterate_bengali(rel_name)
                rel_type_en = _RELATION_MAP.get(rel, "")
                sex_en = _SEX_MAP.get(sex, "")

                if name_en:
                    stats["name_en"] += 1
                if rel_name_en:
                    stats["rel_name_en"] += 1

                row["elector_name_en"] = name_en
                row["relation_name_en"] = rel_name_en
                row["relation_type_en"] = rel_type_en
                row["sex_en"] = sex_en
                writer.writerow(row)

                if stats["total"] % 1_000_000 == 0:
                    print(f"  … {stats['total']:,} rows", flush=True)

        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Transliterate {csv_path}:")
    print(f"  {stats['total']:,} rows processed")
    print(f"  {stats['name_en']:,} elector_name_en populated")
    print(f"  {stats['rel_name_en']:,} relation_name_en populated")


# ── CSV post-processing ──────────────────────────────────────────────────

VALID_SEX = {"পু", "ম", ""}
VALID_RELATION = {"পি", "স্বা", "মা", "ক", "অ", ""}


def _postprocess(csv_path):
    """Fix column-bleeding issues in an already-extracted CSV.

    Assam-specific fixes:
    1. Sex column has name suffix concatenated with পু/ম (e.g. "মলপু" → পু,
       "সেখম" → ম). If sex ends with পু or ম, extract it; prepend rest to name.
    2. Relation column has garbled font values ("সমিÊ", ASCII garbage)
       → clear to empty.
    3. Relation empty but relation_name starts with a known relation prefix
       (পি, স্বা, মা, ক, অ) → split prefix into relation column.
    """
    import tempfile, shutil

    fixes = {"sex_cleaned": 0, "rel_cleaned": 0, "rel_split": 0, "total": 0}

    # Relation prefixes, longest first so স্বা matches before স
    _REL_PREFIXES = ["স্বা", "পি", "মা", "অ", "ক"]

    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path),
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                fixes["total"] += 1
                sex = row["sex"].strip()
                rel = row["relation"].strip()
                rel_name = row["relation_name"].strip()

                if sex not in VALID_SEX:
                    if sex.endswith("পু"):
                        prefix = sex[:-len("পু")]
                        if prefix:
                            row["elector_name"] = (row["elector_name"].strip() + " " + prefix).strip()
                        row["sex"] = "পু"
                        fixes["sex_cleaned"] += 1
                    elif sex.endswith("ম"):
                        prefix = sex[:-len("ম")]
                        if prefix:
                            row["elector_name"] = (row["elector_name"].strip() + " " + prefix).strip()
                        row["sex"] = "ম"
                        fixes["sex_cleaned"] += 1
                    else:
                        row["sex"] = ""
                        fixes["sex_cleaned"] += 1

                if rel not in VALID_RELATION:
                    row["relation"] = ""
                    fixes["rel_cleaned"] += 1

                # Fix 3: relation empty, but relation_name starts with
                # a known relation prefix (পি, স্বা, মা, ক, অ)
                if not row["relation"].strip() and rel_name:
                    for prefix in _REL_PREFIXES:
                        if rel_name.startswith(prefix + " "):
                            row["relation"] = prefix
                            row["relation_name"] = rel_name[len(prefix):].strip()
                            fixes["rel_split"] += 1
                            break
                        elif rel_name == prefix:
                            # relation_name is just the code with no name
                            row["relation"] = prefix
                            row["relation_name"] = ""
                            fixes["rel_split"] += 1
                            break

                writer.writerow(row)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed")
    print(f"  {fixes['sex_cleaned']} sex values fixed")
    print(f"  {fixes['rel_cleaned']} relation values fixed")
    print(f"  {fixes['rel_split']} relation split from relation_name")


def main():
    ap = argparse.ArgumentParser(description="Extract Assam SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/assam")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    ap.add_argument("--postprocess", metavar="CSV", default=None,
                    help="post-process an existing CSV to fix column bleeding")
    ap.add_argument("--transliterate", metavar="CSV", default=None,
                    help="add English transliteration columns to an existing CSV")
    args = ap.parse_args()

    if args.transliterate:
        _transliterate_csv(args.transliterate)
        return

    if args.postprocess:
        _postprocess(args.postprocess)
        return

    raw_dir = os.path.join("data", "raw", STATE_ID)
    os.makedirs(args.out_dir, exist_ok=True)

    meta = _load_meta()
    ac_map = {ac["ac_no"]: ac for ac in meta}

    zip_files = sorted([f for f in os.listdir(raw_dir) if f.endswith(".zip")])
    if args.ac:
        wanted = set(int(x) for x in args.ac.split(","))
        zip_files = [f for f in zip_files if int(re.search(r"\d+", f).group()) in wanted]
    if args.limit:
        zip_files = zip_files[:args.limit]

    print(f"{STATE_ID}: extracting {len(zip_files)} ACs")

    all_rows = []
    for zf in zip_files:
        zip_path = os.path.join(raw_dir, zf)
        ac_no = int(re.search(r"\d+", zf).group())
        ac_info = ac_map.get(ac_no, {})
        ac_name = ac_info.get("ac_name", "")
        district = ac_info.get("district_name", "")

        print(f"  {zf}: AC{ac_no:03d} {ac_name}...", end=" ", flush=True)
        rows, _ = _extract_ac_zip(zip_path)
        full_rows = [[STATE_ID, district, ac_no, ac_name] + r + [ROLL_YEAR] for r in rows]

        if args.combined:
            all_rows.extend(full_rows)
        else:
            csv_path = os.path.join(args.out_dir, f"AC{ac_no:03d}.csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADERS)
                w.writerows(full_rows)

        print(f"{len(rows)} rows")

    if args.combined:
        csv_path = os.path.join(args.out_dir, f"{STATE_ID}_combined.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADERS)
            w.writerows(all_rows)
        print(f"Combined: {csv_path} ({len(all_rows)} rows)")
    else:
        print(f"Per-AC CSVs in {args.out_dir}/")


if __name__ == "__main__":
    main()
