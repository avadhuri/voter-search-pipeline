"""
Download the 2002 roll for West Bengal ACs into
data/raw/west_bengal/<AC_CODE>.zip, ready for `build_db.py --combine`.

West Bengal publishes one PDF per polling-station part rather than one file
per AC, so an AC's fetch is a few hundred requests and its raw artefact is a
ZIP of the part PDFs (see states/west_bengal.py). Two consequences for the
CLI, both different from download_2002_all.py:

  * --rate is applied between *parts*, not between ACs, because parts are
    where essentially all the requests are.
  * a partially-downloaded AC is written to a .part file and only renamed on
    success, so an interrupted run never leaves a truncated ZIP that the
    resume logic would then skip.

Statewide this is 61,531 part PDFs (~15 GB), so start with --limit or --ac.

Usage:
    download_west_bengal.py [--out-dir data/raw/west_bengal] [--rate 0.5]
                            [--force] [--limit N] [--ac AC001]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.west_bengal import WestBengalConnector


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/raw/west_bengal")
    ap.add_argument("--rate", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--force", action="store_true", help="re-download even if file exists")
    ap.add_argument("--limit", type=int, default=None, help="only fetch the first N ACs (for testing)")
    ap.add_argument("--ac", default=None, help="fetch only these AC codes, comma-separated, e.g. AC001 or AC141,AC142")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    connector = WestBengalConnector()
    acs = connector.list_constituencies()
    if args.ac:
        wanted = set(args.ac.split(","))
        acs = [ac for ac in acs if ac.ac_code in wanted]
        missing = wanted - {ac.ac_code for ac in acs}
        if missing:
            print(f"Unknown AC code(s): {', '.join(sorted(missing))}")
            sys.exit(1)
    elif args.limit:
        acs = acs[: args.limit]
    print(f"{len(acs)} ACs to process, {sum(ac.total_parts for ac in acs)} parts.")

    ok, skipped, failed = 0, 0, []
    for i, ac in enumerate(acs, 1):
        out_path = os.path.join(args.out_dir, f"{ac.ac_code}.zip")
        if os.path.exists(out_path) and not args.force:
            skipped += 1
            continue
        print(f"[{i}/{len(acs)}] {ac.ac_code} {ac.ac_name} ({ac.district}), {ac.total_parts} parts")
        done = [0]

        def progress(part, nbytes, ac=ac, done=done):
            done[0] += 1
            print(
                f"    part {part['part_no']} ({done[0]}/{ac.total_parts}) {nbytes} bytes",
                end="\r",
                flush=True,
            )
            time.sleep(args.rate)

        try:
            raw = connector.fetch_raw(ac, roll_year=2002, on_part=progress)
            tmp_path = out_path + ".part"
            with open(tmp_path, "wb") as f:
                f.write(raw)
            os.replace(tmp_path, out_path)
            ok += 1
            print(f"    -> {out_path}, {len(raw)} bytes" + " " * 20)
        except Exception as e:
            failed.append((ac.ac_code, str(e)))
            print(f"    FAILED: {e}" + " " * 20)

    print(f"\nDone. {ok} downloaded, {skipped} already present, {len(failed)} failed.")
    if failed:
        print("Failed ACs:", ", ".join(code for code, _ in failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
