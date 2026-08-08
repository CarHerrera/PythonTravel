import subprocess
import sys
import time

# Run manually whenever you want fresh data — never on a schedule/cron.
# Sequential, not parallel: every scraper shares one lock file
# (scraping_common.install_process_guards) and runs its own browser, so two
# at once would just fail the second with "another instance is running."
#
# southwest_script.py runs first because it writes app/data/hotels_*.jsonl,
# which every chain script reads to know which hotels to even look for.
#
# Add a new chain here as it's built — (script, needs_xvfb).
SCRIPTS = [
    ("southwest_script.py", False),
    ("riu_script.py", False),
    ("palladium_script.py", False),
    ("bahia_principe_script.py", False),
    ("iberostar_script.py", True),  # Akamai-blocked headless; headed via Xvfb gets through (confirmed live)
]


def run_one(script, needs_xvfb):
    cmd = ["xvfb-run", "-a", "python3", script] if needs_xvfb else ["python3", script]
    print(f"\n{'=' * 60}\nRunning {script} ({'headed via Xvfb' if needs_xvfb else 'headless'})\n{'=' * 60}")
    start = time.monotonic()
    result = subprocess.run(cmd)
    elapsed = time.monotonic() - start
    ok = result.returncode == 0
    print(f"--- {script} {'OK' if ok else 'FAILED (exit ' + str(result.returncode) + ')'} in {elapsed:.0f}s ---")
    return ok


def main():
    results = []
    for script, needs_xvfb in SCRIPTS:
        # One script crashing shouldn't stop the rest — same reasoning each
        # scraper already applies internally (one bad hotel/destination
        # doesn't lose everything else).
        ok = run_one(script, needs_xvfb)
        results.append((script, ok))

    print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
    for script, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'}  {script}")

    if any(not ok for _, ok in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
