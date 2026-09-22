# yes-dev-linux — Linux port of Yes, Dev

A Linux engine that watches for Chrome's **"Allow remote debugging?"**
consent dialog and approves it, so parallel automation clients stop
blocking on a human click.

Upstream: [dev-newb/yes-dev](https://github.com/dev-newb/yes-dev) (Windows +
macOS, MIT). This repo is the unofficial Linux port: same log contract
(`[ACTION]` lines), same option shapes, different guts (AT-SPI instead of
UI Automation / Accessibility API).

> **Status: v0.4.** Detection covers titled dialogs AND untitled
> Wayland bubbles (child-count bump on the host frame). Clicking is gated
> three ways (ACTIVE-frame check, single-instance lock, burst guard) but a
> full live-prompt approval is still **unproven** — run `--observe` first
> and capture one with `--probe`. See *Known limitations*.

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
                        | watcher_linux.py |--- yes-dev.log ([ACTION] lines,
                        |  detect -> gate  |    1 MB rollover)
                        |  -> act -> verify|
                        +--------+---------+
                                 | only when gated AND no AT-SPI button:
                                 | ydotool key Enter (needs ydotoold)
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
  +-- no match  -->  quiet (the common case costs one ~50ms scan)
  |
  +-- match     -->  dedupe (2s window, geometry signature)
                       |
                       +-- buttons exposed?
                       |     YES -> AT-SPI Action press -> verify gone -> [ACTION]
                       |     NO  -> children=0 candidate (normal on Wayland):
                       |              --observe?            log OBSERVE, stop
                       |              --enable-click off?   log WARN, stop
                       |              frame not ACTIVE?     refuse, log WARN, stop
                       |              burst-paused?         log WARN, stop
                       |              else: ydotool Enter -> rescan gone? -> [ACTION]
```

Rules the engine never breaks:

- **Verify before logging.** `[ACTION]` is written only after the dialog
  is confirmed gone — by AT-SPI ref identity for the Action path, by
  rescan for the keyboard path. Never by screen geometry: Chrome draws a
  queued successor exactly where the last prompt was.
- **Never moves the mouse, never steals focus.** AT-SPI Action and
  ydotool-key need neither.
- **--observe always wins.** Passing `--observe --enable-click` together
  runs fully observe-only (the flags are ANDed, not ORed).
- **One engine.** A second copy exits on the single-instance lock instead
  of double-pressing a dialog. `--once`/`--probe` bypass the lock on
  purpose (diagnostics must work beside the service).

## Requirements

```bash
sudo apt install python3-gi gir1.2-atspi-2.0   # AT-SPI via GObject Introspection
```

No `pip install` — use the distro python (it ships `python3-gi`).
For the experimental keyboard fallback only: `ydotoold` running
(`ps aux | grep ydotoold`; Ubuntu ships `ydotool`).

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
| `--observe` | Master safety: log only, never click. Overrides `--enable-click`. |
| `--enable-click` | Arm the experimental keyboard fallback (default off). |
| `--once` | One sweep then exit (diagnostic, bypasses lock). |
| `--probe` | Dump the AT-SPI tree around Chrome, then exit. |
| `--interval-ms` | Poll interval, default 250 (150/250/750 like upstream). |
| `--include-edge` | Also watch Microsoft Edge windows. |
| `--dialog-pattern` | Dialog title regex (default `^allow remote debugging\?$`). Localised Chrome? Start here. |
| `--approve-pattern` | Button label regex (default `^(allow\|approve)$`). Anchored so *Turn off in settings* is never hit. |
| `--burst-limit` | Pause clicks 60s after this many approvals/min (default 60, `0` disables). Same default as upstream, measured not guessed. |
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
#    ExecStart=/usr/bin/python3 /home/USER/yes-dev-linux/watcher_linux.py --enable-click
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

## Read this before you arm the clicker

The prompt exists to stop a malicious local program from seizing your
signed-in browser. Auto-approving lets **any** local process in, not just
yours. Upstream's tray adds a stay-on window and an ask-first burst guard;
this port has a silent minimal guard (pause 60s past the per-minute limit)
and no tray yet, so:

- run `--observe` first and capture a live prompt with `--probe`,
- keep it supervised until the click path is proven against YOUR Chrome
  build,
- prefer a throwaway `--user-data-dir` profile wherever you do not need
  real browser state.

## Known limitations

- **Wayland frames expose zero AT-SPI children** (verified: Chrome 153,
  GNOME/Wayland, every frame `child_count 0`). The AT-SPI button path
  therefore rarely fires; the gated keyboard fallback carries the load
  until a live prompt is captured and characterised.
- **English Chrome only** (same as upstream): matched by title string —
  but `--dialog-pattern`/`--approve-pattern` are exposed flags, so a
  localised build can be attempted without code changes.
- **Enter-is-default assumption**: disproven live (initial focus is on
  **Cancel**, and blind Enter did nothing). The engine therefore never
  sends bare Enter; a future clicker must reach Allow by coordinates or
  by Tab-walking from a verified start — check `yes-dev.log` for
  `APPROVED` vs `FAILED` lines.
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

- **v0.4** — untitled-bubble detection: track titled-frame child-count
  baselines, flag +1 bumps (field-verified per pending attach), same
  gating, verify by count returning to baseline. TESTING.md procedure.
- **v0.3** — single-instance lock, minimal burst guard (`--burst-limit`),
  `atspi=ok` startup check, `--observe` documented as winning over
  `--enable-click`, systemd persistence + this README.
- **v0.2** — gated keyboard fallback (`--enable-click`, ACTIVE-frame
  check, rescan verify), `--probe`, `--dialog/approve-pattern` flags.
- **v0.1** — AT-SPI observe-only detection, upstream log contract.
