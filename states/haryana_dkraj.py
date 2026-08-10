"""
DK-RAJ -> Unicode Devanagari transcoder, for Haryana's 2002 electoral roll PDFs.

WHY THIS EXISTS
---------------
ceoharyana.gov.in's 2002 roll PDFs do have a real, selectable text layer -- but
the text is NOT Unicode. It is 8-bit legacy-font text: bytes in the WinAnsi
range that only render as Devanagari through the embedded "DK-RAJ,Bold" font
(an ISFOC/Shree-Lipi-family typeface predating widespread Unicode adoption in
Indian government publishing). Extracting with pdfplumber yields strings like
"ÊxÉ´ÉÉÇSÉEò", not "\u0928\u093f\u0930\u094d\u0935\u093e\u091a\u0915".
There is no /ToUnicode CMap, so no PDF library can do this mapping for us.

Two structural properties of the encoding drive the whole design:

  1. Consonants are stored as HALF forms (consonant + virama). A separate
     vertical-stem glyph, which we map to U+093E (AA), completes a half form
     into a full consonant. So "half form + stem" collapses to the plain
     consonant, and other matras may sit BETWEEN the two.
  2. It is a VISUAL encoding: glyphs appear in the order they are drawn on
     the page, not in Unicode's logical order. Pre-base "i" (U+093F) is typed
     before its consonant cluster, and repha ("r" + virama) is typed after
     its cluster. Both must be reordered.

HOW THE TABLE WAS DERIVED (not guessed)
---------------------------------------
The font's `post` table is a decoy -- it names glyphs with standard Mac Latin
names (".notdef", "space", "A", "B", ...) even though glyph "A" draws
Devanagari. So the mapping was built by rendering the extracted TrueType
glyphs and then validated computationally against three independent
ground-truth ("Rosetta stone") sources:

  1. Page-1/page-2 BOILERPLATE, identical across all 90 ACs, whose correct
     Hindi is known (form headings, column captions, the footer legend).
     62 hand-written pairs -- 62/62 pass. See tests/test_haryana_dkraj.py.
  2. AC NAMES decoded from the PDFs vs the portal's own English AC names
     from its JSON endpoint (35 of 41 exact transliteration matches, e.g.
     Gurgaon, Mahendragarh, "Ferozepur Jhirka", "Bawani Khera", "Jhajjar (SC)").
  3. ACs 2/31/35 are HYBRID documents that carry the relation and gender
     columns in genuine Unicode (ArialUnicodeMS) alongside DK-RAJ text,
     giving a direct parallel reading of those fields.

Measured on a pilot AC: 17,319 decoded name tokens, 0.11% containing an
unknown glyph, 0% with a stray unresolved virama.

DELIBERATELY UNMAPPED
---------------------
A handful of rare glyphs (0xBC, 0xC8, 0xCF, 0xD9, 0xE7, 0xF3) were NOT
confidently identified and are intentionally left out of MAP. decode()
returns them via its `unknown` list so the caller can attach a per-row
remark, exactly as states/karnataka.py does for unrecognized field values.
Guessing at them would silently corrupt names, which is far worse than a
flagged U+FFFD.
"""
import re
import unicodedata

REPHA_MARK = '\ue000'   # sentinel: keeps the standalone repha distinct from
                        # the subjoined ra already baked into conjunct glyphs
REPHA_DONE = '\ue001'   # a repha already moved into place, so it is not moved twice
I_PLACED   = '\ue003'   # an i-matra already moved into place, so it is not moved twice
IREPHA     = '\ue002'   # combined pre-base i + repha: BOTH belong to the cluster
                        # that follows, unlike a post-base matra's repha

# byte-char -> Unicode fragment. Consonants are stored as HALF forms (C + virama);
# a following stem glyph (mapped to U+093E AA) completes them into the full consonant.
MAP = {
    ' ': ' ', '!': '!', '(': '(', ')': ')', ',': ',', '-': '-', '.': '.', '/': '/',
    ':': ':', ';': ';', '?': '?', '&': 'ः', '%': 'ऽ', '$': 'ॐ', "'": ',',
    '*': 'ा', '+': 'अ', '<': 'इ', '=': 'उ', '>': 'ऊ',
    '@': 'ऋ', 'A': 'ॠ', 'B': 'ए',
    **{c: c for c in '0123456789'},

    # --- base consonants (half forms: consonant + virama) ---
    'C': 'क्', 'E': 'क्', 'F': 'क़्',
    'G': 'क्र्', 'H': 'क्त्',
    'I': 'क्ष्',
    'J': 'ख्', 'K': 'ख़्', 'M': 'ग्', 'N': 'ग़्',
    'O': 'ग्र्',
    'S': 'च्', 'U': 'छ्', 'V': 'ज्', 'W': 'ज़्',
    'Y': 'ज्ञ्', 'Z': 'झ्',
    ']': 'ट्', '`': 'ठ्', 'b': 'ड्', 'c': 'ड़्',
    'f': 'ढ्', 'g': 'ढ़्', 'h': 'ण्',
    'i': 'त्', 'j': 'त्र्', 'k': 'त्त्',
    'l': 'थ्', 'n': 'द्', 'o': 'द्र्',
    'p': 'द्र्', 'q': 'द्द्',
    'r': 'द्ध्', 's': 'ब्र्',
    't': 'द्य्', 'u': 'द्व्',
    'v': 'ध्', 'x': 'न्', 'z': 'न्न्',
    '{': 'प्', '|': 'प्र्', '}': 'फ्',
    '\xa1': 'फ्', '\xa2': 'फ़्', '\xa3': 'फ्र्',
    '\xa4': 'ब्', '\xa6': 'भ्', '\xa8': 'म्',
    '\xaa': 'य्', '\xae': 'र', '\xb0': 'र', '\xaf': 'र',
    '\xb1': 'ल्', '\xb4': 'व्',
    '\xb6': 'श्', '\xb7': 'श्व्',
    '\xb8': 'श्र्', '\xb9': 'ष्',
    '\xba': 'स्', '\xbd': 'ह्',
    'P': 'घ्', 'Q': 'घ़्', 'L': 'ख़्',

    # --- matras / diacritics ---  (stem variants all map to AA)
    '\xc9': 'ा', '\xf2': 'ा', '\xf9': 'ा', '\xfe': 'ा',
    '\xf5': 'ा', '\xf7': 'ा', '\xf8': 'ा', '\xfb': 'ा',
    '\xf4': 'ा', '\xfa': '',
    '\xca': 'ि', '\xcb': 'िं', '\xcc': IREPHA,
    '\xcd': 'िं', '\xcf': 'िं', '\xce': 'ि',
    '\xd2': 'ी', '\xd3': 'ीं', '\xd4': 'ी' + REPHA_MARK,
    '\xd6': 'ु', '\xda': 'ू', '\xfc': 'ू',
    '\xe4': 'े', '\xe8': 'ै', '\xec': 'ॉ',
    '\xe9': 'ैं', '\xc4': 'ँ', '\xc5': 'ँ',
    '\xc6': 'ं', '\xc3': '', '\xde': 'ृ',
    '\xf6': 'ा', '\xf0': '', '\xf1': '',
    '\xe5': 'ें', '\xe0': 'े', '\xc2': '्',
    '\xa5': 'ब्र्', '\xc0': 'ह्म्', '\xbb': 'स्र्',
    '\xe6': 'े' + REPHA_MARK, '\xe7': 'ें' + REPHA_MARK,
    '\xea': 'ै' + REPHA_MARK, '\xeb': 'ं' + REPHA_MARK,
    '\xc7': REPHA_MARK, '\xc8': REPHA_MARK,   # repha -- typed after its cluster, reordered below
}

CONS = '[क-हक़-य़]'
CLUSTER = '(?:' + CONS + '्)*' + CONS
MATRAS = '[ा-ौंःँृ]'
# matras that can sit between a half form and its stem (everything except AA itself)
INNER = '[ि-ौंःँृ]'
AA = 'ा'
VIRAMA = '्'
REPHA = REPHA_MARK


def decode(s):
    """Transcode a DK-RAJ string to Unicode Devanagari. Returns (text, unknown_chars)."""
    out, unknown = [], []
    for ch in s:
        if ch in MAP:
            out.append(MAP[ch])
        elif ch.isascii() and (ch.isdigit() or ch.isalpha() or ch in '()[]{}/.,:;-'):
            out.append(ch)
        else:
            unknown.append(ch)
            out.append('�')
    t = ''.join(out)

    # Pull a repha that sits behind other matras up against its virama first,
    # so the reordering rule below sees a single canonical shape.
    t = re.sub(VIRAMA + '(' + INNER + '*)' + REPHA_MARK, VIRAMA + REPHA_MARK + r'\1', t)
    # A repha typed between a half form and its stem belongs before that consonant.
    t = re.sub('(' + CONS + ')' + VIRAMA + REPHA + '(' + INNER + '*?)' + AA,
               REPHA_DONE + r'\1\2', t)
    # Half form + stem -> full consonant (other matras may sit in between).
    t = re.sub(VIRAMA + '(' + INNER + '*?)' + AA, r'\1', t)
    # Two-part vowel signs and independent vowels composed from parts.
    for a, b in (('ाे', 'ो'), ('ाै', 'ौ'),
                 ('ाॉ', 'ॉ'),
                 ('अा', 'आ'), ('अो', 'ओ'),
                 ('अौ', 'औ'), ('अे', 'ए'),
                 ('अै', 'ऐ')):
        t = t.replace(a, b)
    # Pre-base i is typed before its cluster; Unicode wants it after. The
    # combined i+repha glyph carries its repha forward to the same cluster.
    t = re.sub(IREPHA + '(' + CLUSTER + ')', REPHA_DONE + r'\1' + I_PLACED, t)
    t = re.sub('िं(' + CLUSTER + ')', r'\1िं', t)
    t = re.sub('ि(' + CLUSTER + ')', r'\1ि', t)
    # A repha typed after the whole syllable belongs before its cluster.
    t = re.sub('(' + CLUSTER + ')(' + MATRAS + '*)' + REPHA, REPHA_DONE + r'\1\2', t)
    t = t.replace('\u0907' + REPHA_MARK, 'ई')   # the hook after independent I forms II
    t = t.replace(REPHA_DONE, 'र्').replace(REPHA_MARK, 'र्').replace(I_PLACED, 'ि')
    return unicodedata.normalize('NFC', t), unknown
