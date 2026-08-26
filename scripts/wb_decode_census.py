"""Census the West Bengal Shree-Lipi decoder against what Bengali forbids.

Every bug found in this glyph table has been a *seating* error -- an element
emitted before the consonant it belongs to (র্নিমল for নির্মল, দুযর্্যোধন for
দুর্যোধন) -- and a seating error leaves a signature the language itself makes
impossible: something lands at the start of a word that can never start one.

Two families are impossible word-initially, and this script counts both over
the real corpus:

  repha   A repha (র্) is a র in the *coda* of the syllable before it; it is
          drawn after the glyph it clears, so it needs a consonant in front of
          it. A word cannot begin with one.
  mark    A dependent vowel sign, a hasant, a nukta, or a candrabindu/anusvara/
          visarga all attach to a preceding consonant. A word cannot begin with
          any of them either.

Why this exists as a committed check rather than a one-off scan: the OCR oracle
(`decoder_oracle.py`) is a *sampling* instrument scored against a paid external
reader, and it is nearly blind to this class. The two fixes of round six moved
the oracle's aggregate disagreement by a single cell out of 6,536 while this
census moved word-initial rephas from 19 to 0 across 889,050 tokens. An
aggregate similarity metric cannot detect a regression that improves the
aggregate; an invariant the language makes impossible to violate can. So each
decode fix ships with one, and this is where they live.

Exhaustive over whatever it is pointed at, and free -- no network, no API key.
Nonzero exit if any impossible token is found, so it can gate a build.

    make wb-decode-census                  # default sample, ~4 min
    make wb-decode-census STRIDE=1 PARTS=8 # the whole corpus, slow
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import pdfplumber

# Imported for its import-time side effect, not for a name: west_bengal's
# _patch_pdfminer_gid_encoding() is what makes pdfplumber hand back /GXX glyph
# ids as U+E000+N. Without it every page extracts as unmapped characters, the
# PUA test below matches nothing, and this census scores zero tokens and
# reports OK -- which is exactly the false green it exists to catch, so the
# zero-token case is a failure in main() rather than a pass.
from states import west_bengal as _patches_pdfminer  # noqa: F401
from states import west_bengal_shreelipi as sl
from states.west_bengal_shreelipi import looks_like_shreelipi

# A repha is stored as the letter plus a hasant, so it is two code points.
REPHA = "র্"

# Everything below attaches to a preceding consonant and so cannot open a word.
COMBINING_MARKS = frozenset(
    "ঁংঃ"              # candrabindu, anusvara, visarga
    "়"                          # nukta
    "ািীুূৃৄ"   # aa i ii u uu ri rri
    "েৈ"                    # e ai
    "োৌ"                    # o au
    "্"                          # hasant
    "ৗ"                          # au length mark
)


# What a clean run still turns up, measured 2026-08-26 over 889,050 tokens in
# 98 ACs with every word-initial repha eliminated. Keyed by the LEADING GLYPH
# ID rather than the decoded form, and carrying a RATE rather than a count:
# the scan's stride is a knob, so counts move with it and rates do not.
#
# A new seating bug announces itself either as a leading glyph that is not on
# this list -- which is how the repha family would have announced itself -- or
# as one of these rising above its ceiling. Ceilings sit at roughly twice the
# measured rate, which is headroom for a different sample, not for a
# regression.
#
# A row here is one of exactly two things and conflating them is how an
# allowlist stops working. SOURCE_NOISE is damage in the roll itself, spread
# too thin for any rule to have produced it. Everything else is a bug that was
# found, filed and deliberately not fixed in the same breath, and it carries
# its issue number. The distinction is ENFORCED below rather than described
# here, because the failure mode of a table like this is somebody quieting a
# real finding by adding a row to it: you cannot add a gid without either
# citing an issue or asserting, in one word a reviewer sees in the diff, that
# the source is damaged.
SOURCE_NOISE = "source-noise"

ALLOWED_LEADING_GIDS: dict[int, tuple[float, str, str]] = {
    211: (2.4e-4, "#34",
          "candrabindu opening a name that has none: 107 tokens, 65 forms, "
          "8 ACs, concentrated in AC082/AC232/AC238. NOT the repha seating "
          "class -- these names carry no candrabindu anywhere for it to seat "
          "on (ঁবৃন্দাবন, "
          "ঁডমন, ঁরাবন), so the "
          "glyph is wrong rather than misplaced, and the same id decodes "
          "correctly mid-word in the same cells "
          "(হাঁসদা)."),
    35: (7.0e-5, "#35",
         "a literal 'ঃঃ' in the name column: 30 tokens, one distinct "
         "form, 13 ACs, first seen AC001/part0002. Punctuation, or a name the "
         "layout hid; either way not a name -- and not blank, so an "
         "empty-name check cannot see it."),
    # The genuine matra tail: 10 tokens in all, nine distinct forms, no two in
    # the same constituency, 1.1e-5 of the corpus. That is a discriminating
    # test rather than a small number waved through -- a wrong seating RULE is
    # wrong everywhere its glyph occurs, gid 206 above being 128 cells out of
    # 128, so a family this thin cannot be one.
    214: (2.0e-5, SOURCE_NOISE, "vocalic-r singleton, 4 tokens in 3 ACs"),
    201: (1.0e-5, SOURCE_NOISE, "uu singleton, 2 tokens in 2 ACs"),
    193: (1.0e-5, SOURCE_NOISE, "aa singleton, 2 tokens in 1 AC"),
    196: (1.0e-5, SOURCE_NOISE, "ii singleton, 1 token"),
    198: (1.0e-5, SOURCE_NOISE, "u singleton, 1 token"),
}

def assert_every_entry_is_cited(table=None) -> None:
    """Refuse an allowlist row that neither cites an issue nor names the source.

    Runs at import, so there is no way to load this module with an uncited row
    in the table. A test drives it with a bad table too, because an assertion
    that only ever sees good input is not an assertion.
    """
    for gid, (_ceiling, cite, _why) in (table or ALLOWED_LEADING_GIDS).items():
        if cite != SOURCE_NOISE and not re.fullmatch(r"#\d+", cite):
            raise AssertionError(
                f"gid {gid} sits on ALLOWED_LEADING_GIDS citing {cite!r}. "
                f"Every entry is either an open issue ('#123') or "
                f"{SOURCE_NOISE!r}. If you are here because the census went "
                f"red, the census found something: file it and cite the "
                f"number.")


assert_every_entry_is_cited()


def classify(decoded: str) -> str | None:
    """The impossibility this decode opens with, or None if it opens legally."""
    if not decoded:
        return None
    if decoded.startswith(REPHA):
        return "repha"
    if decoded[0] in COMBINING_MARKS:
        return "mark"
    return None


def _hex(token: str) -> str:
    """The raw glyph ids behind a PUA token, for pasting into a fixture."""
    return " ".join(
        str(ord(c) - sl.PUA_BASE) if sl.PUA_BASE <= ord(c) < sl.PUA_BASE + 4096
        else repr(c)
        for c in token
    )


def scan(raw_dir: Path, stride: int, parts: int, first_page: int, last_page: int,
         progress=print):
    """Decode the corpus and tally every word-initial impossibility."""
    zips = sorted(raw_dir.glob("AC*.zip"))[::stride]
    if not zips:
        raise SystemExit(f"no AC*.zip under {raw_dir}")

    tokens = 0
    hits: dict[str, collections.Counter] = {"repha": collections.Counter(),
                                            "mark": collections.Counter()}
    where: dict[str, tuple[str, str]] = {}
    leading: collections.Counter = collections.Counter()
    acs: dict[str, set] = collections.defaultdict(set)
    started = time.time()

    for i, zp in enumerate(zips, 1):
        try:
            with zipfile.ZipFile(zp) as zf:
                names = sorted(n for n in zf.namelist() if n.lower().endswith(".pdf"))
                for name in names[:parts]:
                    with pdfplumber.open(io.BytesIO(zf.read(name))) as pdf:
                        probe = ""
                        for page in pdf.pages:
                            probe = page.extract_text() or ""
                            if probe:
                                break
                        if not probe or not looks_like_shreelipi(probe):
                            continue        # an OCR'd or Latin-typeset AC
                        for page in pdf.pages[first_page:last_page]:
                            for token in (page.extract_text() or "").split():
                                if not any(sl.PUA_BASE <= ord(c) < sl.PUA_BASE + 4096
                                           for c in token):
                                    continue
                                tokens += 1
                                decoded = sl.decode(token)[0]
                                kind = classify(decoded)
                                if kind is None:
                                    continue
                                hits[kind][decoded] += 1
                                lead = next((ord(c) - sl.PUA_BASE for c in token
                                             if sl.PUA_BASE <= ord(c)
                                             < sl.PUA_BASE + 4096), None)
                                leading[lead] += 1
                                acs[decoded].add(zp.stem)
                                where.setdefault(decoded, (f"{zp.stem}/{name}",
                                                           _hex(token)))
        except Exception as exc:            # a damaged zip is not a decode bug
            progress(f"  [{i}/{len(zips)} {zp.stem}] skipped: {type(exc).__name__}")
        if i % 10 == 0:
            bad = sum(sum(c.values()) for c in hits.values())
            progress(f"  [{i}/{len(zips)}] {tokens:,} tokens, {bad} impossible,"
                     f" {time.time() - started:.0f}s")

    return {"acs": len(zips), "tokens": tokens, "hits": hits,
            "where": where, "acs_by_form": acs, "leading": leading,
            "seconds": round(time.time() - started, 1)}


def verdict(result) -> tuple[list[str], list[str]]:
    """(failures, notes) -- what must stop a build, and what is merely known.

    A word-initial repha is never allowed: it is the malformation this
    census was built for, it went from 19 to 0, and there is no legitimate
    source of one. Marks are judged against ALLOWED_LEADING_GIDS instead,
    because a handful genuinely survive in the corpus and pretending
    otherwise would leave a permanently red check, which defends nothing.
    """
    failures, notes = [], []
    tokens = result["tokens"] or 1

    n_repha = sum(result["hits"]["repha"].values())
    if n_repha:
        failures.append(f"{n_repha} word-initial repha(s) -- a Bengali word "
                        f"cannot begin with র্; this is a seating bug")

    for gid, count in sorted(result["leading"].items(),
                             key=lambda kv: -kv[1]):
        rate = count / tokens
        known = ALLOWED_LEADING_GIDS.get(gid)
        if known is None:
            failures.append(
                f"gid {gid} opens {count} token(s) with a mark ({rate:.2e} of "
                f"the corpus) and is not in ALLOWED_LEADING_GIDS -- a leading "
                f"glyph nobody has looked at yet")
        elif rate > known[0]:
            failures.append(
                f"gid {gid} opens {count} token(s), rate {rate:.2e} over its "
                f"ceiling {known[0]:.2e} -- {known[2].split(':')[0]}")
        else:
            tag = ("source noise" if known[1] == SOURCE_NOISE
                   else f"tracked defect {known[1]}")
            notes.append(f"gid {gid}: {count} token(s), {rate:.2e} "
                         f"(ceiling {known[0]:.2e}) [{tag}] -- {known[2]}")
    return failures, notes


def report(result, out=sys.stdout) -> int:
    """Print the census and return the number of impossible tokens."""
    hits = result["hits"]
    total = sum(sum(c.values()) for c in hits.values())
    print(f"\ntokens decoded : {result['tokens']:,} over {result['acs']} ACs"
          f" in {result['seconds']}s", file=out)
    for kind, label in (("repha", "word-initial repha (র্...)"),
                        ("mark", "word-initial combining mark")):
        counter = hits[kind]
        n = sum(counter.values())
        print(f"{label:32s}: {n:,} tokens in {len(counter):,} distinct forms",
              file=out)
        for form, count in counter.most_common(25):
            src, gids = result["where"][form]
            print(f"    {count:6,} {len(result['acs_by_form'][form]):3d}AC  "
                  f"{form}   gids=[{gids}]  {src}", file=out)
    print(f"\nimpossible tokens: {total:,}", file=out)
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw/west_bengal"))
    ap.add_argument("--stride", type=int, default=3,
                    help="scan every Nth AC (default 3, ~98 of 294)")
    ap.add_argument("--parts", type=int, default=3,
                    help="parts per AC (default 3)")
    ap.add_argument("--first-page", type=int, default=2,
                    help="skip the cover pages (default 2)")
    ap.add_argument("--last-page", type=int, default=14)
    ap.add_argument("--strict", action="store_true",
                    help="fail on ANY word-initial mark, ignoring the "
                         "measured baseline in ALLOWED_LEADING_GIDS")
    ap.add_argument("--json", type=Path, help="also write the tallies here")
    args = ap.parse_args(argv)

    result = scan(args.raw_dir, args.stride, args.parts,
                  args.first_page, args.last_page)
    total = report(result)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"acs": result["acs"], "tokens": result["tokens"],
             "seconds": result["seconds"],
             "repha": dict(result["hits"]["repha"]),
             "mark": dict(result["hits"]["mark"]),
             "leading": {str(k): v for k, v in result["leading"].items()},
             "where": {k: list(v) for k, v in result["where"].items()},
             "acs_by_form": {k: sorted(v)
                             for k, v in result["acs_by_form"].items()}},
            ensure_ascii=False, indent=2))

    if not result["tokens"]:
        print("\nFAIL: zero tokens decoded -- the scan found no Shree-Lipi text"
              f" at all under {args.raw_dir}. A census that scored nothing is"
              " not a clean census; check the raw path and that importing"
              " states.west_bengal still patches pdfminer's glyph encoding.")
        return 1

    failures, notes = verdict(result)
    if notes:
        print("\nknown and allowed:")
        for note in notes:
            print(f"  {note}")
    if args.strict and total:
        failures.append(f"--strict: {total} word-initial mark(s), baseline "
                        f"ignored")
    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"\nOK: no word-initial repha, and every word-initial mark is one of"
          f" the {len(ALLOWED_LEADING_GIDS)} known and explained cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
