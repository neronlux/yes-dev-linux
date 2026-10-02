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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

# Chrome's dark-theme button fill measured on Chrome 153 / GNOME 47:
# (0, 75, 118). Also accept the lighter brand blues within tolerance.
_BLUE = lambda r, g, b: (r < 70 and 55 <= g <= 150 and 95 <= b <= 210
                         and (b - r) >= 55 and (b - g) >= 20)

SCREENSHOT_TIMEOUT_S = 20
MIN_CLUSTER_PX = 120          # sampled every 2px; a ~64x40 button is ~600
BUTTON_H_RANGE = (16, 70)     # px
BUTTON_W_RANGE = (40, 180)    # px


def _pictures_dir() -> Path:
    try:
        cfg = Path.home() / ".config" / "user-dirs.dirs"
        for line in cfg.read_text().splitlines():
            line = line.strip()
            if line.startswith("XDG_PICTURES_DIR="):
                val = line.split("=", 1)[1].strip().strip('"')
                return Path(val.replace("$HOME", str(Path.home())))
    except Exception:
        pass
    return Path.home() / "Pictures"


def _is_portal_drop(path: str) -> bool:
    """True for files the portal backend creates per capture (safe to delete).

    On GNOME every Screenshot API call persists a sequential
    Screenshot-N.png under ~/Pictures; other backends use the temp dir.
    Never true for anything else, so user files are never touched."""
    try:
        p = Path(path)
        if p.parent == Path(tempfile.gettempdir()):
            return True
        return (p.parent == _pictures_dir() and p.suffix == ".png"
                and p.name.startswith("Screenshot"))
    except Exception:
        return False


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


STALE_TRANSIENT_AGE_S = 24 * 3600  # crash leftovers older than this are ours to take
_swept_once = False


def sweep_stale_transients(max_age_s: int = STALE_TRANSIENT_AGE_S) -> int:
    """Delete our temp captures abandoned by crashed runs (SIGTERM skips
    cleanup handlers, so each restart could otherwise orphan one). Returns
    the count removed. Only touches our own ysd-shot-*.png files in the
    temp dir that are older than max_age_s."""
    removed = 0
    try:
        now = time.time()
        with os.scandir(tempfile.gettempdir()) as it:
            for entry in it:
                try:
                    if (entry.name.startswith("ysd-shot-")
                            and entry.name.endswith(".png")
                            and entry.is_file(follow_symlinks=False)
                            and now - entry.stat(follow_symlinks=False).st_mtime > max_age_s):
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def _maybe_sweep() -> None:
    global _swept_once
    if _swept_once:
        return
    _swept_once = True
    try:
        sweep_stale_transients()
    except Exception:
        pass


def portal_drop_backlog() -> tuple[int, int]:
    """(count, bytes) of portal Screenshot drops in the Pictures dir.

    A healthy engine leaves none (drops are relocated + deleted per
    capture); thousands here means the cleanup regressed. One scandir of
    a huge dir costs ~seconds, so callers must rate-limit (hourly)."""
    n = total = 0
    try:
        with os.scandir(_pictures_dir()) as it:
            for entry in it:
                try:
                    if (entry.name.startswith("Screenshot")
                            and entry.name.endswith(".png")
                            and entry.is_file(follow_symlinks=False)):
                        n += 1
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return n, total


@contextmanager
def screenshot(dest: str | None = None):
    """Capture and yield a PNG path, deleting our temp copy afterwards.

    Usage: callers must consume the shot inside the block (every current
    caller does: size/detect run before the next capture). Files created
    via `dest` belong to the caller and are left alone."""
    path = _portal_screenshot(dest)
    try:
        yield path
    finally:
        if dest is None:
            _unlink_quiet(path)


def _portal_screenshot(dest: str | None = None) -> str | None:
    """Take a screenshot via xdg-desktop-portal. Returns the PNG path.

    Runs its own GLib loop for the request/response, safe to call from a
    long-lived non-GLib process.

    Two hard-won cleanups live here, because a 24/7 engine trips both:
    - the D-Bus signal match is removed in a finally (each leaked match
      counts toward the 50k/connection cap, after which every portal
      call fails);
    - the portal backend persists every capture to disk (GNOME: a
      sequential Screenshot-N.png under ~/Pictures - 150k files / 53GB
      observed live). The drop is relocated to a temp file and deleted,
      so steady-state disk use is ~1 screenshot, not one per capture."""
    try:
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib
    except Exception:
        return None
    _maybe_sweep()  # one cheap pass per process: clear crashed runs' leftovers
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    token = f"ysd{os.getpid()}{int(time.time() * 1000) % 100000}"
    sender = bus.get_unique_name().replace(":", "").replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    got: dict = {}
    loop = GLib.MainLoop()

    def on_response(code, results):
        got["code"] = int(code)
        got["uri"] = str(results.get("uri", ""))
        loop.quit()

    bus.add_signal_receiver(on_response, signal_name="Response",
                            dbus_interface="org.freedesktop.portal.Request",
                            path=req_path)
    try:
        obj = bus.get_object("org.freedesktop.portal.Desktop",
                             "/org/freedesktop/portal/desktop")
        iface = dbus.Interface(obj, "org.freedesktop.portal.Screenshot")
        try:
            iface.Screenshot("", {"handle_token": token, "interactive": False})
        except Exception:
            return None
        GLib.timeout_add_seconds(SCREENSHOT_TIMEOUT_S, lambda: loop.quit())
        loop.run()
    finally:
        try:
            bus.remove_signal_receiver(
                on_response, signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request",
                path=req_path)
        except Exception:
            pass
    if got.get("code") != 0 or not got.get("uri"):
        return None
    src = urllib.parse.urlparse(got["uri"]).path
    if not src or not os.path.isfile(src):
        return None
    if dest:
        if os.path.abspath(src) == os.path.abspath(dest):
            return dest
        try:
            shutil.copyfile(src, dest)
        except OSError:
            return None
        if _is_portal_drop(src):
            _unlink_quiet(src)
        return dest
    try:
        fd, tmp = tempfile.mkstemp(prefix="ysd-shot-", suffix=".png")
        os.close(fd)
        shutil.copyfile(src, tmp)
    except OSError:
        return None
    if _is_portal_drop(src):
        _unlink_quiet(src)
    return tmp


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


def monitor_count() -> int:
    """Number of logical monitors from Mutter's DisplayConfig (0 unknown).
    Multi-monitor coordinate mapping is not yet validated, so the engine
    only warns when it sees more than one."""
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, timeout=10).stdout
        return len(re.findall(r"\(-?\d+,\s*-?\d+,\s*[\d.]+,\s*uint32", out))
    except Exception:
        return 0


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
    with screenshot() as shot:
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

    def __init__(self, size: tuple[int, int] | None = None) -> None:
        self.dev = None
        self.size = size  # (w, h) in logical desktop pixels; None = ask Mutter

    def close(self) -> None:
        try:
            if self.dev is not None:
                self.dev.close()
        except Exception:
            pass
        self.dev = None

    def available(self, size: tuple[int, int] | None = None) -> bool:
        if self.dev is not None and (size is None or size == self.size):
            return True
        if size is not None and size == self.size and self.dev is not None:
            return True
        try:
            import evdev
        except Exception:
            return False
        if not os.access("/dev/uinput", os.W_OK):
            return False
        want = size or self.size or _screen_size()
        if want is None or want[0] < 320 or want[1] < 200:
            return False
        # A different size means the session changed (RDP resize, or we were
        # created before the compositor existed): rebuild at the new size.
        self.close()
        try:
            from evdev import AbsInfo, UInput, ecodes as e
            cap = {
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(0, 0, want[0] - 1, 0, 0, 1)),
                    (e.ABS_Y, AbsInfo(0, 0, want[1] - 1, 0, 0, 1)),
                ],
                e.EV_KEY: [e.BTN_LEFT, e.KEY_LEFTMETA, e.KEY_LEFTALT,
                           e.KEY_TAB, e.KEY_UP, e.KEY_ESC,
                           e.KEY_PAGEUP, e.KEY_PAGEDOWN],
            }
            dev = UInput(cap, name="yesdev absolute pointer", version=1)
        except Exception:
            return False
        time.sleep(0.4)  # let the compositor see the device
        self.dev = dev
        self.size = want
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

    def key_combo(self, *codes, hold: float = 0.05, gap: float = 0.08) -> bool:
        """Press keys in order, release in reverse (e.g. Super+Up)."""
        if self.dev is None and not self.available():
            return False
        from evdev import ecodes as e
        try:
            for c in codes:
                self.dev.write(e.EV_KEY, c, 1)
                self.dev.syn()
                time.sleep(hold)
            for c in reversed(codes):
                self.dev.write(e.EV_KEY, c, 0)
                self.dev.syn()
                time.sleep(gap)
            return True
        except Exception:
            return False


def combo(clicker, which: str) -> bool:
    """Named window-management chords, used only to bring Chrome to the
    front and maximize it (verified via AT-SPI before the maximize)."""
    from evdev import ecodes as e
    combos = {
        "maximize": (e.KEY_LEFTMETA, e.KEY_UP),      # GNOME maximize (toggle on Ubuntu-style setups)
        "nextwindow": (e.KEY_LEFTMETA, e.KEY_GRAVE),  # cycle same-app windows
        "alttab": (e.KEY_LEFTALT, e.KEY_TAB),         # switch to previous window
        "escape": (e.KEY_ESC,),                       # dismiss a popup (selftest)
        "wsdown": (e.KEY_LEFTMETA, e.KEY_PAGEDOWN),   # next workspace
        "wsup": (e.KEY_LEFTMETA, e.KEY_PAGEUP),       # previous workspace
    }
    codes = combos.get(which)
    if not codes or clicker is None:
        return False
    return clicker.key_combo(*codes)


def alttab_held(clicker, n: int) -> bool:
    """Hold Alt and tap Tab n times, then release: walks n windows down the
    MRU list in one switcher pass. Repeated single Alt+Tabs ping-pong
    between two windows and never reach a third, so probing steps n up."""
    if clicker is None:
        return False
    if clicker.dev is None and not clicker.available():
        return False
    from evdev import ecodes as e
    try:
        clicker.dev.write(e.EV_KEY, e.KEY_LEFTALT, 1)
        clicker.dev.syn()
        time.sleep(0.12)
        for _ in range(max(1, int(n))):
            clicker.dev.write(e.EV_KEY, e.KEY_TAB, 1)
            clicker.dev.syn()
            time.sleep(0.1)
            clicker.dev.write(e.EV_KEY, e.KEY_TAB, 0)
            clicker.dev.syn()
            time.sleep(0.1)
        clicker.dev.write(e.EV_KEY, e.KEY_LEFTALT, 0)
        clicker.dev.syn()
        return True
    except Exception:
        return False


def image_size(png_path: str) -> tuple[int, int] | None:
    """Logical pixel size of a screenshot (also the desktop's logical size)."""
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            return im.size
    except Exception:
        return None


def allow_click(png_path: str | None = None) -> tuple[bool, str]:
    """Screenshot -> find Allow -> click. Returns (clicked, detail)."""
    if png_path:
        return _allow_click_on(png_path)
    with screenshot() as shot:
        if not shot:
            return False, "screenshot failed (portal unavailable?)"
        return _allow_click_on(shot)


def _allow_click_on(shot: str) -> tuple[bool, str]:
    pt = find_allow_button(shot)
    if pt is None:
        return False, "Allow button not found in screenshot"
    clicker = AbsoluteClicker()
    if not clicker.available():
        return False, "absolute pointer unavailable (evdev/uinput/screen size)"
    if not clicker.click(*pt):
        return False, "click emit failed"
    return True, f"clicked ({pt[0]},{pt[1]})"


def list_button_clusters(png_path: str):
    """All blue button-shaped clusters found: (cx, cy, w, h, px). Calibration aid."""
    from PIL import Image
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    px = im.load()
    step = 2
    pts = set()
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            if _BLUE(r, g, b):
                pts.add((x, y))
    clusters, seen = [], set()
    for p in pts:
        if p in seen:
            continue
        stack, comp = [p], []
        seen.add(p)
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
            clusters.append(((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2,
                             max(xs) - min(xs), max(ys) - min(ys), len(comp)))
    clusters.sort(key=lambda c: c[0])
    return clusters


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--clusters":
        for c in list_button_clusters(sys.argv[2]):
            print(f"cluster cx={c[0]} cy={c[1]} w={c[2]} h={c[3]} px={c[4]}")
    elif len(sys.argv) >= 3 and sys.argv[1] == "--detect":
        print(find_allow_button(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "--click":
        c = AbsoluteClicker()
        ok = c.available() and c.click(int(sys.argv[2]), int(sys.argv[3]))
        print("clicked" if ok else "FAILED")
    elif len(sys.argv) >= 2 and sys.argv[1] == "--shot":
        print(_portal_screenshot(sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        print("usage: auto_click.py --detect PNG | --clusters PNG | "
              "--click X Y | --shot [DEST]")
