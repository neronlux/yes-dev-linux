#!/usr/bin/python3
"""Yes, Dev engine for Linux: watch for Chrome's "Allow remote debugging?"
consent dialog through AT-SPI.

Status: v0.3. Detection via AT-SPI; click via AT-SPI Action when a button
is exposed, otherwise an opt-in keyboard fallback (ydotool Enter) gated on
the dialog's frame being ACTIVE window, with a minimal burst guard
(pause clicks 60s after 60 approvals/min) and a single-instance lock.
The keyboard fallback is still UNPROVEN against a live prompt - keep
--observe until you have captured one with --probe.

Why no plain Invoke like Windows/macOS: on GNOME + Wayland (Chrome 153,
verified Sep 2026, real profile, remote-debugging on port 9222 in
DevToolsActivePort mode) every Chrome frame reports child_count 0 over
AT-SPI, so usually there is no button object to press. The dialog is a
Views bubble inside the frame; until a live prompt is captured with
--probe, the keyboard path stays experimental. Run:

    /usr/bin/python3 watcher_linux.py --observe    # log dialogs, never click
    /usr/bin/python3 watcher_linux.py --once       # one sweep then exit
    /usr/bin/python3 watcher_linux.py --probe      # dump AT-SPI tree, exit

Contract with the tray (identical on all platforms): append a line
containing `[ACTION]` per approval, in the existing format

    2026-08-27 16:11:28.644 [ACTION]   APPROVED via AtspiAction:press

and the counter / clouds / burst guard work unchanged. `[ACTION]` is only
written after the dialog is verified gone.

Requires the distro python (it ships python3-gi) and, only for the
opt-in keyboard fallback, a running ydotoold:
    sudo apt install python3-gi gir1.2-atspi-2.0
    /usr/bin/python3 watcher_linux.py --observe
    /usr/bin/python3 watcher_linux.py --observe --enable-click  # experimental
"""
from __future__ import annotations

import argparse
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
VERIFY_WAIT_S = 0.5
# Minimal burst guard (the tray owns the real one upstream; this keeps an
# unsupervised engine from approving a runaway loop forever).
BURST_LIMIT_DEFAULT = 60   # approvals per window before pausing clicks
BURST_WINDOW_S = 60.0
BURST_PAUSE_S = 60.0

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


def _is_active(node) -> bool:
    """Whether the window is the ACTIVE one. Guards the keyboard fallback
    so Enter never lands in an editor while Chrome sits in the background."""
    try:
        return Atspi.StateType.ACTIVE in set(node.get_state_set().get_states())
    except Exception:
        return False


def _ydotool_key(keycode: int = 28) -> tuple[bool, str]:
    """Press a key via ydotool (needs ydotoold running). Keycode 28 = Enter,
    the default-button activator. Returns (ok, detail). Never raises."""
    exe = shutil.which("ydotool")
    if exe is None:
        return False, "ydotool not on PATH"
    try:
        proc = subprocess.run([exe, "key", str(keycode)],
                              capture_output=True, text=True, timeout=5)
    except Exception as exc:
        return False, f"ydotool spawn failed: {exc!r}"
    if proc.returncode == 0:
        return True, "ok"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200]


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
                 enable_click=False, burst_limit=BURST_LIMIT_DEFAULT):
        self.observe = observe
        self.poll_s = max(0.05, poll_ms / 1000.0)
        self.include_edge = include_edge
        self.log_path = Path(log_path)
        self.exit_with_parent = exit_with_parent
        self.diagnostics = diagnostics
        self.dialog_pattern = dialog_pattern
        self.approve_pattern = approve_pattern
        self.enable_click = enable_click
        self.burst_limit = burst_limit
        self._action_times: deque[float] = deque()
        self._burst_paused_until = 0.0
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

    def _approve_keyboard(self, key: str) -> str | None:
        """Experimental fallback: press Enter via ydotool, then verify the
        candidate signature is gone on rescan. Only call with the host frame
        ACTIVE. Returns how it fell, or None."""
        ok, detail = _ydotool_key(28)
        if not ok:
            self.log(f"  keyboard fallback unavailable ({detail})", "WARN")
            return None
        time.sleep(VERIFY_WAIT_S)
        try:
            sigs = {self._dedupe_key(h) for _, h in self.find_dialog_hosts()}
        except Exception:
            return None
        if key not in sigs:
            return "ydotool:Enter"
        # One retry; a queued successor shares the signature, so a second
        # identical prompt is served next sweep rather than here.
        ok2, _ = _ydotool_key(28)
        time.sleep(VERIFY_WAIT_S)
        if ok2:
            try:
                sigs2 = {self._dedupe_key(h) for _, h in self.find_dialog_hosts()}
            except Exception:
                return None
            if key not in sigs2:
                return "ydotool:Enter+retry"
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
        except Exception as exc:
            self.log(f"scan error: {exc!r}", "ERROR")
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
                    # Expected on Wayland today: frame visible, 0 AT-SPI
                    # children. Keyboard fallback only with --enable-click
                    # AND the host frame ACTIVE, so Enter cannot land in
                    # an editor behind Chrome.
                    self.log(f"dialog candidate ({kind}) host={key} role={_role_name(host)!r} "
                             f"title={_node_name(host)!r} children=0 - no AT-SPI button")
                    if self.observe:
                        self.log("  observe mode - not clicking", "OBSERVE")
                        continue
                    if not self.enable_click:
                        self.log("  left alone (re-run with --enable-click for the experimental keyboard fallback)", "WARN")
                        continue
                    if not _is_active(host):
                        self.log("  host frame not ACTIVE - keyboard fallback refused; left alone", "WARN")
                        continue
                    self.log("  keyboard fallback: host ACTIVE, sending Enter via ydotool", "AUDIT")
                    if not self._burst_ok(now):
                        self.log("  burst-paused - not clicking", "WARN")
                        continue
                    how = self._approve_keyboard(key)
                    self._seen.pop(key, None)
                    if how:
                        self._note_action()
                        self.approved += 1
                        self.log(f"  APPROVED via {how}", "ACTION")
                        self.log(f"  total approved this session: {self.approved}")
                    else:
                        self.log("  FAILED: candidate still present after keyboard fallback - retrying next sweep", "ERROR")
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
        self.log(f"engine started (observe={self.observe}, interval={int(self.poll_s*1000)}ms, "
                 f"enable_click={self.enable_click}, burst_limit={self.burst_limit}, "
                 f"atspi={detail}, pid={os.getpid()})")
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
    ap.add_argument("--enable-click", action="store_true",
                    help="EXPERIMENTAL: when no AT-SPI button exists and the host "
                         "frame is ACTIVE, press Enter via ydotool (needs ydotoold). "
                         "Off by default; --observe always wins.")
    ap.add_argument("--burst-limit", type=int, default=BURST_LIMIT_DEFAULT,
                    help="pause clicks for 60s after this many approvals in a "
                         "minute (0 disables; default 60, same as upstream)")
    args = ap.parse_args(argv)
    engine = Engine(observe=args.observe, poll_ms=args.interval_ms,
                    include_edge=args.include_edge, log_path=Path(args.log_path),
                    exit_with_parent=args.exit_with_parent, diagnostics=args.diagnostics,
                    dialog_pattern=re.compile(args.dialog_pattern, re.I),
                    approve_pattern=re.compile(args.approve_pattern, re.I),
                    enable_click=args.enable_click and not args.observe,
                    burst_limit=args.burst_limit)
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
