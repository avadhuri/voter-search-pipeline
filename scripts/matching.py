"""
Shared, selectable name-matching scorers.

Both the CLI (search.py) and the web app (app.py) import ALGORITHMS from
here, so the README's algorithm write-up and the actual scoring code can't
drift apart.

Two scorers are shipped, chosen deliberately rather than blended into one
number:

- "wratio" (rapidfuzz.fuzz.WRatio): a composite token-based ratio. It tries
  several string-comparison strategies (plain ratio, token sort, token set,
  partial ratio) and returns the best, so it's tolerant of reordered tokens
  ("Kumar Ravi" vs "Ravi Kumar") and of one name being a substring of the
  other (a shortened/abbreviated entry). Good default for messy, OCR-era
  transliterations where word order isn't reliable.

- "jaro_winkler" (rapidfuzz.distance.JaroWinkler): a character-edit-distance
  metric purpose-built for short strings like personal names, standard in
  record-linkage literature. It scores based on matching characters within
  a bounded window plus transpositions, then boosts the score further for a
  shared prefix -- so "Shivaram" vs "Shivram" scores higher than an
  edit-distance metric would suggest, because the first several characters
  agree. Weaker than WRatio on reordered tokens (it compares the strings
  as-is, no token shuffling).

Neither is silently "better" -- they fail differently, which is why both are
exposed as an explicit user choice instead of averaged into a single score.
"""
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler


def _wratio_score(query, candidate):
    return fuzz.WRatio(query, candidate)


def _jaro_winkler_score(query, candidate):
    return JaroWinkler.normalized_similarity(query, candidate) * 100


ALGORITHMS = {
    "wratio": {
        "label": "WRatio (composite, token-based)",
        "score": _wratio_score,
    },
    "jaro_winkler": {
        "label": "Jaro-Winkler (prefix-weighted, record-linkage standard)",
        "score": _jaro_winkler_score,
    },
}

DEFAULT_ALGORITHM = "wratio"


def get_scorer(algorithm):
    if algorithm not in ALGORITHMS:
        raise ValueError(
            f"Unknown algorithm '{algorithm}'. Choose one of: {', '.join(ALGORITHMS)}"
        )
    return ALGORITHMS[algorithm]["score"]


def score_fields(scorer, query_fields, record_fields):
    """
    query_fields / record_fields: parallel lists, e.g. [name_query, relative_query]
    and [record_full_name, record_full_relative_name]. Empty query fields are
    skipped; the final score is the mean over fields actually queried.
    """
    parts = []
    for q, r in zip(query_fields, record_fields):
        if q:
            parts.append(scorer(q, r or ""))
    if not parts:
        return None
    return sum(parts) / len(parts)
