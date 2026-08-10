"""
Download the 2002 roll for Haryana ACs into data/raw/haryana/<AC_CODE>.zip,
ready for `build_db.py --combine`.

Haryana publishes one PDF per *part* rather than one file per AC, so each
AC's couple-of-hundred part PDFs are bundled into a single ZIP (plus a
manifest recording which parts the portal listed and which actually
existed) -- see states/haryana.py. No CAPTCHA or auth is involved; the one
on the CEO site's page is client-side decoration that never validates.

Only the 44 ACs with a real text layer are fetched by default. The other 46
are page scans that this connector cannot parse, so downloading them would
produce raw files nothing can read -- pass --include-scanned to archive them
anyway.

Usage:
    download_haryana.py [--out-dir data/raw/haryana] [--rate 0.5] [--force]
                        [--limit N] [--ac HR61 | HR61,HR22] [--include-scanned]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.haryana import HaryanaConnector


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/raw/haryana")
    ap.add_argument("--rate", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--force", action="store_true", help="re-download even if file exists")
    ap.add_argument("--limit", type=int, default=None, help="only fetch the first N ACs (for testing)")
    ap.add_argument("--ac", default=None, help="fetch only these AC codes, comma-separated, e.g. HR61 or HR61,HR22")
    ap.add_argument("--include-scanned", action="store_true",
                    help="also fetch the 46 ACs published as unparseable page scans")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    connector = HaryanaConnector(request_delay=args.rate)
    acs = connector.list_constituencies()
    if args.ac:
        wanted = set(args.ac.split(","))
        acs = [ac for ac in acs if ac.ac_code in wanted]
        missing = wanted - {ac.ac_code for ac in acs}
        if missing:
            print(f"Unknown AC code(s): {', '.join(sorted(missing))}")
            sys.exit(1)
    else:
        if not args.include_scanned:
            scanned = [ac for ac in acs if ac.extra["roll_format"] == "scanned"]
            acs = [ac for ac in acs if ac.extra["roll_format"] == "text"]
            print(f"Skipping {len(scanned)} scanned ACs (use --include-scanned to fetch them).")
        if args.limit:
            acs = acs[: args.limit]
    print(f"{len(acs)} ACs to process, {sum(ac.total_parts for ac in acs)} part PDFs.")

    ok, skipped, failed = 0, 0, []
    for i, ac in enumerate(acs, 1):
        out_path = os.path.join(args.out_dir, f"{ac.ac_code}.zip")
        if os.path.exists(out_path) and not args.force:
            skipped += 1
            continue
        try:
            raw = connector.fetch_raw(ac, roll_year=2002)
            with open(out_path, "wb") as f:
                f.write(raw)
            ok += 1
            print(f"[{i}/{len(acs)}] {ac.ac_code} {ac.ac_name} ({ac.district}) "
                  f"-> {ac.total_parts} parts, {len(raw)} bytes")
        except Exception as e:
            failed.append((ac.ac_code, str(e)))
            print(f"[{i}/{len(acs)}] {ac.ac_code} FAILED: {e}")
        time.sleep(args.rate)

    print(f"\nDone. {ok} downloaded, {skipped} already present, {len(failed)} failed.")
    if failed:
        print("Failed ACs:", ", ".join(code for code, _ in failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
