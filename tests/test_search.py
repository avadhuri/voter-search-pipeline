import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import search as search_mod

ROWS = [
    {"full_name": "Shivaram", "full_relative_name": "Ramaiah", "ac_code": "A085",
     "part_no": 5, "serial_no": 201, "gender": "M", "age": 45},
    {"full_name": "Shivram", "full_relative_name": "Ramayya", "ac_code": "A085",
     "part_no": 5, "serial_no": 202, "gender": "M", "age": 46},
    {"full_name": "Lakshmi", "full_relative_name": "Shivaram", "ac_code": "A085",
     "part_no": 5, "serial_no": 203, "gender": "F", "age": 38},
]


def test_search_ranks_exact_match_first():
    results = search_mod.search(ROWS, name="Shivaram", min_score=0, limit=10)
    assert results[0][1]["full_name"] == "Shivaram"
    assert results[0][0] == 100


def test_search_finds_typo_tolerant_match():
    results = search_mod.search(ROWS, name="Shivaram", min_score=70, limit=10)
    names = [r["full_name"] for _, r in results]
    assert "Shivram" in names  # one-character-dropped typo should still surface


def test_search_min_score_filters_out_weak_matches():
    results = search_mod.search(ROWS, name="Zzzznomatch", min_score=70, limit=10)
    assert results == []


def test_load_rows_rejects_more_than_max_acs():
    import pytest
    with pytest.raises(ValueError):
        search_mod.load_rows(":memory:", ac_codes=["A001", "A002", "A003", "A004", "A005", "A006"])
