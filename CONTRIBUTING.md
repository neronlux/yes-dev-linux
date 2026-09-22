# Contributing to yes-dev-linux

Thanks for helping. This project is small on purpose: one engine, three
files, no pip. The fastest useful contributions are **calibration
captures** (new Chrome layouts/locales/themes) and **field reports from
other desktops** (KDE, X11, Hyprland). Code is welcome too — read the
invariants below first.

## Repo layout

```
watcher_linux.py   the engine: AT-SPI detection, sweep loop, click path,
                   self-healing (v0.8). All CLI flags live in main().
auto_click.py      the visual click stack: portal screenshot, PIL button
                   finder, absolute uinput pointer. Has a small CLI:
                   --shot [DEST], --detect PNG, --click X Y.
platform_linux.py  paths, single-instance lock, logging setup.
tools/consent-check.py  the CDP trigger used by every live test.
tools/doctor.py    read-only health check + recovery suggestions
                   (service, debug port, covering windows, strays).
tests/test_selfheal.py  offline checks for the v0.8 self-healing logic.
TESTING.md         the full field record and repeatable test procedure.
```

## Dev setup

Use the **distro python** (`/usr/bin/python3`), never a venv/pip — the
engine depends on distro-shipped `python3-gi`:

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 python3-pil python3-evdev \
                python3-dbus
# write access to /dev/uinput for the clicker (udev rule + input group):
#   KERNEL=="uinput", MODE="0660", GROUP="input"
```

Verify the environment first (AT-SPI, portal, pointer, click delivery):

```bash
/usr/bin/python3 watcher_linux.py --selftest
```

Then run it:

```bash
/usr/bin/python3 watcher_linux.py --observe            # watch only, safe
/usr/bin/python3 watcher_linux.py --probe              # dump AT-SPI tree
/usr/bin/python3 watcher_linux.py --once --diagnostics # one timed sweep
```

**Never start a second long-running engine beside the service** — it
holds the single-instance lock and crash-loops the real one. Diagnostics
are `--once`/`--probe`, which bypass the lock on purpose.

## Testing

Two layers, both documented in [TESTING.md](TESTING.md):

1. **Offline (no desktop, no Chrome, safe anywhere):**

   ```bash
   tools/run-tests.sh                          # compile + both suites
   ```

   (or run `tests/test_selfheal.py` / `tests/test_finder.py` directly;
   both exit non-zero on failure)

   Covers the v0.8 self-healing: pointer retry (no latch), rebuild on
   size change, AT-SPI re-init after repeated failures. Run this before
   every PR; add a check when you touch that logic.

   ```bash
   /usr/bin/python3 tests/test_finder.py   # synthetic dialog images: 7/7
   ```

   Locks the button-finder contract (dark pair accepted; single,
   oversized, distant or misaligned clusters refused). Add a synthetic
   image when you touch the finder; capture real restyles with
   `auto_click.py --clusters shot.png`.

2. **Live (needs a GNOME/Wayland session + Chrome with remote
   debugging):** reproduce the hang with
   `tools/consent-check.py 12`, capture with `--probe`, and follow the
   Stage 1–3 procedure in TESTING.md. Chrome restarts kill the main
   browser — get the machine owner's explicit OK first.

## Calibration captures (most wanted)

The visual button finder is tuned on one layout: Chrome 153, dark
theme, English. If your prompt looks different, we need one capture
while the bubble is **up**:

```bash
/usr/bin/python3 auto_click.py --shot /tmp/shot.png
/usr/bin/python3 auto_click.py --detect /tmp/shot.png   # prints (x, y) or None
```

Open an issue with: the screenshot (crop personal content), the
`--detect` output, and your Chrome version, locale, and desktop
theme. `None` from `--detect` is exactly the data we need to extend the
finder (see `find_allow_button` in `auto_click.py`: blue-fill clusters,
two side-by-side buttons, rightmost = Allow).

Trigger a prompt any time with `tools/consent-check.py 12`.

## Code conventions

- Python 3 stdlib + gi/PIL/evdev/dbus from distro packages. No new
  dependencies, no pip, no build step.
- `/usr/bin/python3` shebangs; comments explain *why*, not *what*.
- The log contract is load-bearing: `[ACTION]` lines, `APPROVED via ...`,
  `engine started (...)`. Anything watching the log depends on them —
  do not rename.
- The self-healing pattern is: **acquire lazily, retry forever, warn
  once per outage.** New resources (screenshots, devices, buses) should
  follow it; a startup failure at boot time is normal, not fatal.

## Invariants a PR must not break

These are the safety gates. A change that weakens any of them will be
rejected regardless of other merit:

1. **Verify before logging.** `[ACTION]` is written only after the
   dialog is confirmed gone (ref identity, or child total back to base).
   Never rescan-absence, never screen geometry.
2. **Click only what the screenshot shows.** No blind coordinates, no
   keyboard fallbacks without focus knowledge (TESTING.md part 2/4 is
   the graveyard — read it before proposing one). Two bounded
   exceptions: the uncovered-corner raise click for a covered host
   (once per bubble, never in the dialog area), and window-management
   keys (Super+Up / Alt+Tab / Super+`) sent only after AT-SPI proves a
   Chrome window is active — never to drive the dialog itself.
3. **Off by default.** `--enable-click` stays opt-in; `--observe` always
   wins.
4. **Burst guard + attempt cap stay.** 3 attempts per cycle, `--cool-off-s`
   pause between cycles (default 30s), approvals only after verification.

## Release process (maintainers)

1. Bump `VERSION` and the `Status:` note in the `watcher_linux.py`
   docstring, and add a History bullet in README.
2. Run `tests/test_selfheal.py`, then a live regression (Stage 3) with
   the service restarted on the new code.
3. Commit, tag `vX.Y.Z`, push both.
4. Update the tap: new tarball URL + sha256 in
   `neronlux/homebrew-tap`'s `Formula/yes-dev-linux.rb`, then
   `brew reinstall neronlux/tap/yes-dev-linux && brew test ...`.

## Reporting bugs

Include: engine log (`~/.local/share/YesDev/yes-dev.log`), `--probe`
output during a hang, Chrome version, desktop (GNOME/X11/Wayland
compositor), and whether the service or a foreground run was active.
Mark anything personal before attaching screenshots.
