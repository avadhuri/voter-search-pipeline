# -*- coding: utf-8 -*-
"""
Regression test for the SHREE550 -> Unicode transcoder
(states/west_bengal_shreelipi.py).

Each pair is (glyph-id sequence as hex, expected Unicode Bengali). The sources
are real tokens lifted from ceowestbengal.nic.in's 2002 roll PDFs; the
expectations are what the roll form's own printed headings say, what the
portal's own English AC names transliterate to, and -- for the name tokens --
what the name plainly is to anyone who reads Bengali. An independent oracle in
all three cases, not a snapshot of the decoder's current output.

Coverage is deliberate: the set exercises every structural rule in the
transcoder -- half-form plus stem composition, pre-base matra reordering
(including over a below-base phala), repha reordering in both its positions,
the ka- and dha- hooks, the two-glyph digraphs, and conjuncts.

The pairs come in two blocks, and the second one is the point. Page-1
headings alone were enough to build the table and not enough to make it
right: the first build over actual voter names turned up six unmapped glyphs
and three placement bugs no heading exercises. Every name pair below records
what the decoder emitted for it BEFORE that fix, so the regression each one
guards is legible rather than implied.
"""
import re

import pytest

from states import west_bengal_shreelipi as sl
from states.west_bengal_shreelipi import decode, looks_like_shreelipi

PAIRS = [
    ("c2856dc16faf8f82c1", "বিধানসভা"),   # AC001
    ("c26f85d4c13a822482", "নির্বাচক"),   # AC001
    ("5782c1c29b2482c1", "তালিকা"),   # AC001
    ("afd2cca6c16dcc6f9a", "সংশোধনের"),   # AC001
    ("7cc952d4", "পূর্ণ"),   # AC001
    ("c285859a52", "বিবরণ"),   # AC001
    ("7cd924d682c25782", "প্রকৃতি"),   # AC001
    ("5782c1c29a2d", "তারিখ"),   # AC001
    ("3ac982b9c1765d", "চূড়ান্ত"),   # AC001
    ("7cd92482c1a66fc19a", "প্রকাশনার"),   # AC001
    ("cc8f82c14c82", "ভোট"),   # AC001
    ("31d9b352", "গ্রহণ"),   # AC001
    ("cc2482cc7b9a", "কেন্দ্রের"),   # AC001
    ("afd22d99c1", "সংখ্যা"),   # AC001
    ("c285b25dd6c25782", "বিস্তৃতি"),   # AC001
    ("cca6d952c4", "শ্রেণী"),   # AC001
    ("31d9c194c452", "গ্রামীণ"),   # AC001
    ("9ac1b25dc1", "রাস্তা"),   # AC001
    ("7c4882c1ccbbcd57829a", "পঞ্চায়েতের"),   # AC001
    ("0dbbc1d44f82", "ওয়ার্ড"),   # AC001
    ("cc7cd09aaf8f82c1", "পৌরসভা"),   # AC001
    ("5cdc82c2942482", "ক্রমিক"),   # AC001
    ("85c1c2b9", "বাড়ি"),   # AC042
    ("c26f85d4c13a82cc24829a", "নির্বাচকের"),   # AC001
    ("6fc194", "নাম"),   # AC001
    ("9ac13f99", "রাজ্য"),   # AC001
    ("cc3f9bc1", "জেলা"),   # AC001
    ("afc16dc19a52", "সাধারণ"),   # AC001
    ("57827cc2a69bc4", "তপশিলী"),   # AC001
    ("3fc1c257", "জাতি"),   # AC001
    ("851eaf9a", "বৎসর"),   # AC001
    ("05765d8fd4c6825982dc82", "অন্তর্ভুক্ত"),   # AC001
    ("b264c1cc6f", "স্থানে"),   # AC001
    ("b28dc194c4", "স্বামী"),   # AC001
    ("c27c5782c1", "পিতা"),   # AC001
    ("94c15782c1", "মাতা"),   # AC001
    ("b25fc4", "স্ত্রী"),   # AC001
    ("7cc6d2", "পুং"),   # AC001
    ("cc942dc29b2e4a", "মেখলিগঞ্জ"),   # AC001
    ("6dc26fbbc12dc1c29b", "ধনিয়াখালি"),   # AC191
    ("3a827bcd2482c152c1", "চন্দ্রকোণা"),   # AC196
    ("ccafc16fc194c62dc4", "সোনামুখী"),   # AC245
    ("3fc194c6c29abbc1", "জামুরিয়া"),   # AC262
    ("cc9494c1c29a", "মেমারি"),   # AC275
    ("b82e9bc4", "হুগলী"),   # AC177
    ("8569ded48294c16f", "বর্ধমান"),   # AC257
    ("3f9b7cc1b3dd3382c2b9", "জলপাইগুড়ি"),   # AC001
    ("6dc97c3382c2b9", "ধূপগুড়ি"),   # AC015
    ("24c6823a82c285b3c19a", "কুচবিহার"),   # AC001
    ("85c19ac185c26f", "বারাবনি"),   # AC258
    ("afc65782c1b3c14c82c1", "সুতাহাটা"),   # AC205
    ("7cc985d4b2649bc4", "পূর্বস্থলী"),   # AC277
    ("4f82dd59829a", "উত্তর"),   # AC004
    ("65c2bc8252", "দক্ষিণ"),   # AC035
    ("7cc2a73a8294", "পশ্চিম"),   # AC005
    # --- Names, not headings -------------------------------------------------
    # Everything above is page-1 boilerplate, which turns out to be a narrow
    # slice of the font: the headings never need glyph 85 at all, and every
    # repha in them sits in the easy position. Voter names are what the decoder
    # is FOR, and they broke it -- 9.5% of rows in a six-AC sample carried an
    # unmapped glyph, and thousands more decoded to a wrong spelling with
    # nothing malformed to notice.
    #
    # `n` is how many times this exact glyph run occurred in that sample;
    # `was` is what the decoder emitted for it before the fix. The oracle is
    # that these are ordinary, independently-known Bengali names -- Mondal,
    # Barman, Naskar, Chakraborty, Murmu, Banerjee, Mukherjee -- not a snapshot
    # of current output.
    ("9455829b", "মণ্ডল"),                # n=3585  was মল       (85 unmapped)
    ("859597d46f", "বর্ম্মন"),             # n=1261  was বম্র্মন
    ("6fb22b9a", "নস্কর"),                # n=798   was নস্ক্র
    ("3a825cdc82855782c4d4", "চক্রবর্তী"),  # n=213   was চক্রবতীর্
    ("7cc255825782", "পণ্ডিত"),            # n=86    was পতি
    ("7cd9b4c165", "প্রসাদ"),              # n=62    was প্রাদ     (180 unmapped)
    ("24c68255c682", "কুণ্ডু"),            # n=54    was কুু
    ("468252c1d4", "ঝর্ণা"),               # n=54    was ঝণার্
    ("94c694c6d4", "মুর্মু"),              # n=52    was মুমুর্
    ("85c2368294", "বঙ্কিম"),              # n=52    was বমি      (54 unmapped)
    ("a636829a", "শঙ্কর"),                # n=51    was শর
    ("afc66dc1d2a882", "সুধাংশু"),         # n=50    was সুধাং    (168 unmapped)
    ("05c1a882cd5782c1a9", "আশুতোষ"),      # n=37    was আতোষ
    ("8599c16fc13fc4d4", "ব্যানার্জী"),     # n=32    was ব্যানাজীর্
    ("94564cc682", "মন্টু"),               # n=17    was মটু      (86 unmapped)
    ("a694c1d4", "শর্মা"),                # n=13    was শমার্
    ("94c62dc13fc4d4", "মুখার্জী"),        # n=4     was মুখাজীর্
    ("65c1afcf859ac12ec4", "দাসবৈরাগী"),   # n=396   was দাসবরাগী (207 unmapped)

    # A second sample, 6 North Bengal ACs x 8 parts, taken to chase the four
    # highest-frequency ids still being reported. Three turned out to be
    # conjuncts and are read off Latin-origin names the roll spells
    # phonetically -- Francis, Victor, Brahmachari -- which pin the letters
    # far harder than a Bengali word would, having only one possible reading.
    ("83d982c1c272af", "ফ্রান্সিস"),        # n=11    was ফ্রাসি   (114 unmapped)
    ("9bcd9a72", "লরেন্স"),                # n=2     was লরে      (114 unmapped)
    ("94c672c4", "মুন্সী"),                # n=2     was মুী      (114 unmapped)
    ("c28f82269a", "ভিক্টর"),              # n=8     was ভির      (38 unmapped)
    ("c28f82cd26c1c29abbc1", "ভিক্টোরিয়া"), # n=3     was ভিােরিয়া (38 unmapped)
    ("85d9b6823a82c19ac4", "ব্রহ্মচারী"),   # n=2     was ব্রচারী   (182 unmapped)
    ("85d9b6826fc19ac1bb6f", "ব্রহ্মনারায়ন"),# n=2    was ব্রনারায়ন (182 unmapped)
    # 223 is the fourth. Note the `was` column: the spelling was already
    # right, so what it cost was the damage remark, not the name.
    ("85b9dfc6bbc1", "বড়ুয়া"),           # n=4     was বড়ুয়া   (223 unmapped)
    ("4682b9dfc6", "ঝড়ু"),                # n=13    was ঝড়ু     (223 unmapped)
]

# Page-1 headings, truncated. AC001 is SHREE550; AC022 (Kalimpong) is one of
# Darjeeling's five constituencies, which use a different glyph space.
SHREELIPI_HEADING = (
    "c26f85d4c13a822482035782c1c29b2482c103151313150f039ac13f997cc2a73a82948538c2856dc"
)
DARJEELING_HEADING = (
    "52523d3d47475050873087302b2b070739395050525245452b2b07075050151513131313151544445"
)


def _pua(hexstr):
    """A run of two-hex-digit glyph ids, as the private-use text decode() takes."""
    return "".join(
        chr(sl.PUA_BASE + int(hexstr[i:i + 2], 16)) for i in range(0, len(hexstr), 2)
    )


@pytest.mark.parametrize("hexstr,expected", PAIRS, ids=[p[1] for p in PAIRS])
def test_real_tokens_decode_to_correct_bengali(hexstr, expected):
    text, unknown = decode(_pua(hexstr))
    assert unknown == [], f"unmapped glyph ids {unknown} in {expected!r}"
    assert text == expected


def test_the_hooks_are_not_letters():
    """Glyphs 220 and 222 modify the glyph before them instead of adding one.

    Mapping either as a character corrupts every word containing it -- 220
    alone runs to roughly three uses per page -- so this pins the behaviour
    rather than leaving it to the token pairs above.
    """
    # 92 alone is ত্র; 92 + 220 is ক্র.
    assert decode(_pua("5c"))[0] == "ত্র"
    assert decode(_pua("5cdc"))[0] == "ক্র"
    # 89 alone is ত্ত; 89 + 220 is ক্ত.
    assert decode(_pua("59"))[0] == "ত্ত"
    assert decode(_pua("59dc"))[0] == "ক্ত"
    # 105 alone is দ্ব; 105 + 222 is ধ.
    assert decode(_pua("69"))[0] == "দ্ব"
    assert decode(_pua("69de"))[0] == "ধ"



def test_a_repha_rides_the_whole_conjunct_not_just_its_base():
    """বর্ম্মন (Barman) is drawn ব + ম(half) + ম + repha. Seating the repha
    immediately before the base ম -- the consonant it is literally drawn on --
    gives বম্র্মন, because the half ম ahead of it belongs to the same cluster
    and the repha has to clear all of it. 1,261 of those in a six-AC sample,
    against the most common surname in the state.
    """
    assert decode(_pua("859597d46f"))[0] == "বর্ম্মন"
    # The simple case still has to work: nothing ahead of the base consonant,
    # so the repha seats directly before it.
    assert decode(_pua("8569ded48294c16f"))[0] == "বর্ধমান"


def test_a_held_repha_lands_on_the_consonant_the_matra_belongs_to():
    """A repha drawn after a MATRA is buffered, on the reading that it rides
    the consonant still to come -- কার্তিক is drawn ক া র্ ত ি. That reading
    is only right when a consonant actually follows. চক্রবর্তী ends there
    instead (চ ক্র ব ত ী র্), and the font drew the repha last only because it
    had to clear the ী. Appending it produced চক্রবতীর্ -- a word ending in a
    hasant, which Bengali orthography does not allow, which is what makes this
    self-detecting (see test_decode_leaves_no_stray_hasant).
    """
    assert decode(_pua("3a825cdc82855782c4d4"))[0] == "চক্রবর্তী"
    assert decode(_pua("c26f85d4c13a822482"))[0] == "নির্বাচক"   # held, and used


def test_a_held_repha_never_crosses_a_word_boundary():
    """Worse than losing it: carried across the space it gets consumed by the
    NEXT word's first consonant, corrupting two names instead of one and
    leaving nothing malformed for the stray-hasant check to catch."""
    both = decode(_pua("3a825cdc82855782c4d4" + "03" + "9455829b"))[0]
    assert both == "চক্রবর্তী মণ্ডল"          # was 'চক্রবতী র্মল'


def test_a_half_form_after_a_half_form_completes_it():
    """A half form is completed either by a bare stem glyph or by the next
    consonant -- and that next consonant may itself be a glyph that is usually
    drawn as a half form. নস্কর is ন + স(half) + ক(half-glyph) + র; treating
    the ক as another half gave নস্ক্র, i.e. a ra-phala that is not there."""
    assert decode(_pua("6fb22b9a"))[0] == "নস্কর"
    # Stem completion is unchanged: স(half) + stem is a whole স.
    assert decode(_pua("b281"))[0] == "স"


def test_the_glyphs_added_for_names_are_all_reachable():
    """Six ids the page-1 headings never exercise. Each was silently absent
    from thousands of names -- decode() reported them, which is how they were
    found, and this pins them so a table edit cannot quietly drop one."""
    for gid in (54, 85, 86, 168, 180, 207):
        assert gid in sl.KNOWN_GIDS, gid
    assert decode(_pua("36"))[0] == "ঙ্ক"
    assert decode(_pua("55"))[0] == "ণ্ড"
    assert decode(_pua("a8"))[0] == "শু"
    assert decode(_pua("b4"))[0] == "স"
    assert decode(_pua("56b4"))[0] == "ন্স"          # 86 is a half form
    assert decode(_pua("cf85"))[0] == "বৈ"           # 207 is pre-base

def test_the_three_conjuncts_read_off_latin_origin_names():
    """ক্ট, ন্স and হ্ম were each absent from the table, so every name needing
    one lost a whole syllable -- Victor decoded as ভির, Francis as ফ্রাসি.

    The evidence is the names themselves: these are transliterations of
    Victor/Victoria/Benedict, Francis/Lawrence/Florence/Munshi and
    Brahmachari/Brahmananda, which admit exactly one Bengali spelling each. A
    second, independent check is where the ids sit -- 38 among ক্ক/ক্ম/ক্স, 114
    among ন্ন/ন্ড, 182 between হ and হু -- i.e. each falls inside the varga
    block its reading belongs to."""
    for gid in (38, 114, 182):
        assert gid in sl.KNOWN_GIDS, gid
    assert decode(_pua("26"))[0] == "ক" + sl.HASANT + "ট"
    assert decode(_pua("72"))[0] == "ন" + sl.HASANT + "স"
    assert decode(_pua("b6"))[0] == "হ" + sl.HASANT + "ম"
    # 114 is a single glyph for the same cluster 86+180 spells as two.
    assert decode(_pua("72"))[0] == decode(_pua("56b4"))[0]


def test_the_du_connector_is_silence_not_a_missing_letter():
    """223 is only ever drawn between ড়/ঢ় and a following ু/ূ -- the u-matra
    cannot sit under the nukta, so the font draws a connector for it. Dropping
    it was always right; reporting it was not, and that report was the single
    largest source of damage remarks in the state."""
    assert 223 in sl.IGNORE_GIDS
    for hexstr, expected in (("4682c1b9dfc6", "ঝাড়ু"), ("2dc1b9dfc6bbc1", "খাড়ুয়া"),
                             ("05c1a9c1badfc6", "আষাঢ়ু"), ("6fc1b9dfc6", "নাড়ু")):
        text, unknown = decode(_pua(hexstr))
        assert text == expected
        assert unknown == []
    # Dropping it is not the same as it being absent: the ড় and the ু are both
    # still real glyphs either side of it.
    assert decode(_pua("b9c6"))[0] == "ড়ু"


def test_unknown_glyphs_are_reported_not_silently_dropped():
    """A hole has to be visible. A missing letter is a bug report; a guessed
    one is a corrupted name that still searches and still ranks."""
    unmapped = sorted(set(range(1, 225)) - sl.KNOWN_GIDS)
    assert unmapped, "expected some glyph ids to be deliberately unmapped"
    text, unknown = decode(_pua("24" + f"{unmapped[0]:02x}" + "24"))
    assert unknown == [unmapped[0]]
    assert text == "কক"


def test_decode_leaves_no_private_use_code_points_behind():
    for hexstr, _ in PAIRS:
        text, _ = decode(_pua(hexstr))
        assert not any(0xE000 <= ord(c) <= 0xF8FF for c in text)


def test_decode_leaves_no_stray_hasant():
    """A hasant with nothing after it means a half form was never completed."""
    for hexstr, expected in PAIRS:
        text, _ = decode(_pua(hexstr))
        assert not re.search(sl.HASANT + r"(?:\s|$)", text), expected


def test_non_pua_text_passes_through():
    """The rolls mix Latin in -- EPIC numbers, the state code -- and it must
    survive untouched."""
    text, unknown = decode("WB/03/001/000405")
    assert text == "WB/03/001/000405"
    assert unknown == []


def test_darjeeling_is_not_run_through_this_table():
    """AC022-AC026 use a different glyph space. Their ids overlap this table's
    numerically, so recognised-id counting says yes to them -- what separates
    them is whether the result is Bengali at all."""
    assert looks_like_shreelipi(_pua(SHREELIPI_HEADING))
    assert not looks_like_shreelipi(_pua(DARJEELING_HEADING))


def test_pua_base_agrees_with_the_connector():
    """decode() consumes what west_bengal.py's pdfminer patch produces, so the
    two constants have to stay equal."""
    from states import west_bengal

    assert sl.PUA_BASE == west_bengal.PUA_BASE
