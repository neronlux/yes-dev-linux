# yes-dev-linux — Linux port of Yes, Dev

A Linux engine that watches for Chrome's **"Allow remote debugging?"**
consent dialog and approves it, so parallel automation clients stop
blocking on a human click.

Upstream: [dev-newb/yes-dev](https://github.com/dev-newb/yes-dev) (Windows +
macOS, MIT). This repo is the unofficial Linux port: same log contract
(`[ACTION]` lines), same option shapes, different guts (AT-SPI instead of
UI Automation / Accessibility API).

> **Status: v0.6 watchdog.** Detection is proven live; synthetic
> activation was attempted exhaustively and removed — on GNOME/Wayland no
> programmatic input reaches the secure Views bubble (details in
> TESTING.md), so the engine detects, logs, and counts pending prompts
> instead of pretending to click. If your stack exposes the button
> (e.g. X11), the AT-SPI Action path still approves it.

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
                                 | AT-SPI (read-only scan, 250ms)
                        +--------v---------+
                        | watcher_linux.py |--- yes-dev.log (candidates,
                        |  detect -> log   |    1 MB rollover; [ACTION]
                        |  (no input)      |    only if a button is ever
                        +--------+---------+    exposed and pressed)
                                 |
                        +--------v---------+
                        | systemd user unit|  Restart=always, starts at boot
                        | yes-dev.service  |  (linger + enabled)
                        +------------------+
```

Upstream's seams are plain text (`tray -> engine -> overlay`), which is
why a second platform drops in without touching shared logic. This port
keeps the one seam that matters: the engine's entire interface is the
`[ACTION]` log line, so a future tray (counter, clouds, burst guard) works
unchanged whichever engine is running.

## How one sweep works

```
scan Chrome app frames (AT-SPI, title match, max 1 level deep)
  |
  +-- no match  -->  geometry check: window child totals vs baseline
  |                    |
  |                    +-- no growth --> quiet (~50ms scan is the common case)
  |                    +-- growth with frame count unchanged + titled window
  |                                     --> untitled bubble candidate, log WARN
  |
  +-- match     -->  dedupe (2s window, geometry signature)
                       |
                       +-- buttons exposed?
                             YES -> AT-SPI Action press -> ref-identity verify -> [ACTION]
                             NO  -> log WARN (no safe activation on Wayland), stop
```

Rules the engine never breaks:

- **Verify before logging.** `[ACTION]` is written only after the dialog
  is confirmed gone by AT-SPI ref identity. Never by rescan-absence
  (which once logged two false approvals) and never by screen geometry:
  Chrome draws a queued successor exactly where the last prompt was.
- **No synthetic input.** The engine never moves the mouse, never steals
  focus, never sends keys — v0.3–v0.5 proved none of it reaches the
  secure Views bubble, and blind Enter once risked Turn-off.
- **One engine.** A second copy exits on the single-instance lock instead
  of double-pressing a dialog. `--once`/`--probe` bypass the lock on
  purpose (diagnostics must work beside the service).

## Requirements

```bash
sudo apt install python3-gi gir1.2-atspi-2.0   # AT-SPI via GObject Introspection
```

No `pip install` — use the distro python (it ships `python3-gi`).

## Install & run

```bash
git clone https://github.com/neronlux/yes-dev-linux.git
cd yes-dev-linux
/usr/bin/python3 watcher_linux.py --observe    # log dialogs, never click
/usr/bin/python3 watcher_linux.py --once       # one sweep then exit
/usr/bin/python3 watcher_linux.py --probe      # dump the AT-SPI tree, exit
```

Or via Homebrew (Linuxbrew):

```bash
brew tap neronlux/tap
brew install yes-dev-linux
```

| Flag | What it does |
|---|---|
| `--observe` | Log only, never touch anything (default posture). |
| `--once` | One sweep then exit (diagnostic, bypasses lock). |
| `--probe` | Dump the AT-SPI tree around Chrome, then exit. |
| `--interval-ms` | Poll interval, default 250 (150/250/750 like upstream). |
| `--include-edge` | Also watch Microsoft Edge windows. |
| `--dialog-pattern` | Dialog title regex (default `^allow remote debugging\?$`). Localised Chrome? Start here. |
| `--approve-pattern` | Button label regex (default `^(allow\|approve)$`). Anchored so *Turn off in settings* is never hit. |
| `--burst-limit` | Pause AT-SPI approvals 60s after this many/min (default 60, `0` disables). Same default as upstream, measured not guessed. |
| `--exit-with-parent` | Exit when the launching process goes away (for supervised runs). |
| `--diagnostics` | Log scan timing every 5s. |
| `--log-path` | Default `~/.local/share/YesDev/yes-dev.log`. |

## Staying up: reboot, crashes, updates

```
boot -> user manager (linger) -> default.target -> yes-dev.service
  -> engine loop (250ms) --crash--> systemd Restart=always (5s)
```

Setup (already done on the author's VM; repeat anywhere):

```bash
# 1. persistent copy outside scratch space
git clone https://github.com/neronlux/yes-dev-linux.git ~/yes-dev-linux
# 2. unit file at ~/.config/systemd/user/yes-dev.service:
#    ExecStart=/usr/bin/python3 /home/USER/yes-dev-linux/watcher_linux.py --observe
systemctl --user daemon-reload
systemctl --user enable --now yes-dev.service
# 3. reboot survival needs lingering (usually already on):
loginctl enable-linger $USER
```

Verify:

```bash
systemctl --user is-active yes-dev.service     # active
tail -f ~/.local/share/YesDev/yes-dev.log     # engine started ...
```

Notes:

- Before your desktop session exists (boot, pre-login) AT-SPI has no
  tree to read — sweeps log scan errors and retry; harmless, works after
  login. `Restart=always` covers crashes; the single-instance lock covers
  double-starts.
- Updating: `cd ~/yes-dev-linux && git pull && systemctl --user restart yes-dev.service`.

## Read this before anything else

The prompt exists to stop a malicious local program from seizing your
signed-in browser. This port does not auto-approve on Wayland (nothing
reaches the bubble — proven, see TESTING.md), so it cannot reduce your
protection today; it watches and logs. If a future stack exposes the
button, the AT-SPI path approves with the same two mitigations upstream
ships in its tray (stay-on window, burst guard — minimal silent versions
here), so:

- run `--observe` first and capture a live prompt with `--probe`,
- prefer a throwaway `--user-data-dir` profile wherever you do not need
  real browser state.

## Known limitations

- **No synthetic activation on Wayland** (verdict, not gap): AT-SPI
  exposes zero objects (Collection: zero buttons), uinput pointer clicks
  land everywhere except the secure bubble, blind Enter risks Turn-off
  (focus is unpredictable), and rescan-absence can't tell approval from
  withdraw. The engine is an honest watchdog until that changes.
- **English Chrome only** (same as upstream): matched by title string —
  but `--dialog-pattern`/`--approve-pattern` are exposed flags, so a
  localised build can be attempted without code changes.
- No tray, no stay-on timer, no ask-first burst dialog yet. The engine
  refuses double-run via the lock and `--exit-with-parent` is available
  for supervised launches.

## Contributing captures

Hit a real prompt? While it is up, run
`/usr/bin/python3 watcher_linux.py --probe` and open an issue with the
(redacted) output — that is how the click path gets proven. Reliable
trigger: restart Chrome, then attach once via
`npx -y chrome-devtools-mcp@latest --autoConnect` and call `list_pages`
(the call hangs mid-handshake while the prompt is up).

## History

- **v0.6** — synthetic activation removed (verdict: nothing reaches the
  secure Views bubble); honest watchdog (detect, log, count pending),
  AT-SPI Action path retained for exposing stacks.
- **v0.5** — geometry-keyed bubble detection (survives tab-title churn).
- **v0.4** — untitled-bubble detection via child-count bump.
- **v0.3** — single-instance lock, minimal burst guard (`--burst-limit`),
  `atspi=ok` startup check, `--observe` documented as winning over
  `--enable-click`, systemd persistence + this README.
- **v0.2** — gated keyboard fallback (`--enable-click`, ACTIVE-frame
  check, rescan verify), `--probe`, `--dialog/approve-pattern` flags.
- **v0.1** — AT-SPI observe-only detection, upstream log contract.
