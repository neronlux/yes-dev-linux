# yes-dev-linux — Linux port of Yes, Dev

A Linux engine that watches for Chrome's **"Allow remote debugging?"**
consent dialog and approves it, so parallel automation clients stop
blocking on a human click.

Upstream: [dev-newb/yes-dev](https://github.com/dev-newb/yes-dev) (Windows +
macOS, MIT). This repo is the unofficial Linux port: same log contract
(`[ACTION]` lines), same option shapes, different guts (AT-SPI instead of
UI Automation / Accessibility API).

> **Status: v0.2 experimental.** Detection works. The click path is
> unproven against a live prompt — keep `--observe` until you have captured
> one with `--probe`. See *Known limitations*.

## Why this exists

Chrome 144+ asks for consent on every attach to the remote-debugging
endpoint. The request to persist approval
([chrome-devtools-mcp#825](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/825))
was closed as not planned, and a dedicated throwaway profile
(`--user-data-dir`) is not the thing when you need your real, signed-in
browser. Clicking Allow is the only route — that is what this does.

## How it works

- **Detect** via AT-SPI: scan Chrome application frames for a title
  matching `^allow remote debugging\?$` (fast, no screenshots, page DOM
  never walked — same perf discipline as the macOS engine).
- **Approve**, in order of preference:
  1. AT-SPI Action (`press`/`click`) on the Allow button, when exposed.
  2. Experimental fallback (`--enable-click`): if no button is exposed
     *and* the host frame is the ACTIVE window, press Enter via
     `ydotool` (needs `ydotoold` running). Refused when Chrome is in the
     background, so Enter can never land in your editor.
- **Verify** before logging: `[ACTION]` is written only after the dialog
  is confirmed gone (identity of the AT-SPI refs, never screen geometry —
  Chrome queues prompts at identical coordinates).
- **Never** moves the mouse. No focus stealing.

## Install

```bash
sudo apt install python3-gi gir1.2-atspi-2.0
# optional, only for the experimental keyboard fallback:
# ydotoold must be running (it is on most dev boxes; check `ps aux | grep ydotoold`)
```

No `pip install` needed — uses the distro python (it ships `python3-gi`):

```bash
/usr/bin/python3 watcher_linux.py --observe    # log dialogs, never click
/usr/bin/python3 watcher_linux.py --once       # one sweep then exit
/usr/bin/python3 watcher_linux.py --probe      # dump the AT-SPI tree, exit
/usr/bin/python3 watcher_linux.py --observe --enable-click  # experimental click path
```

## Files

| Path | Role |
|---|---|
| `watcher_linux.py` | The engine. Runs standalone. |
| `platform_linux.py` | Data dir (`~/.local/share/YesDev`), single-instance lock, XDG autostart. |
| `LICENSE` | MIT (upstream), this port likewise. |

Logs: `~/.local/share/YesDev/yes-dev.log` (approvals, `[ACTION]` lines the
tray tails) — 1 MB rollover, same as upstream.

## Read this before you run it with clicking on

The prompt exists to stop a malicious local program from seizing your
signed-in browser. Auto-approving lets **any** local process in, not just
yours. Upstream ships a stay-on window and burst guard in the tray; this
port has neither yet (no tray), so:

- run `--observe` first,
- pass `--exit-with-parent` when supervising it,
- keep sweeps supervised until the click path is proven against YOUR
  Chrome build, and
- prefer a throwaway `--user-data-dir` profile wherever you do not need
  real browser state.

## Known limitations

- **Wayland frames expose zero AT-SPI children** (verified: Chrome 153,
  GNOME/Wayland, every frame `child_count 0`). The AT-SPI button path
  therefore rarely fires; the keyboard fallback carries the load until a
  live prompt is captured and characterised.
- **English Chrome only** (same as upstream): matched by title string.
- **Enter-is-default assumption**: the fallback assumes Allow is the
  default button. If your Chrome build orders buttons differently, the
  fallback may dismiss without approving — check `yes-dev.log`.
- No tray, no burst guard, no stay-on timer yet. The engine refuses to
  outlive its parent with `--exit-with-parent`; use it.

## Contributing captures

Hit a real prompt? Run `/usr/bin/python3 watcher_linux.py --probe` while
it is up and open an issue with the (redacted) output — that is how the
click path gets proven.
