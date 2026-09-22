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
**Field notes, 2026-09-22 (GNOME/Wayland, Chrome 153), part 2 — first live fire:**

- v0.5 detection PROVEN: `untitled bubble candidate (chrome)
  window=0,0,1213x768 children 5->6` fired on a real hung attach.
- First photograph of the Linux dialog: centered modal, three buttons
  `[Turn off in settings] [Cancel] [Allow]`, Allow rightmost/primary.
  Initial keyboard focus is on **Cancel**, not Allow.
- Blind Enter with Cancel focused did nothing (lucky, not harmful) —
  the v0.3 "press Enter" fallback is disproven as an approval method.
  Do not re-add it without focus knowledge.
- `Tab` moves focus (Cancel → … → Allow over two Tabs, verified by
  focus-ring screenshots). `Space`/`Enter` on Allow-focused showed no
  effect; coordinate clicks unverified (origin/accel uncertainty —
  set accel `flat` for tests, restore `default` after).
- `Escape` dismisses the ENTIRE queued stack at once (6 → 1) AND burns
  the grant: afterwards autoConnect fast-fails with "Could not find
  DevToolsActivePort" until remote debugging is toggled OFF/ON again.
  Seen twice. Rule: after any full clear (approve OR dismiss), re-toggle
  before new prompts can appear.
- Operational: a `nohup` foreground engine survived its `timeout` and
  held the single-instance lock, crash-looping the service (NRestarts=3).
  Always `ps | grep watcher_linux` before blaming the service; use
  `--once`/`--probe` beside a running service, never a second loop.
**Field notes, part 3 — activation wall (same day, ctd.):**

- Pointer delivery PROVEN on shell UI (Activities overview toggle,
  calendar popup) in backend coordinate space — but ~10 aimed clicks on
  the bubble (incl. pixel-measured Allow at desktop (856,342) from a 1:1
  window shot) changed nothing. uinput button events appear to be
  swallowed/ignored on the secure Views bubble. (Upstream macOS notes the
  same class of finding: cursorless synthetic clicks ignored.)
- Keyboard: `Tab` focus-walk unverified (focus-ring readings at small
  scales proved unreliable — identical-byte screenshots), `Space`/`Enter`
  on Allow no effect, `Alt+A` nothing, `Escape` dismisses the WHOLE queue
  and burns the grant (re-toggle required).
- AT-SPI Collection on the frame: zero `PUSH_BUTTON` matches;
  `get_active_descendant` unimplemented by Chrome. No object path exists.
- Coordinate traps mapped: AT-SPI extents (0,0) vs shell origin (67,32)
  for the same window — always add the shell origin; screenshots come
  scaled (note `scale`!) — always divide back to 1:1 before measuring.
- Mouse accel was flipped `default`→`flat`→`default` during tests; ydotool
  absolute moves need `flat`, restored after.
- Net: detection shippable; activation on Wayland blocked on (a) real
  pointer delivery to Views bubbles, or (b) focusing Allow by keyboard.
  Next ideas, untested: `F6` pane-focus into the bubble, Alt-underline
   mnemonics check, Chrome under XWayland (`--ozone-platform=x11`) where
   Views AT-SPI exposure may be complete.
**Field notes, part 4 — verdict: no synthetic activation (same day, ctd.):**

- `F6` + `Tab` do nothing observable (byte-identical screenshots before
  and after; earlier "focus walked" readings were compression/time noise
  — only trust same-minute full screenshots, and even those showed no
  change). Keyboard focus cannot be moved into or within the bubble.
- Pixel-measured clicks (desktop (802,340) and (856,342) from 1:1 window
  shots, plus a full row/column sweep) all no-ops on the bubble, while
  the same path toggles Activities and opens the calendar. Delivery
  works; the secure Views bubble ignores synthetic pointer input.
- Net verdict: on GNOME/Wayland + Chrome 153, NOTHING programmatic
  reaches Allow — no AT-SPI objects, no pointer, no keys (except global
  `Esc`, which dismisses AND burns the grant). This matches the threat
  model behind closing #825 as not planned. v0.6 removed all activation
  paths; the engine is a watchdog. Revisit only if Chrome exposes the
  button (X11?) or a new input path appears.
- Keep stray `chrome-devtools-mcp` processes reaped
  (`pkill -f "[c]hrome-devtools-mcp"` — quoted to dodge self-match);
  orphans pile up during probing and muddy the water.

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

## End-to-end proof, 2026-09-22 (v0.6 watchdog + human click)

Proven with a live client (600s window so the click lands while alive):

1. Trigger attach → call hangs mid-handshake.
2. Engine logs `untitled bubble candidate (chrome) window=0,0,1213x768
   children 2->3 ... left for human click`, screenshot confirms the
   dialog on screen.
3. Human clicks **Allow** with a real mouse.
4. The hung call **RESPONDS** (`## Pages ...`, 55.5s in) — grant works.
5. Frame child count returns to baseline 1, toggle intact.

Lesson: approving a DEAD client's prompt grants nothing — the click must
land while its client is still waiting. Earlier "no grant" results were
all expired-window artifacts, not approval failures.
