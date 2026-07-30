import asyncio
import atexit
import os
import random
import signal
import sys

try:
    import psutil
except ImportError:
    psutil = None

DEFAULT_LOCK_PATH = "/tmp/pythontravel_scraper.lock"


def _pid_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def acquire_lock(lock_path):
    """Refuse to start while another instance is already running. Any
    scraper's Chromium left running unattended is the actual risk (see the
    Jul 29 incident, where an orphaned headless Chromium survived a dropped
    SSH session for hours and OOM-killed the box) — not specifically two
    copies of the same script — so every scraper shares one lock path."""
    if os.path.exists(lock_path):
        with open(lock_path) as f:
            content = f.read().strip()
        old_pid = int(content) if content.isdigit() else None
        if old_pid and _pid_is_alive(old_pid):
            raise RuntimeError(
                f"Another instance appears to already be running (pid {old_pid}). "
                f"Refusing to start a second one. If you're sure it's not "
                f"running (check `pgrep -af chrome-headless`), remove {lock_path}."
            )
        print(f"Found a stale lock (pid {old_pid} is not running) — removing it.")
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))


def release_lock(lock_path):
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def _kill_child_processes():
    """Kill every process this script has ever spawned — Playwright's Node
    driver, the browser, and all its renderer/GPU/sandbox helper processes —
    not just the one handle we hold. This is what actually guards against a
    dropped SSH session (SIGHUP) leaving an orphaned Chromium running
    unattended for hours: browser.close() can't help if the parent process
    never gets a chance to reach it, and Chromium's own subprocesses don't
    reliably die with their parent on their own."""
    if psutil is None:
        return
    try:
        me = psutil.Process(os.getpid())
    except psutil.NoSuchProcess:
        return
    for child in me.children(recursive=True):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass


def install_process_guards(lock_path=DEFAULT_LOCK_PATH):
    """Acquire the shared lock and wire cleanup so this scraper's browser can
    never outlive it: on normal exit (atexit) or on SIGTERM/SIGHUP (a dropped
    SSH session sends SIGHUP to the foreground process), every child process
    gets killed and the lock released. Call once at the top of __main__,
    before asyncio.run — no try/finally needed in the caller."""
    acquire_lock(lock_path)
    atexit.register(_kill_child_processes)
    atexit.register(release_lock, lock_path)

    def _handle_termination_signal(signum, frame):
        print(f"Received signal {signum} — killing all child processes before exit.", flush=True)
        _kill_child_processes()
        release_lock(lock_path)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_termination_signal)
    signal.signal(signal.SIGHUP, _handle_termination_signal)

    if psutil is None:
        print(
            "WARNING: psutil is not installed — orphaned browser processes from a "
            "dropped connection or crash will NOT be automatically cleaned up. "
            "Run: pip install psutil",
            flush=True,
        )


async def human_pause(min_ms=150, max_ms=450):
    """Small randomized delay before an automated action. Firing clicks at a
    perfectly uniform machine cadence is exactly the signature bot-mitigation
    (Akamai/PerimeterX/etc., common on airline sites) looks for — real users
    don't click at fixed intervals. Failures that appear ~20+ actions into a
    run and don't reproduce in isolation look like exactly this kind of
    throttling rather than a fixed rendering bug."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


def start_atomic_write(output_path):
    """Truncate a fresh temp file for this run; pass the result to
    finish_atomic_write on success. A crash mid-scrape then leaves the
    previous good output file untouched instead of clobbering it with an
    empty/partial truncation."""
    tmp_path = output_path + ".part"
    open(tmp_path, "w").close()
    return tmp_path


def finish_atomic_write(tmp_path, output_path):
    os.replace(tmp_path, output_path)
