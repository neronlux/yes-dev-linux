#!/usr/bin/python3
"""One-shot health check for the yes-dev engine. Read-only and safe to
run any time, including while the service is running. Prints what it
found and the suggested next step per finding.

    /usr/bin/python3 tools/doctor.py

Exit code is non-zero only when the service itself is not active.
"""
import json
import subprocess
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

LOG = Path.home() / ".local/share/YesDev/yes-dev.log"
STATE = Path.home() / ".local/share/YesDev/state.json"


def sh(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, **kw).stdout
    except Exception:
        return ""


findings = []


def add(name, state, detail, action=None):
    findings.append((name, state, detail, action))


# --- service ----------------------------------------------------------------
active = sh(["systemctl", "--user", "is-active", "yes-dev.service"]).strip()
enabled = sh(["systemctl", "--user", "is-enabled", "yes-dev.service"]).strip()
add("service", "ok" if active == "active" else "FAIL", f"{active or 'inactive'} / {enabled or 'disabled'}",
    None if active == "active" else "systemctl --user enable --now yes-dev.service")

# --- chrome debug port ------------------------------------------------------
listening = ":9222" in sh(["ss", "-tlnp"])
add("chrome debug port", "ok" if listening else "warn",
    "listening on 9222" if listening else "not listening",
    None if listening else "open chrome://inspect/#remote-debugging and toggle "
                            "remote debugging ON (a restart needs the toggle re-armed)")

# --- engine log -------------------------------------------------------------
lines = []
try:
    lines = LOG.read_text(errors="replace").splitlines()[-400:]
except Exception:
    pass
last = lines[-1] if lines else "(no log)"
add("engine log", "ok" if lines else "warn", last[-110:] if lines else "no log file yet")
standdown = next((l for l in reversed(lines) if "no Allow button visible" in l), None)
raise_ = next((l for l in reversed(lines) if "raise the host window" in l), None)
approved = next((l for l in reversed(lines) if "APPROVED" in l), None)

# --- engine state file (v0.8.6+) ---------------------------------------------
try:
    state = json.loads(STATE.read_text())
except Exception:
    state = None
if state:
    try:
        age = int(time.time() - datetime.fromisoformat(state["updated"]).timestamp())
    except Exception:
        age = -1
    pend = state.get("pending") or []
    ptr = state.get("pointer")
    detail = (f"pid {state.get('pid')} approvals {state.get('approved_session')} "
              f"pending {len(pend)} pointer {ptr['size'] if ptr else '-'} "
              f"state age {age}s")
    add("engine state", "ok" if 0 <= age <= 90 else "warn", detail,
        None if 0 <= age <= 90 else "no fresh state - "
        "check systemctl --user status yes-dev.service")
    if state.get("last_action"):
        add("last approval", "ok", state["last_action"], None)
    if state.get("last_error"):
        add("last error", "warn", state["last_error"],
            "see the Troubleshooting playbook in the README")
else:
    add("engine state", "warn", "no state.json yet",
        "written on the first sweep since v0.8.6 - restart the service once")

# --- AT-SPI: chrome windows, bubbles, covering windows -----------------------
try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()

    def children(n):
        try:
            return [n.get_child_at_index(i) for i in range(n.get_child_count())]
        except Exception:
            return []

    def bounds(n):
        try:
            c = n.get_component()
            b = c.get_extents(Atspi.CoordType.SCREEN) if c else None
            return (b.x, b.y, b.width, b.height) if b else None
        except Exception:
            return None

    chrome_frames, other_big = [], []
    for app in children(Atspi.get_desktop(0)):
        try:
            aname = app.get_name() or "?"
        except Exception:
            continue
        is_chrome = any(k in aname.lower() for k in ("chrome", "chromium", "edge"))
        shell_infra = any(k in aname.lower() for k in
                          ("gnome-shell", "gjs", "ibus", "xdg-desktop-portal",
                           "update-notifier", "evolution-alarm"))
        for fr in children(app):
            b = bounds(fr)
            if not b or b[2] < 200 or b[3] < 200:
                continue
            try:
                cnt = fr.get_child_count()
            except Exception:
                cnt = "?"
            if is_chrome:
                chrome_frames.append((aname, (fr.get_name() or "")[:44], b, cnt))
            elif not shell_infra:
                other_big.append((aname, (fr.get_name() or "")[:30], b))

    if not chrome_frames:
        add("chrome windows", "warn", "no sizable Chrome frame found via AT-SPI",
            "is Chrome running in this session?")
    else:
        desc = "; ".join(f"{t or '(untitled)'} children={c}" for _, t, _, c in chrome_frames[:3])
        add("chrome windows", "ok", desc)

    def overlaps(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

    covering = [(n, t, b) for n, t, b in other_big
                if any(overlaps(b, cb) for _, _, cb, _ in chrome_frames)]
    add("covering windows", "warn" if covering else "ok",
        "; ".join(f"{n}: {t or '(untitled)'}" for n, t, b in covering) or "none",
        "v0.8.3+ raises Chrome automatically; if approvals still fail, bring "
        "Chrome to the front" if covering else None)
except Exception as exc:
    add("at-spi", "warn", f"unavailable: {exc!r}",
        "sudo apt install python3-gi gir1.2-atspi-2.0")

# --- orphaned test clients ----------------------------------------------------
orphans = len([p for p in sh(["pgrep", "-f", "chrome-devtools-mcp"]).split() if p])
add("stray mcp clients", "ok" if orphans <= 2 else "warn", str(orphans),
    None if orphans <= 2 else "pkill -f '[c]hrome-devtools-mcp' (they can queue extra attaches)")

# --- suggested next step ------------------------------------------------------
if standdown and (not approved or standdown > approved):
    next_step = ("engine stood down: the bubble is not on the visible workspace or is "
                 "fully covered - switch to the Chrome window and attach again")
elif raise_ and (not approved or raise_ > approved):
    next_step = ("a covered host was raised and retried; if this repeats without an "
                 "APPROVED line, something is fully covering Chrome")
elif not listening:
    next_step = "enable remote debugging at chrome://inspect/#remote-debugging"
elif active != "active":
    next_step = "start the service: systemctl --user enable --now yes-dev.service"
else:
    next_step = "looks healthy - watch tail -f ~/.local/share/YesDev/yes-dev.log during the next attach"

print("yes-dev-linux doctor\n" + "-" * 60)
for name, state, detail, action in findings:
    mark = {"ok": "OK  ", "warn": "WARN", "FAIL": "FAIL"}[state]
    print(f"[{mark}] {name:18} {detail}")
    if action:
        print(f"        -> {action}")
print("-" * 60)
print(f"next step: {next_step}")
sys.exit(0 if active == "active" else 1)
