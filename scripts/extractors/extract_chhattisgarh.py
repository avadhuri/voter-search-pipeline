"""
Extract Chhattisgarh old SIR (2003) voter roll PDFs to CSV.

Handles 4 font types found across 90 ACs:
  1. DV-TTSurekh  (AC001-020) — legacy font, decoded by _dvtt_decode
  2. Arial-Unicode + Mangal (AC021-033, 037, 040-062, 069-071) — garbled Unicode
  3. SHREE708 + DV-TTSurekh/DV-Surekh-Normal (AC034-039, 063-068, 072-082, 089-090)
     — multi-line records, DVTTSurekh-encoded names, SHREE relation/sex codes
  4. CDAC_GISTSurekh (AC083-088) — Unicode Devanagari with char substitutions

Font type is auto-detected from each PDF's first page font names.

Usage:
    python scripts/extractors/extract_chhattisgarh.py                    # all downloaded ACs
    python scripts/extractors/extract_chhattisgarh.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_chhattisgarh.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_chhattisgarh.py --combined         # single CSV for state
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

STATE_ID = "chhattisgarh"
ROLL_YEAR = 2003
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

# ── Per-font-type fallback column centres ────────────────────────────────
# These are used when column-number markers aren't detected on a page.

FALLBACK_CENTRES_DVTT = {1: 78, 2: 121, 3: 184, 4: 292, 5: 328, 6: 420, 7: 453, 8: 500}

FALLBACK_CENTRES_MANGAL = {1: 33, 2: 80, 3: 150, 4: 275, 5: 340, 6: 462, 7: 500, 8: 540}

FALLBACK_CENTRES_SHREE = {1: 105, 2: 137, 3: 185, 4: 306, 5: 340, 6: 435, 7: 459, 8: 510}

FALLBACK_CENTRES_CDAC = {1: 42, 2: 94, 3: 150, 4: 246, 5: 300, 6: 390, 7: 432, 8: 500}

# Alias for backward compat
FALLBACK_CENTRES = FALLBACK_CENTRES_DVTT


# ── DVTTSurekh → Unicode Devanagari transcoder ──────────────────────────
# DVTTSurekh uses the same byte-to-glyph encoding as DK-RAJ (Haryana).
# Consonants are stored as half forms (C + virama); a stem glyph (mapped
# to ा) completes them.  Pre-base ि is typed before its cluster and
# must be reordered.  See states/haryana_dkraj.py for full documentation.

_REPHA = '\ue000'
_REPHA_DONE = '\ue001'
_IREPHA = '\ue002'
_I_PLACED = '\ue003'

_MAP = {
    ' ': ' ', '!': '!', '(': '(', ')': ')', ',': ',', '-': '-', '.': '.', '/': '/',
    ':': ':', ';': ';', '?': '?', '&': 'ः', '%': 'ऽ', '$': 'ॐ', "'": ',',
    '*': 'ा', '+': 'अ', '<': 'इ', '=': 'उ', '>': 'ऊ',
    '@': 'ऋ', 'A': 'ॠ', 'B': 'ए',
    **{c: c for c in '0123456789'},
    'C': 'क्', 'E': 'क्', 'F': 'क़्',
    'G': 'क्र्', 'H': 'क्त्', 'I': 'क्ष्',
    'J': 'ख्', 'K': 'ख़्', 'L': 'ख़्', 'M': 'ग्', 'N': 'ग़्',
    'O': 'ग्र्', 'P': 'घ्', 'Q': 'घ़्',
    'S': 'च्', 'U': 'छ्', 'V': 'ज्', 'W': 'ज़्',
    'Y': 'ज्ञ्', 'Z': 'झ्',
    ']': 'ट्', '`': 'ठ्', 'b': 'ड्', 'c': 'ड़्',
    'f': 'ढ्', 'g': 'ढ़्', 'h': 'ण्',
    'i': 'त्', 'j': 'त्र्', 'k': 'त्त्',
    'l': 'थ्', 'n': 'द्', 'o': 'द्र्',
    'p': 'द्र्', 'q': 'द्द्', 'r': 'द्ध्', 's': 'ब्र्',
    't': 'द्य्', 'u': 'द्व्',
    'v': 'ध्', 'x': 'न्', 'z': 'न्न्',
    '{': 'प्', '|': 'प्र्', '}': 'फ्',
    '\xa1': 'फ्', '\xa2': 'फ़्', '\xa3': 'फ्र्',
    '\xa4': 'ब्', '\xa5': 'ब्र्', '\xa6': 'भ्', '\xa8': 'म्',
    '\xaa': 'य्', '\xae': 'र', '\xaf': 'र', '\xb0': 'र',
    '\xb1': 'ल्', '\xb4': 'व्',
    '\xb6': 'श्', '\xb7': 'श्व्', '\xb8': 'श्र्', '\xb9': 'ष्',
    '\xba': 'स्', '\xbb': 'स्र्', '\xbd': 'ह्',
    '\xc0': 'ह्म्',
    '\xc2': '्', '\xc3': '', '\xc4': 'ँ', '\xc5': 'ँ', '\xc6': 'ं',
    '\xc7': _REPHA, '\xc8': _REPHA,
    '\xc9': 'ा', '\xca': 'ि', '\xcb': 'िं', '\xcc': _IREPHA,
    '\xcd': 'िं', '\xce': 'ि', '\xcf': 'िं',
    '\xd2': 'ी', '\xd3': 'ीं', '\xd4': 'ी' + _REPHA,
    '\xd0': 'ू', '\xd6': 'ु', '\xd9': 'ु', '\xda': 'ू', '\xde': 'ृ',
    '\xe0': 'े', '\xe4': 'े', '\xe5': 'ें', '\xe6': 'े' + _REPHA,
    '\xe7': 'ें' + _REPHA, '\xe8': 'ै', '\xe9': 'ैं',
    '\xea': 'ै' + _REPHA, '\xeb': 'ं' + _REPHA, '\xec': 'ॉ',
    '\xf0': '', '\xf1': '',
    '\xf2': 'ा', '\xf3': 'ा', '\xf4': 'ा', '\xf5': 'ा', '\xf6': 'ा',
    '\xf7': 'ा', '\xf8': 'ा', '\xf9': 'ा', '\xfa': '',
    '\xfb': 'ा', '\xfc': 'ू', '\xfe': 'ा',
}

_CONS = '[\u0915-\u0939\u0958-\u0961]'
_CLUSTER = '(?:' + _CONS + '्)*' + _CONS
_MATRAS = '[\u093E-\u094C\u0902\u0903\u0901\u0943]'
_INNER = '[\u093F-\u094C\u0902\u0903\u0901\u0943]'
_AA = 'ा'
_VIRAMA = '्'


def _dvtt_decode(s):
    """Transcode a DVTTSurekh string to Unicode Devanagari."""
    out = []
    for ch in s:
        if ch in _MAP:
            out.append(_MAP[ch])
        elif ch.isascii() and (ch.isdigit() or ch.isalpha() or ch in '()[]{}/.,:;-'):
            out.append(ch)
        else:
            out.append('�')
    t = ''.join(out)
    t = re.sub(_VIRAMA + '(' + _INNER + '*)' + _REPHA, _VIRAMA + _REPHA + r'\1', t)
    t = re.sub('(' + _CONS + ')' + _VIRAMA + _REPHA + '(' + _INNER + '*?)' + _AA,
               _REPHA_DONE + r'\1\2', t)
    t = re.sub(_VIRAMA + '(' + _INNER + '*?)' + _AA, r'\1', t)
    for a, b in (('ाे', 'ो'), ('ाै', 'ौ'), ('ाॉ', 'ॉ'),
                 ('अा', 'आ'), ('अो', 'ओ'), ('अौ', 'औ'),
                 ('अे', 'ए'), ('अै', 'ऐ')):
        t = t.replace(a, b)
    t = re.sub(_IREPHA + '(' + _CLUSTER + ')', _REPHA_DONE + r'\1' + _I_PLACED, t)
    t = re.sub('िं(' + _CLUSTER + ')', r'\1िं', t)
    t = re.sub('ि(' + _CLUSTER + ')', r'\1ि', t)
    t = re.sub('(' + _CLUSTER + ')(' + _MATRAS + '*)' + _REPHA, _REPHA_DONE + r'\1\2', t)
    t = t.replace('\u0907' + _REPHA, 'ई')
    t = t.replace(_REPHA_DONE, 'र्').replace(_REPHA, 'र्').replace(_I_PLACED, 'ि')
    return unicodedata.normalize('NFC', t)


# ── Font type detection ──────────────────────────────────────────────────

def _detect_font_type(pdf):
    """Detect which font type a PDF uses from its first few pages.

    Returns one of: 'dvtt', 'unicode_mangal', 'shree_dvtt', 'shree_surekh', 'cdac'

    'shree_dvtt'   = SHREE708 headers + DV-TTSurekh data (AC034-039, 063-068)
    'shree_surekh' = SHREE708 headers + DV-Surekh-Normal data (AC072-082, 089-090)
    """
    fonts = set()
    for page in pdf.pages[:3]:
        for char in page.chars[:500]:
            fn = char.get('fontname', '')
            fonts.add(fn)

    fonts_lower = ' '.join(f.lower() for f in fonts)

    if 'cdac' in fonts_lower or 'gistsurekh' in fonts_lower:
        return 'cdac'
    if 'mangal' in fonts_lower or 'arial-unicode' in fonts_lower or 'varial' in fonts_lower:
        return 'unicode_mangal'
    if 'shree' in fonts_lower:
        # Distinguish: DV-TTSurekh data vs DV-Surekh-Normal data
        if 'dv-surekh-normal' in fonts_lower or 'univers' in fonts_lower:
            return 'shree_surekh'
        return 'shree_dvtt'
    # Default: DVTTSurekh or DV-Prakash or similar legacy
    return 'dvtt'


def _get_font_config(font_type):
    """Return (fallback_centres, narrow_cols, decode_names, decode_rel_sex) for a font type."""
    if font_type == 'dvtt':
        return FALLBACK_CENTRES_DVTT, {4, 6, 7}, True, True
    elif font_type == 'unicode_mangal':
        return FALLBACK_CENTRES_MANGAL, {4, 6, 7}, False, False
    elif font_type in ('shree_dvtt', 'shree_surekh'):
        return FALLBACK_CENTRES_SHREE, {4, 6, 7}, font_type == 'shree_dvtt', False
    elif font_type == 'cdac':
        return FALLBACK_CENTRES_CDAC, {4, 6, 7}, False, False
    return FALLBACK_CENTRES_DVTT, {4, 6, 7}, True, True


# ── SHREE708 relation/sex code mapping ───────────────────────────────────
# SHREE708 font uses its own glyph mapping. The relation and sex fields
# use short codes that we normalize to Hindi labels.

_SHREE_REL_MAP = {
    'G–.': 'पिता',   # Father
    'G\u2013.': 'पिता',
    '–.': 'पति',     # Husband (or Mother depending on context)
    '\u2013.': 'पति',
    '(cid:238).': 'माता',  # Mother
}

_SHREE_SEX_MAP = {
    '–.N': 'पुरुष',    # Male
    '\u2013.N': 'पुरुष',
    '(cid:238).': 'महिला',  # Female
}


def _normalize_shree_rel(text):
    """Normalize SHREE708 relation code to Hindi."""
    text = text.strip()
    if text in _SHREE_REL_MAP:
        return _SHREE_REL_MAP[text]
    # Try partial matches
    if text.startswith('G') and '.' in text:
        return 'पिता'
    if '(cid:238)' in text:
        return 'माता'
    if text.endswith('.') and len(text) <= 3:
        return 'पति'
    return text


def _normalize_shree_sex(text):
    """Normalize SHREE708 sex code to Hindi."""
    text = text.strip()
    if text in _SHREE_SEX_MAP:
        return _SHREE_SEX_MAP[text]
    if '(cid:238)' in text:
        return 'महिला'
    if 'N' in text:
        return 'पुरुष'
    return text


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
    # Try (1)(2)... format
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
    # Try bare digits: 1 2 3 ...
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
    # Try "1." "2." "3." ... format (used by unicode_mangal, shree, cdac)
    dotted = [w for w in words if re.fullmatch(r"\d+\.", w["text"])]
    if len(dotted) >= min_cols:
        dotted.sort(key=lambda w: w["top"])
        for i in range(len(dotted)):
            group = [dotted[i]]
            for j in range(i + 1, len(dotted)):
                if abs(dotted[j]["top"] - dotted[i]["top"]) <= ROW_TOL:
                    group.append(dotted[j])
            if len(group) >= min_cols:
                labels = sorted(group, key=lambda w: w["x0"])
                nums = [int(w["text"].rstrip('.')) for w in labels]
                if nums[0] == 1 and nums[1] == 2:
                    return {n: (w["x0"] + w["x1"]) / 2 for n, w in zip(nums, labels)}
    return None


def _make_boundaries(col_centres, narrow_cols=None):
    if narrow_cols is None:
        narrow_cols = NARROW_COLS
    cols = sorted(col_centres.keys())
    boundaries = {}
    for i, col in enumerate(cols):
        if i == 0:
            left = 0
        else:
            gap = col_centres[col] - col_centres[cols[i - 1]]
            if col in narrow_cols:
                left = col_centres[col] - gap * 0.2
            elif cols[i - 1] in narrow_cols:
                left = col_centres[cols[i - 1]] + gap * 0.2
            else:
                left = col_centres[cols[i - 1]] + gap * 0.5
        if i == len(cols) - 1:
            right = 9999
        else:
            gap = col_centres[cols[i + 1]] - col_centres[col]
            if col in narrow_cols:
                right = col_centres[col] + gap * 0.2
            elif cols[i + 1] in narrow_cols:
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
        # Match (1), 1, or 1. formats
        if re.fullmatch(r"\(?\d\)?\.?", txt):
            cx = (w["x0"] + w["x1"]) / 2
            digit = int(re.search(r"\d", txt).group())
            if digit in col_centres and abs(cx - col_centres[digit]) < 20:
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

    DVTTSurekh text is non-ASCII, so "alphabetic" checks must look for
    absence of digits rather than ASCII letters.
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


# ── Type 1: DVTTSurekh extraction (original logic) ──────────────────────

def _extract_page_dvtt(page, fallback_centres=None):
    """Extract data from a DVTTSurekh-font page."""
    words = page.extract_words()
    if not words:
        return [], fallback_centres
    fc = fallback_centres or FALLBACK_CENTRES_DVTT
    col_centres = _find_column_row(words) or fc
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
            # Decode DVTTSurekh text to Unicode Devanagari (skip serial, age, epic)
            for idx in (_HOUSE, _NAME, _REL, 4, 5):
                cells[idx] = _dvtt_decode(cells[idx])
            records.append(cells)
    return records, col_centres


# ── Type 2: Unicode Mangal extraction ────────────────────────────────────

def _normalize_mangal_relation(rel_code):
    """Map English relation codes (H/F/O) to Hindi."""
    rel_code = rel_code.strip().upper()
    if rel_code == 'F':
        return 'पिता'
    elif rel_code == 'H':
        return 'पति'
    elif rel_code in ('M', 'O'):
        return 'माता' if rel_code == 'M' else 'अन्य'
    return rel_code


def _normalize_mangal_sex(sex_code):
    """Map English sex codes (M/F) to Hindi."""
    sex_code = sex_code.strip().upper()
    if sex_code == 'M':
        return 'पुरुष'
    elif sex_code == 'F':
        return 'महिला'
    return sex_code


def _extract_page_mangal(page, fallback_centres=None):
    """Extract data from an Arial-Unicode/Mangal page."""
    words = page.extract_words()
    if not words:
        return [], fallback_centres
    fc = fallback_centres or FALLBACK_CENTRES_MANGAL
    col_centres = _find_column_row(words) or fc
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
            # Normalize English relation/sex codes to Hindi
            cells[_REL] = _normalize_mangal_relation(cells[_REL])
            cells[5] = _normalize_mangal_sex(cells[5])
            # Remove stray quote marks from EPIC column
            cells[7] = cells[7].strip().strip('"').strip()
            records.append(cells)
    return records, col_centres


# ── Type 4: CDAC_GISTSurekh extraction ───────────────────────────────────

def _extract_page_cdac(page, fallback_centres=None):
    """Extract data from a CDAC_GISTSurekh page."""
    words = page.extract_words()
    if not words:
        return [], fallback_centres
    fc = fallback_centres or FALLBACK_CENTRES_CDAC
    col_centres = _find_column_row(words) or fc
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
            records.append(cells)
    return records, col_centres


# ── Type 3: SHREE708 extraction (multi-line records) ─────────────────────
# SHREE ACs have records spread across multiple y-positions per record:
#   Line A (y+0.0): relation code (x≈304) + sex code (x≈435)  [SHREE font]
#   Line B (y+3.4): name (x≈172) + relname (x≈328)  [DV-TTSurekh/DV-Surekh]
#   Line C (y+4.2): house (x≈137)
#   Line D (y+5.1): serial (x≈105) + age (x≈459)   [Times-Roman]
#   Line E (y+6.3): epic (x≈503)                    [Times-Roman]
# Within-record gap ≈ 0.8-3.4pt; between-record gap ≈ 7.2-8.4pt.

_SHREE_RECORD_GAP = 6.5  # threshold to split records (> 3.4 within, < 7.2 between)


def _extract_page_shree(page, fallback_centres=None, decode_names=True):
    """Extract data from a SHREE708 + DVTTSurekh/DV-Surekh page.

    Uses multi-line record merging: groups nearby lines into records
    using gap-based splitting, then assigns fields by x-position.

    If decode_names is True, applies DVTTSurekh decoding to name fields.
    If False (DV-Surekh-Normal), extracts text as-is.
    """
    # Use tight y_tolerance to avoid merging separate lines
    words = page.extract_words(x_tolerance=2, y_tolerance=1)
    if not words:
        return [], fallback_centres

    fc = fallback_centres or FALLBACK_CENTRES_SHREE

    # Detect column header row to skip
    col_centres = _find_column_row(words) or fc
    col_row_top = _find_col_row_top(words, col_centres)
    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]

    if not data_words:
        return [], col_centres

    # Sort by y position
    data_words.sort(key=lambda w: (w["top"], w["x0"]))

    # Group words into "record blocks" — sequences of lines separated by
    # gaps <= _SHREE_RECORD_GAP.  Track the max top within each block.
    blocks = []
    current_block = [data_words[0]]
    current_max_top = data_words[0]["top"]
    for w in data_words[1:]:
        if w["top"] - current_max_top <= _SHREE_RECORD_GAP:
            current_block.append(w)
            current_max_top = max(current_max_top, w["top"])
        else:
            blocks.append(current_block)
            current_block = [w]
            current_max_top = w["top"]
    blocks.append(current_block)

    records = []
    for block in blocks:
        # Within each block, identify fields by x-position
        serial = ""
        house = ""
        name_parts = []
        rel_code = ""
        relname_parts = []
        sex_code = ""
        age = ""
        epic = ""

        for w in sorted(block, key=lambda w: w["x0"]):
            x = w["x0"]
            text = w["text"].strip()
            if not text:
                continue

            # Serial number: x < 120, pure digits
            if x < 120 and re.fullmatch(r"\d+", text):
                if not serial:
                    serial = text
                continue

            # House number: x ≈ 120-165, usually has digits
            if 120 <= x < 165 and re.search(r"\d", text):
                house_text = text.rstrip('/')
                if house:
                    house += " " + house_text
                else:
                    house = house_text
                continue

            # Name: x ≈ 165-295
            if 165 <= x < 295:
                name_parts.append(text)
                continue

            # Relation code: x ≈ 295-325 (SHREE codes like G–., –.)
            if 295 <= x < 325:
                if rel_code:
                    rel_code += text
                else:
                    rel_code = text
                continue

            # Relative name: x ≈ 325-430
            if 325 <= x < 430:
                relname_parts.append(text)
                continue

            # Sex code: x ≈ 430-458 (SHREE codes like –.N, (cid:238).)
            if 430 <= x < 458:
                if sex_code:
                    sex_code += text
                else:
                    sex_code = text
                continue

            # Age: x ≈ 458-500, pure digits
            if 458 <= x < 500 and re.fullmatch(r"\d+", text):
                age = text
                continue

            # EPIC: x >= 500
            if x >= 500 and re.search(r"[A-Z0-9/]", text):
                if epic:
                    epic += text
                else:
                    epic = text
                continue

        if not serial or not re.fullmatch(r"\d+", serial):
            continue

        name_raw = " ".join(name_parts)
        relname_raw = " ".join(relname_parts)

        # Decode name/relname text (DVTTSurekh only; DV-Surekh-Normal is kept as-is)
        if decode_names:
            name_decoded = _dvtt_decode(name_raw)
            relname_decoded = _dvtt_decode(relname_raw)
            house_decoded = _dvtt_decode(house) if house and not re.fullmatch(r"[\d/\- ]+", house) else house
        else:
            name_decoded = name_raw
            relname_decoded = relname_raw
            house_decoded = house

        # Normalize SHREE relation/sex codes
        rel_normalized = _normalize_shree_rel(rel_code)
        sex_normalized = _normalize_shree_sex(sex_code)

        # Clean EPIC: strip SHREE font artifacts, keep only valid EPIC chars
        epic = re.sub(r'\(cid:\d+\)', '', epic)
        epic = ''.join(c for c in epic if c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/')
        epic = epic.strip('/')

        cells = [serial, house_decoded, name_decoded, rel_normalized,
                 relname_decoded, sex_normalized, age, epic]
        records.append(cells)

    return records, col_centres


# ── Main PDF extraction dispatcher ───────────────────────────────────────

def _extract_pdf(pdf_bytes, font_type=None):
    """Extract voter records from a PDF, auto-detecting font type if needed."""
    all_rows = []
    col_centres = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if font_type is None:
            font_type = _detect_font_type(pdf)

        if font_type in ('shree_dvtt', 'shree_surekh'):
            # SHREE uses its own multi-line extraction per page
            decode = (font_type == 'shree_dvtt')
            for page in pdf.pages:
                rows, col_centres = _extract_page_shree(page, col_centres, decode_names=decode)
                all_rows.extend(rows)
        elif font_type == 'unicode_mangal':
            for page in pdf.pages:
                rows, col_centres = _extract_page_mangal(page, col_centres)
                all_rows.extend(rows)
        elif font_type == 'cdac':
            for page in pdf.pages:
                rows, col_centres = _extract_page_cdac(page, col_centres)
                all_rows.extend(rows)
        else:
            # dvtt (default)
            for page in pdf.pages:
                rows, col_centres = _extract_page_dvtt(page, col_centres)
                all_rows.extend(rows)
    return all_rows


def _extract_ac_zip(zip_path):
    all_rows = []
    manifest = {}
    with zipfile.ZipFile(zip_path) as zf:
        if "manifest.json" in zf.namelist():
            manifest = json.loads(zf.read("manifest.json"))
        part_files = sorted([n for n in zf.namelist() if n.endswith(".pdf")])

        # Detect font type from the first PDF
        font_type = None
        if part_files:
            first_pdf = part_files[min(1, len(part_files) - 1)]  # skip part 1 cover
            with pdfplumber.open(io.BytesIO(zf.read(first_pdf))) as pdf:
                font_type = _detect_font_type(pdf)

        for pf in part_files:
            m = re.search(r"(\d+)", os.path.basename(pf))
            if not m:
                continue
            part_no = int(m.group(1))
            rows = _extract_pdf(zf.read(pf), font_type=font_type)
            for cells in rows:
                all_rows.append([part_no] + cells)
    return all_rows, manifest


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


# -- Hindi transliteration (Devanagari → English) -----------------------------

_RELATION_EN_MAP = {
    'पिता': 'Father', 'पि.': 'Father', 'पि-': 'Father', 'पि': 'Father',
    'पति': 'Husband', 'प.': 'Husband', 'प-': 'Husband', 'प': 'Husband',
    'माता': 'Mother', 'मा.': 'Mother', 'मा-': 'Mother', 'मा': 'Mother',
    'अन्य': 'Other', 'अ-': 'Other', 'अ.': 'Other', 'अ': 'Other',
}

_SEX_EN_MAP = {
    'पु.': 'M', 'पुरुष': 'M', 'पुरूष': 'M', 'पु-': 'M', 'M': 'M',
    'म.': 'F', 'महिला': 'F', 'स्त्री': 'F', 'म-': 'F', 'F': 'F',
}


def _transliterate_hindi(text):
    """Transliterate Devanagari text to readable English (Title Case)."""
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate as _translit

    if not text or not text.strip():
        return ''
    itrans = _translit(text.strip(), sanscript.DEVANAGARI, sanscript.ITRANS)

    result = itrans
    result = result.replace('j~n', 'gy')
    result = result.replace('~n', 'n')
    result = result.replace('.n', 'n')
    result = result.replace('.N', 'n')
    result = result.replace('RRi', 'ri').replace('R^i', 'ri')
    result = result.replace('.Dh', 'dh').replace('.D', 'd')

    # anusvara before h -> ngh (सिंह -> singh), else -> n
    result = re.sub(r'Mh', 'ngh', result)
    result = result.replace('M', 'n')

    result = result.replace('Sh', 'sh')
    result = result.replace('Ch', 'chh')
    result = result.replace('shh', 'sh')
    result = result.replace('T', 't')
    result = result.replace('D', 'd')
    result = result.replace('N', 'n')
    result = result.replace('H', 'h')

    # Long vowels: mark long-A so schwa deletion skips it
    MARKER = '\x01'
    result = result.replace('A', MARKER)
    result = result.replace('I', 'i')
    result = result.replace('U', 'u')
    result = result.replace('a' + MARKER, MARKER)

    # Schwa deletion: drop trailing inherent 'a' (unmarked) from words
    words = result.split()
    cleaned = []
    for w in words:
        if len(w) > 2 and w.endswith('a') and not w.endswith(MARKER):
            if w[-2] not in 'aeiou' + MARKER:
                w = w[:-1]
        cleaned.append(w)

    result = ' '.join(cleaned)
    result = result.replace(MARKER, 'a')
    return result.title()


def _transliterate_csv(csv_path):
    """Add elector_name_en, relation_name_en, relation_type_en, sex_en columns.

    Uses a two-pass streaming approach to avoid loading all rows into memory
    (critical for states with 30M+ rows like Rajasthan/MP).
    """
    import shutil
    import tempfile

    NEW_COLS = ['elector_name_en', 'relation_name_en', 'relation_type_en', 'sex_en']

    # Pass 1: read header and collect unique names (not full rows)
    with open(csv_path, encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if any(c in reader.fieldnames for c in NEW_COLS):
            print(f"  Columns already present, skipping: {csv_path}")
            return
        out_fields = list(reader.fieldnames) + NEW_COLS

        names = set()
        total = 0
        for row in reader:
            names.add(row.get('elector_name', ''))
            names.add(row.get('relation_name', ''))
            total += 1
    names.discard('')

    print(f"  Pass 1: {total:,} rows, {len(names):,} unique names")
    print(f"  Transliterating...", end=' ', flush=True)
    cache = {'': ''}
    for n in names:
        cache[n] = _transliterate_hindi(n)
    del names  # free memory
    print("done")

    # Pass 2: stream rows, add transliterated columns, write to temp file
    tmp = tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                      dir=os.path.dirname(csv_path),
                                      suffix='.csv', delete=False)
    try:
        empty_name = 0
        with open(csv_path, encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=out_fields)
            writer.writeheader()
            for row in reader:
                en = cache.get(row.get('elector_name', ''), '')
                row['elector_name_en'] = en
                row['relation_name_en'] = cache.get(row.get('relation_name', ''), '')
                rel = row.get('relation', '').strip()
                row['relation_type_en'] = _RELATION_EN_MAP.get(rel, '')
                sex = row.get('sex', '').strip()
                row['sex_en'] = _SEX_EN_MAP.get(sex, '')
                writer.writerow(row)
                if not en:
                    empty_name += 1
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"  {total:,} rows, {len(cache)-1:,} unique names transliterated")
    print(f"  Empty elector_name_en: {empty_name:,} ({100*empty_name/max(total,1):.1f}%)")


# -- Main ----------------------------------------------------------------------


# ── CSV post-processing ──────────────────────────────────────────────────

# Normalize sex: all font variants → पु. / म.
_SEX_MAP = {
    "पुरुष": "पु.", "पु.": "पु.", "पु": "पु.",
    "महिला": "म.", "म.": "म.", "म": "म.",
    "पपरर": "पु.",     # garbled Arial-Unicode for पुरुष
    "मवहलध": "म.",     # garbled Arial-Unicode for महिला
    "!पु.": "पु.", "!म.": "म.",
    "": "",
}

# Normalize relation: all font variants → पि. / प. / मा. / अ.
_REL_MAP = {
    "पिता": "पि.", "पि.": "पि.", "पि": "पि.",
    "पति": "प.", "प.": "प.", "प": "प.",
    "माता": "मा.", "मा.": "मा.", "मा": "मा.",
    "अ.": "अ.",
    "वपतध": "पि.",     # garbled Arial-Unicode for पिता
    "पवत": "प.",       # garbled Arial-Unicode for पति
    "मधतध": "मा.",     # garbled Arial-Unicode for माता
    "": "",
}


def _postprocess(csv_path):
    """Fix column-bleeding issues in an already-extracted CSV.

    Chhattisgarh-specific fixes (5 font types across 90 ACs):
    1. Normalize sex values from all font variants to पु./म.
    2. Normalize relation values from all font variants to पि./प./मा./अ.
    3. Clear unrecognized font garbage to empty.
    """
    import tempfile, shutil

    fixes = {"sex_cleaned": 0, "rel_cleaned": 0, "total": 0}

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

                if sex in _SEX_MAP:
                    row["sex"] = _SEX_MAP[sex]
                else:
                    row["sex"] = ""
                    fixes["sex_cleaned"] += 1

                if rel in _REL_MAP:
                    row["relation"] = _REL_MAP[rel]
                else:
                    row["relation"] = ""
                    fixes["rel_cleaned"] += 1

                writer.writerow(row)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed")
    print(f"  {fixes['sex_cleaned']} sex values cleared (unrecognized)")
    print(f"  {fixes['rel_cleaned']} relation values cleared (unrecognized)")


def main():
    ap = argparse.ArgumentParser(description="Extract Chhattisgarh SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/chhattisgarh")
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
