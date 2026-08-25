"""
Run the OCR preprocessing pass over Haryana's scanned ACs.

This is the batch stage between `make download-haryana` and `make build-db`
for the 46 ACs that are page scans (see states/haryana_ocr.py for why it is a
separate stage, and states/haryana.py's SCANNED_ACS for which ACs those are).
It adds a partNNNN.ocr.json artifact next to each partNNNN.pdf inside the
AC's existing raw ZIP; build_db.py then picks those up with no changes.

Usage:

    python -m ocr_haryana --ac HR18                     # a whole AC
    python -m ocr_haryana --ac HR18,HR38 --parts 2      # first 2 parts each
    python -m ocr_haryana --ac HR18 --force             # redo existing artifacts

Already-OCRed parts are skipped, so an interrupted run is safe to re-run.
Expect roughly 5 seconds per page at the default 300 dpi -- an AC of ~250
parts at ~20 pages each is several hours, which is why --parts exists.
"""
import argparse
import os
import sys
import time

from states.haryana_ocr import (
    DEFAULT_DPI,
    DEFAULT_LANG,
    DEFAULT_PSM,
    TesseractUnavailableError,
    ocr_zip,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ac", required=True,
                        help="AC code or comma-separated list, e.g. HR18,HR38")
    parser.add_argument("--raw-dir", default=os.path.join("data", "raw", "haryana"),
                        help="directory holding <AC>.zip (default: %(default)s)")
    parser.add_argument("--parts", type=int,
                        help="only OCR this many not-yet-OCRed parts per AC")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--lang", default=DEFAULT_LANG)
    parser.add_argument("--psm", default=DEFAULT_PSM)
    parser.add_argument("--force", action="store_true",
                        help="re-OCR parts that already have an artifact")
    args = parser.parse_args(argv)

    for ac_code in [c.strip() for c in args.ac.split(",") if c.strip()]:
        path = os.path.join(args.raw_dir, f"{ac_code}.zip")
        if not os.path.exists(path):
            sys.exit(f"{path} not found -- run `make download-haryana AC={ac_code}` first")

        started = time.time()

        def progress(done, total, part_id, ac_code=ac_code):
            print(f"  [{ac_code}] part {part_id} ({done}/{total})", flush=True)

        try:
            written = ocr_zip(
                path, parts=args.parts, dpi=args.dpi, lang=args.lang,
                psm=args.psm, force=args.force, progress=progress,
            )
        except TesseractUnavailableError as exc:
            sys.exit(str(exc))
        print(f"{ac_code}: {written} part(s) OCR'd in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
