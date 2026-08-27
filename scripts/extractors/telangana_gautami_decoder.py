"""
Telangana Gautami font decoder.

The Telangana PDFs use an embedded Gautami Telugu font with a broken ToUnicode
CMap: all vowel signs (matras), virama, anusvara, and conjunct forms are stripped,
leaving only bare consonants.  This module rebuilds the correct byte → Unicode
mapping per-AC using glyph-outline fingerprinting against a manually-verified
reference font (from AC188).

Usage in extract_telangana.py:

    from telangana_gautami_decoder import build_corrected_cmap, decode_gautami_text

    corrected = build_corrected_cmap(zip_path)
    # Then for each page's Telugu text bytes:
    text = decode_gautami_text(raw_bytes, corrected)
"""

import hashlib
import io
import re
from collections import defaultdict

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1


# ── Master mapping (from AC188 manual verification) ─────────────────────
# byte value in AC188 embedded font → correct Unicode string.
# Built by aligning OCR output, rendered glyph images, and glyph-width
# analysis across dozens of verified Telugu words.
#
# Categories:
#  - Single Telugu codepoints (consonants, vowels, matras)
#  - Two-codepoint sequences (consonant+matra, virama+consonant subscripts)

_MASTER_CORRECTED = {
    # ── Independent vowels ──
    0x20: '\u0C05',         # అ (a)
    0x56: '\u0C08',         # ఈ (ii)
    0x98: '\u0C07',         # ఇ (i)
    0xAC: '\u0C0E',         # ఎ (e)
    0xB0: '\u0C12',         # ఒ (o)
    0xB1: '\u0C09',         # ఉ (u)
    0xBA: '\u0C06',         # ఆ (aa)
    0xBC: '\u0C06',         # ఆ (aa)
    0xD5: '\u0C06',         # ఆ (aa)
    0xEF: '\u0C12',         # ఒ (o)
    0xF6: '\u0C06',         # ఆ (aa) (or virama — context-dependent, safe as ఆ)

    # ── Consonants (base, with inherent 'a' vowel) ──
    0x29: '\u0C2A',         # ప (pa)
    0x2B: '\u0C24',         # త (ta)
    0x2D: '\u0C35',         # వ (va)
    0x2E: '\u0C39',         # హ (ha)
    0x31: '\u0C2E',         # మ (ma)
    0x32: '\u0C39',         # హ (ha)
    0x35: '\u0C2C',         # బ (ba)
    0x37: '\u0C17',         # గ (ga)
    0x38: '\u0C38',         # స (sa)
    0x3C: '\u0C2D',         # భ (bha)
    0x3E: '\u0C38',         # స (sa)
    0x3F: '\u0C38',         # స (sa)
    0x40: '\u0C15',         # క (ka)
    0x42: '\u0C30',         # ర (ra)
    0x44: '\u0C28',         # న (na)
    0x48: '\u0C2F',         # య (ya)
    0x4A: '\u0C21',         # డ (Da)
    0x4C: '\u0C24',         # త (ta)
    0x4D: '\u0C32',         # ల (la)
    0x55: '\u0C28',         # న (na)
    0x57: '\u0C30',         # ర (ra)
    0x58: '\u0C28',         # న (na)
    0x5A: '\u0C21',         # డ (Da)
    0x5C: '\u0C38',         # స (sa)
    0x60: '\u0C36',         # శ (sha)
    0x61: '\u0C2E',         # మ (ma)
    0x62: '\u0C1C',         # జ (ja)
    0x65: '\u0C35',         # వ (va)
    0x67: '\u0C15',         # క (ka)
    0x68: '\u0C1F',         # ట (Ta)
    0x6B: '\u0C33',         # ళ (LLa)
    0x6C: '\u0C30',         # ర (ra)
    0x6F: '\u0C36',         # శ (sha)
    0x71: '\u0C1A',         # చ (cha)
    0x73: '\u0C2A',         # ప (pa)
    0x76: '\u0C2A',         # ప (pa)
    0x7E: '\u0C27',         # ధ (dha)
    0x7F: '\u0C23',         # ణ (Na)
    0x80: '\u0C17',         # గ (ga)
    0x82: '\u0C1C',         # జ (ja)
    0x85: '\u0C1C',         # జ (ja)
    0x88: '\u0C28',         # న (na)
    0x89: '\u0C2C',         # బ (ba)
    0x8A: '\u0C38',         # స (sa)
    0x8B: '\u0C32',         # ల (la)
    0x8D: '\u0C18',         # ఘ (gha)
    0x8E: '\u0C15',         # క (ka)
    0x8F: '\u0C35',         # వ (va)
    0x91: '\u0C24',         # త (ta)
    0x92: '\u0C32',         # ల (la)
    0x93: '\u0C2D',         # భ (bha)
    0x94: '\u0C33',         # ళ (LLa)
    0x95: '\u0C33',         # ళ (LLa)
    0x99: '\u0C1A',         # చ (cha)
    0x9C: '\u0C37',         # ష (ssa)
    0x9E: '\u0C21',         # డ (Da)
    0xA0: '\u0C1A',         # చ (cha)
    0xA1: '\u0C2D',         # భ (bha)
    0xA2: '\u0C15',         # క (ka)
    0xA8: '\u0C2E',         # మ (ma)
    0xAB: '\u0C1A',         # చ (cha)
    0xAD: '\u0C15',         # క (ka)
    0xAE: '\u0C2C',         # బ (ba)
    0xAF: '\u0C37',         # ష (ssa)
    0xB2: '\u0C2E',         # మ (ma)
    0xB5: '\u0C1C',         # జ (ja)
    0xB6: '\u0C35',         # వ (va)
    0xB7: '\u0C38',         # స (sa)
    0xB8: '\u0C2C',         # బ (ba)
    0xBE: '\u0C32',         # ల (la)
    0xC1: '\u0C36',         # శ (sha)
    0xC2: '\u0C38',         # స (sa)
    0xC3: '\u0C15',         # క (ka)
    0xC5: '\u0C2F',         # య (ya)
    0xC7: '\u0C36',         # శ (sha)
    0xC8: '\u0C1C',         # జ (ja)
    0xC9: '\u0C28',         # న (na)
    0xCA: '\u0C26',         # ద (da)
    0xCB: '\u0C28',         # న (na)
    0xCC: '\u0C2E',         # మ (ma)
    0xCF: '\u0C25',         # థ (tha)
    0xD0: '\u0C15',         # క (ka)
    0xD1: '\u0C38',         # స (sa)
    0xD4: '\u0C15',         # క (ka)
    0xD6: '\u0C2F',         # య (ya)
    0xD7: '\u0C2E',         # మ (ma)
    0xD9: '\u0C26',         # ద (da)
    0xDA: '\u0C38',         # స (sa)
    0xDD: '\u0C30',         # ర (ra)
    0xE0: '\u0C16',         # ఖ (kha)
    0xE1: '\u0C30',         # ర (ra)
    0xE2: '\u0C1A',         # చ (cha)
    0xE3: '\u0C28',         # న (na)
    0xE4: '\u0C17',         # గ (ga)
    0xE5: '\u0C2D',         # భ (bha)
    0xE6: '\u0C24',         # త (ta)
    0xE8: '\u0C33',         # ళ (LLa)
    0xE9: '\u0C2F',         # య (ya)
    0xEA: '\u0C32',         # ల (la)
    0xEB: '\u0C1F',         # ట (Ta)
    0xED: '\u0C28',         # న (na)
    0xEE: '\u0C2A',         # ప (pa)
    0xF0: '\u0C2F',         # య (ya)
    0xF2: '\u0C2B',         # ఫ (pha)
    0xF3: '\u0C2B',         # ఫ (pha)
    0xF4: '\u0C21',         # డ (Da)
    0xF5: '\u0C35',         # వ (va)
    0xF7: '\u0C2A',         # ప (pa)

    # ── Combining vowel signs (matras) ──
    0x28: '\u0C3E',         # ా (aa-matra)
    0x2A: '\u0C41',         # ు (u-matra)
    0x46: '\u0C3F',         # ి (i-matra)
    0x47: '\u0C3E',         # ా (aa-matra)
    0x53: '\u0C41',         # ు (u-matra)
    0x63: '\u0C47',         # ే (ee-matra)
    0x64: '\u0C41',         # ు (u-matra)
    0x66: '\u0C46',         # ె (e-matra)
    0x7A: '\u0C41',         # ు (u-matra)
    0x7B: '\u0C42',         # ూ (uu-matra)
    0x7C: '\u0C42',         # ూ (uu-matra)
    0x7D: '\u0C46',         # ె (e-matra)
    0x81: '\u0C4B',         # ో (oo-matra)
    0x90: '\u0C41',         # ు (u-matra)
    0x96: '\u0C43',         # ృ (ri-matra)
    0x9B: '\u0C43',         # ృ (ri-matra)
    0xA4: '\u0C41',         # ు (u-matra)
    0xA5: '\u0C46',         # ె (e-matra)
    0xA6: '\u0C23',         # ణ (Na) — context: follows ష్ in కృష్ణ
    0xAA: '\u0C4C',         # ౌ (au-matra)
    0x3B: '\u0C40',         # ీ (ii-matra)
    0x4B: '\u0C4A',         # ొ (o-matra)
    0x51: '\u0C4A',         # ొ (o-matra)
    0x5D: '\u0C3E',         # ా (aa-matra)

    # ── Anusvara / special ──
    0x2C: '\u0C02',         # ం (anusvara)
    0x22: ' ',              # space
    0x49: '.',              # period

    # ── Virama (standalone) ──
    0x2F: '\u0C4D',         # ్
    0x36: '\u0C4D',         # ్
    0x4E: '\u0C4D',         # ్
    0x52: '\u0C4D',         # ్
    0x77: '\u0C4D',         # ్
    0x83: '\u0C4D',         # ్
    0x84: '\u0C4D',         # ్

    # ── Pre-composed consonant + matra ──
    0x23: '\u0C16\u0C3E',   # ఖా (kha + aa)
    0x72: '\u0C32\u0C3F',   # లి (la + i)
    0x59: '\u0C24\u0C4A',   # తొ (ta + o)
    0x70: '\u0C24\u0C3F',   # తి (ta + i)
    0x43: '\u0C2F\u0C3E',   # యా (ya + aa)

    # ── Pre-composed consonant + virama (dead consonants) ──
    0x4F: '\u0C32\u0C4D',   # ల్
    0x50: '\u0C37\u0C4D',   # ష్
    0x6A: '\u0C33\u0C4D',   # ళ్

    # ── Subscript consonant forms (virama + consonant, zero-width) ──
    0x39: '\u0C4D\u0C24',   # ్త
    0x3A: '\u0C4D\u0C30',   # ్ర
    0x69: '\u0C4D\u0C2F',   # ్య
    0x74: '\u0C4D\u0C2A',   # ్ప
    0x78: '\u0C4D\u0C32',   # ్ల
    0x87: '\u0C4D\u0C21',   # ్డ
    0x8C: '\u0C4D\u0C1C',   # ్జ
    0x97: '\u0C4D\u0C28',   # ్న
    0x9A: '\u0C4D\u0C1A',   # ్చ
    0x9D: '\u0C23',         # ణ (appears after ష్ in conjuncts)
    0x9F: '\u0C4D\u0C30',   # ్ర
    0xA3: '\u0C4D\u0C2A',   # ్ప
    0xA7: '\u0C4D\u0C39',   # ్హ ... actually హా? context-dependent
    0xA9: '\u0C4D\u0C39',   # ్హ
    0xB3: '\u0C4D\u0C2E',   # ్మ
    0xB4: '\u0C4D\u0C39',   # ్హ
    0xB9: '\u0C4D\u0C1F',   # ్ట
    0xBD: '\u0C4D\u0C2E',   # ్మ
    0xBF: '\u0C4D\u0C1F',   # ్ట
    0xC0: '\u0C4D\u0C15',   # ్క
    0xC4: '\u0C4D\u0C30',   # ్ర
    0xC6: '\u0C4D\u0C35',   # ్వ
    0xCD: '\u0C4D\u0C17',   # ్గ
    0xCE: '\u0C4D\u0C2A',   # ్ప
    0xD2: '\u0C4D\u0C28',   # ్న
    0xD3: '\u0C4D\u0C24',   # ్త
    0xD8: '\u0C4D\u0C17',   # ్గ
    0xDC: '\u0C4D\u0C32',   # ్ల
    0xDE: '\u0C4D\u0C24',   # ్త
    0xE7: '\u0C4D\u0C1A',   # ్చ
    0xEC: '\u0C4D\u0C32',   # ్ల
    0xF1: '\u0C4D\u0C2F',   # ్య

    # ── Remaining full-width consonants (CMap base is correct) ──
    0x21: '\u0C32',         # ల (la)
    0x24: '\u0C16',         # ఖ (kha) — or variant; CMap base
    0x25: '\u0C28',         # న (na)
    0x26: '\u0C2E',         # మ (ma)
    0x27: '\u0C26',         # ద (da)
    0x30: '\u0C26',         # ద (da)
    0x33: '\u0C2E',         # మ (ma)
    0x34: '\u0C21',         # డ (Da)
    0x3D: '\u0C2E',         # మ (ma)
    0x41: '\u0C15',         # క (ka)
    0x45: '\u0C28',         # న (na)
    0x54: '\u0C28',         # న (na)
    0x5B: '\u0C30',         # ర (ra)
    0x5E: '\u0C38',         # స (sa)
    0x5F: '\u0C2E',         # మ (ma)
    0x6D: '\u0C3F',         # ి (i-matra)
    0x6E: '\u0C30',         # ర (ra)
    0x75: '\u0C26',         # ద (da)
    0x79: '\u0C36',         # శ (sha)
    0x86: '\u0C1F',         # ట (Ta)
    0xBB: '\u0C27',         # ధ (dha)
}


def _glyph_fingerprint(glyphset, gname, hmtx_table=None):
    """Compute an outline-based fingerprint for a TrueType glyph.

    Uses absolute coordinates, bounding box, advance width, and full contour
    data to produce a hash that is unique across the ~215 glyphs in a
    Gautami subset font.  Earlier versions normalised to a unit box which
    caused massive collisions; using raw coordinates avoids that.
    """
    rpen = RecordingPen()
    bpen = BoundsPen(glyphset)
    try:
        glyphset[gname].draw(rpen)
        glyphset[gname].draw(bpen)
    except Exception:
        return None
    bounds = bpen.bounds
    if not bounds:
        return None

    xmin, ymin, xmax, ymax = bounds
    width = 0
    if hmtx_table is not None:
        try:
            width, _lsb = hmtx_table[gname]
        except KeyError:
            pass

    parts = [f"w={width}", f"b={xmin:.0f},{ymin:.0f},{xmax:.0f},{ymax:.0f}"]
    for op, args in rpen.value:
        if op in ("moveTo", "lineTo"):
            x, y = args[0]
            parts.append(f"{op[0]}{x:.0f},{y:.0f}")
        elif op == "qCurveTo":
            pts = ",".join(f"{p[0]:.0f},{p[1]:.0f}" for p in args)
            parts.append(f"q{pts}")
        elif op == "closePath":
            parts.append("c")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


# ── Build master fingerprint table (lazy, once per process) ────────────

_MASTER_FP = None  # fingerprint → corrected Unicode string


def _ensure_master_fp(reference_font_path=None):
    """Build master fingerprint → corrected-unicode map from a reference font.

    If *reference_font_path* is None the function looks for the font file
    that was extracted during investigation at ``/tmp/gautami_ac188.ttf``.
    If that is missing it falls back to extracting the font from the first
    Telangana AC ZIP it can find.
    """
    global _MASTER_FP
    if _MASTER_FP is not None:
        return

    _MASTER_FP = {}

    # Try to load a reference font
    font_data = None
    if reference_font_path:
        try:
            with open(reference_font_path, "rb") as f:
                font_data = f.read()
        except FileNotFoundError:
            pass

    if font_data is None:
        # Try /tmp cache
        import os
        for p in ("/tmp/gautami_ac188.ttf",):
            if os.path.exists(p) and os.path.getsize(p) > 0:
                with open(p, "rb") as f:
                    font_data = f.read()
                break

    if font_data is None:
        # Cannot build master FP; corrected CMap will rely on fallback only
        return

    ttf = TTFont(io.BytesIO(font_data))
    gs = ttf.getGlyphSet()
    cmap = ttf["cmap"].tables[0].cmap
    hmtx = ttf["hmtx"]

    for pua_cp, gname in cmap.items():
        bv = pua_cp - 0xF000
        fp = _glyph_fingerprint(gs, gname, hmtx)
        if fp and bv in _MASTER_CORRECTED:
            _MASTER_FP[fp] = _MASTER_CORRECTED[bv]


# ── Public API ─────────────────────────────────────────────────────────


def build_corrected_cmap(zip_path):
    """Build a corrected byte → Unicode mapping for one Telangana AC ZIP.

    Returns a dict  ``{byte_value: unicode_string, ...}``  covering every
    byte used by the Gautami font in that ZIP's first PDF.
    """
    import zipfile

    _ensure_master_fp()

    z = zipfile.ZipFile(zip_path)
    pdfs = sorted(n for n in z.namelist() if n.endswith(".pdf"))
    if not pdfs:
        return {}

    pdf_bytes = z.read(pdfs[0])
    parser = PDFParser(io.BytesIO(pdf_bytes))
    doc = PDFDocument(parser)

    for pageno, pg in enumerate(PDFPage.create_pages(doc)):
        if pageno > 8:
            break
        resources = pg.resources
        fonts = resources.get("Font", {})
        for _fname, font_obj in fonts.items():
            font = resolve1(font_obj)
            if "Gautami" not in str(font.get("BaseFont", "")):
                continue

            # ── 1. Read the existing (broken) ToUnicode CMap as fallback ──
            local_cmap = {}
            if "ToUnicode" in font:
                data = resolve1(font["ToUnicode"]).get_data().decode("latin-1")
                for m in re.finditer(r"<(\w+)>\s+<\w+>\s+<(\w+)>", data):
                    local_cmap[int(m.group(1), 16)] = int(m.group(2), 16)

            # ── 2. Extract embedded font & fingerprint glyphs ──
            desc = resolve1(font.get("FontDescriptor", {}))
            for key in ("FontFile2", "FontFile", "FontFile3"):
                if key not in desc:
                    continue
                font_data = resolve1(desc[key]).get_data()
                try:
                    emb = TTFont(io.BytesIO(font_data))
                except Exception:
                    continue
                gs = emb.getGlyphSet()
                cmap_tbl = emb["cmap"].tables[0].cmap
                emb_hmtx = emb["hmtx"]

                corrected = {}
                for pua_cp, gname in cmap_tbl.items():
                    bv = pua_cp - 0xF000
                    fp = _glyph_fingerprint(gs, gname, emb_hmtx)
                    if fp and _MASTER_FP and fp in _MASTER_FP:
                        corrected[bv] = _MASTER_FP[fp]
                    elif bv in local_cmap:
                        corrected[bv] = chr(local_cmap[bv])
                    else:
                        corrected[bv] = "?"
                return corrected

    # No Gautami font found — return empty (AC might be English-only)
    return {}


def extract_gautami_text(pdf_bytes, page_idx, corrected_cmap):
    """Extract decoded Telugu text lines from one PDF page.

    Returns a list of ``(y_position, decoded_text)`` tuples, where
    *decoded_text* uses the corrected CMap.
    """
    parser = PDFParser(io.BytesIO(pdf_bytes))
    doc = PDFDocument(parser)

    for pageno, pg in enumerate(PDFPage.create_pages(doc)):
        if pageno != page_idx:
            continue
        contents = pg.contents
        if isinstance(contents, list):
            raw = b""
            for ref in contents:
                raw += resolve1(ref).get_data()
        else:
            raw = resolve1(contents).get_data()
        break
    else:
        return []

    stream = raw.decode("latin-1")
    current_font = None
    tx = ty = 0.0
    ops = []

    for line in stream.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"/(\w+)\s+([\d.]+)\s+Tf", line)
        if m:
            current_font = m.group(1)
            continue
        m = re.match(
            r"([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)"
            r"\s+([\d.\-]+)\s+([\d.\-]+)\s+Tm",
            line,
        )
        if m:
            tx, ty = float(m.group(5)), float(m.group(6))
            continue
        m = re.match(r"([\d.\-]+)\s+([\d.\-]+)\s+Td", line)
        if m:
            tx += float(m.group(1))
            ty += float(m.group(2))
            continue

        # Identify the Gautami font reference (usually 'b', but can vary)
        # We detect it as any font that isn't Arial-based
        if current_font and current_font not in ("9", "a"):
            for pm in re.findall(r"\(([^)]*)\)\s*Tj", line):
                raw_bytes_str = pm.encode("latin-1")
                raw_bytes = []
                i = 0
                while i < len(raw_bytes_str):
                    if raw_bytes_str[i] == 0x5C and i + 1 < len(raw_bytes_str):
                        raw_bytes.append(raw_bytes_str[i + 1])
                        i += 2
                    else:
                        raw_bytes.append(raw_bytes_str[i])
                        i += 1
                ops.append((tx, ty, raw_bytes))

    # Group by y position, decode
    by_y = defaultdict(list)
    for x, y, rb in ops:
        by_y[round(y, 0)].append((x, rb))

    result = []
    for y in sorted(by_y.keys(), reverse=True):
        entries = sorted(by_y[y], key=lambda e: e[0])
        all_bytes = []
        for _x, rb in entries:
            all_bytes.extend(rb)
        decoded = "".join(corrected_cmap.get(b, chr(b)) for b in all_bytes)
        result.append((y, decoded))

    return result


def decode_gautami_text(raw_bytes, corrected_cmap):
    """Decode a sequence of raw Gautami font bytes to correct Telugu Unicode."""
    return "".join(corrected_cmap.get(b, chr(b)) for b in raw_bytes)
