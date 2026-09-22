#!/usr/bin/python3
"""Yes, Dev engine for Linux: watch for Chrome's "Allow remote debugging?"
consent dialog through AT-SPI.

Status: v0.8.3. Detection via AT-SPI is proven live (titled dialogs by
title, untitled Wayland bubbles by window-geometry child totals).
Auto-approval works via a visual pipeline, proven end-to-end on
2026-09-22: xdg-desktop-portal screenshot (no prompt) -> PIL finds the
rightmost blue button = Allow -> click through a dedicated ABSOLUTE
uinput pointer (1:1 with the logical desktop; ydotool's relative device
is accel-warped and misses). The hung CDP client then completes its
handshake. Verification is visual (a fresh screenshot no longer shows
the button); child totals are a second signal only because queued
attaches leak nodes. If no button is visible the engine raises the host
window with one uncovered-corner click (another app covering Chrome
hides the bubble and swallows every click - observed live) and stands
down after 3 empty looks. Gates: --enable-click (off by default),
--observe always wins, burst guard, 3 attempts per cycle with a
--cool-off-s pause (default 30s) and a fresh pointer before the next
cycle, and [ACTION] is logged only after verification. The AT-SPI
Action path (below) still serves stacks that expose the button (X11).

Boot-safe: the systemd user service starts before the desktop exists
(linger + default.target), so every dependency is re-acquired lazily and
retried - the absolute pointer is rebuilt every 15s until Mutter/portal
answer (and rebuilt on RDP resolution changes), and AT-SPI is
re-initialised after 20 consecutive scan failures. A reboot recovers
without a manual restart.

Why no plain Invoke like Windows/macOS: on GNOME + Wayland (Chrome 153,
verified Sep 2026, real profile, remote-debugging on port 9222 in
DevToolsActivePort mode) every Chrome frame reports child_count 0 over
AT-SPI and the bubble exposes no objects, so the button is found
visually instead. Run:

    /usr/bin/python3 watcher_linux.py --observe              # watch only
    /usr/bin/python3 watcher_linux.py --observe --enable-click  # arm (observe wins)
    /usr/bin/python3 watcher_linux.py --enable-click         # auto-approve
    /usr/bin/python3 watcher_linux.py --once                 # one sweep
    /usr/bin/python3 watcher_linux.py --probe                # dump AT-SPI tree

Auto-click deps (all distro packages):
    sudo apt install python3-gi gir1.2-atspi-2.0 python3-pil python3-evdev
    # dbus-python for the portal screenshot; user must be in the input
    # group (or otherwise have rw on /dev/uinput) to create the pointer.

Contract with the tray (identical on all platforms): append a line
containing `[ACTION]` per approval, in the existing format

    2026-08-27 16:11:28.644 [ACTION]   APPROVED via AtspiAction:press

and the counter / clouds / burst guard work unchanged. `[ACTION]` is only
written after the dialog is verified gone.

Requires the distro python (it ships python3-gi):
    sudo apt install python3-gi gir1.2-atspi-2.0
    /usr/bin/python3 watcher_linux.py --observe
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    from platform_linux import (DATA_DIR, LOG_PATH, acquire_single_instance,
                                atspi_available, ensure_data_dir)
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from platform_linux import (DATA_DIR, LOG_PATH, acquire_single_instance,
                                atspi_available, ensure_data_dir)

try:
    import auto_click
except Exception:
    auto_click = None

try:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
except Exception:
    sys.exit(
        "Yes, Dev Linux engine needs AT-SPI via GObject Introspection:\n"
        "    sudo apt install python3-gi gir1.2-atspi-2.0\n"
        "and run with the distro python: /usr/bin/python3 watcher_linux.py"
    )

DIALOG_PATTERN = re.compile(r"^allow remote debugging\??$", re.I)
APPROVE_PATTERN = re.compile(r"^(allow|approve)$", re.I)

CHROME_NAMES = ("Google Chrome", "Chromium", "Chrome")
EDGE_NAMES = ("Microsoft Edge", "Edge")
TEST_NAMES = ("Google Chrome for Testing",)

POLL_MS_DEFAULT = 250
DEDUPE_SECONDS = 2.0
DEDUPE_MAX = 400
VERIFY_WAIT_S = 0.8
# Minimal burst guard (the tray owns the real one upstream; this keeps an
# unsupervised engine from approving a runaway loop forever).
BURST_LIMIT_DEFAULT = 60   # approvals per window before pausing clicks
BURST_WINDOW_S = 60.0
BURST_PAUSE_S = 60.0
CLICK_MAX_ATTEMPTS = 3     # per cycle, then cool off and start a fresh one
CLICK_RETRY_S = 1.0        # minimum gap between click attempts on one bubble
CLICK_COOL_OFF_S = 30.0    # pause between attempt cycles (0 = wait for clear)
NO_BUTTON_STOP = 3         # consecutive no-button looks before standing down
CLICKER_RETRY_S = 15.0     # re-try clicker creation (boot order, RDP resize)
ATSPI_REINIT_AFTER = 20    # consecutive failed scans before re-initialising

_DIALOG_ROLES = {"dialog", "alert", "window", "frame"}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _role_name(node) -> str:
    return _safe(lambda: node.get_role_name() or "", "") or ""


def _node_name(node) -> str:
    return _safe(lambda: node.get_name() or "", "") or ""


def _children(node) -> list:
    out = []
    try:
        n = node.get_child_count()
    except Exception:
        return out
    for i in range(n):
        try:
            ch = node.get_child_at_index(i)
        except Exception:
            continue
        if ch is not None:
            out.append(ch)
    return out


def _bounds(node):
    """(x, y, w, h) via Component, or None if not exposed."""
    try:
        comp = node.get_component_iface()
        if comp is None:
            return None
        # extents are in window or screen coords; screen is stable for dedupe.
        ext = comp.get_extents(Atspi.CoordType.SCREEN)
        return (int(ext.x), int(ext.y), int(ext.width), int(ext.height))
    except Exception:
        return None


def _is_defunct(node) -> bool | None:
    """True if the node is gone, False if alive, None if no answer."""
    try:
        # A torn-down node raises or reports a nonsensical role.
        node.get_role()
        _ = node.get_name()
        return False
    except Exception:
        return True


def _do_action(node, preferred=("press", "click", "activate")) -> str | None:
    try:
        act = node.get_action_iface()
        if act is None:
            return None
        try:
            available = [(i, act.get_action_name(i)) for i in range(act.get_n_actions())]
        except Exception:
            return None
        ordered = [a for want in preferred for a in available if a[1].lower() == want]
        ordered += [a for a in available if a not in ordered]
        for idx, name in ordered:
            try:
                if act.do_action(idx):
                    return f"AtspiAction:{name}"
            except Exception:
                continue
    except Exception:
        pass
    return None


class Engine:
    def __init__(self, observe=False, poll_ms=POLL_MS_DEFAULT,
                 include_edge=False, log_path=LOG_PATH,
                 exit_with_parent=False, diagnostics=False,
                 dialog_pattern=DIALOG_PATTERN, approve_pattern=APPROVE_PATTERN,
                 burst_limit=BURST_LIMIT_DEFAULT, enable_click=False,
                 cool_off_s=CLICK_COOL_OFF_S):
        self.observe = observe
        self.poll_s = max(0.05, poll_ms / 1000.0)
        self.include_edge = include_edge
        self.log_path = Path(log_path)
        self.exit_with_parent = exit_with_parent
        self.diagnostics = diagnostics
        self.dialog_pattern = dialog_pattern
        self.approve_pattern = approve_pattern
        self.burst_limit = burst_limit
        self.cool_off_s = cool_off_s
        self._cycle_next: dict[str, float] = {}
        self._nobutton: dict[str, int] = {}
        self._raised: set[str] = set()
        self.enable_click = enable_click and not observe
        self._action_times: deque[float] = deque()
        self._burst_paused_until = 0.0
        self._rects: dict[tuple, tuple] = {}
        self._pending: dict[str, int] = {}
        self._click_attempts: dict[str, int] = {}
        self._click_last: dict[str, float] = {}
        self._clicker = None
        self._clicker_retry_at = 0.0
        self._clicker_warned = False
        self._scan_errors = 0
        self.approved = 0
        self._parent_pid = os.getppid()
        self._seen: dict[str, float] = {}
        self._next_diagnostic_at = 0.0
        try:
            Atspi.init()
        except Exception:
            pass

    def log(self, message, level="INFO"):
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        line = f"{stamp} [{level}] {message}"
        print(line, flush=True)
        try:
            ensure_data_dir()
            if self.log_path.exists() and self.log_path.stat().st_size > 1_048_576:
                self.log_path.replace(self.log_path.with_name(self.log_path.name + ".1"))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def _desktop(self):
        try:
            return Atspi.get_desktop(0)
        except Exception:
            return None

    def _is_browser_app(self, node) -> str | None:
        name = _node_name(node)
        if any(k.lower() in name.lower() for k in CHROME_NAMES) or name in TEST_NAMES:
            return "chrome"
        if self.include_edge and any(k.lower() in name.lower() for k in EDGE_NAMES):
            return "edge"
        return None

    def _browser_apps(self):
        desk = self._desktop()
        if desk is None:
            return []
        out = []
        for child in _children(desk):
            if _role_name(child) != "application":
                continue
            kind = self._is_browser_app(child)
            if kind:
                out.append((kind, child))
        return out

    def find_dialog_hosts(self, diag=None):
        """Frames/dialogs whose title matches. Checks app children and, where
        the server exposes them, one level deeper - but never a deep recursive
        walk (perf: the page DOM must stay out of scope)."""
        hits = []
        seen = set()
        for kind, app in self._browser_apps():
            app_name = _node_name(app)
            frames = _children(app)
            if diag is not None:
                diag.append(f"{app_name}:{len(frames)}")
            for frame in frames:
                title = _node_name(frame).strip()
                role = _role_name(frame).lower()
                if self.dialog_pattern.match(title) and role in _DIALOG_ROLES:
                    sig = self._host_signature(frame)
                    if sig not in seen:
                        seen.add(sig)
                        hits.append((kind, frame))
                    continue
                # Bubble case: dialog nested one level inside the frame, if
                # the server exposes children at all (Wayland today: 0).
                for sub in _children(frame)[:20]:
                    stitle = (_node_name(sub) or _safe(lambda: sub.get_description() or "", "")).strip()
                    srole = _role_name(sub).lower()
                    if self.dialog_pattern.match(stitle) and srole in _DIALOG_ROLES:
                        sig = self._host_signature(sub)
                        if sig not in seen:
                            seen.add(sig)
                            hits.append((kind, sub))
        return hits

    def find_approve_button(self, host, depth=0):
        if depth > 6:
            return None
        if _role_name(host).lower() in ("push button", "button"):
            label = (_node_name(host) or "").strip()
            if self.approve_pattern.match(label):
                return host
        for child in _children(host):
            found = self.find_approve_button(child, depth + 1)
            if found is not None:
                return found
        return None

    def _button_labels(self, host, depth=0, out=None):
        if out is None:
            out = []
        if depth > 6:
            return out
        if _role_name(host).lower() in ("push button", "button"):
            out.append(_node_name(host))
        for child in _children(host):
            self._button_labels(child, depth + 1, out)
        return out

    def _host_signature(self, host) -> str:
        b = _bounds(host)
        if b is not None:
            return f"rect:{b[0]},{b[1]},{b[2]}x{b[3]}"
        title = _node_name(host).strip()
        if title:
            return f"title:{title.lower()}"
        return f"obj:{id(host)}"

    _dedupe_key = _host_signature

    def _dialog_still_up(self, host, button) -> bool:
        """Identity, not geometry: a queued successor renders at the same
        coordinates, so geometry alone would call a dismissed dialog alive."""
        if _is_defunct(host) or _is_defunct(button):
            return False
        b = _bounds(host)
        if b is not None and (b[2] <= 0 or b[3] <= 0):
            return False
        return True

    def _approve(self, host, button) -> str | None:
        how = _do_action(button)
        if how is None:
            return None
        time.sleep(VERIFY_WAIT_S)
        if not self._dialog_still_up(host, button):
            return how
        # One retry before giving up; never synthesize pointer input here.
        how2 = _do_action(button)
        time.sleep(VERIFY_WAIT_S)
        if how2 and not self._dialog_still_up(host, button):
            return how2 + "+retry"
        return None

    def _burst_ok(self, now: float) -> bool:
        """Minimal burst guard. True when clicking is allowed right now."""
        if self.burst_limit <= 0:
            return True
        if now < self._burst_paused_until:
            return False
        cutoff = now - BURST_WINDOW_S
        while self._action_times and self._action_times[0] < cutoff:
            self._action_times.popleft()
        if len(self._action_times) >= self.burst_limit:
            self._burst_paused_until = now + BURST_PAUSE_S
            self.log(f"burst guard: {len(self._action_times)} approvals in "
                     f"{int(BURST_WINDOW_S)}s >= limit {self.burst_limit} - "
                     f"pausing clicks for {int(BURST_PAUSE_S)}s", "ERROR")
            return False
        return True

    def _note_action(self) -> None:
        self._action_times.append(time.time())

    def _get_clicker(self, size=None):
        """Absolute-pointer device, created on demand and retried until the
        desktop is ready (boot order: the service starts before the a11y
        bus, portal and Mutter exist, so a one-shot create would boot dead).
        Passing `size` (from a fresh screenshot) also rebuilds on RDP
        resolution changes."""
        if self._clicker is not None and (size is None or size == self._clicker.size):
            return self._clicker
        now = time.time()
        if self._clicker is None and now < self._clicker_retry_at:
            return None
        if auto_click is None:
            if not self._clicker_warned:
                self.log("auto-click unavailable: auto_click module missing", "ERROR")
                self._clicker_warned = True
            self._clicker_retry_at = now + CLICKER_RETRY_S
            return None
        try:
            clicker = auto_click.AbsoluteClicker(size)
            if not clicker.available(size):
                raise RuntimeError("evdev/uinput/screen-size not ready")
        except Exception as exc:
            if not self._clicker_warned:
                self.log(f"auto-click not ready yet ({exc}); retrying every "
                         f"{int(CLICKER_RETRY_S)}s - normal in the first seconds "
                         f"after boot", "WARN")
                self._clicker_warned = True
            self._clicker_retry_at = now + CLICKER_RETRY_S
            return None
        self._clicker = clicker
        self._clicker_warned = False
        self._clicker_retry_at = 0.0
        self.log(f"auto-click ready: absolute pointer {clicker.size}", "INFO")
        return clicker

    def _drop_clicker(self, why: str) -> None:
        """Release the pointer device so the next cycle builds a fresh one.

        Empirically a long-lived device stops delivering clicks in some
        session states (seat/RDP churn) while a freshly created identical
        device delivers immediately - observed live 2026-09-22: the
        engine's device failed every click for minutes while a fresh CLI
        click approved the same bubble first try. Recreating is cheap.
        """
        if self._clicker is None:
            return
        try:
            self._clicker.close()
        except Exception:
            pass
        self._clicker = None
        self.log(f"  released the pointer device ({why}) - the next cycle "
                 f"creates a fresh one", "WARN")

    def _host_raise_point(self, bounds) -> tuple[int, int] | None:
        """A point inside the host window not covered by another app.

        Other apps' top-level windows are read from AT-SPI (Chrome's own
        windows are ignored - clicking another Chrome window still raises
        Chrome). Preference order is window edges and bottom corners,
        away from tabs, toolbars and window controls.
        """
        x, y, w, h = bounds
        if w < 120 or h < 120:
            return None
        covered = []
        try:
            desk = Atspi.get_desktop(0)
        except Exception:
            return None
        for app in _children(desk):
            try:
                aname = (app.get_name() or '').lower()
            except Exception:
                continue
            if 'chrome' in aname or 'chromium' in aname or 'edge' in aname:
                continue
            for fr in _children(app):
                b = _bounds(fr)
                if b and b[2] >= 200 and b[3] >= 200:
                    covered.append(b)
        cands = [
            (x + w - 14, y + h - 14),   # bottom-right
            (x + w // 2, y + h - 14),   # bottom-centre
            (x + 20, y + h - 14),       # bottom-left
            (x + w - 14, y + h // 2),   # mid-right
            (x + w - 14, y + 60),       # upper-right, below the tab strip
        ]
        for px, py in cands:
            if all(not (cx <= px < cx + cw and cy <= py < cy + ch)
                   for cx, cy, cw, ch in covered):
                return (px, py)
        return None

    def _raise_host(self, dkey: str) -> bool:
        """Click an uncovered point of the bubble's host window to raise it.

        On Wayland clicks only reach the active window: when another app
        covers the host, the bubble is invisible to screenshots and every
        click vanishes into the covering window (observed live 2026-09-22:
        a second app's window hid the bubble for an hour; a corner click
        raised Chrome and the bubble reappeared). Once per bubble.
        """
        if dkey in self._raised:
            return False
        self._raised.add(dkey)
        try:
            _, _, rect = dkey.split(":", 2)
            xs, ys, wh = rect.split(",")
            w, h = wh.split("x")
            bounds = (int(xs), int(ys), int(w), int(h))
        except Exception:
            return False
        pt = self._host_raise_point(bounds)
        if pt is None:
            return False
        if auto_click is None:
            return False
        shot = auto_click._portal_screenshot()
        size = auto_click.image_size(shot) if shot else None
        clicker = self._get_clicker(size)
        if clicker is None or not clicker.click(*pt):
            return False
        self.log(f"  clicked an uncovered corner {pt} to raise the host window "
                 f"(it was covered or inactive)", "AUDIT")
        return True

    def _note_scan(self, ok: bool) -> None:
        """Re-init AT-SPI after a run of failures: at boot the a11y bus can
        start after us, and a connection made too early never heals."""
        if ok:
            self._scan_errors = 0
            return
        self._scan_errors += 1
        if self._scan_errors == ATSPI_REINIT_AFTER:
            self.log(f"{self._scan_errors} consecutive scan failures - re-initialising "
                     f"AT-SPI", "WARN")
            try:
                Atspi.init()
            except Exception as exc:
                self.log(f"AT-SPI re-init failed: {exc!r}", "ERROR")
            self._scan_errors = 0

    def _approve_visual(self, dkey: str, baseline: int) -> tuple[str, str | None]:
        """Screenshot -> find Allow -> click it -> verify visually.

        Returns (status, detail):
        - ("approved", how)   the click was emitted and a fresh screenshot
          no longer shows the button;
        - ("no-button", None) the button is not in the screenshot at all
          (bubble already gone, or not renderable);
        - ("failed", None)    the button is still visible after the click.

        Child totals are a second signal only. Queued/orphaned attaches
        leak nodes: a bubble that is gone can leave the window's child
        total elevated indefinitely (observed live 2026-09-22), so the
        totals alone cannot decide whether a click landed.
        """
        if auto_click is None:
            return "failed", None
        shot = auto_click._portal_screenshot()
        if not shot:
            self.log("  screenshot failed (xdg-desktop-portal?)", "WARN")
            return "failed", None
        size = auto_click.image_size(shot)
        clicker = self._get_clicker(size)
        if clicker is None:
            return "failed", None
        pt = auto_click.find_allow_button(shot)
        if pt is None and self._raise_host(dkey):
            time.sleep(VERIFY_WAIT_S)
            shot = auto_click._portal_screenshot() or shot
            pt = auto_click.find_allow_button(shot)
        if pt is None:
            return "no-button", None
        where = f"{pt} on {size[0]}x{size[1]}" if size else str(pt)
        self.log(f"  visual approve: clicking Allow at {where}", "AUDIT")
        if not clicker.click(*pt):
            self.log("  click emit failed", "WARN")
            return "failed", None
        time.sleep(VERIFY_WAIT_S)
        try:
            after = auto_click._portal_screenshot()
        except Exception:
            after = None
        if after and auto_click.find_allow_button(after) is None:
            # Visual verify: the button is gone -> the bubble is dismissed.
            try:
                key = tuple(dkey.split(":", 2)[1:])  # "bubble:<app>:<rect>"
                e = self._rect_snapshot().get((key[0], key[1]))
                if e is not None and e["total"] > baseline:
                    self.log("  (child total still elevated - queued attaches "
                             "leak nodes; visual verify says gone)", "INFO")
            except Exception:
                pass
            return "approved", f"abs-pointer click {pt}"
        return "failed", None

    def _rect_snapshot(self) -> dict:
        """(app, rect) -> {frames, total, titled, kind, frame}: child totals
        grouped by window geometry.

        On Wayland the consent bubble exposes no title and no actionable
        children, but its host window's child total bumps +1 per pending
        bubble (field-verified 1->2->3). Geometry keys survive tab-title
        churn (which defeats title keys) and window-open/close is told
        apart by the per-app frame count: a bubble adds children to an
        EXISTING window (frame count unchanged), while a new window or a
        status bubble adds a frame."""
        out = {}
        for kind, app in self._browser_apps():
            app_name = _node_name(app).strip() or kind
            frames = _children(app)
            fam = len(frames)
            rects: dict = {}
            for frame in frames:
                if _role_name(frame).lower() not in _DIALOG_ROLES:
                    continue
                b = _bounds(frame)
                if b is None:
                    continue
                rect = f"{b[0]},{b[1]},{b[2]}x{b[3]}"
                try:
                    n = frame.get_child_count()
                except Exception:
                    continue
                e = rects.setdefault(rect, {"total": 0, "titled": False, "frame": None})
                e["total"] += n
                if _node_name(frame).strip():
                    e["titled"] = True
                    if e["frame"] is None:
                        e["frame"] = frame
            for rect, e in rects.items():
                out[(app_name, rect)] = {"frames": fam, "kind": kind, **e}
        return out

    def sweep(self):
        now = time.time()
        diag_sweep = self.diagnostics and now >= self._next_diagnostic_at
        if diag_sweep:
            self._next_diagnostic_at = now + 5
            self.log(f"diagnostic sweep start parent={os.getppid()} expected={self._parent_pid}", "DIAG")
        t0 = time.monotonic()
        diag = [] if diag_sweep else None
        try:
            hosts = self.find_dialog_hosts(diag=diag)
            self._note_scan(True)
        except Exception as exc:
            self.log(f"scan error: {exc!r}", "ERROR")
            self._note_scan(False)
            hosts = []
        if diag_sweep:
            ms = int((time.monotonic() - t0) * 1000)
            self.log(f"diagnostic scan hosts={len(hosts)} ({ms}ms) {' '.join(diag or [])}", "DIAG")
        for kind, host in hosts:
            try:
                labels = [b for b in self._button_labels(host) if b]
                key = self._dedupe_key(host)
                last = self._seen.get(key)
                if last is not None and now - last < DEDUPE_SECONDS:
                    continue
                self._seen[key] = now
                if not labels:
                    # Expected on Wayland: the bubble exposes no title and
                    # no actionable children. No synthetic input is known
                    # to activate it (AT-SPI: nothing exposed; pointer:
                    # ignored on the secure bubble; blind Enter: focus is
                    # unpredictable and once sat on Turn-off). Watchdog
                    # only: log it and leave it for a human click.
                    self.log(f"dialog candidate ({kind}) host={key} role={_role_name(host)!r} "
                             f"title={_node_name(host)!r} children=0 - no safe activation; left alone", "WARN")
                    continue
                self.log(f"dialog found ({kind}) buttons: " + ", ".join(f"'{b}'" for b in labels))
                if self.observe:
                    self.log("  observe mode - not clicking", "OBSERVE")
                    continue
                button = self.find_approve_button(host)
                if button is None:
                    self.log(f"  no button matched /{self.approve_pattern.pattern}/ - left alone", "WARN")
                    continue
                if not self._burst_ok(now):
                    self.log("  burst-paused - not clicking", "WARN")
                    continue
                how = self._approve(host, button)
                self._seen.pop(key, None)
                if how:
                    self._note_action()
                    self.approved += 1
                    self.log(f"  APPROVED via {how}", "ACTION")
                    self.log(f"  total approved this session: {self.approved}")
                else:
                    self.log("  FAILED: dialog still up after AT-SPI action - pressing again next sweep", "ERROR")
            except Exception as exc:
                self.log(f"  host error: {exc!r}", "ERROR")
        if len(self._seen) > DEDUPE_MAX:
            cutoff = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        # Untitled bubbles: window geometries whose child total grew while
        # the per-app frame count stayed put (a bubble joins an EXISTING
        # window; a new window or status bubble adds a frame instead).
        # Geometry keys survive tab-title churn, which defeats title keys.
        try:
            snap = self._rect_snapshot()
            self._note_scan(True)
        except Exception as exc:
            self.log(f"bubble scan error: {exc!r}", "ERROR")
            self._note_scan(False)
            snap = {}
        for key, e in snap.items():
            prev = self._rects.get(key)
            self._rects[key] = (e["frames"], e["total"])
            if prev is None:
                continue
            pframes, ptotal = prev
            dkey = "bubble:%s:%s" % (key[0], key[1])
            if (e["frames"] != pframes or e["total"] <= ptotal or not e["titled"]):
                # No new elevation this sweep. If the total has fallen to the
                # bubble's pre-bump base, it is gone: forget it entirely.
                base = self._pending.get(dkey)
                if base is not None and e["total"] <= base:
                    self._pending.pop(dkey, None)
                    self._click_attempts.pop(dkey, None)
                    self._cycle_next.pop(dkey, None)
                    self._click_last.pop(dkey, None)
                    self._nobutton.pop(dkey, None)
                    self._raised.discard(dkey)
                continue
            # A bump happened. First time: record the base and announce.
            if dkey not in self._pending:
                self._pending[dkey] = ptotal
                self._click_attempts[dkey] = 0
                self.log(f"untitled bubble candidate ({e['kind']}) window={key[1]} "
                         f"children {ptotal}->{e['total']} - consent bubble suspected")
                if self.observe:
                    self.log("  observe mode - not clicking", "OBSERVE")
                elif not self.enable_click:
                    self.log("  left alone (run with --enable-click to arm auto-approval)", "WARN")
                else:
                    self.log("  armed - will click Allow when found", "INFO")

        # Serve every pending bubble: this is what retries after a click that
        # Chrome swallowed mid-animation. Runs every sweep (cadence-gated),
        # not only on transitions, so a persistent bubble is never abandoned.
        for dkey, base in list(self._pending.items()):
            if self.observe or not self.enable_click:
                continue
            attempts = self._click_attempts.get(dkey, 0)
            if attempts >= CLICK_MAX_ATTEMPTS:
                # Cycle exhausted. Cool off, then start a fresh cycle - a
                # stuck bubble (occluded window, animation race, pointer
                # hiccup) recovers on its own instead of needing a new bump
                # or a human. cool_off_s <= 0 restores the old wait-forever.
                if self.cool_off_s <= 0:
                    continue
                if now < self._cycle_next.get(dkey, 0.0):
                    continue
                self._click_attempts[dkey] = 0
                attempts = 0
                self._cycle_next.pop(dkey, None)
                self.log(f"  cool-off elapsed - fresh {CLICK_MAX_ATTEMPTS}-attempt "
                         f"cycle on this bubble (pointer device re-created)", "INFO")
            last_try = self._click_last.get(dkey, 0.0)
            if now - last_try < CLICK_RETRY_S:
                continue
            if not self._burst_ok(now):
                self.log("  burst-paused - not clicking", "WARN")
                continue
            self._click_last[dkey] = now
            status, how = self._approve_visual(dkey, base)
            if status == "approved":
                self._note_action()
                self._pending.pop(dkey, None)
                self._click_attempts.pop(dkey, None)
                self._cycle_next.pop(dkey, None)
                self._click_last.pop(dkey, None)
                self._nobutton.pop(dkey, None)
                self._raised.discard(dkey)
                self.approved += 1
                self.log(f"  APPROVED via {how}", "ACTION")
                self.log(f"  total approved this session: {self.approved}")
            elif status == "no-button":
                # No visible button: the bubble is gone (dismissed elsewhere,
                # or a leaked child total is all that remains) or it is not
                # renderable. Stand down after a couple of looks instead of
                # cycling forever on a phantom.
                nb = self._nobutton.get(dkey, 0) + 1
                if nb >= NO_BUTTON_STOP:
                    self.log(f"  no Allow button visible {nb}x - leaving this window "
                             f"alone (a new child bump re-arms)", "WARN")
                    self._pending.pop(dkey, None)
                    self._click_attempts.pop(dkey, None)
                    self._cycle_next.pop(dkey, None)
                    self._click_last.pop(dkey, None)
                    self._nobutton.pop(dkey, None)
                    self._raised.discard(dkey)
                else:
                    self._nobutton[dkey] = nb
                    self.log(f"  Allow button not visible ({nb}/{NO_BUTTON_STOP}) - "
                             f"bubble may already be gone", "WARN")
            else:
                self._nobutton.pop(dkey, None)
                self._raise_host(dkey)  # activate/raise before the retry
                attempts += 1
                self._click_attempts[dkey] = attempts
                if attempts >= CLICK_MAX_ATTEMPTS:
                    self._cycle_next[dkey] = now + self.cool_off_s
                    self._drop_clicker("cycle exhausted")
                    self.log(f"  FAILED {attempts}/{CLICK_MAX_ATTEMPTS}: bubble still "
                             f"present - cooling off {int(self.cool_off_s)}s, then a "
                             f"fresh cycle with a new pointer", "ERROR")
                else:
                    self.log(f"  FAILED {attempts}/{CLICK_MAX_ATTEMPTS}: bubble still "
                             f"present after click - will retry", "WARN")
        for stale in [s for s in self._rects if s not in snap]:
            del self._rects[stale]
        for stale in [s for s in list(self._pending)
                      if not any(s.startswith(f"bubble:{k[0]}:{k[1]}") for k in snap)]:
            self._pending.pop(stale, None)
            self._click_attempts.pop(stale, None)
            self._cycle_next.pop(stale, None)
            self._click_last.pop(stale, None)
            self._nobutton.pop(stale, None)
            self._raised.discard(stale)
        if len(self._rects) > DEDUPE_MAX:
            for s in list(self._rects)[:len(self._rects) - DEDUPE_MAX]:
                del self._rects[s]

    def probe(self):
        """Dump the AT-SPI tree around Chrome for porting work. No clicks."""
        desk = self._desktop()
        if desk is None:
            self.log("probe: no AT-SPI desktop", "ERROR")
            return 1
        self.log(f"probe: desktop children={len(_children(desk))}")
        for kind, app in self._browser_apps():
            name = _node_name(app)
            frames = _children(app)
            self.log(f"probe: app {name!r} ({kind}) frames={len(frames)}")
            for i, fr in enumerate(frames[:20]):
                b = _bounds(fr)
                self.log(f"probe:   [{i}] role={_role_name(fr)!r} title={_node_name(fr)[:100]!r} "
                         f"bounds={b} children={_safe(lambda: fr.get_child_count(), '?')}")
                for j, sub in enumerate(_children(fr)[:20]):
                    self.log(f"probe:     [{j}] role={_role_name(sub)!r} name={_node_name(sub)[:100]!r} "
                             f"bounds={_bounds(sub)} children={_safe(lambda: sub.get_child_count(), '?')}")
        self.log("probe complete - trigger a real Allow prompt (restart Chrome, attach via "
                 "--autoConnect) then re-run --probe to capture its shape")
        return 0

    def run(self):
        ok, detail = atspi_available()
        click_note = "off"
        if self.enable_click:
            clicker = self._get_clicker()
            click_note = "ready" if clicker else "pending (will retry)"
        self.log(f"engine started (observe={self.observe}, interval={int(self.poll_s*1000)}ms, "
                 f"burst_limit={self.burst_limit}, cool_off={int(self.cool_off_s)}s, "
                 f"enable_click={self.enable_click} "
                 f"({click_note}), atspi={detail}, pid={os.getpid()})")
        while True:
            if self.exit_with_parent and os.getppid() != self._parent_pid:
                self.log("parent process is gone - exiting rather than approving unsupervised", "WARN")
                return 0
            try:
                self.sweep()
            except Exception as exc:
                self.log(f"loop error: {exc!r}", "ERROR")
            time.sleep(self.poll_s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Yes, Dev Linux engine (AT-SPI)")
    ap.add_argument("--observe", action="store_true", help="log dialogs, never click")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--probe", action="store_true", help="dump AT-SPI tree around Chrome, then exit")
    ap.add_argument("--interval-ms", type=int, default=POLL_MS_DEFAULT)
    ap.add_argument("--include-edge", action="store_true")
    ap.add_argument("--log-path", default=str(LOG_PATH))
    ap.add_argument("--dialog-pattern", default=r"^allow remote debugging\?$")
    ap.add_argument("--approve-pattern", default=r"^(allow|approve)$")
    ap.add_argument("--exit-with-parent", action="store_true")
    ap.add_argument("--diagnostics", action="store_true")
    ap.add_argument("--burst-limit", type=int, default=BURST_LIMIT_DEFAULT,
                    help="pause approvals for 60s after this many in a "
                         "minute (0 disables; default 60, same as upstream)")
    ap.add_argument("--cool-off-s", type=float, default=CLICK_COOL_OFF_S,
                    help="pause between click cycles after 3 failed attempts "
                         "on one bubble, then a fresh cycle starts (0 = old "
                         "wait-for-clear behaviour; default 30)")
    ap.add_argument("--enable-click", action="store_true",
                    help="arm auto-approval: screenshot + absolute-pointer click "
                         "on the Allow button (needs the auto-click deps; "
                         "--observe always wins)")
    args = ap.parse_args(argv)
    engine = Engine(observe=args.observe, poll_ms=args.interval_ms,
                    include_edge=args.include_edge, log_path=Path(args.log_path),
                    exit_with_parent=args.exit_with_parent, diagnostics=args.diagnostics,
                    dialog_pattern=re.compile(args.dialog_pattern, re.I),
                    approve_pattern=re.compile(args.approve_pattern, re.I),
                    cool_off_s=args.cool_off_s,
                    burst_limit=args.burst_limit,
                    enable_click=args.enable_click)
    if args.probe:
        return engine.probe()
    if args.once:
        engine.sweep()
        return 0
    # One engine is enough; a second would double-press the same dialog.
    # --once/--probe are diagnostic and bypass the lock on purpose.
    if not acquire_single_instance("engine"):
        engine.log("another engine already holds the lock - exiting", "WARN")
        return 0
    return engine.run()


if __name__ == "__main__":
    raise SystemExit(main())
