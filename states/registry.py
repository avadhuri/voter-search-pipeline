"""
Single source of truth for "which states exist and where's their data,"
imported by both build_db.py (building the combined DB) and app.py (the AC
picker + search) so the two can't drift out of sync on what a state's raw
files look like or where they live.

Adding a state means writing its connector module (states/<state>.py) plus
one entry here -- nothing else needs to know about the new state by name.
"""
from states.karnataka import KarnatakaConnector
from states.west_bengal import WestBengalConnector
from states.haryana import HaryanaConnector

STATE_CONNECTORS = {
    "karnataka": {
        "connector_cls": KarnatakaConnector,
        "label": "Karnataka",
        "raw_dir": "data/raw",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "west_bengal": {
        "connector_cls": WestBengalConnector,
        "label": "West Bengal",
        "raw_dir": "data/raw/west_bengal",
        "raw_glob": "*.zip",
        # No longer only the Latin-typeset Kolkata subset: the Shree-Lipi
        # glyph table decodes the ~265 Bengali-typeset ACs to real Bengali
        # text, and the three page-scan ACs come back as Bengali from OCR.
        # This flag answers one question -- "could any row in this state be
        # non-Latin?" -- and it is now yes. Which scheme a given *string*
        # needs is decided per string by transliteration.py's detect_scheme(),
        # and whether a *query* needs the Latin columns at all is decided per
        # query by is_latin_query(), so the 19 Latin-typeset Kolkata ACs are
        # unaffected: their names hold no Indic characters, the backfill
        # writes them through unchanged, and a Latin query still matches
        # their raw columns directly.
        "script": "bengali",
    },
    "haryana": {
        "connector_cls": HaryanaConnector,
        "label": "Haryana",
        "raw_dir": "data/raw/haryana",
        "raw_glob": "*.zip",
        # DK-RAJ legacy-font PDFs decode to real Devanagari text (see
        # states/haryana_dkraj.py) -- scripts/transliteration.py uses this
        # flag to know which states need *_latin columns backfilled/matched.
        "script": "devanagari",
    },
}
