#!/usr/bin/python3
"""Offline test for the v0.8 self-healing logic — no desktop needed.

Stubs the auto_click module and the pointer device, then checks that
the Engine:

1. retries pointer creation on failure instead of latching it
   (the boot-order bug: the service starts before the desktop exists);
2. rebuilds the pointer when the screenshot size changes (RDP resize);
3. re-initialises AT-SPI after ATSPI_REINIT_AFTER consecutive failed
   scans instead of holding a dead connection forever.

Run with the distro python (it imports watcher_linux, whose gi/Atspi
imports are guarded, so a headless box without python3-gi also works):

    /usr/bin/python3 tests/test_selfheal.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watcher_linux as w
import types

calls = {"n": 0}
MADE = []


class FakeClicker:
    """Stands in for auto_click.AbsoluteClicker; fails the first two
    available() probes, then reports the size it was constructed with."""

    def __init__(self, size=None):
        self.size = size
        MADE.append(self)

    def available(self, size=None):
        calls["n"] += 1
        return calls["n"] > 2

    def click(self, x, y):
        return True


stub = types.SimpleNamespace(AbsoluteClicker=FakeClicker, image_size=None,
                             _portal_screenshot=lambda: None,
                             find_allow_button=lambda p: None)
w.auto_click = stub

saved_retry = w.CLICKER_RETRY_S
saved_reinit = w.ATSPI_REINIT_AFTER
w.CLICKER_RETRY_S = 0.05
failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


e = w.Engine(observe=False, enable_click=True, log_path="/dev/null")

# 1. fail, fail, then succeed — never latched.
check("attempt 1 returns None (not ready yet)",
      e._get_clicker((1280, 800)) is None)
check("engine will retry (no permanent latch)",
      not getattr(e, "_clicker_failed", False))
time.sleep(w.CLICKER_RETRY_S + 0.05)
check("attempt 2 returns None again (retry window)",
      e._get_clicker((1280, 800)) is None)
time.sleep(w.CLICKER_RETRY_S + 0.05)
c3 = e._get_clicker((1280, 800))
check("attempt 3 succeeds", c3 is not None and c3.size == (1280, 800))
check("device is cached afterwards", e._get_clicker((1280, 800)) is c3)

# 2. size change forces a rebuild.
c4 = e._get_clicker((1920, 1080))
check("resize rebuilds the pointer",
      c4 is not None and c4.size == (1920, 1080) and c4 is not c3)

# 3. AT-SPI re-init after the failure threshold.
reinit_calls = []


def fake_init():
    reinit_calls.append(1)
    return True


saved_init = getattr(w, "Atspi", None)
w.Atspi = types.SimpleNamespace(init=fake_init)
for _ in range(w.ATSPI_REINIT_AFTER):
    e._note_scan(False)
check(f"re-init fires at {saved_reinit} consecutive failures",
      len(reinit_calls) == 1)
e._note_scan(True)
e._note_scan(False)
check("counter resets on a successful scan", len(reinit_calls) == 1)
w.Atspi = saved_init

# 4. dropping the pointer device forces a fresh one on the next use
#    (a long-lived device can stop delivering clicks; observed live).
c5 = e._get_clicker((1280, 800))
e._drop_clicker("test")
check("drop releases the cached device", e._clicker is None)
c6 = e._get_clicker((1280, 800))
check("next get builds a fresh device",
      c6 is not None and c6 is not c5 and c6.size == (1280, 800))

w.CLICKER_RETRY_S = saved_retry
print(f"\n{'FAILURES: ' + ', '.join(failures) if failures else 'ALL PASS'}")
sys.exit(1 if failures else 0)
