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
        # Mixed in principle: the Kolkata ACs are Latin-typeset, the rest are
        # Bengali-typeset and only yield names under WB_OCR=1 (see
        # west_bengal.py's docstring). "script" is read only to pick the
        # Devanagari->Latin transliteration bridge and there is no Bengali
        # equivalent, so "latin" stays right either way -- with the caveat
        # that an OCR'd name is stored in Bengali script, which a
        # Latin-script query therefore cannot match.
        "script": "latin",
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
