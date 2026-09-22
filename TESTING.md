# Testing the Linux engine against a live prompt

The click path can only be proven against a REAL
"Allow remote debugging?" prompt. This file is the repeatable procedure
plus the field notes so far.

## Background

- Main Chrome runs with remote debugging toggled on at
  `chrome://inspect/#remote-debugging` (DevToolsActivePort mode, port
  9222). Attaching a CDP client (e.g. `chrome-devtools-mcp --autoConnect`
  + `list_pages`) raises the consent prompt; the call hangs mid-handshake
  until someone clicks Allow.
- Consent looks like **once per Chrome restart**, not per connection
  (matches the `pi` broker pattern in
  [chrome-devtools-mcp#825](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/825)):
  the first attach after a restart prompts, later ones sail through.

## Stage 1 — reproduce the hang (safe, no restart)

```bash
/usr/bin/python3 /tmp/consent-check.py   # autoConnect + list_pages, 12s window
```

- `RESPONDED` quickly → consent already granted; go to Stage 3.
- `HUNG` → a prompt is (probably) up. Go to Stage 2.

Record the trigger script (kept short on purpose):

```python
# spawn: npx -y chrome-devtools-mcp@latest --autoConnect (stdio)
# send: initialize -> notifications/initialized -> tools/call list_pages
# a hung call with no reply in 12s == consent gate (or a sick server;
# rule that out first: `ss -tlnp | grep 9222` must show chrome listening)
```

## Stage 2 — capture the dialog (the money step)

While the call hangs, in parallel:

```bash
/usr/bin/python3 ~/yes-dev-linux/watcher_linux.py --probe
```

What to record: frame titles + bounds + `children=` counts, and a
screenshot of every Chrome window. Compare against the no-prompt baseline
(all frames `children=1`, titled frames are tab titles).

**Field notes, 2026-09-22 (GNOME/Wayland, Chrome 153):**

- No frame ever carries the title `Allow remote debugging?` — the v0.1/v0.3
  title scan fires on nothing. The dialog is a Views bubble with an empty
  accessible name (same as the persistent untitled frames at
  `(135,25,799x88/298/218)`).
- New signal: the ACTIVE frame's `children=` count bumps **1 → 2 → 3**,
  one per pending attach, while `get_child_at_index` keeps returning
  `None` for the extras. Count deltas are detectable; the nodes are not
  actionable via AT-SPI.
- One `ydotool key 28` (Enter) moved the count 3 → 1. Approval efficacy
  NOT proven by that step (the test client's window had already expired).
- Screenshots during hangs showed no dialog on the two captured windows —
  capture ALL Chrome windows next time, and faster (the bubble may live
  only seconds if something else answers it).
- Keep stray `chrome-devtools-mcp` processes reaped
  (`pkill -f "[c]hrome-devtools-mcp"` — quoted to dodge self-match);
  35 orphans piled up during probing and muddy the water.

## Stage 3 — fresh prompt (needs a Chrome restart)

Consent resets on restart. This kills the main browser (tabs restore;
automation Chromes on other profiles are unaffected) — get explicit
approval first.

1. Arm the engine: `--enable-click` (service already runs armed), or a
   foreground `--observe` run for a no-touch rehearsal.
2. Note the active tab URL (misfire check later).
3. Restart Chrome (session restore on).
4. Trigger Stage 1. Expect: HUNG + count bump on the ACTIVE frame.
5. Watch `tail -f ~/.local/share/YesDev/yes-dev.log` for:
   `dialog candidate` → `host ACTIVE` → `APPROVED via ydotool:Enter`
   (`[ACTION]`), and the MCP call responding.
6. Misfire checks: active tab URL unchanged, no stray bookmark/download
   bubbles approved, no text entered anywhere.

## If autoConnect fast-fails with "Could not find DevToolsActivePort"

Seen once after manual Enter experimenting: file present, port listening,
toggle still `user-enabled: true`, yet discovery fails. Suspect stale
browser-endpoint ID in the file. Fix: toggle remote debugging OFF and ON
at `chrome://inspect/#remote-debugging`, then retry Stage 1.

## What success looks like

- Log shows `APPROVED via ...` with `[ACTION]`, total counter increments.
- The hung MCP call answers within ~1s of the log line.
- `--probe` during a later hang shows the same count-bump shape, served
  next sweep (dedupe) — i.e. N queued prompts approved one per sweep.
