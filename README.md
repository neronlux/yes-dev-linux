# yes-dev-linux — Linux port of Yes, Dev

A Linux engine that watches for Chrome's **"Allow remote debugging?"**
consent dialog and approves it, so parallel automation clients stop
blocking on a human click.

Upstream: [dev-newb/yes-dev](https://github.com/dev-newb/yes-dev) (Windows +
macOS, MIT). This repo is the unofficial Linux port: same log contract
(`[ACTION]` lines), same option shapes, different guts (AT-SPI instead of
UI Automation / Accessibility API).

> **Status: v0.8 — auto-approval works, proven end to end, and the
> service is boot-safe.** The engine detects the consent bubble through
> AT-SPI, screenshots it through xdg-desktop-portal, finds the Allow
> button visually, and clicks it through a dedicated absolute uinput
> pointer. A genuinely hung CDP client then completes its handshake —
> measured at ~1 second from bubble to grant, unattended. Started at
> boot (before the desktop exists) it waits and re-acquires every
> dependency until the session is up. On GNOME/Wayland the button is
> invisible to every automation API, which is why the click is visual;
> details and the full field record are in [TESTING.md](TESTING.md).

## Why this exists

Chrome 144+ asks for consent on every attach to the remote-debugging
endpoint. The request to persist approval
([chrome-devtools-mcp#825](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/825))
was closed as not planned, and a dedicated throwaway profile
(`--user-data-dir`) is not the thing when you need your real, signed-in
browser. Clicking Allow is the only route — that is what this does.

## Architecture

```
                        +------------------+
                        |  Google Chrome   |  real profile, remote
                        |  (Views bubble   |  debugging on (:9222,
                        |  dialog host)    |  DevToolsActivePort mode)
                        +--------+---------+
                                 | AT-SPI scan, 250ms (detect)
                                 | portal screenshot (locate)
                                 | abs-uinput pointer (click)
                        +--------v---------+
                        | watcher_linux.py |--- yes-dev.log ([ACTION] lines,
                        |  detect -> locate|    1 MB rollover)
                        |  -> click ->     |
                        |  verify gone     |
                        +--------+---------+
                                 |
                        +--------v---------+
                        | systemd user unit|  Restart=always, starts at boot
                        | yes-dev.service  |  (linger + enabled)
                        +------------------+
```

Three seams stay text-only, as upstream designed: the engine's interface to
anything watching is the `[ACTION]` log line; the button location comes from
pixels; and the click is a kernel input device. A future tray (counter,
clouds, burst guard) works unchanged whichever engine is running.

## How one sweep works

```
scan Chrome app frames (AT-SPI, title match, max 1 level deep)
  |
  +-- no match  -->  geometry check: window child totals vs baseline
  |                    |
  |                    +-- no growth --> quiet (~50ms scan is the common case)
  |                    +-- growth with frame count unchanged + titled window
  |                                     --> bubble pending, remember base total
  |                                          |
  |                                          +-- observe?        log OBSERVE
  |                                          +-- not armed?      log WARN hint
  |                                          +-- armed: screenshot via portal
  |                                                -> rightmost blue button in
  |                                                   the lower dialog area
  |                                                   = Allow
  |                                                -> click via absolute pointer
  |                                                -> fresh screenshot: button
  |                                                   gone? YES -> [ACTION]
  |                                                   NO  -> retry (1s gap,
  |                                                          max 3, then 30s
  |                                                          cool-off, fresh
  |                                                          cycle + new pointer)
  |                                                -> button not in the shot?
  |                                                   raise host (one
  |                                                   uncovered-corner click)
  |                                                   and re-shoot; then
  |                                                   focus Chrome (held-Alt
  |                                                   MRU walk) and Super+Up
  |                                                   maximize (toggle-guarded),
  |                                                   retry; 2 normalizes,
  |                                                   then stand down
  |
  +-- match     -->  dedupe (2s window, geometry signature)
                       |
                       +-- buttons exposed?
                             YES -> AT-SPI Action press -> ref-identity verify -> [ACTION]
                             NO  -> log WARN (no safe activation on Wayland), stop
```

Rules the engine never breaks:

- **Verify before logging.** `[ACTION]` is written only after the dialog is
  confirmed gone — by AT-SPI ref identity for the Action path, by the
  window's child total returning to its pre-bubble base for the click path.
  Never by rescan-absence alone, and never by screen geometry.
- **Click only what the screenshot shows.** The Allow button is located in
  a live full-screen capture, so if the bubble is occluded or the layout is
  unexpected, detection finds no button pair and nothing is clicked. The
  one other click the engine may emit is a single uncovered-corner click
  to *raise the bubble's host window* when it is covered or inactive —
  never in the dialog area, never more than once per bubble.
- **Off by default.** Auto-click needs `--enable-click`; `--observe` always
  wins, and the burst guard pauses clicking if approvals spike.
- **One engine.** A second copy exits on the single-instance lock instead
  of double-clicking a dialog. `--once`/`--probe` bypass the lock on
  purpose (diagnostics must work beside the service).

## Requirements

No `pip install` — use the distro python (it ships `python3-gi`).
Auto-click needs the visual + input stack as distro packages:

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 python3-pil python3-evdev python3-dbus
```

(`python3-dbus` is dbus-python, used for the portal screenshot — the
plain `dbus` daemon package is not enough.)

and the user must be able to write `/dev/uinput` (a udev rule giving the
`input` group access, e.g. `KERNEL=="uinput", MODE="0660", GROUP="input"`,
plus membership in `input`), because the click is a dedicated absolute
pointer device, not a tool you have to install.

## Install & run

```bash
git clone https://github.com/neronlux/yes-dev-linux.git
cd yes-dev-linux
/usr/bin/python3 watcher_linux.py --observe                 # watch only
/usr/bin/python3 watcher_linux.py --enable-click            # auto-approve
/usr/bin/python3 watcher_linux.py --once                    # one sweep then exit
/usr/bin/python3 watcher_linux.py --probe                   # dump AT-SPI tree, exit
```

Verify the whole stack in five seconds (AT-SPI, portal screenshot,
pointer device, and a real click-delivery probe — briefly opens and
closes the clock popup):

```bash
/usr/bin/python3 watcher_linux.py --selftest
```

Or via Homebrew (Linuxbrew):

```bash
brew tap neronlux/tap
brew install yes-dev-linux
```

| Flag | What it does |
|---|---|
| `--observe` | Log only, never click. Always wins over `--enable-click`. |
| `--enable-click` | Arm auto-approval (screenshot → find Allow → click). Off by default. |
| `--once` | One sweep then exit (diagnostic, bypasses lock). |
| `--probe` | Dump the AT-SPI tree around Chrome, then exit. |
| `--selftest` | Check AT-SPI, portal screenshot, pointer and click delivery, then exit. |
| `--interval-ms` | Poll interval, default 250 (150/250/750 like upstream). |
| `--include-edge` | Also watch Microsoft Edge windows. |
| `--dialog-pattern` | Dialog title regex (default `^allow remote debugging\?$`). Localised Chrome? Start here. |
| `--approve-pattern` | Button label regex (default `^(allow\|approve)$`). Anchored so *Turn off in settings* is never hit. |
| `--burst-limit` | Pause clicks 60s after this many approvals/min (default 60, `0` disables). Same default as upstream, measured not guessed. |
| `--cool-off-s` | After 3 failed click attempts on one bubble, pause this long, then start a fresh 3-attempt cycle (default 30, `0` = old wait-forever). |
| `--visual-backstop-s` | While idle and armed, glance at the screen every N seconds for an Allow button the child-count missed (0 disables; default 5). |
| `--no-workspace-hunt` | Disable hunting workspaces for a hidden bubble (default: hunt up to 3, then switch back). |
| `--restart-chrome-on-stuck` | Opt-in: when clustered stand-downs suggest a stuck consent queue, restart the Chrome holding :9222 (tabs restore) once per 30 min. Off by default — disruptive. |
| `--exit-with-parent` | Exit when the launching process goes away (for supervised runs). |
| `--diagnostics` | Log scan timing every 5s. |
| `--log-path` | Default `~/.local/share/YesDev/yes-dev.log`. |

## Staying up: reboot, crashes, updates

```
boot -> user manager (linger) -> default.target -> yes-dev.service
  -> engine loop (250ms) --crash--> systemd Restart=always (5s)
```

The service starts at boot **before** the desktop exists (on this VM the
GNOME session comes up ~11s after the user manager), so nothing is
acquired once-and-forgotten:

- the absolute pointer is rebuilt every 15s until Mutter and the portal
  answer, and again whenever the screenshot size changes (RDP resize);
- AT-SPI is re-initialised after 20 consecutive failed scans;
- a failed screenshot only warns for that bubble; the next sweep retries.

A reboot therefore recovers without a manual restart. Use a **systemd
user service, not cron**: the engine needs the session D-Bus (AT-SPI,
portal), which cron does not have.

Setup (already done on the author's VM; repeat anywhere):

```bash
# 1. persistent copy outside scratch space
git clone https://github.com/neronlux/yes-dev-linux.git ~/yes-dev-linux

# 2. unit file at ~/.config/systemd/user/yes-dev.service (adjust the
#    /home paths):
cat > ~/.config/systemd/user/yes-dev.service <<'EOF'
[Unit]
Description=Yes, Dev Linux engine (Chrome remote-debugging auto-approver)
Documentation=https://github.com/neronlux/yes-dev-linux
After=dbus.socket

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/USER/yes-dev-linux/watcher_linux.py --enable-click
WorkingDirectory=/home/USER/yes-dev-linux
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 3. activate
systemctl --user daemon-reload
systemctl --user enable --now yes-dev.service

# 4. reboot survival needs lingering (usually already on):
loginctl enable-linger $USER
```

Verify:

```bash
systemctl --user is-active yes-dev.service     # active
systemctl --user is-enabled yes-dev.service    # enabled
loginctl show-user $USER -p Linger              # Linger=yes
tail -f ~/.local/share/YesDev/yes-dev.log      # "engine started ... (ready)"
```

After an actual reboot, confirm in the log (within a minute of login):
`engine started`, then `auto-click ready: absolute pointer (W, H)`, then
`untitled bubble candidate` + `APPROVED` on the first attach. A postboot
self-check (`yes-dev-postboot.service`) also runs once per boot —
relaunching Chrome with session restore if needed, firing a test attach
and writing the verdict to `~/.local/share/YesDev/postboot.log`;
`tools/doctor.py` surfaces the latest verdict.

Notes:

- Before your desktop session exists AT-SPI has no tree to read — sweeps
  log scan errors and retry; harmless, works after login.
- Updates: `cd ~/yes-dev-linux && git pull && systemctl --user restart yes-dev.service`.

## Troubleshooting & recovery

First thing to try when anything looks off: `watcher_linux.py
--selftest` proves the whole stack (AT-SPI, screenshot, pointer, click
delivery) in one command. Then the deeper check — read-only, safe any
time, works beside the service. It reports the service, Chrome's debug port, `/dev/uinput` and
the portal, lock state, AT-SPI visibility, windows covering Chrome,
stray clients, the engine's live state file, and its last actions, then
prints the suggested next step:

```bash
/usr/bin/python3 tools/doctor.py
```

Every sweep also writes `~/.local/share/YesDev/state.json` (pid,
approvals, pointer age, pending bubbles, last error and last action) -
`doctor` prints it, scripts can read it directly.

Manual playbook, in escalation order (all of it was walked through live
in TESTING.md's v0.8.3 field notes):

1. **Usually nothing.** v0.8.3+ raises a covered or inactive Chrome
   window itself, retries swallowed first clicks, cools off between
   cycles and rebuilds the pointer. A stubborn prompt can take ~30s.
   Watch it work:

   ```bash
   tail -f ~/.local/share/YesDev/yes-dev.log
   ```

2. **`no Allow button visible 3x - leaving this window alone`** — the
   bubble is not on the visible workspace, or is fully covered by
   another window. Switch to the Chrome window showing the prompt
   (window title visible), then attach again — a new request re-arms
   the engine. This is the one case it cannot fix itself.

3. **`clicked an uncovered corner ... to raise the host window`** —
   automatic: another app's window was covering Chrome, the engine
   raised it and is retrying. Nothing to do. If it repeats without an
   `APPROVED` line, something is fully covering Chrome: bring it to the
   front.

4. **Attaches hang, the log is silent, no bubble ever appears** — the
   browser's consent queue is stuck (pending requests leaked by killed
   clients). Reset it, least destructive first:
   - toggle remote debugging **OFF and ON** at
     `chrome://inspect/#remote-debugging` (no restart), or
   - restart Chrome (tabs restore). For a headless/RDP session:

     ```bash
     pkill -TERM -f '^/opt/google/chrome/chrome ?$'   # only the browser root
     setsid -f env XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 \
       DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
       /usr/bin/google-chrome --ozone-platform=wayland --restore-last-session
     ```

5. **Stray test clients** (`pgrep -f chrome-devtools-mcp` shows piles) —
   terminated clients can survive as orphans and queue extra attaches:

   ```bash
   pkill -f "[c]hrome-devtools-mcp"
   ```

## Read this before anything else

The prompt exists to stop a malicious local program from seizing your
signed-in browser. Auto-approving means **any** local process that attaches
gets in, not just the ones you started — that is the trade this tool makes,
the same one upstream's tray makes. This port ships with:

- `--enable-click` off by default, and `--observe` always wins over it;
- a burst guard (pause clicks 60s after 60 approvals/min, same default as
  upstream);
- at most 3 click attempts per cycle, then a `--cool-off-s` pause
  (default 30s) before a fresh cycle — never hammering;
- a click that only ever fires on a button found in a live screenshot,
  so an occluded or unexpected dialog is left untouched — plus at most
  one uncovered-corner click per bubble to raise a covered host window;
- synthetic keyboard used only for window management (Super+Up maximize,
  Alt+Tab / Super+` focus walking), never before AT-SPI proves a Chrome
  window is active, and never to drive the dialog itself;
- clicks pause entirely while the session is locked;
- the idle visual backstop only fires for a button in the dialog region
  (15-85% x, 20-80% y) seen twice at the same spot, and still verifies
  before logging; `--visual-backstop-s 0` turns it off;
- the only automation that kills anything is `--restart-chrome-on-stuck`,
  off by default, rate-limited to once per 30 min, and it logs loudly
  before terminating the browser (tabs restore).

Run `--observe` first, and prefer a throwaway `--user-data-dir` profile
wherever you do not need real browser state.

## Known limitations

- **Visual button detection**: the Allow button must appear as one of two
  blue buttons side by side in the lower half of the screenshot (Chrome
  153 dark theme, measured). A drastically restyled dialog, a very light
  theme, or a localised Chrome may not match — then the engine logs
  `Allow button not found` and leaves the bubble. Calibrate with
  `auto_click.py --detect shot.png` on a real prompt.
- **Invisible-snapshot race**: the first click is occasionally swallowed
  while the bubble animates in; the engine retries after ~1s. A bubble
  that survives a full 3-attempt cycle cools off (`--cool-off-s`, 30s
  default) and then starts a fresh cycle, so a stuck bubble self-heals
  instead of needing a human.
- **English Chrome only** (same as upstream): matched by title string —
  but `--dialog-pattern`/`--approve-pattern` are exposed flags, so a
  localised build can be attempted without code changes.
- No tray, no stay-on timer, no ask-first burst dialog yet. The engine
  refuses double-run via the lock and `--exit-with-parent` is available
  for supervised launches.
- **Covered or inactive host window**: only the active window receives
  clicks on Wayland. If another app covers Chrome, or the shell holds
  focus, the engine raises the host (uncovered-corner click), focuses a
  Chrome window (held-Alt MRU walk), and maximizes it with Super+Up —
  guarded, because Ubuntu-style GNOME maps Super+Up to a toggle: it is
  only sent when the window does not already fill the screen, and the
  area is re-checked, pressing once more if the toggle had restored a
  tiled window. Two normalize attempts per bubble, then it stands down
  and logs it — bring the Chrome window forward and a new attach
  re-arms it.
- Untested: a bubble host on another workspace (raise clicks can only
  reach the current workspace; the stand-down covers it).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, the offline test
suite, calibration captures, code conventions, and the release process.

Hit a real prompt? While it is up, run
`/usr/bin/python3 auto_click.py --shot /tmp/shot.png` and
`--detect /tmp/shot.png`, and open an issue with the result and your
Chrome version — that is how new layouts get calibrated. Reliable
trigger: `tools/consent-check.py` (attach via
`npx -y chrome-devtools-mcp@latest --autoConnect` and call `list_pages`;
the call hangs mid-handshake while the prompt is up).

## History

- **v0.8.13** — hygiene release: GitHub "Calibration capture" issue
  template (cluster data, --detect output, environment, screenshot) so
  restyle reports arrive in a usable shape; .editorconfig; brew caveats
  now lead with the `--selftest` verification step. Engine unchanged
  since v0.8.12.
- **v0.8.12** — cheap-win batch: the visual backstop gains unit tests
  (region prior + candidate creation), a late child bump for a bubble a
  visual candidate already owns is ignored (no double-serve), observe
  mode logs backstop sightings (throttled, never clicks), the doctor
  surfaces the latest postboot verdict (`postboot check` row), the
  engine takes `--version`, and `tools/run-tests.sh` runs the compile
  check plus both offline suites in one command.
- **v0.8.11** — docs release: TESTING.md gains a "Verified use cases -
  live ledger" (every capability with an honest LIVE / UNIT / OPEN
  status and its evidence). Engine unchanged since v0.8.10.
- **v0.8.10** — visual backstop: the child-count bump can be missed when
  a leaked node clears exactly as a new attach bumps (the count returns
  to the value already recorded - observed live post-reboot, engine
  blind to a visible prompt). While idle and armed the engine now
  glances at the screen every 5s (`--visual-backstop-s`), and a button
  found twice at the same spot in the dialog region becomes a candidate
  through the normal click/verify flow. LIVE-PROVEN: caught the missed
  bubble ~2s after restart, approved it with the leak note, client
  unblocked. Totals stay unknown for backstop bubbles (base 0), so the
  visual verify decides.
- **v0.8.9** — workspace hunting + calibration: when the bubble is not
  reachable on the current workspace, the engine switches forward (up to
  3 workspaces, `--no-workspace-hunt` to disable), finds it, clicks and
  switches everything back — live-proven on a bubble parked on ws2
  (approved first click, client unblocked, workspace restored). Leftover
  switches from a crashed run are restored on startup (state.json).
  Multi-monitor setups are now detected and warned about (coordinates
  are only validated single-monitor). `auto_click.py --clusters`
  dumps calibration cluster data, and tests/test_finder.py locks the
  finder contract with synthetic dark/light/ambiguous images (7/7).
- **v0.8.8** — host-targeted focusing and a self-test: the normalize
  ladder's window cycling now checks the ACTIVE window's rect against
  the tracked bubble host and keeps cycling until they match, instead
  of maximizing whichever Chrome window happened to be active (a real
  failure mode with two Chrome windows); `--selftest` proves AT-SPI,
  portal screenshot, pointer creation and click delivery in one
  command (briefly opens the clock popup), so installs can be verified
  without waiting for a real prompt.
- **v0.8.7** — operator-visible edges: the engine pauses all clicking
  while the session is locked (org.gnome.ScreenSaver, cached 5s) and
  logs it once a minute; `tools/doctor.py` now also checks
  `/dev/uinput` writability, the xdg-desktop-portal backend, and the
  lock state; and an opt-in `--restart-chrome-on-stuck` turns the stuck-
  queue hint into an action (restarts the Chrome holding :9222, tabs
  restore, once per 30 min). Edge-case matrix gains row 25 (locked).
- **v0.8.6** — loop hardening: two-way verification (either the fresh
  screenshot no longer showing the button *or* the child total dropping
  counts as approved - kills screenshot-lag false negatives); the
  normalize ladder runs 3 rounds including Super+` same-app cycling;
  reason-coded stand-downs (`reason=no-focus`, `no-visible-button`,
  `pointer-unavailable`) with a stuck-queue remedy hint after clustered
  stand-downs; a machine-readable `state.json` (pid, approvals, pointer
  age, pending bubbles and their stage, last error/action, scan time)
  that `tools/doctor.py` surfaces. The full edge-case matrix - 24
  conditions with detection, automatic response and residual - is in
  TESTING.md.
- **v0.8.5** — "force Chrome to the front and full screen, then click":
  when the bubble is not visible the engine normalizes the window —
  uncovered-corner raise, held-Alt MRU focus walk (single Alt+Tabs
  ping-pong and never reach a third window), Super+Up maximize — then
  retries, with geometry migration following a moved or maximized
  window. Guarded: focus is AT-SPI-verified before any key is sent;
  Super+Up is skipped when the window already fills the screen and
  re-pressed once if the toggle restored a tiled window; two normalize
  attempts per bubble, then stand-down. Validated live: the toggle
  guard, focus walk, maximize and migration all fired in real runs.
- **v0.8.4** — `tools/doctor.py` (read-only health check that names the
  covering window, the stuck queue, or strays and prints the next step)
  and a README "Troubleshooting & recovery" playbook: the covered-window
  raise, the workspace stand-down, the consent-queue reset (toggle or
  restart, with headless commands), and orphan reaping. Docs and tools
  only; the engine is unchanged.
- **v0.8.3** — reliability batch, after a live incident where a second
  app's window covered Chrome for an hour: clicks vanish on a covered or
  inactive window, so now (1) the engine raises the host with one
  uncovered-corner click and re-shoots when no button is visible, and
  retries raise the host after a failed click; (2) approval is verified
  visually (fresh screenshot no longer shows the button) — child totals
  leak with queued attaches and were lying; (3) 3 consecutive
  no-button looks stand the engine down instead of cycling forever on a
  phantom; (4) cool-off cycles re-arm the attempt counter correctly.
  Validated live: raise-click revealed a hidden bubble; full e2e after
  (candidate → click → APPROVED, client answered).
- **v0.8.2** — cool-off + retry cycles: a bubble that survives 3
  attempts no longer waits forever for it to clear; the engine pauses
  `--cool-off-s` (default 30s) and starts a fresh cycle. Found live:
  a bubble whose clicks were all swallowed sat stuck after the old
  permanent backoff.
- **v0.8.1** — docs release (CONTRIBUTING.md, tests/, tools/ in the
  tarball; formula caveats corrected to python3-dbus). Engine unchanged.
- **v0.8** — boot-safe: clicker creation retried every 15s (a latched
  failure would have booted dead when the service started before the
  desktop), rebuild on screenshot-size change (RDP resize), AT-SPI
  re-init after repeated scan failures. Verified live after restart.
- **v0.7** — auto-approval works end to end: portal screenshot → PIL finds
  the Allow button → dedicated absolute uinput pointer clicks it →
  verified by the window's child total returning to base → `[ACTION]`.
  Retry model for swallowed first clicks; `--enable-click` arms it.
  Supersedes the v0.6 "no synthetic activation" verdict — that verdict
  was wrong: the failures were a relative input device (accel-warped)
  plus coordinate arithmetic, not Chrome rejecting the input.
- **v0.6** — synthetic activation removed (incorrectly concluded nothing
  reaches the secure Views bubble); honest watchdog (detect, log, count
  pending), AT-SPI Action path retained for exposing stacks.
- **v0.5** — geometry-keyed bubble detection (survives tab-title churn).
- **v0.4** — untitled-bubble detection via child-count bump.
- **v0.3** — single-instance lock, minimal burst guard (`--burst-limit`),
  `atspi=ok` startup check, `--observe` documented as winning over
  `--enable-click`, systemd persistence + this README.
- **v0.2** — gated keyboard fallback (`--enable-click`, ACTIVE-frame
  check, rescan verify), `--probe`, `--dialog/approve-pattern` flags.
- **v0.1** — AT-SPI observe-only detection, upstream log contract.
