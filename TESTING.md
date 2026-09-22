# Testing the Linux engine against a live prompt

The click path is proven (see Part 5). This file is the repeatable
procedure plus the field notes so far.

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
/usr/bin/python3 tools/consent-check.py 12   # autoConnect + list_pages, 12s window
```

- `RESPONDED` quickly → consent already granted; go to Stage 3.
- `HUNG` → a prompt is (probably) up. Go to Stage 2.

The script (`tools/consent-check.py`, kept short on purpose):

```python
# spawn: npx -y chrome-devtools-mcp@latest --autoConnect (stdio)
# send: initialize -> notifications/initialized -> tools/call list_pages
# a hung call with no reply in the window == consent gate (or a sick
# server; rule that out first: `ss -tlnp | grep 9222` must show chrome
# listening)
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

> **RETRACTED in part 5.** The two conclusions below were wrong: the
> failures were a *relative* input device (subject to pointer
> acceleration) plus coordinate arithmetic from scaled screenshots —
> not Chrome rejecting synthetic input. Absolute-pointer clicks on the
> correctly-measured button work, first try most of the time. Keep the
> trap-lists (they are the reason for the retraction) and read part 5.

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

## Part 5 — breakthrough: auto-approval works

Two root causes, both on our side, both found in one afternoon:

1. **The input device was relative.** `ydotoold`'s device is a RELATIVE
   evdev device (`REL=147`, no ABS), so its "absolute" moves are deltas
   subject to GNOME pointer acceleration — they land wherever. The
   `computer-use-linux absolute pointer` device is a true ABS device
   (range 0..1279 / 0..799, i.e. 1:1 with the logical desktop) and hits
   what you ask it to. A dedicated absolute device of our own
   (`auto_click.AbsoluteClicker`, python3-evdev, `yesdev absolute
   pointer`) does the same: verified by clicking the clock (calendar
   opened) and then Allow.
2. **The aim was wrong.** Screenshots arrive downscaled (`scale` field);
   measuring Allow in the returned image and treating it as desktop
   pixels put every click 20-50px off — outside the button. Measure in
   a 1:1 capture, or detect the button programme-side.

The working pipeline (v0.7, `auto_click.py`):

```
bubble suspected (child-total bump)
  -> xdg-desktop-portal Screenshot (works from a plain process, no prompt)
  -> PIL: blue-fill clusters (Chrome 153 dark measured fill (0,75,118))
       -> the two side-by-side buttons; rightmost = Allow
  -> AbsoluteClicker.click(x, y)  (1:1 absolute uinput)
  -> child total back to base?  YES -> [ACTION] APPROVED
```

Live, unattended, on this VM (2026-09-22):

```
17:14:19.292 [INFO] untitled bubble candidate ... children 2->3
17:14:19.292 [INFO]   armed - will click Allow when found
17:14:19.485 [AUDIT]   visual approve: clicking Allow at (837, 363)
17:14:20.114 [ACTION]   APPROVED via abs-pointer click (837, 363)
client: "RESULT after 9.0s: RESPONDED"  # hung CDP call completed, no human
```

Also observed and handled: the **first click is occasionally swallowed**
while the bubble animates in. The engine therefore retries (1s gap,
max 3 attempts per cycle); every retry after a swallowed click approved
on attempt 2 — until the one that didn't: on 2026-09-22 17:54 a bubble
swallowed all 3 and the engine then waited forever for it to clear (it
never did). v0.8.2 adds a `--cool-off-s` pause (default 30s) and a
fresh 3-attempt cycle, repeated until the bubble goes. `[ACTION]` is
logged only after the child total returns to its pre-bubble base.

Recap of what NOT to retry (all tried, all dead ends for good reasons):
relative-device absolute moves, blind Enter (`Esc` burns the grant,
focus is unpredictable), AT-SPI Collection walks (Chrome exposes no
objects on Wayland), and F6/Tab focus walks.

## v0.8 — boot-safety (same day, ctd.)

The systemd unit starts at boot ~11s before the desktop session on the
author's VM, and v0.7's clicker failure **latched permanently** — the
first unavailable probe before Mutter existed would have disabled
auto-click until a manual restart. Fixes, each with an offline check in
`tests/test_selfheal.py` (8/8 pass) plus a live regression after
restart (candidate → click → `[ACTION]`, client 11.5s):

- pointer creation retried every 15s (`_get_clicker`), logged once, no
  latch;
- pointer rebuilt when the screenshot size changes — also covers RDP
  resolution changes between sessions;
- AT-SPI re-initialised after 20 consecutive failed scans (`Atspi.init`),
  counter resets on any success;
- a failed screenshot or scan only warns for that sweep; the next sweep
  retries.

Real reboot still unverified (the author's session dies with the VM);
post-reboot checklist lives in README ("Staying up").

## v0.8.3 — the covering-window incident (same day, ctd.)

After the v0.8.2 e2e passed, sessions still stalled: the gate was up,
child totals bumped, but every screenshot said `Allow button not found`
and every click vanished. Root cause found by elimination: a second
app's window (`grok-bot`, 1023x665) sat over Chrome — the bubble was
hidden under it and all clicks went to the covering window. Giving the
scene focus is also load-bearing: on Wayland only the active window
receives clicks, which is why a bubble click sometimes needed a second
attempt (first click activates, second lands).

Evidence and the fixes:

- A corner click at (1150,720) — inside Chrome (1213x768) but outside
  the covering window — raised Chrome (`AT-SPI active` flipped) and the
  bubble reappeared in the very next screenshot (`--detect` found it).
- v0.8.3 therefore: raises the host window itself (one uncovered-corner
  click, computed from other apps' AT-SPI frame bounds) when no button
  is visible, and raises after a failed click before retrying;
- verifies approval visually (fresh screenshot no longer shows the
  button) because child totals leak with queued/orphaned attaches and
  claimed bubbles were still present long after they were gone;
- stands down after 3 no-button looks instead of cycling on a phantom;
- cools off between failed 3-attempt cycles and rebuilds the pointer
  device (a long-lived device once stopped delivering while a fresh
  one approved immediately).

Live validation, 19:46: fresh attach -> candidate -> first click
swallowed (focus race) -> retry -> `APPROVED via abs-pointer click
(837, 363)` -> client answered with its page list, no block.

## Stage 3 — fresh prompt (needs a Chrome restart)

Consent resets on restart. This kills the main browser (tabs restore;
automation Chromes on other profiles are unaffected) — get explicit
approval first.

1. Arm the engine: `--enable-click` (service already runs armed), or a
   foreground `--observe` run for a no-touch rehearsal.
2. Note the active tab URL (misfire check later).
3. Restart Chrome (session restore on).
4. Trigger Stage 1. Expect: HUNG + count bump on the ACTIVE frame.
5. Watch `tail -f ~/.local/share/YesDev/yes-dev.log` for the current
   contract: `untitled bubble candidate` → `armed - will click Allow
   when found` → `visual approve: clicking Allow at (x, y)` →
   `APPROVED via abs-pointer click (x, y)` (`[ACTION]`), and the MCP
   call responding. (A first click is sometimes swallowed during the
   bubble's entry animation; the engine retries and logs
   `FAILED 1/3` in between.)
6. Misfire checks: active tab URL unchanged, no stray bookmark/download
   bubbles approved, no text entered anywhere.

## If autoConnect fast-fails with "Could not find DevToolsActivePort"

Seen once after manual Enter experimenting: file present, port listening,
toggle still `user-enabled: true`, yet discovery fails. Suspect stale
browser-endpoint ID in the file. Fix: toggle remote debugging OFF and ON
at `chrome://inspect/#remote-debugging`, then retry Stage 1.

## What success looks like

- Log shows `APPROVED via ...` with `[ACTION]`, total counter increments.
- The hung MCP call answers within seconds of the log line.
- `--probe` during a later hang shows the same count-bump shape, served
  next sweep (dedupe) — i.e. N queued prompts approved one per sweep.

## End-to-end proof, 2026-09-22

Two proofs, in order of automation:

1. **Human click (v0.6), 55.5s in:** engine logged the candidate, a human
   clicked Allow, the hung call responded, count returned to baseline.
   Lesson: approving a DEAD client's prompt grants nothing — the click
   must land while its client is still waiting.
2. **Fully unattended (v0.7), 9.0s in:** engine logged candidate, clicked
   Allow itself via the absolute pointer at 17:14:19-20, `[ACTION]`
   written, hung client responded — no human in the loop.
