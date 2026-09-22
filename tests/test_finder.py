#!/usr/bin/python3
"""Synthetic-image tests for the Allow-button finder (no Chrome needed).

Renders the dialog's button geometry in a few variations and locks the
contract: dark-theme pair accepted (rightmost = Allow), light-theme
dialog with the same blue buttons accepted, everything ambiguous refused
(single button, oversized blue rectangle, distant pair, pair on
different rows). Run with the distro python:

    /usr/bin/python3 tests/test_finder.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

import auto_click

OUT = Path("/tmp/finder-tests")
OUT.mkdir(exist_ok=True)
failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


BG = (54, 54, 58)       # measured Chrome dark chrome
DIALOG = (40, 44, 52)   # measured dialog body
BLUE = (0, 75, 118)     # measured Chrome 153 primary fill
LIGHT = (235, 235, 235)


def make(name, dialog_bg, buttons, size=(1280, 800)):
    im = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((640 - 260, 300, 640 + 260, 420), 12, fill=dialog_bg)
    for (x, y, w, h, col) in buttons:
        d.rounded_rectangle((x, y, x + w, y + h), 6, fill=col)
    p = OUT / name
    im.save(p)
    return str(p)


# 1. dark-theme pair — the live-proven geometry; Allow is rightmost.
p = make("pair_dark.png", DIALOG, [(700, 360, 90, 32, BLUE), (810, 360, 90, 32, BLUE)])
r = auto_click.find_allow_button(p)
check("dark pair: rightmost returned",
      r is not None and abs(r[0] - 854) <= 3 and abs(r[1] - 375) <= 3, str(r))

# 2. light dialog, same blue buttons — hue-based detection should not care.
p = make("pair_light.png", LIGHT, [(700, 360, 90, 32, BLUE), (810, 360, 90, 32, BLUE)])
r = auto_click.find_allow_button(p)
check("light dialog with blue buttons: found", r is not None, str(r))

# 3. single blue button (e.g. Cancel greyed in a restyle): refused today.
p = make("single_blue.png", DIALOG, [(810, 360, 90, 32, BLUE)])
r = auto_click.find_allow_button(p)
check("single blue button: refused (pair required)", r is None, str(r))

# 4. oversized blue rectangle is page content, not a button.
p = make("big_blue.png", DIALOG, [(600, 340, 300, 60, BLUE)])
r = auto_click.find_allow_button(p)
check("oversized blue rect: refused", r is None, str(r))

# 5. pair too far apart to be dialog buttons.
p = make("far_pair.png", DIALOG, [(300, 360, 90, 32, BLUE), (900, 360, 90, 32, BLUE)])
r = auto_click.find_allow_button(p)
check("pair too far apart: refused", r is None, str(r))

# 6. pair not on one row.
p = make("rows_pair.png", DIALOG, [(700, 320, 90, 32, BLUE), (810, 380, 90, 32, BLUE)])
r = auto_click.find_allow_button(p)
check("pair not on one row: refused", r is None, str(r))

# 7. the calibration helper lists both clusters.
p = make("clusters.png", DIALOG, [(700, 360, 90, 32, BLUE), (810, 360, 90, 32, BLUE)])
cs = auto_click.list_button_clusters(p)
check("clusters helper: both buttons listed", len(cs) == 2, str(cs))

print(f"\n{'FAILURES: ' + ', '.join(failures) if failures else 'ALL PASS'}")
sys.exit(1 if failures else 0)
