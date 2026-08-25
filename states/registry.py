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
from states.csv_connector import _make_csv_connector_cls

STATE_CONNECTORS = {
    # ── Original 3 connectors (raw-file based) ──────────────────────────
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

    # ── CSV-based connectors (pre-extracted + transliterated) ───────────
    # All use the same CsvConnector class.  Per-AC CSV files live in
    # csv_output/<state>/per_ac/ (produced by scripts/split_csv.py from
    # the combined CSVs that scripts/extractors/extract_*.py output).
    #
    # CsvConnector stores native script in full_name and the CSV's own
    # romanization (_en columns) in full_name_latin.  script must match
    # the actual script so backfill_latin_columns() fills any rows where
    # _en was empty.

    # Hindi (Devanagari) states
    "madhya_pradesh": {
        "connector_cls": _make_csv_connector_cls("madhya_pradesh"),
        "label": "Madhya Pradesh",
        "raw_dir": "csv_output/madhya_pradesh/per_ac",
        "raw_glob": "*.csv",
        "script": "devanagari",
    },
    "rajasthan": {
        "connector_cls": _make_csv_connector_cls("rajasthan"),
        "label": "Rajasthan",
        "raw_dir": "csv_output/rajasthan/per_ac",
        "raw_glob": "*.csv",
        "script": "devanagari",
    },
    "chhattisgarh": {
        "connector_cls": _make_csv_connector_cls("chhattisgarh"),
        "label": "Chhattisgarh",
        "raw_dir": "csv_output/chhattisgarh/per_ac",
        "raw_glob": "*.csv",
        "script": "devanagari",
    },
    "uttarakhand": {
        "connector_cls": _make_csv_connector_cls("uttarakhand"),
        "label": "Uttarakhand",
        "raw_dir": "csv_output/uttarakhand/per_ac",
        "raw_glob": "*.csv",
        "script": "devanagari",
    },
    "himachal_pradesh": {
        "connector_cls": _make_csv_connector_cls("himachal_pradesh"),
        "label": "Himachal Pradesh",
        "raw_dir": "csv_output/himachal_pradesh/per_ac",
        "raw_glob": "*.csv",
        "script": "devanagari",
    },
    "chandigarh": {
        "connector_cls": _make_csv_connector_cls("chandigarh"),
        "label": "Chandigarh",
        "raw_dir": "csv_output/chandigarh/per_ac",
        "raw_glob": "*.csv",
        "script": "devanagari",
    },

    # Telugu
    "telangana": {
        "connector_cls": _make_csv_connector_cls("telangana"),
        "label": "Telangana",
        "raw_dir": "csv_output/telangana/per_ac",
        "raw_glob": "*.csv",
        "script": "telugu",
    },

    # Bengali
    "assam": {
        "connector_cls": _make_csv_connector_cls("assam"),
        "label": "Assam",
        "raw_dir": "csv_output/assam/per_ac",
        "raw_glob": "*.csv",
        "script": "bengali",
    },
    "tripura": {
        "connector_cls": _make_csv_connector_cls("tripura"),
        "label": "Tripura",
        "raw_dir": "csv_output/tripura/per_ac",
        "raw_glob": "*.csv",
        "script": "bengali",
    },

    # Gurmukhi
    "punjab": {
        "connector_cls": _make_csv_connector_cls("punjab"),
        "label": "Punjab",
        "raw_dir": "csv_output/punjab/per_ac",
        "raw_glob": "*.csv",
        "script": "gurmukhi",
    },

    # Malayalam
    "lakshadweep": {
        "connector_cls": _make_csv_connector_cls("lakshadweep"),
        "label": "Lakshadweep",
        "raw_dir": "csv_output/lakshadweep/per_ac",
        "raw_glob": "*.csv",
        "script": "malayalam",
    },

    # Multi-script (Tamil/Telugu/Malayalam — dominant is Tamil)
    "puducherry": {
        "connector_cls": _make_csv_connector_cls("puducherry"),
        "label": "Puducherry",
        "raw_dir": "csv_output/puducherry/per_ac",
        "raw_glob": "*.csv",
        "script": "tamil",
    },

    # English states (no transliteration needed)
    "delhi": {
        "connector_cls": _make_csv_connector_cls("delhi"),
        "label": "Delhi",
        "raw_dir": "csv_output/delhi/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "nagaland": {
        "connector_cls": _make_csv_connector_cls("nagaland"),
        "label": "Nagaland",
        "raw_dir": "csv_output/nagaland/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "goa": {
        "connector_cls": _make_csv_connector_cls("goa"),
        "label": "Goa",
        "raw_dir": "csv_output/goa/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "arunachal_pradesh": {
        "connector_cls": _make_csv_connector_cls("arunachal_pradesh"),
        "label": "Arunachal Pradesh",
        "raw_dir": "csv_output/arunachal_pradesh/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "meghalaya": {
        "connector_cls": _make_csv_connector_cls("meghalaya"),
        "label": "Meghalaya",
        "raw_dir": "csv_output/meghalaya/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "mizoram": {
        "connector_cls": _make_csv_connector_cls("mizoram"),
        "label": "Mizoram",
        "raw_dir": "csv_output/mizoram/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
    "sikkim": {
        "connector_cls": _make_csv_connector_cls("sikkim"),
        "label": "Sikkim",
        "raw_dir": "csv_output/sikkim/per_ac",
        "raw_glob": "*.csv",
        "script": "latin",
    },
}
