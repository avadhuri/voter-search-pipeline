"""
Regenerate states/meta/sir_source_urls/state_roll_years.json from the
workbook it is derived from.

The workbook (`state_roll_years.xlsx`) is a *received* artifact: it arrived
in the same `sir_excel.zip` drop as the 35 per-state source-URL workbooks,
and nothing here produces it -- see that directory's README for provenance.
The JSON beside it is a projection of that workbook, and until this script
existed it was one nobody could re-derive: the two files were committed
together in `80d4045` with no code linking them, so the only thing asserting
they agreed was that someone had once made them agree.

That matters more than the usual derived-file tidiness. `roll_year` is not
decoration -- the serving app derives an elector's year of birth as
`roll_year - age` for its *required* year-of-birth filter, so a state carrying
the wrong year by four years tells four years of its own electors they were
never on the roll. Ten of the states currently being onboarded are non-2002
(2003, 2005 and 2006), 94.9M rows between them. A hand-maintained mapping
is the wrong shape for that, and this repo's standing convention says every
pipeline step ends up runnable unattended rather than re-derived by hand.

So: the workbook is the source of truth, this regenerates the JSON from it,
and `tests/test_roll_years.py` fails if the committed JSON has drifted from
what this would produce. The JSON stays committed rather than being read
straight from the workbook at build time because a wrong year here mis-stamps
tens of millions of rows, and a JSON diff is reviewable in a pull request
where an .xlsx diff is a binary blob.

    python -m build_roll_years            # rewrite the committed JSON
    python -m build_roll_years --check    # exit 1 if it would change
"""
import argparse
import json
import os
import sys

import openpyxl

_HERE = os.path.dirname(os.path.abspath(__file__))
_META_DIR = os.path.join(_HERE, "..", "states", "meta", "sir_source_urls")
WORKBOOK_PATH = os.path.normpath(os.path.join(_META_DIR, "state_roll_years.xlsx"))
JSON_PATH = os.path.normpath(os.path.join(_META_DIR, "state_roll_years.json"))

# Sheet header -> JSON key. The state code is the JSON's own key, not a field.
COLUMNS = {
    "State": "state",
    "Roll Year": "roll_year",
    "Source": "source",
    "Format": "format",
}
CODE_COLUMN = "State Code"


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def roll_years_from_workbook(path=WORKBOOK_PATH):
    """The workbook as the JSON's nested dict: state code -> fields.

    Headers are matched by name rather than position -- a reordered or
    extended sheet in a future drop should still resolve, and a *renamed*
    one should fail loudly here rather than silently produce a file with a
    column of nulls.
    """
    ws = openpyxl.load_workbook(path, read_only=True).active
    rows = ws.iter_rows(values_only=True)
    header = [_clean(cell) for cell in next(rows)]
    missing = [name for name in (CODE_COLUMN, *COLUMNS) if name not in header]
    if missing:
        raise ValueError(
            f"{os.path.basename(path)}: expected column(s) {missing} not in "
            f"{header}. The workbook's shape changed -- update COLUMNS rather "
            f"than hand-editing the JSON."
        )
    index = {name: header.index(name) for name in (CODE_COLUMN, *COLUMNS)}

    out = {}
    for row in rows:
        code = _clean(row[index[CODE_COLUMN]])
        if not code:
            continue
        if code in out:
            raise ValueError(f"{os.path.basename(path)}: state code {code!r} twice")
        out[code] = {
            key: _clean(row[index[name]]) for name, key in COLUMNS.items()
        }
    return out


def render(data):
    """The exact bytes the committed file should hold.

    One state per line, with the fields inline, rather than json.dumps'
    default indent=2 nesting: 36 lines that each read as a complete
    statement about one state, and a diff that shows a changed year as one
    changed line. Matching the file's existing hand-written layout also
    means the first run of this script produces no diff at all, which is
    the only way to demonstrate the generator agrees with what was already
    committed rather than merely replacing it.
    """
    lines = ["{"]
    entries = list(data.items())
    for i, (code, fields) in enumerate(entries):
        body = ", ".join(
            f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
            for k, v in fields.items()
        )
        comma = "" if i == len(entries) - 1 else ","
        lines.append(f"  {json.dumps(code)}: {{{body}}}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="don't write; exit 1 if the committed JSON is out of date",
    )
    args = parser.parse_args(argv)

    rendered = render(roll_years_from_workbook())
    current = None
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            current = f.read()

    if current == rendered:
        print(f"{os.path.basename(JSON_PATH)} is up to date "
              f"({len(json.loads(rendered))} states/UTs).")
        return 0
    if args.check:
        print(
            f"{JSON_PATH} does not match {os.path.basename(WORKBOOK_PATH)}. "
            f"Run `make roll-years` and commit the result.",
            file=sys.stderr,
        )
        return 1
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"wrote {JSON_PATH} ({len(json.loads(rendered))} states/UTs).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
