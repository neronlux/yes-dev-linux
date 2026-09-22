#!/usr/bin/python3
"""Auto-click support for Yes, Dev Linux: screenshot, find Chrome's Allow
button, click it with a dedicated ABSOLUTE pointer device.

Why this shape (field work 2026-09-22):
- Chrome's consent bubble exposes no AT-SPI objects on Wayland, so the
  button must be found visually. The xdg-desktop-portal Screenshot API
  works from a plain session process with no prompt, so the engine can
  take its own screenshot.
- ydotool's virtual device is RELATIVE, so absolute moves are subject to
  pointer acceleration and land anywhere. An absolute uinput device maps
  1:1 onto the logical desktop (proven: the computer-use-linux absolute
  pointer uses range 0..w-1 / 0..h-1) and hits the button dead-centre.

Deps: python3-gi, dbus-python (portal screenshot), python3-pil (button
detection), python3-evdev (uinput device). All distro packages.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.parse

# Chrome's dark-theme button fill measured on Chrome 153 / GNOME 47:
# (0, 75, 118). Also accept the lighter brand blues within tolerance.
_BLUE = lambda r, g, b: (r < 70 and 55 <= g <= 150 and 95 <= b <= 210
                         and (b - r) >= 55 and (b - g) >= 20)

SCREENSHOT_TIMEOUT_S = 20
MIN_CLUSTER_PX = 120          # sampled every 2px; a ~64x40 button is ~600
BUTTON_H_RANGE = (16, 70)     # px
BUTTON_W_RANGE = (40, 180)    # px


def _portal_screenshot(dest: str | None = None) -> str | None:
    """Take a screenshot via xdg-desktop-portal. Returns the PNG path.

    Runs its own GLib loop for the request/response, safe to call from a
    long-lived non-GLib process."""
    try:
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib
    except Exception:
        return None
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    token = f"ysd{os.getpid()}{int(time.time() * 1000) % 100000}"
    sender = bus.get_unique_name().replace(":", "").replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    got: dict = {}

    def on_response(code, results):
        got["code"] = int(code)
        got["uri"] = str(results.get("uri", ""))
        loop.quit()

    bus.add_signal_receiver(on_response, signal_name="Response",
                            dbus_interface="org.freedesktop.portal.Request",
                            path=req_path)
    obj = bus.get_object("org.freedesktop.portal.Desktop",
                         "/org/freedesktop/portal/desktop")
    iface = dbus.Interface(obj, "org.freedesktop.portal.Screenshot")
    try:
        iface.Screenshot("", {"handle_token": token, "interactive": False})
    except Exception:
        return None
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(SCREENSHOT_TIMEOUT_S, lambda: loop.quit())
    loop.run()
    if got.get("code") != 0 or not got.get("uri"):
        return None
    src = urllib.parse.urlparse(got["uri"]).path
    if dest:
        try:
            shutil.copyfile(src, dest)
            return dest
        except OSError:
            return None
    return src


def find_allow_button(png_path: str) -> tuple[int, int] | None:
    """(x, y) of the Allow button centre, in desktop pixels, or None.

    The consent dialog carries two identically-filled buttons (Cancel,
    Allow) with Allow rightmost. Require at least two blue clusters so a
    stray blue page element can never be mistaken for Allow."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        im = Image.open(png_path).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    px = im.load()
    step = 2
    pts = set()
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            if _BLUE(r, g, b):
                pts.add((x, y))
    if not pts:
        return None
    clusters = []
    seen = set()
    for p in pts:
        if p in seen:
            continue
        stack = [p]
        seen.add(p)
        comp = []
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for dx in (-step, 0, step):
                for dy in (-step, 0, step):
                    n = (cx + dx, cy + dy)
                    if n in pts and n not in seen:
                        seen.add(n)
                        stack.append(n)
        if len(comp) >= MIN_CLUSTER_PX:
            xs = [q[0] for q in comp]
            ys = [q[1] for q in comp]
            bw = max(xs) - min(xs)
            bh = max(ys) - min(ys)
            if (BUTTON_W_RANGE[0] <= bw <= BUTTON_W_RANGE[1]
                    and BUTTON_H_RANGE[0] <= bh <= BUTTON_H_RANGE[1]):
                clusters.append(((min(xs) + max(xs)) // 2,
                                 (min(ys) + max(ys)) // 2, bw, bh, len(comp)))
    if len(clusters) < 2:
        return None
    clusters.sort(key=lambda c: c[0])
    cancel, allow = clusters[-2], clusters[-1]
    # Sanity: the pair must sit side by side on the same row.
    if abs(cancel[1] - allow[1]) > 24 or (allow[0] - cancel[0]) > 260:
        return None
    return allow[0], allow[1]


def _screen_size() -> tuple[int, int] | None:
    """Logical monitor size via Mutter's DisplayConfig (no prompt)."""
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, timeout=10).stdout
        # third return value is the list of logical monitors; each has (x, y, scale, transform, primary, ... monitors)
        # avoiding full parsing: take the largest resolution-like pair
        import re
        mons = re.findall(r"\((\d+),\s*(\d+),\s*([\d.]+),", out)
        best = None
        for mx, my, scale in mons:
            try:
                s = float(scale)
                if s <= 0:
                    continue
                w = int(int(mx) / s)
                h = int(int(my) / s)
                if w >= 640 and h >= 400 and (best is None or w * h > best[0] * best[1]):
                    best = (w, h)
            except ValueError:
                continue
        if best:
            return best
    except Exception:
        pass
    # Fallback: the portal screenshot size is the logical desktop size.
    shot = _portal_screenshot()
    if shot:
        try:
            from PIL import Image
            with Image.open(shot) as im:
                return im.size
        except Exception:
            pass
    return None


class AbsoluteClicker:
    """A dedicated absolute uinput pointer. One instance per process."""

    def __init__(self) -> None:
        self.dev = None
        self.size = None

    def available(self) -> bool:
        if self.dev is not None:
            return True
        try:
            import evdev
        except Exception:
            return False
        if not os.access("/dev/uinput", os.W_OK):
            return False
        size = _screen_size()
        if size is None or size[0] < 320 or size[1] < 200:
            return False
        try:
            from evdev import AbsInfo, UInput, ecodes as e
            cap = {
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(0, 0, size[0] - 1, 0, 0, 1)),
                    (e.ABS_Y, AbsInfo(0, 0, size[1] - 1, 0, 0, 1)),
                ],
                e.EV_KEY: [e.BTN_LEFT],
            }
            dev = UInput(cap, name="yesdev absolute pointer", version=1)
        except Exception:
            return False
        time.sleep(0.4)  # let the compositor see the device
        self.dev = dev
        self.size = size
        return True

    def click(self, x: int, y: int) -> bool:
        if self.dev is None and not self.available():
            return False
        from evdev import ecodes as e
        w, h = self.size
        ax = max(0, min(w - 1, int(x)))
        ay = max(0, min(h - 1, int(y)))
        try:
            self.dev.write(e.EV_ABS, e.ABS_X, ax)
            self.dev.write(e.EV_ABS, e.ABS_Y, ay)
            self.dev.syn()
            time.sleep(0.05)
            self.dev.write(e.EV_KEY, e.BTN_LEFT, 1)
            self.dev.syn()
            time.sleep(0.06)
            self.dev.write(e.EV_KEY, e.BTN_LEFT, 0)
            self.dev.syn()
            return True
        except Exception:
            return False


def allow_click(png_path: str | None = None) -> tuple[bool, str]:
    """Screenshot -> find Allow -> click. Returns (clicked, detail)."""
    shot = png_path or _portal_screenshot()
    if not shot:
        return False, "screenshot failed (portal unavailable?)"
    pt = find_allow_button(shot)
    if pt is None:
        return False, "Allow button not found in screenshot"
    clicker = AbsoluteClicker()
    if not clicker.available():
        return False, "absolute pointer unavailable (evdev/uinput/screen size)"
    if not clicker.click(*pt):
        return False, "click emit failed"
    return True, f"clicked ({pt[0]},{pt[1]})"


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--detect":
        print(find_allow_button(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "--click":
        c = AbsoluteClicker()
        ok = c.available() and c.click(int(sys.argv[2]), int(sys.argv[3]))
        print("clicked" if ok else "FAILED")
    elif len(sys.argv) >= 2 and sys.argv[1] == "--shot":
        print(_portal_screenshot(sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        print("usage: auto_click.py --detect PNG | --click X Y | --shot [DEST]")
