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
        # The loaded ACs are the Latin-typeset Kolkata subset (see this
        # module's docstring in west_bengal.py) -- the ~275 Bengali-typeset
        # ACs aren't fetchable at all, so nothing Bengali-scripted ever
        # reaches the DB today. "latin" reflects what's actually loaded.
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
