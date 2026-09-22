#!/usr/bin/python3
"""Yes, Dev engine for Linux: watch for Chrome's "Allow remote debugging?"
consent dialog through AT-SPI.

Status: v0.8.13. Detection via AT-SPI is proven live (titled dialogs by
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
Approval is verified two ways (fresh screenshot or child total), the
normalize ladder runs 3 rounds, stand-downs are reason-coded, and a
state.json feeds tools/doctor.py; the edge-case matrix lives in
TESTING.md. Clicks pause while the session is locked, and an opt-in
--restart-chrome-on-stuck turns the stuck-queue hint into a restart, and
--selftest proves the whole stack without waiting for a real prompt.
Off-workspace bubbles are hunted (up to 3 workspaces, switched back),
and multi-monitor setups are detected and warned about. An idle
visual backstop catches prompts whose child-count bump was missed
(leak aliasing - observed live after a reboot).

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
import json
import os
import re
import shutil
import subprocess
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

VERSION = "0.8.13"

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
NORMALIZE_ROUNDS = 3       # focus/maximize rounds per bubble before giving up
LOCK_CACHE_S = 5           # how long a lock-state reading is trusted
WORKSPACE_HUNT_MAX = 3     # off-workspace bubbles: hunt up to N workspaces over
VISUAL_BACKSTOP_S = 5.0    # idle visual glance for bubbles the count missed (0=off)
CHROME_RESTART_COOLDOWN_S = 1800  # opt-in restart: at most once per 30 min
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
                 cool_off_s=CLICK_COOL_OFF_S, restart_chrome_on_stuck=False,
                 workspace_hunt=True, visual_backstop_s=VISUAL_BACKSTOP_S):
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
        self._normalized: dict[str, int] = {}
        self.enable_click = enable_click and not observe
        self._action_times: deque[float] = deque()
        self._burst_paused_until = 0.0
        self._rects: dict[tuple, tuple] = {}
        self._pending: dict[str, int] = {}
        self._click_attempts: dict[str, int] = {}
        self._click_last: dict[str, float] = {}
        self._clicker = None
        self._clicker_created = 0.0
        self._clicker_retry_at = 0.0
        self._clicker_warned = False
        self._scan_errors = 0
        self._norm_reason = ""
        self._standdowns: deque[float] = deque()
        self._last_hint = 0.0
        self._started = time.time()
        self._last_error = None
        self._last_action = None
        self._last_scan_ms = 0
        self._last_state_write = 0.0
        self._locked_checked = 0.0
        self._locked_state = False
        self._last_locked_log = 0.0
        self.restart_chrome_on_stuck = restart_chrome_on_stuck
        self._last_chrome_restart = 0.0
        self.workspace_hunt = workspace_hunt
        self._ws_shift: dict[str, int] = {}
        self._ws_restore_on_start = 0
        self._monitors_warned = False
        self.visual_backstop_s = visual_backstop_s
        self._last_backstop = 0.0
        self._last_observe_backstop_log = 0.0
        try:
            import json as _json
            st = _json.loads((Path(DATA_DIR) / "state.json").read_text())
            self._ws_restore_on_start = int(st.get("ws_shift_total", 0) or 0)
        except Exception:
            pass
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
        if level == "ERROR":
            self._last_error = f"{stamp} {message[:160]}"
        elif level == "ACTION":
            self._last_action = f"{stamp} {message[:160]}"
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
        self._clicker_created = time.time()
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

    def _active_chrome_frame(self):
        """(kind, (x, y, w, h)) of an ACTIVE Chrome frame, or None.

        Keyboard window-management (Super+Up, Alt+Tab) acts on whatever
        has focus, so it is only ever sent after this check proves a
        Chrome window is active - never blind.
        """
        if Atspi is None:
            return None
        for kind, app in self._browser_apps():
            for fr in _children(app):
                try:
                    if not fr.get_state_set().contains(Atspi.StateType.ACTIVE):
                        continue
                    b = _bounds(fr)
                    if b and b[2] > 200:
                        return kind, b
                except Exception:
                    continue
        return None

    def _normalize_host(self, dkey: str):
        """Bring Chrome to the front and maximize it, so the bubble has a
        deterministic position. Returns (kind, rect) of the active Chrome
        frame after maximizing, or None if Chrome could not be focused.

        Focus path: uncovered-corner click (partial cover), then Alt+Tab
        probing (fully covered); a second normalize attempt also cycles
        same-app windows with Super+` (bubble may be on another Chrome
        window). Maximize is GNOME's Super+Up, idempotent when already
        maximized. Focus is verified before any key is sent.
        """
        if auto_click is None:
            return None
        n = self._normalized.get(dkey, 0)
        if n >= NORMALIZE_ROUNDS:
            return None
        self._normalized[dkey] = n + 1
        self._norm_reason = "no-visible-button"

        act = self._active_chrome_frame()
        if act is None:
            self._raise_host(dkey)          # corner click, if any is uncovered
            time.sleep(0.4)
            act = self._active_chrome_frame()
        clicker = self._get_clicker(None)
        if clicker is None:
            self._norm_reason = "pointer-unavailable"
            return None
        k = 1
        while act is None and k <= 3:
            auto_click.alttab_held(clicker, k)   # walk k windows down the MRU
            time.sleep(0.5)
            act = self._active_chrome_frame()
            k += 1
        if act is None:
            self._norm_reason = "no-focus"
            self.log("  could not bring Chrome to the front (no focus within "
                     "reach); leaving this bubble alone", "WARN")
            return None
        if n >= 1:
            # Cycle same-app windows until the ACTIVE one is the bubble's
            # host (its rect matches the tracked dkey) - maximizing the
            # wrong Chrome window was a real failure mode. Bounded.
            try:
                _, _, want = dkey.split(":", 2)
            except Exception:
                want = None
            for _ in range(4):
                act = self._active_chrome_frame() or act
                have = None
                try:
                    b0 = act[1]
                    have = f"{b0[0]},{b0[1]},{b0[2]}x{b0[3]}"
                except Exception:
                    pass
                if want is None or have == want:
                    break
                auto_click.combo(clicker, "nextwindow")
                time.sleep(0.5)
        kind, b = act
        # Ubuntu-style GNOME maps Super+Up to TOGGLE maximize: only send it
        # when the window clearly does not already fill the screen, or we
        # would restore it to windowed mid-bubble.
        sw, sh = auto_click._screen_size() or (0, 0)
        fills = sw and b[2] >= sw - 64 and b[3] >= sh - 24
        if fills:
            self.log("  Chrome already fills the screen - maximize not needed", "AUDIT")
        elif not auto_click.combo(clicker, "maximize"):
            return None
        else:
            before_area = b[2] * b[3]
            time.sleep(0.5)
            act2 = self._active_chrome_frame() or act
            if act2[1][2] * act2[1][3] < before_area:
                # Toggle was mid-state (e.g. half-tiled): first press
                # restored it. One more press maximizes from windowed.
                auto_click.combo(clicker, "maximize")
                time.sleep(0.5)
                act2 = self._active_chrome_frame() or act2
            self.log("  Chrome focused - maximized; the bubble now has a "
                     "deterministic position", "AUDIT")
        time.sleep(0.4)
        act = self._active_chrome_frame() or act
        kind, b = act
        rect = f"{b[0]},{b[1]},{b[2]}x{b[3]}"
        return kind, rect

    def _migrate(self, old: str, new: str) -> None:
        """Move bubble tracking to a new geometry key (window moved or was
        maximized while the bubble was pending)."""
        if old == new or old not in self._pending:
            return
        self._pending[new] = self._pending.pop(old)
        self._click_attempts[new] = 0
        self._click_last.pop(old, None)
        self._cycle_next.pop(old, None)
        self._nobutton.pop(old, None)
        self._raised.discard(old)
        self._normalized[new] = self._normalized.pop(old, 0)
        shift = self._ws_shift.pop(old, 0)
        if shift:
            self._ws_shift[new] = shift

    def _ws_cleanup(self, dkey: str) -> None:
        self._pending.pop(dkey, None)
        self._click_attempts.pop(dkey, None)
        self._cycle_next.pop(dkey, None)
        self._click_last.pop(dkey, None)
        self._nobutton.pop(dkey, None)
        self._raised.discard(dkey)
        self._normalized.pop(dkey, None)

    def _ws_restore(self, dkey: str) -> None:
        """Switch back any workspaces the hunt advanced for this bubble."""
        shift = self._ws_shift.pop(dkey, 0)
        if not shift:
            return
        clicker = self._get_clicker(None)
        if clicker is None:
            self.log(f"  WARNING: could not restore {shift} workspace switch(es) - "
                     f"press Super+PageUp {shift} time(s) yourself", "ERROR")
            return
        for _ in range(shift):
            auto_click.combo(clicker, "wsup")
            time.sleep(0.4)
        self.log(f"  restored the workspace ({shift} switch(es) back)", "INFO")

    def _backstop_scan(self, now: float) -> None:
        """Idle visual glance: the child-count bump can be missed when a
        leaked node clears at the same time a new attach bumps (the count
        returns to the value we already recorded - observed live after a
        reboot). While armed and idle, look at the screen; if the Allow
        button sits in the dialog region twice in a row at the same spot,
        treat it as a candidate and let the normal click flow handle it."""
        if auto_click is None:
            return
        shot = auto_click._portal_screenshot()
        if not shot:
            return
        size = auto_click.image_size(shot)
        pt = auto_click.find_allow_button(shot)
        if pt is None or size is None:
            return
        # position prior: the consent bubble sits centre-screen, not in
        # page flow; this keeps a stray page button pair from firing.
        if not (0.15 * size[0] <= pt[0] <= 0.85 * size[0]
                and 0.20 * size[1] <= pt[1] <= 0.80 * size[1]):
            return
        time.sleep(0.8)
        shot2 = auto_click._portal_screenshot()
        pt2 = auto_click.find_allow_button(shot2) if shot2 else None
        if pt2 is None or abs(pt2[0] - pt[0]) > 10 or abs(pt2[1] - pt[1]) > 10:
            return
        if self.observe:
            if now - self._last_observe_backstop_log > 60:
                self._last_observe_backstop_log = now
                self.log(f"visual backstop (observe): Allow button on screen at "
                         f"{pt2} - would click", "OBSERVE")
            return
        dkey = "bubble:chrome:visual"
        if dkey in self._pending:
            return
        self._pending[dkey] = 0          # totals unknown; visual verify decides
        self._click_attempts[dkey] = 0
        self.log(f"visual backstop: Allow button on screen at {pt2} with no child "
                 f"bump (detection missed) - approving", "WARN")

    def _locked(self) -> bool:
        """True while the session is locked (org.gnome.ScreenSaver), cached
        ~5s. Clicks are pointless and risky on a lock screen, so the serve
        loop waits instead of burning attempts."""
        now = time.time()
        if now - self._locked_checked < LOCK_CACHE_S:
            return self._locked_state
        self._locked_checked = now
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
                 "--object-path", "/org/gnome/ScreenSaver",
                 "--method", "org.gnome.ScreenSaver.GetActive"],
                capture_output=True, text=True, timeout=5).stdout
            self._locked_state = "true" in out
        except Exception:
            self._locked_state = False
        return self._locked_state

    def _port_open(self) -> bool:
        try:
            return ":9222" in subprocess.run(
                ["ss", "-tlnp"], capture_output=True, text=True,
                timeout=10).stdout
        except Exception:
            return False

    def _restart_chrome(self, now: float) -> None:
        """Opt-in (--restart-chrome-on-stuck): restart the Chrome that
        holds the debug port so a stuck consent queue clears. Tabs
        restore; rate-limited to once per 30 minutes."""
        if now - self._last_chrome_restart < CHROME_RESTART_COOLDOWN_S:
            self.log("  chrome restart already attempted recently - not repeating", "WARN")
            return
        self._last_chrome_restart = now
        try:
            ss = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True,
                                timeout=10).stdout
            pid = None
            for line in ss.splitlines():
                if ":9222" in line and "pid=" in line:
                    m = re.search(r"pid=(\d+)", line)
                    if m:
                        pid = int(m.group(1))
                        break
            if pid is None:
                self.log("  chrome restart skipped: no process holds :9222", "WARN")
                return
            self.log(f"  restart-chrome-on-stuck: terminating Chrome pid {pid} "
                     f"(tabs will restore)", "WARN")
            subprocess.run(["kill", "-TERM", str(pid)], timeout=10)
            for _ in range(30):
                time.sleep(1)
                if not self._port_open():
                    break
            uid = os.getuid()
            wl = "wayland-0"
            try:
                cands = sorted(p.name for p in Path(f"/run/user/{uid}").glob("wayland-*")
                               if not p.name.endswith(".lock"))
                if cands:
                    wl = cands[0]
            except Exception:
                pass
            binary = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
                      or "/usr/bin/google-chrome")
            subprocess.Popen(
                ["setsid", "-f", "env",
                 f"XDG_RUNTIME_DIR=/run/user/{uid}", f"WAYLAND_DISPLAY={wl}",
                 f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                 binary, "--ozone-platform=wayland", "--restore-last-session"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            for _ in range(30):
                time.sleep(1)
                if self._port_open():
                    self.log("  chrome restart: debug port is back; the next attach "
                             "will prompt", "INFO")
                    return
            self.log("  chrome restart: debug port did not return within 30s", "ERROR")
        except Exception as exc:
            self.log(f"  chrome restart failed: {exc!r}", "ERROR")

    def _stuck_hint_due(self, now: float) -> bool:
        """True at most once per 10 min, and only when stand-downs are
        clustering - the known stuck-consent-queue signature."""
        self._standdowns.append(now)
        while self._standdowns and now - self._standdowns[0] > 600:
            self._standdowns.popleft()
        if len(self._standdowns) >= 3 and now - self._last_hint > 600:
            self._last_hint = now
            return True
        return False

    def _write_state(self, now: float) -> None:
        """Machine-readable health snapshot for tools/doctor.py (throttled
        to ~5s, atomic replace)."""
        if now - self._last_state_write < 5.0:
            return
        self._last_state_write = now
        try:
            clicker = None
            if self._clicker is not None:
                clicker = {"size": list(self._clicker.size),
                           "age_s": int(now - self._clicker_created)}
            state = {
                "pid": os.getpid(),
                "started": datetime.fromtimestamp(self._started).isoformat(timespec="seconds"),
                "updated": datetime.now().isoformat(timespec="seconds"),
                "observe": self.observe,
                "enable_click": self.enable_click,
                "approved_session": self.approved,
                "pointer": clicker,
                "pending": [{"key": k,
                             "attempts": self._click_attempts.get(k, 0),
                             "normalize_rounds": self._normalized.get(k, 0),
                             "no_button_looks": self._nobutton.get(k, 0),
                             "ws_shift": self._ws_shift.get(k, 0)}
                            for k in self._pending],
                "workspace_hunt": self.workspace_hunt,
                "ws_shift_total": sum(self._ws_shift.values()),
                "last_scan_ms": self._last_scan_ms,
                "last_error": self._last_error,
                "last_action": self._last_action,
            }
            path = Path(DATA_DIR) / "state.json"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

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
        vis_gone = after is not None and auto_click.find_allow_button(after) is None
        tot_gone = False
        if baseline:  # base 0 = visual backstop bubble, totals unknown
            try:
                key = tuple(dkey.split(":", 2)[1:])  # "bubble:<app>:<rect>"
                e = self._rect_snapshot().get((key[0], key[1]))
                tot_gone = e is None or e["total"] <= baseline
            except Exception:
                pass
        if vis_gone or tot_gone:
            # Either signal is enough: screenshots can lag a frame, and
            # queued attaches can keep the child total elevated after the
            # bubble is gone (both observed live). Disagreements are noted
            # instead of stalling the loop.
            if vis_gone and not tot_gone:
                self.log("  (child total still elevated - queued attaches leak "
                         "nodes; visual verify says gone)", "INFO")
            if tot_gone and not vis_gone:
                self.log("  (screenshot still shows the button but the child "
                         "total dropped - screenshot lag; accepting)", "INFO")
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
        if self._ws_restore_on_start and self.enable_click and not self._locked():
            clicker = self._get_clicker(None)
            if clicker is not None:
                for _ in range(self._ws_restore_on_start):
                    auto_click.combo(clicker, "wsup")
                    time.sleep(0.4)
                self.log(f"  restored {self._ws_restore_on_start} leftover workspace "
                         f"switch(es) from a previous run", "INFO")
                self._ws_restore_on_start = 0
        if not self._monitors_warned and self.enable_click and auto_click is not None:
            self._monitors_warned = True
            try:
                n = auto_click.monitor_count()
                if n > 1:
                    self.log(f"  {n} logical monitors detected - click coordinates are "
                             f"only validated on single-monitor setups; please report "
                             f"your experience", "WARN")
            except Exception:
                pass
        t0 = time.monotonic()
        diag = [] if diag_sweep else None
        try:
            hosts = self.find_dialog_hosts(diag=diag)
            self._note_scan(True)
        except Exception as exc:
            self.log(f"scan error: {exc!r}", "ERROR")
            self._note_scan(False)
            hosts = []
        self._last_scan_ms = int((time.monotonic() - t0) * 1000)
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
                    self._ws_restore(dkey)
                    self._pending.pop(dkey, None)
                    self._click_attempts.pop(dkey, None)
                    self._cycle_next.pop(dkey, None)
                    self._click_last.pop(dkey, None)
                    self._nobutton.pop(dkey, None)
                    self._raised.discard(dkey)
                    self._normalized.pop(dkey, None)
                continue
            # A bump happened. First time: record the base and announce.
            if dkey not in self._pending:
                if any(k.endswith(":visual") for k in self._pending):
                    self.log("  (child bump while a visual candidate is pending - "
                             "ignoring the bump)", "INFO")
                    continue
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
            if self._locked():
                if now - self._last_locked_log > 60:
                    self._last_locked_log = now
                    self.log("  session is locked - waiting for unlock before "
                             "clicking", "WARN")
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
                self._ws_restore(dkey)
                self._pending.pop(dkey, None)
                self._click_attempts.pop(dkey, None)
                self._cycle_next.pop(dkey, None)
                self._click_last.pop(dkey, None)
                self._nobutton.pop(dkey, None)
                self._raised.discard(dkey)
                self._normalized.pop(dkey, None)
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
                    # Before standing down: force Chrome to the front and
                    # maximize it, then keep tracking the bubble under the
                    # new geometry. Focus is AT-SPI-verified before any key
                    # is sent; capped at three normalize attempts per bubble.
                    shift = self._ws_shift.get(dkey, 0)
                    norm = (self._normalize_host(dkey) if shift == 0 else None)
                    if norm:
                        # Keep tracking the original host: if its geometry
                        # changed (maximized), the stale-rect migration
                        # follows it to the rect whose total is still
                        # elevated - never blindly to whichever Chrome
                        # window happens to be active.
                        self._click_attempts[dkey] = 0
                        self._nobutton.pop(dkey, None)
                        self._raised.discard(dkey)
                        self.log("  retrying on the re-focused window", "INFO")
                    elif self.workspace_hunt and shift < WORKSPACE_HUNT_MAX:
                        # The bubble may live on another workspace. Advance
                        # one workspace per stand-down (bounded), keep the
                        # pending, and switch everything back afterwards.
                        clicker = self._get_clicker(None)
                        if clicker is not None and auto_click.combo(clicker, "wsdown"):
                            self._ws_shift[dkey] = shift + 1
                            self._click_attempts[dkey] = 0
                            self._nobutton.pop(dkey, None)
                            self._raised.discard(dkey)
                            self.log(f"  bubble not reachable here - hunting the next "
                                     f"workspace ({shift + 1}/{WORKSPACE_HUNT_MAX}); "
                                     f"switches back afterwards", "AUDIT")
                            continue
                        reason = "workspace-hunt-keys-unavailable"
                        self._ws_restore(dkey)
                        self.log(f"  giving up on this bubble - reason={reason}", "WARN")
                        self._ws_cleanup(dkey)
                    else:
                        reason = self._norm_reason or "no-visible-button"
                        self._ws_restore(dkey)
                        self.log(f"  giving up on this bubble after "
                                 f"{NORMALIZE_ROUNDS} normalize rounds - "
                                 f"reason={reason}; a new child bump re-arms",
                                 "WARN")
                        if self._stuck_hint_due(now):
                            self.log("  if sessions stay blocked, the browser's consent "
                                     "queue may be stuck", "WARN")
                            if self.restart_chrome_on_stuck:
                                self._restart_chrome(now)
                            else:
                                self.log("  remedy: toggle remote debugging OFF/ON at "
                                         "chrome://inspect/#remote-debugging, restart "
                                         "Chrome, or rerun with --restart-chrome-on-stuck",
                                         "WARN")
                        self._ws_cleanup(dkey)
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
            # The host window may simply have moved or been resized while
            # the bubble was pending: follow it to the one new geometry of
            # the same kind whose child total is still above its base.
            migrated = False
            try:
                _, skind, _ = stale.split(":", 2)
                base = self._pending.get(stale)
                cands = [k for k, e in snap.items()
                         if k[0] == skind and base is not None and e["total"] > base]
                if len(cands) == 1:
                    new_key = f"bubble:{cands[0][0]}:{cands[0][1]}"
                    self._migrate(stale, new_key)
                    self.log(f"  bubble window moved - following it to {new_key}", "INFO")
                    migrated = True
            except Exception:
                pass
            if migrated:
                continue
            self._ws_restore(stale)
            self._pending.pop(stale, None)
            self._click_attempts.pop(stale, None)
            self._cycle_next.pop(stale, None)
            self._click_last.pop(stale, None)
            self._nobutton.pop(stale, None)
            self._raised.discard(stale)
            self._normalized.pop(stale, None)
        if len(self._rects) > DEDUPE_MAX:
            for s in list(self._rects)[:len(self._rects) - DEDUPE_MAX]:
                del self._rects[s]
        if (self.visual_backstop_s > 0 and not self._locked()
                and now - self._last_backstop >= self.visual_backstop_s
                and (self.observe or (self.enable_click and not self._pending))):
            self._last_backstop = now
            self._backstop_scan(now)
        self._write_state(now)

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
                 f"backstop={int(self.visual_backstop_s)}s, "
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


def run_selftest() -> int:
    """End-to-end environment check: AT-SPI, portal screenshot, pointer
    device, and a real click-delivery probe (briefly opens and closes the
    clock popup). Friendly output; exit 0 when the stack works."""
    print(f"yes-dev-linux selftest (v{VERSION})")
    print("-" * 52)
    ok = True
    try:
        desk = Atspi.get_desktop(0)
        print(f"[OK]   AT-SPI reachable ({len(_children(desk))} desktop apps)")
    except Exception as exc:
        print(f"[FAIL] AT-SPI unreachable: {exc!r}")
        print("       sudo apt install python3-gi gir1.2-atspi-2.0 (then re-login)")
        return 1
    if auto_click is None:
        print("[FAIL] auto_click module missing - run with /usr/bin/python3")
        return 1
    shot = auto_click._portal_screenshot()
    size = auto_click.image_size(shot) if shot else None
    if size:
        print(f"[OK]   portal screenshot works ({size[0]}x{size[1]})")
    else:
        print("[FAIL] portal screenshot failed - is xdg-desktop-portal running?")
        ok = False
    clicker = None
    if size:
        clicker = auto_click.AbsoluteClicker(size)
        if clicker.available(size):
            print(f"[OK]   absolute pointer created at {size[0]}x{size[1]}")
        else:
            print("[FAIL] cannot create the pointer: /dev/uinput not writable?")
            print('       udev rule KERNEL=="uinput", MODE="0660", GROUP="input" '
                  "+ membership in the input group")
            clicker = None
            ok = False
    if clicker is not None and size and size[0] >= 400:
        before = auto_click._portal_screenshot()
        clicker.click(size[0] // 2, 8)      # clock / top-bar centre
        time.sleep(1.2)
        after = auto_click._portal_screenshot()
        changed = False
        try:
            changed = (before is not None and after is not None
                       and open(before, "rb").read() != open(after, "rb").read())
        except Exception:
            pass
        auto_click.combo(clicker, "escape")  # dismiss whatever opened
        if changed:
            print("[OK]   click delivery: the screen changed after a test click")
        else:
            print("[WARN] click delivery: no visible change - the pointer may not "
                  "reach the compositor")
            print("       confirm with --observe on a real prompt")
    if clicker is not None:
        clicker.close()
    print("-" * 52)
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Yes, Dev Linux engine (AT-SPI)")
    ap.add_argument("--version", action="version",
                    version=f"yes-dev-linux {VERSION}")
    ap.add_argument("--observe", action="store_true", help="log dialogs, never click")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--probe", action="store_true", help="dump AT-SPI tree around Chrome, then exit")
    ap.add_argument("--visual-backstop-s", type=float, default=VISUAL_BACKSTOP_S,
                    help="while idle and armed, glance at the screen every N seconds "
                         "for an Allow button the child-count missed (0 disables; "
                         "default 5)")
    ap.add_argument("--no-workspace-hunt", action="store_false", dest="workspace_hunt",
                    help="do not switch through workspaces looking for a hidden "
                         "bubble (default: hunt up to 3, switch back after)")
    ap.add_argument("--selftest", action="store_true",
                    help="check AT-SPI, portal screenshot, pointer device and click "
                         "delivery, then exit (briefly opens the clock popup)")
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
    ap.add_argument("--restart-chrome-on-stuck", action="store_true",
                    help="if the consent queue looks stuck (cluster of stand-downs), "
                         "restart the Chrome holding :9222 once per 30 min; tabs "
                         "restore. Off by default: disruptive")
    ap.add_argument("--cool-off-s", type=float, default=CLICK_COOL_OFF_S,
                    help="pause between click cycles after 3 failed attempts "
                         "on one bubble, then a fresh cycle starts (0 = old "
                         "wait-for-clear behaviour; default 30)")
    ap.add_argument("--enable-click", action="store_true",
                    help="arm auto-approval: screenshot + absolute-pointer click "
                         "on the Allow button (needs the auto-click deps; "
                         "--observe always wins)")
    args = ap.parse_args(argv)
    if args.selftest:
        return run_selftest()
    engine = Engine(observe=args.observe, poll_ms=args.interval_ms,
                    include_edge=args.include_edge, log_path=Path(args.log_path),
                    exit_with_parent=args.exit_with_parent, diagnostics=args.diagnostics,
                    dialog_pattern=re.compile(args.dialog_pattern, re.I),
                    approve_pattern=re.compile(args.approve_pattern, re.I),
                    cool_off_s=args.cool_off_s,
                    restart_chrome_on_stuck=args.restart_chrome_on_stuck,
                    workspace_hunt=args.workspace_hunt,
                    visual_backstop_s=args.visual_backstop_s,
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
