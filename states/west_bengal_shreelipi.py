# -*- coding: utf-8 -*-
"""
SHREE550 (Shree-Lipi Bengali) -> Unicode Bengali transcoder, for West Bengal's
2002 electoral roll PDFs.

WHY THIS EXISTS
---------------
ceowestbengal.nic.in publishes 294 constituencies. Classifying every one of
them by what its parts are actually typeset in:

    265  SHREE550 Bengali          this module
     23  Latin                     always been searchable (AC139-AC160, AC186)
      3  another Bengali font      AC022-AC024, Darjeeling -- see the end
      3  no text layer at all      AC287, AC291, AC294 -- page scans, need OCR

(Whole-AC verdicts. AC025 and AC026 sit in the 265 on a majority of their
parts; see NOT COVERED BY THIS MODULE at the end.)

So the Latin 23 were never the ceiling -- they were 8% of the state. The 265
do carry a real text layer, but not Unicode. Their fonts are subsetted
Type1C/CFF (`FontFile3`, distiller-named `MSTT31c*`) whose charset names are
bare glyph ids (`/G03`, `/G05`), and there is no usable /ToUnicode CMap: AC186 carries 66 of them and they yield eight code points,
all C1 control characters, three of which disagree with each other. So no PDF
library can do this mapping, and this repo's own docs recorded those ACs as
unsearchable "regardless of effort spent". They are not.

`states/west_bengal.py` already surfaces each glyph id as a private-use code
point (`_patch_pdfminer_gid_encoding`, gid N -> U+E000+N), so this module is a
pure function over that: PUA text in, Bengali out.

WHAT MAKES THIS FONT AWKWARD
----------------------------
It is a VISUAL encoding -- glyphs are stored in the order they are drawn, not
in Unicode's logical order -- and it economises on glyph slots in ways that
have no Unicode counterpart. Four properties drive the design, three of which
states/haryana_dkraj.py already meets in the DK-RAJ Devanagari font (same
vendor family, Modular Infotech):

  1. A pre-base matra (ি ে ৈ) is drawn to the LEFT of the cluster it follows,
     so it must be buffered and released once the cluster is complete --
     including any below-base phala, or শ + ্র + ে decodes as শে্র.
  2. A repha is drawn AFTER the glyph it clears. That glyph is the consonant
     it rides when one is directly behind it, but is the matra when the
     cluster already carries one -- and there the repha usually belongs to the
     consonant still to come, EXCEPT when the cluster behind the matra is
     itself a conjunct, which the matra sits after (see the repha branch of
     decode(): কার্তিক and সর্ব্বানন্দ are the same glyph shape either way).
  3. A consonant may be drawn as a HALF form, completed either by a bare
     vertical stem glyph or by the next consonant in a conjunct -- or marked
     as one retroactively, by a joiner glyph drawn after the letter it applies
     to (JOINER_GIDS; ব্ব is ব + joiner + ব).
  4. NEW here, and the part most likely to be mis-read as a letter: some
     glyphs are HOOKS, not characters. Glyph 220 is the stroke that turns a
     ta-conjunct into a ka-conjunct (92 alone is ত্র, 92+220 is ক্র; 89 alone
     is ত্ত, 89+220 is ক্ত). Glyph 222 turns দ্ব into ধ. Mapping either as a
     letter corrupts every word containing it -- 220 alone appears ~3x per
     page. Two independent instances of the same trick suggests it is
     systematic in Shree-Lipi rather than a one-off.

  Separately, a few letters are drawn as two ordinary glyphs whose
  juxtaposition means something else (DIGRAPHS): হ + ্র is ই, ড + ্র is উ,
  অ + া is আ.

HOW THE TABLE WAS DERIVED (not guessed)
---------------------------------------
Reading 225 glyph outlines by eye produces an unverifiable table. Instead the
table was corrected against a Rosetta stone the source hands us for free:
EVERY constituency's part-1 header states its own AC name and district in
Bengali, and all 294 English AC names are already in
states/meta/west_bengal_ac_meta.json. That is 228 independent parallel
readings, scored offline while the table was being built.

Measured across the 228 downloaded Bengali ACs: decoded-AC-name vs portal
English, after romanising through ITRANS and reducing to a consonant
skeleton, scores median 89 / mean 83, with 161 at >=80. The residue below 80
is overwhelmingly NOT decoder error -- it is the portal's colonial-era English
diverging from the Bengali it is nominally transliterating (বজবজ/"BUDGE
BUDGE", হাওড়া/"HOWRAH", কাঁথি/"CONTAI", বর্ধমান/"BURDWAN") or translating it
outright (মধ্য/"CENTRAL", শহর/"TOWN"). Eight ACs score a clean 100.

Several entries were pinned by context rather than by shape, which is the
more reliable of the two: glyph 30 is ৎ because it makes বৎসর, জগৎ and উৎপল
simultaneously; glyph 100 is থ from স্থানে and হিন্দুস্থানী; glyph 51 is গু
because the portal spells both ধূপগুড়ি and জলপাইগুড়ি "-GURI".

The headings are a Rosetta stone but a narrow one, and a table that reads
them perfectly can still be wrong where it matters. NAMES are a second,
harder corpus, and the first build over them found six holes and three
placement bugs the 228 headings never touch -- 9.5% of rows in a six-AC
sample carried an unmapped glyph, and thousands more decoded to a wrong
spelling with nothing malformed to notice. Those six (54 ঙ্ক, 85 ণ্ড, 86
half-ন, 168 শু, 180 স, 207 ৈ) were each pinned the same contextual way,
from several unrelated names at once rather than from glyph shape: 85 makes
মণ্ডল, কুণ্ডু and পণ্ডিত simultaneously, 54 makes শঙ্কর and বঙ্কিম, 168
makes আশুতোষ and সুধাংশু. `tests/test_west_bengal_shreelipi.py` carries the
real glyph runs for all of them, with the pre-fix spelling recorded beside
each -- that file is where this module's readings are pinned, both the
headings and the names.

DELIBERATELY UNMAPPED
---------------------
A gid with no entry contributes NOTHING and is reported through decode()'s
`unknown` list, never guessed at -- same discipline as
states/haryana_dkraj.py. A hole that shows up as a missing letter is a bug
report; a hole filled with a guess is a corrupted name that still searches,
still ranks, and passes every quality assertion we have.

The way out of a hole is a wider sample, not a better guess. The four ids
that were still reported most often -- 114, 38, 182, 223 -- were resolved by
printing every name they occur in across six North Bengal ACs and reading the
names, and all four turned out to be legible once there were enough of them:

    114  ন্স   ফ্রান্সিস, লরেন্স, ফ্লোরেন্স, মুন্সী, আলফন্স, কন্সটান্টিউস
     38  ক্ট   ভিক্টর, ভিক্টোরিয়া, বেনেডিক্ট
    182  হ্ম   ব্রহ্মচারী, ব্রহ্মানন্দ, ব্রহ্মপ্রকাশ, ব্রহ্মনারায়ণ, ব্রহ্মদেব
    223  --    see IGNORE_GIDS: a connector, not a letter

Three of those four are read off names of Latin origin, which the roll spells
phonetically -- Victor, Francis, Benedict, Florence -- and which therefore
admit exactly one Bengali spelling apiece. That is stronger evidence than a
Bengali word gives, and it is why the earlier single ব্র⟦182⟧দেব witness was
correctly left alone: one witness is a guess, nine are a reading. Each id also
falls inside the varga block its reading belongs to (38 among ক্ক/ক্ম/ক্স, 114
among ন্ন/ন্ড, 182 between হ and হু), which is corroboration, not the evidence.

Measured over the first 32 ACs built (4.8M electors): those four were 13,260
of 18,015 reported glyph hits, i.e. three quarters of all the damage in the
state. What is left is a tail of 46 ids over 4,755 hits, the largest of them
499 -- worth another pass with the same method if anyone wants it, but no
longer the thing standing between this font and a searchable West Bengal.

NOT COVERED BY THIS MODULE
--------------------------
A different glyph space, decoding to nothing recognisable here, appears in
Darjeeling district and nowhere else. A Gorkha-majority district's roll being
typeset in a different font is not a coincidence; the untested hypothesis is
Devanagari for Nepali. It needs its own table, and `looks_like_shreelipi()`
exists so a caller can tell the two apart rather than emitting garbage.

But it is THREE constituencies, not the district's five. Classifying all
1,320 of Darjeeling's parts individually:

    AC022 KALIMPONG    219/219 parts  other font
    AC023 DARJEELING   221/221 parts  other font
    AC024 KURSEONG     223/223 parts  other font
    AC025 SILIGURI      37/359 parts  other font -- the other 322 are SHREE550
    AC026 PHANSIDEWA     8/298 parts  other font -- the other 290 are SHREE550

Siliguri and Phansidewa are the district's two Bengali-majority
constituencies and their rolls are mostly typeset accordingly, so writing
off the district by name costs ~600,000 electors who are decodable right
now. The font is decided per PART in west_bengal.py's `_parse_part`, not per
AC, which is what makes serving them the default rather than a special case:
the minority parts fall through to the "unrecognized Bengali-script font"
branch, which blanks the name columns and says so in `remark` instead of
guessing. Expect AC025 to land near 90% name-population and AC026 near 97%,
and expect that to be the reason.
"""
import unicodedata

# Must agree with states/west_bengal.py's PUA_BASE; the test asserts it.
PUA_BASE = 0xE000

HASANT = "্"                # ্
REPHA = "র" + HASANT        # র্

# --------------------------------------------------------------------------
# structural glyphs -- these are not letters
# --------------------------------------------------------------------------

SPACE_GIDS = {3}

# Contributes no character: a spacing/head-stroke filler emitted after certain
# consonants. Established empirically -- it appears between a consonant and its
# matra in words whose correct spelling has nothing there (তালিকা, ভা, ক্ষেত্র),
# and never where a letter is actually missing.
#
# 223 is the same thing in one narrow place: it is only ever emitted between
# ড়/ঢ় (185/186) and a following ু/ূ, because the u-matra cannot hang under the
# nukta and needs a connector drawn for it. Every word it appears in is already
# spelled correctly with it dropped -- বড়ুয়া, ঝাড়ু, নাড়ু, পাঁড়ু, খাড়ুয়া,
# আষাঢ়ু -- so it is silence, not a hole. It is listed here rather than left
# unmapped so it stops reporting 2.6k rows per 2.4M as damaged.
IGNORE_GIDS = {130, 223}

# Hooks, not characters. See property 4 in the module docstring.
KA_HOOK_GIDS = {220}             # ত-conjunct -> ক-conjunct
DHA_HOOK_GIDS = {222}            # দ্ব -> ধ

# One curl, three readings. This font gives র, হ and ন্ব their own attached
# forms rather than composing them from a base plus a free-standing sign, and
# gid 203 is the curl all three attach. What it means therefore depends on the
# glyph already drawn -- the same trick as the two hooks above, and as 223.
#
# It was mapped as a second "া", which is the one thing it never is: across 20
# ACs it occurs 920 times and follows exactly four gids -- 191, 154 (র), 179
# (হ) and 112 -- never anything else, and is itself always followed by the 130
# filler or a ু. Reading it as া turned সন্ধ্যা into সস্বা্যা, জগবন্ধু into
# জগবস্বাু, হৃদয় into হাদয়, রূপচাঁদ into রাপচাঁদ, and the ERO's own heading
# নিবন্ধন into নিবান.
#
# Each reading is non-redundant, which is what makes it a distinct glyph
# rather than a duplicate: র already writes রু with 200, হ্র has its own 221,
# the free-standing ৃ is 214 -- and the table carries no ন্ধ at all, which for
# a 225-glyph Bengali font is itself evidence of where it went.
CURL_HOOK_GIDS = {203}
CURL_HOOK_READINGS = {
    "র": "র" + "ূ",                      # রূপ, স্বরূপ, অরূপ, নুরূল
    "হ": "হ" + "ৃ",                      # হৃদয়, হৃষিকেশ
    "ন" + HASANT + "ব": "ন" + HASANT + "ধ",   # বন্ধু, সন্ধ্যা, গান্ধী, সিন্ধু
}

# Drawn after the glyph it clears; Unicode wants র + hasant before the cluster.
REPHA_GIDS = {212, 213}

# Below-base phalas. Part of the cluster, so a pre-base matra buffered ahead of
# the cluster has to wait for them.
CLUSTER_TAIL_GIDS = {217, 153}

# Consonants drawn without their right-hand vertical, completed either by a
# stem glyph or by the consonant that follows them in a conjunct.
HALF_GIDS = {
    43: "ক", 50: "গ", 61: "চ", 86: "ন", 94: "ত", 108: "দ", 118: "ন",
    120: "ন", 149: "ম", 163: "ল", 167: "শ", 174: "ষ", 178: "স",
}
STEM_GIDS = {129, 219}           # bare vertical: completes a preceding half form

# Known residue, left as-is deliberately: gid 216 is mapped ু but only ever
# follows a half স or a half ম (13 each over six ACs), where a matra cannot go.
# Its 26 occurrences split -- মিস্রা, ইস্রাইল, ইস্রাফিল, সম্রাট and আম্রপালী all
# read as a below-base র, while আম্বিকা wants a ব -- so neither reading is
# safe to assert from this corpus, and at 0.024% of name occurrences it is two
# orders of magnitude below the half-form bug above. Needs more ACs to settle.

# The opposite of a stem: retro-halves the consonant ALREADY emitted. The font
# draws ব্ব and ধ্ব as a full first consonant, this connector, then a subjoined
# ব, so the hasant arrives after the letter it applies to.
#
# This was read as a second space (SPACE_GIDS = {3, 224}) for as long as the
# module has existed, which split one name into two and dropped the conjunct:
# আব্বাস came out "আব বাস", জব্বার "জব বার", পার্ব্বতী "পাব র্বতী". It is not a
# space. Over AC001+AC003 (10 parts) gid 3 occurs 4,502 times and 224 only 107,
# never at a word boundary -- its neighbours are ব_ব 100 times and ধ_ব the
# other 7, i.e. always between two consonants, while the genuine word break in
# those same rows is a literal ASCII space sitting beside it. Every one of the
# 58 distinct runs containing it reads as a real name once it is a hasant:
# আব্বাস, জব্বার, সর্ব্বানন্দ, সর্ব্বেশ্বর, পার্ব্বতী.
#
# It attaches to out[-1] rather than appending a standalone "্" because
# _cluster_start() identifies a conjunct by walking back over elements that END
# in a hasant. A loose "্" element satisfies that test on its own and stops the
# walk one glyph short, which seats a following repha inside the cluster it
# rides: পাবর্্বতী instead of পার্ব্বতী.
JOINER_GIDS = {224}

# gid 140 was read as a bare ব, which spelled every -eshwar/-ishwa name in the
# corpus without its hasant: বিশ্বকর্মা as বিশবকর্মা, খগেশ্বর as খগেশবর,
# যজ্ঞেশ্বর as যজ্ঞেশবর -- including সর্ব্বেশ্বর, which the note above cites as
# a name that reads correctly and which in fact came out সর্ব্বেশবর. It is not
# a ব. In 216 of 217 occurrences it is preceded by gid 166 (শ) and it is never
# word-initial, and the font keeps a separate glyph -- gid 133, which we
# already decoded correctly -- for a genuine standalone ব after শ. The font
# distinguishes শব from শ্ব; the table did not. It hangs its hasant on the
# letter already in `out` for the same reason the joiner above does.
BA_PHALA_GIDS = {140}            # subjoined ব -- শ + this is শ্ব, never শব

# Matras the font draws to the LEFT of the cluster they follow in Unicode.
PREBASE_GIDS = {
    194: "ি",   # ি
    204: "ে",   # ে
    205: "ে",   # ে
    206: "ে",   # ে
    207: "ৈ",   # ৈ
}

# Letters the font draws as two ordinary glyphs. Applied after decoding: the
# pieces are individually correct, it is only their juxtaposition that stands
# for a different letter.
DIGRAPHS = [
    ("ড" + HASANT + "র", "উ"),   # ড + ্র -> উ  (উৎপল, সিউড়ি)
    ("হ" + HASANT + "র", "ই"),   # হ + ্র -> ই  (এই, জলপাইগুড়ি)
    ("অা", "আ"),                  # অ + া  -> আ
]

# --------------------------------------------------------------------------
# glyph id -> Unicode fragment
#
# Duplicate readings are real: the font carries several width/position variants
# of the same letter, and collapsing them here is what lets the rest of the
# module stay simple.
# --------------------------------------------------------------------------

GID_MAP = {
    # punctuation
    11: "(", 12: ")", 14: "+", 15: ",", 16: "-", 17: ".", 18: "/",
    29: ":", 34: "?",
    # digits ০-৯ occupy a contiguous run
    19: "০", 20: "১", 21: "২", 22: "৩", 23: "৪",
    24: "৫", 25: "৬", 26: "৭", 27: "৮", 28: "৯",

    # independent vowels and signs
    5: "অ", 6: "ঈ", 7: "উ", 9: "এ", 10: "ঐ", 13: "ও", 31: "ঋ",
    96: "ও", 35: "ঃ",
    30: "ৎ",                                   # khanda ta (বৎসর, জগৎ, উৎপল)

    # ka-varga
    36: "ক",
    37: "ক" + HASANT + "ক",
    38: "ক" + HASANT + "ট",                    # ভিক্টর, ভিক্টোরিয়া, বেনেডিক্ট
    41: "ক" + HASANT + "ম",
    42: "ক" + HASANT + "স",
    45: "খ",
    46: "গ", 49: "গ",
    47: "গ" + HASANT + "ন",
    48: "গ" + HASANT + "র",
    51: "গু",                                  # গু (ধূপগুড়ি, জলপাইগুড়ি)
    52: "ঘ",
    53: "ঙ",
    54: "ঙ" + HASANT + "ক",                    # বঙ্কিম, শঙ্কর, দীপঙ্কর
    56: "ঙ" + HASANT + "গ",

    # cha-varga
    58: "চ",
    62: "ছ",
    63: "জ",
    64: "জ" + HASANT + "জ",
    65: "জ" + HASANT + "ঞ",
    67: "জ" + HASANT + "ঞ",                    # (জ্ঞানেন্দ্র)
    68: "জ" + HASANT + "ব",
    70: "ঝ",
    71: "ঞ",
    72: "ঞ" + HASANT + "চ",
    73: "ঞ" + HASANT + "ছ",
    74: "ঞ" + HASANT + "জ",

    # Ta-varga (retroflex)
    76: "ট",
    77: "ট" + HASANT + "ট",
    78: "ঠ",
    79: "ড",
    80: "ড" + HASANT + "ড",
    81: "ঢ",
    82: "ণ",
    84: "ণ" + HASANT + "ঠ",
    85: "ণ" + HASANT + "ড",                    # মণ্ডল -- 416 distinct given names, one surname

    # ta-varga (dental)
    87: "ত", 93: "ত",
    89: "ত" + HASANT + "ত",                    # +220 -> ক্ত
    91: "ত" + HASANT + "থ",
    92: "ত" + HASANT + "র",                    # +220 -> ক্র
    95: "ত" + HASANT + "র",                    # subjoined form (স্ত্রী)
    99: "থ", 100: "থ",                         # (স্থানে, হিন্দুস্থানী)
    101: "দ",
    102: "দ" + HASANT + "দ",
    105: "দ" + HASANT + "ব",                   # +222 -> ধ
    106: "দ" + HASANT + "ম",
    107: "দ" + HASANT + "র",
    109: "ধ",
    111: "ন", 119: "ন", 121: "ন",
    113: "ন" + HASANT + "ন",
    114: "ন" + HASANT + "স",                    # ফ্রান্সিস, লরেন্স, ফ্লোরেন্স, মুন্সী
    117: "ন" + HASANT + "ড",
    123: "ন" + HASANT + "দ" + HASANT + "র",

    # pa-varga
    124: "প",
    126: "প" + HASANT + "ত",
    127: "প" + HASANT + "র",
    131: "ফ",
    133: "ব", 141: "ব",             # 140 is the subjoined form, see BA_PHALA_GIDS
    135: "ব" + HASANT + "দ",
    143: "ভ", 146: "ভ",                        # (স্তম্ভ)
    145: "ভ" + HASANT + "র",
    148: "ম", 150: "ম", 151: "ম",

    # ya .. ha
    152: "য",
    153: HASANT + "য",                         # ya-phala
    154: "র",
    155: "ল", 164: "ল",
    157: "ল" + HASANT + "ল",
    159: "ল" + HASANT + "প",
    160: "ল" + HASANT + "প",
    161: "ল" + HASANT + "ল",
    166: "শ",
    168: "শু",                                 # আশুতোষ, বিশু, কুলশুম (cf. 184 হু)
    169: "ষ",
    171: "ষ" + HASANT + "ট",
    172: "ষ" + HASANT + "ঠ",
    173: "ষ" + HASANT + "ণ",
    175: "স",
    177: "স" + HASANT + "ট",
    179: "হ",
    180: "স",                                 # another স variant: প্রসাদ, আসাদী
    182: "হ" + HASANT + "ম",                   # ব্রহ্মচারী, ব্রহ্মানন্দ, ব্রহ্মদেব
    184: "হু",                                 # (হুগলী)
    185: "ড়", 186: "ঢ়", 187: "য়",
    188: "ক" + HASANT + "ষ",
    189: "ক" + HASANT + "ষ" + HASANT + "ম",
    112: "ন" + HASANT + "ব", 191: "ন" + HASANT + "ব",

    # post-base matras and signs
    193: "া",
    196: "ী",
    198: "ু", 199: "ু", 200: "ু", 216: "ু",
    201: "ূ",
    208: "ৗ",
    210: "ং",
    211: "ঁ",
    214: "ৃ",
    217: "্র",                                 # ra-phala
    221: HASANT + "র",
}

# Every glyph the module knows how to act on. A gid outside this set is
# reported, not rendered.
# A pre-base matra buffered ahead of a cluster is released once the cluster is
# complete. These are the glyphs that continue one, so the matra has to wait
# for them -- a below-base phala, the joiner, and the curl hook. Releasing it
# early strands the matra between the cluster and the glyph that modifies it,
# which is how বন্ধ্যোপাধ্যায় came out বন্বে্যাপাধ্যায় and সাব্বির came out সাবি বর.
CLUSTER_CONTINUES_GIDS = (CLUSTER_TAIL_GIDS | JOINER_GIDS | CURL_HOOK_GIDS
                          | BA_PHALA_GIDS)

KNOWN_GIDS = (
    set(GID_MAP) | set(HALF_GIDS) | set(PREBASE_GIDS) | SPACE_GIDS
    | IGNORE_GIDS | STEM_GIDS | JOINER_GIDS | REPHA_GIDS | KA_HOOK_GIDS
    | DHA_HOOK_GIDS | CURL_HOOK_GIDS | BA_PHALA_GIDS
)

_TA = "ত"
_KA = "ক"
_DA_BA = "দ" + HASANT + "ব"
_DHA = "ধ"


def _gid_of(ch):
    cp = ord(ch)
    return cp - PUA_BASE if PUA_BASE <= cp < PUA_BASE + 0x1000 else None


def _is_mark(ch):
    return len(ch) == 1 and unicodedata.category(ch) in ("Mn", "Mc")


def _cluster_start(out, k):
    """Index at which the conjunct cluster with its base consonant at out[k]
    begins -- i.e. reaching back over any half forms drawn ahead of the base.

    A repha rides the whole cluster, not its base consonant. বর্ম্মন is drawn
    ব + ম(half) + ম + repha, and seating the repha immediately before the base
    ম gave বম্র্মন -- 1,261 of them in a six-AC sample, against a surname
    (Barman) that is the most common in the state.
    """
    while k > 0 and out[k - 1].endswith(HASANT):
        k -= 1
    return max(k, 0)


def _flush_held(out, held):
    """Attach a repha that never found a consonant AHEAD of it.

    REPHA_GIDS buffers into `held` when the glyph before the repha is a matra,
    on the reading that the repha rides the consonant still to come (কার্তিক:
    ক া র্ ত ি). That reading is right only when a consonant actually follows.
    When the word ends there instead, the repha rides the consonant the matra
    itself belongs to -- the font draws it after that matra because it has to
    clear the tallest glyph in the cluster. চক্রবর্তী is emitted চ ক্র ব ত ী র্,
    and appending the held repha instead of re-seating it produced চক্রবতীর্,
    a word ending in a hasant, which Bengali orthography does not allow.

    Also called at a space: a repha left held across a word boundary would
    otherwise be consumed by the NEXT word's first consonant, which corrupts
    two words instead of one and leaves nothing malformed to notice.
    """
    if not held:
        return
    k = len(out)
    while k > 0 and _is_mark(out[k - 1]):
        k -= 1                                  # trailing matras
    k = _cluster_start(out, max(k - 1, 0))      # base consonant, then its cluster
    out[k:k] = held
    del held[:]


def decode(s):
    """Transcode a SHREE550 PUA-gid string to Unicode Bengali.

    Returns (text, unknown_gids). Text outside the private-use range -- the
    Latin and ASCII the rolls mix in, e.g. EPIC numbers -- passes through
    untouched.
    """
    out, pending, held, unknown = [], [], [], []
    half = False
    i, n = 0, len(s)

    while i < n:
        ch = s[i]
        gid = _gid_of(ch)
        i += 1

        if gid is None:
            out.extend(pending)
            del pending[:]
            _flush_held(out, held)
            half = False
            out.append(ch)

        elif gid in IGNORE_GIDS:
            continue

        elif gid in SPACE_GIDS:
            out.extend(pending)
            del pending[:]
            _flush_held(out, held)
            half = False
            out.append(" ")

        elif gid in JOINER_GIDS:
            if out and not _is_mark(out[-1]) and not out[-1].endswith(HASANT):
                out[-1] += HASANT               # the letter already drawn is a half form
                half = True

        elif gid in STEM_GIDS:
            if half and out and out[-1].endswith(HASANT):
                out[-1] = out[-1][:-1]          # the half form is now whole
            half = False

        elif gid in CURL_HOOK_GIDS:
            # Unknown rather than silent when it lands on a glyph with no
            # reading: a wrong curl corrupts the word either way, and reported
            # damage is the kind the per-AC census can see.
            if out and out[-1] in CURL_HOOK_READINGS:
                out[-1] = CURL_HOOK_READINGS[out[-1]]
            else:
                unknown.append(gid)

        elif gid in DHA_HOOK_GIDS:
            if out and out[-1] == _DA_BA:
                out[-1] = _DHA

        elif gid in KA_HOOK_GIDS:
            # Reaches back past any matra to the consonant the hook is drawn on.
            for j in range(len(out) - 1, -1, -1):
                if out[j].startswith(_TA):
                    out[j] = _KA + out[j][1:]
                    break
                if not _is_mark(out[j]):
                    break

        elif gid in REPHA_GIDS:
            if out and _is_mark(out[-1]):
                # A repha drawn after a matra rides either the consonant ahead
                # (কার্তিক is ক া র্ ত ি) or the cluster the matra itself
                # belongs to. The two are indistinguishable from glyph order
                # alone, so the tiebreak is whether that cluster is a conjunct:
                # a matra sits after the whole conjunct, so the repha clearing
                # it lands past the matra. সর্ব্বানন্দ is স ব্ব া র্ ন ন দ, and
                # reading that repha as riding the ন gave সব্বার্ননদ.
                k = len(out)
                while k > 0 and _is_mark(out[k - 1]):
                    k -= 1                      # back over trailing matras
                if k >= 2 and out[k - 2].endswith(HASANT):
                    out.insert(_cluster_start(out, k - 1), REPHA)
                else:
                    held.append(REPHA)          # belongs to the consonant ahead
            else:
                out.insert(_cluster_start(out, len(out) - 1), REPHA)

        elif gid in PREBASE_GIDS:
            pending.append(PREBASE_GIDS[gid])

        elif gid in CLUSTER_TAIL_GIDS:
            out.append(GID_MAP[gid])
            if _peek(s, i) not in CLUSTER_CONTINUES_GIDS:
                out.extend(pending)
                del pending[:]
            half = False

        elif gid in HALF_GIDS:
            if half:
                out.append(HALF_GIDS[gid])      # completes the half form ahead
                half = False
            else:
                out.append(HALF_GIDS[gid] + HASANT)
                half = True

        elif gid in GID_MAP or gid in BA_PHALA_GIDS:
            if gid in BA_PHALA_GIDS:
                if out and not _is_mark(out[-1]) and not out[-1].endswith(HASANT):
                    out[-1] += HASANT           # the letter drawn before it is a half form
                out.append("ব")
            else:
                out.append(GID_MAP[gid])
            if held:
                out[-1:-1] = held
                del held[:]
            # Hold a buffered pre-base matra back over a following phala
            # or joiner -- both continue the cluster the matra is waiting on.
            if _peek(s, i) not in CLUSTER_CONTINUES_GIDS:
                out.extend(pending)
                del pending[:]
            half = False

        else:
            unknown.append(gid)

    out.extend(pending)
    _flush_held(out, held)
    text = "".join(out)
    for src, dst in DIGRAPHS:
        text = text.replace(src, dst)
    # ে + া and ে + ৗ are the canonical decompositions of ো and ৌ, which is
    # exactly how this font draws them -- so composing is a no-op on anything
    # already correct.
    return unicodedata.normalize("NFC", text), unknown


def _peek(s, i):
    return _gid_of(s[i]) if i < len(s) else None


# Boilerplate that appears in the page-1 heading of every West Bengal roll,
# whatever the constituency. Used to tell this font from another legacy one.
_ANCHORS = ("বিধানসভা", "নির্বাচক", "তালিকা", "ভোট")


def looks_like_shreelipi(page_text):
    """Does this page's PUA text decode to Bengali under THIS table?

    Darjeeling's five constituencies (AC022-AC026) use a different glyph space
    and must not be run through this table -- but their glyph ids overlap this
    one's numerically, so counting recognised ids does not separate them (it
    was tried; it says yes to Darjeeling). What does separate them is whether
    the result is Bengali: every roll's page-1 heading carries the same few
    boilerplate words, and under the wrong table none of them appear.

    Measured over all 294 constituencies, sampling three parts of each: true
    for the 265 Bengali ACs this table covers, false for the 23 Latin-typeset
    ACs (which do not need it), the three text-layerless page scans (AC287,
    AC291, AC294) and Darjeeling.

    Darjeeling is not five whole ACs, which is why the decision is made per
    part rather than per AC: AC022-AC024 are Devanagari throughout, but AC025
    and AC026 are mixed -- 322 of AC025's 359 parts and 290 of AC026's 298 are
    Shree-Lipi Bengali, and only the remainder are Devanagari.
    """
    text, _ = decode(page_text)
    return any(a in text for a in _ANCHORS)
