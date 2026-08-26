"""Recompute every corpus-level figure states/west_bengal_ocr.py quotes.

That docstring carries about a dozen numbers -- rows parsed, blank names,
what fraction of rows kept a sex, an EPIC, a serial, an age -- and each one
is an argument for a decision made in the module. They are also the numbers
most likely to be wrong, because every change that stores more rows moves
the denominator under all of them at once, and a figure written as fact goes
on reading as fact long after the code beneath it has moved. That is not
hypothetical here: a stale count survived a review inside a docstring whose
own subject was a previous stale count.

So the figures are computed by a committed script rather than by hand, and
the docstring says which one. Rerunning it is the whole audit.

Exhaustive over whatever it is pointed at -- no sampling, no network, no API
key. Free, and about six minutes for all three ACs.

    make wb-ocr-figures                       # the whole OCR corpus
    make wb-ocr-figures ACS=AC287             # one AC
    make wb-ocr-figures BASELINE=/path/to/checkout/states/west_bengal_ocr.py

BASELINE= loads a second copy of the connector -- another worktree, another
commit, `git show <rev>:states/west_bengal_ocr.py` written to a file -- and
parses the same corpus with both, so a change's effect on these figures is
stated rather than assumed small.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import re
import sys

AC_DIR = re.compile(r"^AC\d+$")

# Columns whose recovery rate the module's docstring quotes, and the row key
# each is stored under. Age is here and is not in the docstring's own table,
# which predates the age rule; the table is the thing that is stale, not this.
COVERAGE = (("sex", "gender"), ("EPIC", "local_ref"), ("serial", "serial_no"),
            ("age", "age"))

# The three ways a row reaches the corpus that would have been refused
# outright before the refused-rows change, each identifiable from the parsed
# row alone -- so this needs no BASELINE to count them, and cannot drift out
# of step with a baseline nobody re-ran.
#
# They overlap, and the overlap is small enough to lose: exactly one row in
# the corpus is both glued and `onya`, so summing the three columns reports
# 728 rows where 727 exist. The union is what gets printed as the total, and
# the classes are printed under it as what they are -- counts that do not add
# up to it. The one-row version of a discrepancy is the one worth printing,
# because the same summation over a future class could be off by thousands.
RECOVERED = (
    ("relation word glued to the next token", lambda r: "glued to the next token" in r["remark"]),
    ("relation word `onya` (the fourth code)", lambda r: r["relation_code"] == "O"),
    ("row begins at the relation word", lambda r: "row begins at the relation word" in r["remark"]),
)


def load_connector(path, tag):
    """Import one copy of the connector by path, not by module name.

    `python -m` would resolve to whichever copy is pip-installed, which is
    the one thing this script must not assume when it is asked to compare
    two of them.
    """
    spec = importlib.util.spec_from_file_location(tag, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(connector, ocr_dir, acs, worst_n):
    total = collections.Counter()
    per_ac = collections.defaultdict(collections.Counter)
    worst = []
    for ac in acs:
        for part in sorted(d for d in os.listdir(os.path.join(ocr_dir, ac))
                           if os.path.isdir(os.path.join(ocr_dir, ac, d))):
            rows = connector.parse_part(os.path.join(ocr_dir, ac, part))
            if not rows:
                continue
            counts = collections.Counter(rows=len(rows))
            counts["blank"] = sum(1 for r in rows if not r["full_name"])
            for label, key in COVERAGE:
                # Present, not truthy: a serial of 0 is a cell that was read
                # (badly), and counting it as unread would quietly restate
                # what the recovery figures mean.
                counts[label] = sum(1 for r in rows if r[key] not in (None, ""))
            counts["serial zero"] = sum(1 for r in rows if r["serial_no"] == 0)
            for label, matches in RECOVERED:
                counts[label] = sum(1 for r in rows if matches(r))
            counts["recovered"] = sum(
                1 for r in rows if any(m(r) for _, m in RECOVERED))
            total.update(counts)
            per_ac[ac].update(counts)
            worst.append((100.0 * counts["blank"] / counts["rows"], f"{ac}/{part}",
                          counts["blank"], counts["rows"]))
    return total, per_ac, sorted(worst, reverse=True)[:worst_n]


def pct(n, d):
    return f"{100.0 * n / d:.2f}%" if d else "n/a"


def report(label, total, per_ac, worst, acs):
    print(f"--- {label}")
    if not total["rows"]:
        print("  no rows parsed")
        return
    head = "  " + " " * 10 + "rows".rjust(9) + "".join(
        c.rjust(9) for c, _ in COVERAGE) + "blank".rjust(11)
    print(head)
    for ac in acs:
        c = per_ac[ac]
        if not c["rows"]:
            continue
        print(f"  {ac:<10}{c['rows']:>9}" +
              "".join(pct(c[col], c["rows"]).rjust(9) for col, _ in COVERAGE) +
              f"{c['blank']:>6} {pct(c['blank'], c['rows']):>8}")
    print(f"  {'all':<10}{total['rows']:>9}" +
          "".join(pct(total[col], total["rows"]).rjust(9) for col, _ in COVERAGE) +
          f"{total['blank']:>6} {pct(total['blank'], total['rows']):>8}")

    print(f"  rows stored that a relation-word miss would refuse: {total['recovered']}")
    for lbl, _ in RECOVERED:
        print(f"    {lbl:<42} {total[lbl]:>6}")
    classes = sum(total[lbl] for lbl, _ in RECOVERED)
    if classes != total["recovered"]:
        print(f"    (the classes overlap: they sum to {classes}, "
              f"{classes - total['recovered']} row(s) being in two of them)")
    if total["serial zero"]:
        print(f"  serials read as 0, counted above as read: {total['serial zero']}")
    if worst:
        print("  worst parts by blank name:")
        for rate, name, blank, rows in worst:
            print(f"    {name:<20} {rate:>6.2f}%  ({blank}/{rows})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", default="data/raw/west_bengal",
                    help="the state's raw directory; OCR lives in its ocr/ subdir")
    ap.add_argument("--acs", help="comma list, e.g. AC287,AC291 (default: all found)")
    ap.add_argument("--baseline", help="another checkout's states/west_bengal_ocr.py")
    ap.add_argument("--worst", type=int, default=5,
                    help="how many worst blank-name parts to list (default 5)")
    ap.add_argument("--json", help="also write the figures here, as JSON")
    args = ap.parse_args(argv)

    ocr_dir = os.path.join(args.raw_dir, "ocr")
    if not os.path.isdir(ocr_dir):
        # Not an error: the OCR corpus is gigabytes of paid API output and is
        # not something every checkout has. Say so and leave, so this can sit
        # in a chain of make targets without breaking it.
        print(f"no OCR output under {ocr_dir} -- nothing to measure", file=sys.stderr)
        return 0

    found = sorted(d for d in os.listdir(ocr_dir) if AC_DIR.match(d))
    acs = [a.strip() for a in args.acs.split(",")] if args.acs else found
    missing = [a for a in acs if a not in found]
    if missing:
        ap.error(f"no OCR output for {', '.join(missing)} (have: {', '.join(found) or 'none'})")
    if not acs:
        print(f"no AC directories under {ocr_dir}", file=sys.stderr)
        return 0

    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "states", "west_bengal_ocr.py")
    runs = []
    if args.baseline:
        runs.append(("baseline: " + args.baseline, load_connector(args.baseline, "wb_ocr_baseline")))
    runs.append((here, load_connector(here, "wb_ocr_here")))

    out = {}
    for label, connector in runs:
        total, per_ac, worst = measure(connector, ocr_dir, acs, args.worst)
        report(label, total, per_ac, worst, acs)
        out[label] = {"total": dict(total), "per_ac": {k: dict(v) for k, v in per_ac.items()}}

    if len(runs) == 2:
        before = out[runs[0][0]]["total"]
        after = out[runs[1][0]]["total"]
        print("--- baseline -> this checkout")
        for key in ("rows", "blank") + tuple(c for c, _ in COVERAGE):
            delta = after.get(key, 0) - before.get(key, 0)
            print(f"  {key:<10}{before.get(key, 0):>9} -> {after.get(key, 0):>9}  {delta:+}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
