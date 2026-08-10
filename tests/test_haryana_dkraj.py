"""
Regression test for the DK-RAJ -> Unicode transcoder (states/haryana_dkraj.py).

Each pair is (DK-RAJ source bytes as hex, expected Unicode Devanagari). The
sources are real tokens lifted from ceoharyana.gov.in's 2002 roll PDFs across
all 41 text-layer ACs; the expectations were hand-written from knowledge of
the correct Hindi, NOT copied from the decoder's output, so this is a genuine
oracle rather than a snapshot of current behaviour.

Coverage is deliberate -- the set exercises every structural rule in the
transcoder: half-form + stem composition, pre-base i reordering, repha
reordering (standalone, matra-combined, and the pre-base i+repha glyph),
two-part vowel composition, nukta letters, and conjuncts.
"""
import unicodedata

from states.haryana_dkraj import decode

PAIRS = [
    ("ca78c9b4c9c9c753c945f2", "निर्वाचक"),
    ("78c9c9a8c9c9b4c9b1c9d2", "नामावली"),
    ("bdfecaaefaaac9c968c9c9", "हरियाणा"),
    ("cab4c9b6c9e4b9c9", "विशेष"),
    ("cab4c9ba69c9de69c9", "विस्तृत"),
    ("4fc9c9a8c9", "ग्राम"),
    ("a8c969c96ef9c978c9", "मतदान"),
    ("45e4f27870f9", "केन्द्र"),
    ("a6c9b4c978c9", "भवन"),
    ("45d6f2b1c9", "कुल"),
    ("45f2d2", "की"),
    ("bac9c64aaac9c9", "संख्या"),
    ("aefac956c945f2d2aac9", "राजकीय"),
    ("ba45daf2b1c9", "स्कूल"),
    ("78c9c9a8c9", "नाम"),
    ("bdfe6ef9a4c9ba69c9", "हदबस्त"),
    ("bac945c7f2b1c9", "सर्कल"),
    ("45f2c978c9da78c94dc9c9e4", "कानूनगो"),
    ("69c9bdfebac9d2b1c9", "तहसील"),
    ("ca56c9b1c9c9", "जिला"),
    ("cab4c976c9c978c9", "विधान"),
    ("bac9a6c9c9", "सभा"),
    ("49c9e46ac9", "क्षेत्र"),
    ("2bc9aefa49c968c9", "आरक्षण"),
    ("b1c9c9e445f2", "लोक"),
    ("ca78c9b4c9c9c753c978c9", "निर्वाचन"),
    ("7bc9d678c9aefad249c968c9", "पुनरीक्षण"),
    ("cab4c9b4c9aefa68c9", "विवरण"),
    ("bab4c9b0fc7bc9", "स्वरूप"),
    ("b4c9b9c9c7", "वर्ष"),
    ("7bc9c96ac969c9c9", "पात्रता"),
    ("ca69c9ca6cc9", "तिथि"),
    ("b1c9c94dc9da", "लागू"),
    ("bdfec9e478c9e4", "होने"),
    ("a6c9c94dc9", "भाग"),
    ("a8c9d64aaac9", "मुख्य"),
    ("7cc945f2c9aefa", "प्रकार"),
    ("bac9bdfec9aac945f2", "सहायक"),
    ("7bc9deb960f6", "पृष्ठ"),
    ("a8c9cabdfeb1c9c9", "महिला"),
    ("7bc9d6b0fcb9c9", "पुरूष"),
    ("cab4c9ba69c9c9aefa", "विस्तार"),
    ("47f2a8c9", "क्रम"),
    ("a8c945f2c978c9", "मकान"),
    ("a8c969c96ef9c969c9c9", "मतदाता"),
    ("caaefab669c9c9", "रिश्ता"),
    ("caaefab669c9e46ef9c9aefa", "रिश्तेदार"),
    ("cbb1c94dc9", "लिंग"),
    ("7bc9bdfe53c9c978c9", "पहचान"),
    ("7bc96ac9", "पत्र"),
    ("53c9d678c9c9b4c9", "चुनाव"),
    ("2b78c9d6a6c9c94dc9", "अनुभाग"),
    ("a8c9c969c9c9", "माता"),
    ("7bc9ca69c9", "पति"),
    ("45f2c9ecb1c9a8c92836293a", "कॉलम(6):"),
    ("2b78c9d6bac9c9aefa", "अनुसार"),
    ("4fc9c9a8c9d268c9", "ग्रामीण"),
    ("ceba6cc9ca69c9", "स्थिति"),
    ("ceba6cc969c9", "स्थित"),
    ("2bce7869c9a8c9", "अन्तिम"),
    ("2bc9aefacea8a6c945f2", "आरम्भिक"),
    ("b4c94dc9d445f2aefa68c9", "वर्गीकरण"),
    ("caa8c962f7b1c9", "मिडल"),
    ("4fc9c9a8c92fb6c9bdfeaefa", "ग्राम/शहर"),
    ("7bc95df5b4c9c9aefa", "पटवार"),
    ("bac9a6c9d2", "सभी"),
    ("cab1c942", "लिए"),
    ("a8c976c9d6", "मधु"),
    ("4dc9d663f74dc9c9c6b4c9", "गुड़गांव"),
    ("a8c9bde4fe7870f94dc967f8", "महेन्द्रगढ़"),
    ("56c9d36ef9", "जींद"),
    ("cbbac9bdfe", "सिंह"),
    ("6ee4f9b4c9d2", "देवी"),
    ("aefac9a8c9", "राम"),
    ("45d6f2a8c9c9aefa", "कुमार"),
    ("45def2b968c9", "कृष्ण"),
    ("aefac9a8c945f2b1c9d2", "रामकली"),
    ("a1daf2b1c9b4c969c9d2", "फूलवती"),
    ("78c9c9aefac9aac968c9", "नारायण"),
    ("7bc9aefaa8c9e4b7c9aefad2", "परमेश्वरी"),
    ("ca6ef978c9e4b6c9", "दिनेश"),
    ("aefac945e4f2b6c9", "राकेश"),
    ("ca78c9aec6fa56c978c9", "निरंजन"),
    ("6ed6f94dc9c9c7", "दुर्गा"),
    ("7bc9de6cb4c9d2", "पृथ्वी"),
    ("bac9aefac9e456c9", "सरोज"),
    ("b4c9e46ef97cc945f2c9b6c9", "वेदप्रकाश"),
    ("aefac956c9e47870f9", "राजेन्द्र"),
    ("4dc9d662c2f762f7d2", "गुड्डी"),
    ("ca53c96ac9da", "चित्रू"),
    ("7bc9d6b945f2aefa", "पुष्कर"),
    ("a8c9c9e4bdfe78c9", "मोहन"),
    ("bac969c9d2b6c9", "सतीश"),
    ("55f4c9e45ddaf5", "छोटू"),
    ("caaefa55f47bc9c9b1c9", "रिछपाल"),
    ("55f45656c9da", "छज्जू"),
    ("b1c955f4a8c978c9", "लछमन"),
    ("a8c9dacc69c9", "मूर्ति"),
    ("45f2d2cc69c9", "कीर्ति"),
    ("b6c9cca8c9b1c9c9", "शर्मिला"),
    ("3dcca8c9b1c9c9", "उर्मिला"),
    ("7bc96ef9d47bc9", "पर्दीप"),
    ("2bb6c9a1f2d4", "अशर्फी"),
    ("aefab4c9d370f9", "रवींद्र"),
    ("6ee4f9b4c9d370f9", "देवींद्र"),
    ("bab4c968c9c7", "स्वर्ण"),
    ("76c9a8c9c7b4c9d2aefa", "धर्मवीर"),
    ("76c9a8c9e76ef9aefa", "धर्मेंदर"),
    ("bac9cf69b4c96ef9aefa", "सत्विंदर"),
    ("3cce786ef9aefac9", "इन्दिरा"),
    ("b6c9c9ce7869c9", "शान्ति"),
    ("caa4c9a8c9b1c9c9", "बिमला"),
    ("a4c9b1c9b4c9c978c9", "बलवान"),
    ("56c9aac9a4c9d2aefa", "जयबीर"),
    ("bac9d66ee4f9b6c9", "सुदेश"),
    ("78c9d2b1c9a8c9", "नीलम"),
    ("b8c9d2", "श्री"),
    ("7bc9da68c9c7", "पूर्ण"),
    ("3db9c9c9", "उषा"),
    ("aefac968c9d2", "राणी"),
    ("a4c9a4c9b1c9d2", "बबली"),
    ("aefac9a8c9a1daf2b1c9", "रामफूल"),
    ("a4c9d2aefaa4c9b1c9", "बीरबल"),
    ("bac9cab4c969c9c9", "सविता"),
    ("6ef9aac9c9b1c9", "दयाल"),
    ("76c9a8c9c7", "धर्म"),
    ("7bc9c9b1c9", "पाल"),
    ("bac9b0fc7bc9", "सरूप"),
    ("bac9d678c9d2b1c9", "सुनील"),
    ("a8c9c9aac9c9", "माया"),
    ("56c9aac97bc9c9b1c9", "जयपाल"),
    ("7cc9e4a8c9", "प्रेम"),
    ("2bca78c969c9c9", "अनिता"),
    ("bac969aac9c9", "सत्या"),
    ("b0fc7bc9", "रूप"),
    ("53c9786ef9", "चन्द"),
    ("b1c9c9b1c9", "लाल"),
    ("45f27bc9daaefad2", "कपूरी"),
]


def test_boilerplate_and_names_decode_to_correct_hindi():
    bad = []
    for hexs, want in PAIRS:
        src = bytes.fromhex(hexs).decode("latin-1")
        got, unknown = decode(src)
        if got != unicodedata.normalize("NFC", want) or unknown:
            bad.append((src, want, got, unknown))
    assert not bad, "\n".join(
        f"{s!r}: want {w!r} got {g!r} unknown={u!r}" for s, w, g, u in bad
    )


def test_unknown_glyphs_are_reported_not_silently_dropped():
    # 0xBC, 0xD9 and 0xF3 are deliberately unmapped -- see the module docstring.
    got, unknown = decode("\xbc")
    assert unknown == ["\xbc"]
    assert "\ufffd" in got


def test_decode_leaves_no_private_use_sentinels_behind():
    for hexs, _ in PAIRS:
        got, _ = decode(bytes.fromhex(hexs).decode("latin-1"))
        assert not any("\ue000" <= c <= "\uf8ff" for c in got), got
