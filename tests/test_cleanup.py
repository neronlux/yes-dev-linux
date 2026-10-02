#!/usr/bin/python3
"""Offline tests for the screenshot cleanup paths - no desktop needed.

1. sweep_stale_transients() removes only OUR abandoned temp captures
   (ysd-shot-*.png past the age limit), never fresh ones or other files.
2. Engine._portal_dropcheck() stays silent at healthy backlog levels and
   warns (with the exact cleanup command) when drops accumulate - the
   Sep 2026 incident was 150k files / 53GB before anyone noticed.

Run with the distro python:

    /usr/bin/python3 tests/test_cleanup.py
"""
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

import auto_click
import watcher_linux as w

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


tmp = tempfile.gettempdir()

# 1a. stale decoy removed, fresh decoy and neighbour files kept.
old = os.path.join(tmp, "ysd-shot-test-old.png")
fresh = os.path.join(tmp, "ysd-shot-test-fresh.png")
other = os.path.join(tmp, "ysd-shot-test-other.txt")
for p, blob in ((old, b"x" * 64), (fresh, b"y" * 64), (other, b"z" * 64)):
    with open(p, "wb") as fh:
        fh.write(blob)
ancient = time.time() - (auto_click.STALE_TRANSIENT_AGE_S + 3600)
os.utime(old, (ancient, ancient))
removed = auto_click.sweep_stale_transients()
check("sweep removes the stale capture", removed >= 1 and not os.path.exists(old))
check("sweep keeps the fresh capture", os.path.exists(fresh))
check("sweep ignores non-png files", os.path.exists(other))
for p in (fresh, other):
    try:
        os.unlink(p)
    except OSError:
        pass

# 1b. live portal_drop_backlog() runs and is below warn thresholds now
#     (the loop-era backlog was deleted during the incident cleanup).
n, total = auto_click.portal_drop_backlog()
check("backlog readable", isinstance(n, int) and isinstance(total, int),
      f"{n} files")
check("backlog below warn thresholds",
      n < w.DROPCHECK_WARN_COUNT and total < w.DROPCHECK_WARN_BYTES)

# 2. dropcheck quiet when healthy, loud when accumulating.
e = w.Engine(observe=True, log_path="/dev/null")
saved = w.auto_click
try:
    w.auto_click = types.SimpleNamespace(
        portal_drop_backlog=lambda: (10, 5 * 1024 * 1024))
    e._last_dropcheck = 0.0
    buf = io.StringIO()
    with redirect_stdout(buf):
        e._portal_dropcheck(time.time())
    check("dropcheck silent when healthy", "WARN" not in buf.getvalue())

    w.auto_click = types.SimpleNamespace(
        portal_drop_backlog=lambda: (200000, 60 * 1024 ** 3))
    e._last_dropcheck = 0.0
    buf = io.StringIO()
    with redirect_stdout(buf):
        e._portal_dropcheck(time.time())
    out = buf.getvalue()
    check("dropcheck warns on backlog", "WARN" in out and "200000" in out)
    check("warn carries the cleanup command", "find ~/Pictures" in out)

    # rate limiting: second call within the hour is a no-op.
    buf = io.StringIO()
    with redirect_stdout(buf):
        e._portal_dropcheck(time.time())
    check("dropcheck rate-limited to hourly", buf.getvalue() == "")
finally:
    w.auto_click = saved

print(f"\n{'FAILURES: ' + ', '.join(failures) if failures else 'ALL PASS'}")
sys.exit(1 if failures else 0)
