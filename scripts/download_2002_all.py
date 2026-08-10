"""
Download the 2002 roll CSV for every Karnataka AC (224 total) into
data/raw/<AC_CODE>.csv, ready for `build_db.py --combine`.

No CAPTCHA/auth involved -- these are the CEO site's own digitized files,
fetched directly (see states/karnataka.py). Rate-limited to be a polite
client, resumable (skips ACs whose file already exists so a re-run after a
failure only fetches what's missing).

Usage:
    download_2002_all.py [--out-dir data/raw] [--rate 1.0] [--force]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from states.karnataka import KarnatakaConnector


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/raw")
    ap.add_argument("--rate", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--force", action="store_true", help="re-download even if file exists")
    ap.add_argument("--limit", type=int, default=None, help="only fetch the first N ACs (for testing)")
    ap.add_argument("--ac", default=None, help="fetch only this one AC code, e.g. A085")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    connector = KarnatakaConnector()
    acs = connector.list_constituencies()
    if args.ac:
        acs = [ac for ac in acs if ac.ac_code == args.ac]
        if not acs:
            print(f"Unknown AC code: {args.ac}")
            sys.exit(1)
    elif args.limit:
        acs = acs[: args.limit]
    print(f"{len(acs)} ACs to process.")

    ok, skipped, failed = 0, 0, []
    for i, ac in enumerate(acs, 1):
        out_path = os.path.join(args.out_dir, f"{ac.ac_code}.csv")
        if os.path.exists(out_path) and not args.force:
            skipped += 1
            continue
        try:
            raw = connector.fetch_raw(ac, roll_year=2002)
            with open(out_path, "wb") as f:
                f.write(raw)
            ok += 1
            print(f"[{i}/{len(acs)}] {ac.ac_code} {ac.ac_name} ({ac.district}) -> {len(raw)} bytes")
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
