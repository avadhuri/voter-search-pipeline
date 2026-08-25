# -*- coding: utf-8 -*-
"""
Regression test for the SHREE550 -> Unicode transcoder
(states/west_bengal_shreelipi.py).

Each pair is (glyph-id sequence as hex, expected Unicode Bengali). The sources
are real tokens lifted from ceowestbengal.nic.in's 2002 roll PDFs; the
expectations are what the roll form's own printed headings say and what the
portal's own English AC names transliterate to -- an independent oracle, not a
snapshot of the decoder's current output.

Coverage is deliberate: the set exercises every structural rule in the
transcoder -- half-form plus stem composition, pre-base matra reordering
(including over a below-base phala), repha reordering in both its positions,
the ka- and dha- hooks, the two-glyph digraphs, and conjuncts.
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
