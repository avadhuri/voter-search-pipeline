"""
Pilot downloader for the CURRENT (2025/26 SIR) electoral roll from
voters.eci.gov.in — a real, human-solved CAPTCHA gate, so this is
human-in-the-loop by design, not something to automate around.

What it automates: opening a real (headed) browser, walking the State ->
Year of Revision -> Roll Type -> Assembly Constituency -> Language form,
and ticking "Select All" on that AC's part checklist. What it does NOT
automate: solving the CAPTCHA or clicking "Download Selected PDFs" -- you
do that yourself in the visible browser window, then press Enter in the
terminal so the script can catch the resulting download(s).

Confirmed by inspecting the site directly (2026-08-10): one CAPTCHA
covers a WHOLE constituency (all its parts, via the "Select All"
checkbox + one "Download Selected PDFs" click) -- not one CAPTCHA per
part. For a ~224-AC state that means ~224 CAPTCHAs total, not thousands.

IMPORTANT: as of 2026-08-10, Karnataka's own "SIR FinalRoll - 2026" is
NOT YET published on this portal -- only Bye Election rolls (2 ACs:
Bagalkot, Davanagere South) are available for Karnataka today. Other
states (e.g. Uttar Pradesh, Kerala, Goa) already have their SIR rolls
up, so this script is state-parameterized: point it at whichever state
has data today, and re-point it at Karnataka once ECI publishes its
roll. --check-only lets you check what's published without downloading
anything.

Usage:
    # See what's published for a state without downloading anything:
    venv/bin/python scripts/download_2025_pilot.py --state Karnataka --check-only

    # Pilot 3 ACs of a state that already has data:
    venv/bin/python scripts/download_2025_pilot.py --state "Uttar Pradesh" \\
        --roll-type "SIR FinalRoll" --limit 3 --out-dir data/raw/2025_pilot
"""
import argparse
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"


def _select_by_label_contains(select_locator, contains, page, description):
    select_locator.wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(1000)
    options = select_locator.locator("option").all_inner_texts()
    match = next((o for o in options if contains.lower() in o.lower()), None)
    if match is None:
        raise SystemExit(
            f"No {description} option contains {contains!r}. Available: {options}"
        )
    select_locator.select_option(label=match)
    return match


def list_roll_types(page, state):
    page.goto(BASE_URL, wait_until="networkidle", timeout=45000)
    state_select = page.locator("select[name=stateCode]")
    state_select.wait_for(state="visible", timeout=20000)
    _select_by_label_contains(state_select, state, page, "state")

    role_select = page.locator("select[name=roleType]")
    try:
        role_select.wait_for(state="visible", timeout=15000)
    except Exception:
        return []
    page.wait_for_timeout(1500)
    return [o for o in role_select.locator("option").all_inner_texts() if o != "Select Roll"]


def run_pilot(state, roll_type_contains, ac_limit, out_dir, headless=False):
    os.makedirs(out_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(accept_downloads=True)

        page.goto(BASE_URL, wait_until="networkidle", timeout=45000)
        state_select = page.locator("select[name=stateCode]")
        state_select.wait_for(state="visible", timeout=20000)
        _select_by_label_contains(state_select, state, page, "state")

        role_select = page.locator("select[name=roleType]")
        try:
            role_select.wait_for(state="visible", timeout=15000)
        except Exception:
            browser.close()
            raise SystemExit(
                f"No Roll Type dropdown appeared for state={state!r} — this "
                "state may have no current-roll data published on this "
                "portal yet. Try --check-only to see what IS published."
            )
        page.wait_for_timeout(1500)
        matched_roll = _select_by_label_contains(role_select, roll_type_contains, page, "roll type")
        print(f"Using roll type: {matched_roll}")

        constituency_select = page.locator("select[name=constituency]")
        constituency_select.wait_for(state="visible", timeout=20000)
        page.wait_for_timeout(1500)
        ac_options = [
            o for o in constituency_select.locator("option").all_inner_texts() if o != "Select AC"
        ]
        if ac_limit:
            ac_options = ac_options[:ac_limit]
        print(f"Piloting {len(ac_options)} constituencies: {ac_options}")

        results = []
        start = time.time()

        for i, ac_label in enumerate(ac_options, 1):
            print(f"\n[{i}/{len(ac_options)}] {ac_label}")
            constituency_select.select_option(label=ac_label)
            page.wait_for_timeout(2000)

            lang_select = page.locator("select[name=langCd]")
            lang_select.wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(1000)
            lang_options = [o for o in lang_select.locator("option").all_inner_texts() if o != "Select Language"]
            if lang_options:
                lang_select.select_option(label=lang_options[0])
                page.wait_for_timeout(1500)

            select_all = page.locator("input[type=checkbox]").first
            try:
                select_all.wait_for(state="visible", timeout=15000)
                select_all.check()
            except Exception:
                print("  Could not find the part checklist / Select All checkbox — skipping this AC.")
                results.append({"ac": ac_label, "status": "no_part_list"})
                continue

            print(
                "  >>> In the browser window: solve the CAPTCHA, then click "
                "'Download Selected PDFs'. Press Enter here once the "
                "download finishes (or type 'skip' + Enter to skip this AC)."
            )
            downloaded_files = []

            def on_download(download, _files=downloaded_files):
                ac_num = re.match(r"^\s*(\d+)", ac_label)
                ac_num = ac_num.group(1) if ac_num else ac_label.replace(" ", "_")
                dest = os.path.join(out_dir, f"AC{ac_num}_{download.suggested_filename}")
                download.save_as(dest)
                _files.append(dest)
                print(f"  saved: {dest}")

            page.on("download", on_download)
            answer = input("  > ").strip().lower()
            page.wait_for_timeout(3000)  # let any in-flight download event land
            page.remove_listener("download", on_download)

            if answer == "skip":
                results.append({"ac": ac_label, "status": "skipped"})
            elif downloaded_files:
                results.append({"ac": ac_label, "status": "ok", "files": downloaded_files})
            else:
                results.append({"ac": ac_label, "status": "no_file_captured"})

        elapsed = time.time() - start
        browser.close()

        print("\n--- Pilot summary ---")
        for r in results:
            print(f"  {r['ac']}: {r['status']}")
        ok = sum(1 for r in results if r["status"] == "ok")
        print(f"{ok}/{len(results)} constituencies downloaded successfully in {elapsed:.0f}s")
        if ok:
            print(f"~{elapsed / ok:.0f}s per AC including manual CAPTCHA time.")
            print(f"Extrapolated to 224 ACs: ~{224 * elapsed / ok / 60:.0f} minutes of hands-on time.")
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", required=True, help='e.g. "Karnataka", "Uttar Pradesh"')
    parser.add_argument("--roll-type", default="SIR FinalRoll", help='substring match, e.g. "SIR FinalRoll"')
    parser.add_argument("--limit", type=int, default=3, help="number of ACs to pilot (default 3)")
    parser.add_argument("--out-dir", default="data/raw/2025_pilot")
    parser.add_argument("--check-only", action="store_true", help="list available roll types for --state and exit")
    parser.add_argument("--headless", action="store_true", help="not useful for real runs — you need to see the CAPTCHA")
    args = parser.parse_args()

    if args.check_only:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            roll_types = list_roll_types(page, args.state)
            browser.close()
        if roll_types:
            print(f"Published for {args.state}:")
            for rt in roll_types:
                print(f"  - {rt}")
        else:
            print(f"Nothing published for {args.state} on this portal yet.")
        return

    run_pilot(args.state, args.roll_type, args.limit, args.out_dir, headless=args.headless)


if __name__ == "__main__":
    main()
