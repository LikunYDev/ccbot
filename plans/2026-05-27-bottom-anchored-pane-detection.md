# Bottom-anchored interactive-UI detection

**Date:** 2026-05-27
**Status:** Proposed
**Supersedes the weak point of:** `plans/2026-05-11-pane-as-ui-source.md` (commit `dd93176`)

## TL;DR

Keep the pane as the single source of truth for interactive UIs, but fix the
one thing that makes it fragile: **stop anchoring detection on the UI's *top*
marker (which scrolls off a short pane) and anchor on its *bottom* marker
(which never can).** Add a never-silent backstop so a detected-but-unparseable
prompt is still surfaced. Size panes generously at creation as polish — not as
the thing correctness depends on.

This avoids both the silent-hang bug and every regression we found in the
"switch back to JSON" direction.

---

## Background — how we got here

- **`dd93176` ("pane as the single source of truth")** removed the old
  dual-signal design (JSONL `tool_use` event *and* pane render, both
  authoritative). That dual design caused ~3 months of duplicate / ordering /
  race bugs (Jan–Apr 2026: `fa42b6e`, `4a2ae82`, `ad23d8b`, `9fd58ab`,
  `0643bfe`, `27e0a88`). Making the pane the sole signal was the right call and
  killed the races.
- **But it introduced a silent hang.** `extract_interactive_content()` anchors
  on a UI's **top** delimiter (the tab/checkbox header) and reads downward.
  `capture_pane()` reads only the **visible** region, and bot-managed windows
  run at tmux's detached default **80×24**. A tall prompt (e.g. a 6-option
  `AskUserQuestion` with multi-line descriptions) pushes its top marker above
  the viewport → parser returns `None` → the prompt is never forwarded to
  Telegram → the session waits forever for input the user can't give.

### Incident (2026-05-27)

The `obsidian` session hung ~27 min on this prompt:

```
☐ Escalations                  ← top marker (tab header) — scrolled OFF the 80×24 viewport
When you say "escalations" are over-engineered, which mechanism do you mean?
❯ 1. The note tier ladder
  2. Cross-scope forced-preview
  ...
Enter to select · ↑/↓ to navigate · Esc to cancel   ← bottom marker — ALWAYS visible
```

The structured question was even sitting unread in the JSONL the monitor was
already tailing. Detection failed purely because the top marker was off-screen.

---

## Root cause

The parser **requires** a top delimiter and reads downward. The top is exactly
the part that scrolls out of a short viewport. The bug is the choice of anchor,
not the pane-as-truth architecture.

## The property we exploit

> **An interactive prompt is always the last thing on screen.** Claude is
> blocked awaiting input, so nothing ever prints *below* the prompt. Its bottom
> marker (`Enter to select`, `Esc to cancel`, `ctrl-g to edit`, the numbered
> `1. Yes / 2. No`) is therefore **always visible**, regardless of how tall the
> question is.

We are currently anchoring on the one part that is allowed to disappear. Anchor
on the bottom and the failure mode is gone by construction.

---

## Design — three layers (+ one optional)

### Layer 1 (core): bottom-anchored detection

Change the extractor so the **bottom marker alone is sufficient to detect** that
a UI is present:

- **Detect** = find a known bottom marker within the last few non-empty lines of
  the visible pane.
- **Content** = read *upward* from that marker, capturing as much as is visible.
- The **top marker becomes best-effort** for trimming content, never required
  for detection.

Properties:
- Detection can no longer silently fail on tall prompts. The silent hang is
  eliminated **by construction**, at any pane height.
- **Staleness-free:** the visible pane's bottom *is* the live prompt, never an
  old frame from scrollback — so we never confuse it with a previous question.
- **Answering is unchanged** — keystroke delivery (`CB_ASK_*` → `send_keys`)
  already works on the live pane and stays exactly as-is.

### Layer 2 (safety): never-silent backstop

Add the invariant `dd93176` promised but never guaranteed:

> If a bottom marker is present (a prompt exists) but the content cannot be
> fully parsed, surface **what we can see** plus a note ("Claude is asking
> something — open the session to see the full prompt"). Never stay silent.

Result: **prompt on screen ⇒ user is always notified.** This is what makes
"pane as single source of truth" *safe*, not just usually-correct.

### Layer 3 (polish / safe pass): generous pane size

Give the TUI enough room that the whole prompt fits the visible viewport in
virtually every real case, so the upward read is complete and clean.

- Create windows with a generous height (e.g. **≥50 rows**, env-overridable via
  e.g. `CCBOT_PANE_ROWS` / `CCBOT_PANE_COLS`) **at creation time via the normal
  tmux API** (`new-session`/`new-window` `-x/-y`, or a per-window resize right
  after creation), and re-apply during startup re-resolution.
- **Safe interim pass:** existing windows may also be resized bigger to reduce
  the chance of clipping today.

> ⚠️ **Caution (learned this session):** do **not** mutate a live tmux server
> globally (`tmux set -g window-size manual` + `resize-window` on a running,
> grouped session). On 2026-05-27 that is the credible cause of a
> `server exited unexpectedly` crash that killed all sessions and forced
> `claude --resume` recovery. Resize at **window creation** / startup, prefer
> per-window resizes, and **test specifically with grouped sessions** before
> trusting it.

The key shift: with Layers 1 + 2 in place, **pane size is no longer
load-bearing for correctness.** A pathologically tall prompt is still *detected*
and the user is still *notified*; size only affects how much we can *display*.
That directly answers the "sizing is just a band-aid" objection — here it's
polish, not a crutch.

### Optional (later): scrollback walk-up for over-tall content

For a prompt taller than the viewport, read a bounded amount of scrollback
upward from the bottom marker to fill in the off-screen portion for display.
Carries a small stale-frame risk (off-screen selection state may be from an
earlier redraw), so it is an enhancement, not the core. Anchor strictly to the
live bottom; cap the walk-up height; stop at chrome separators.

---

## Rejected alternatives

### Switch interactive UIs back to JSON ("pure JSON")
Investigated in depth; rejected because:
- **Not achievable** — only `AskUserQuestion` and `ExitPlanMode` have a JSON
  `tool_use`. Permission prompts, Bash approval, RestoreCheckpoint, Settings,
  and the Feedback survey are runtime TUI with no JSON. So the pane code can't
  go away regardless.
- **JSON is incomplete even where it exists.** Verified from the incident: the
  `AskUserQuestion` JSON had **4** options; the TUI showed **6** — it adds
  **"Type something"** (free text) and **"Chat about this"** (escape), neither
  in the JSON. `ExitPlanMode` JSON has the plan text but **not** the approval
  choices. Rendering from JSON loses these.
- **Re-introduces the historical race** if a pane fallback is kept for the same
  UIs (dual authority = the exact duplicate/ordering bugs from Jan–Apr 2026).
- **Restart/resume miss:** a `tool_use` past the read offset (after restart or
  `--resume` session-id mismatch) is never observed — one of the original
  failure modes the pane design was created to escape.

### Sizing alone
A bigger pane only changes *how tall* a prompt must be before it breaks; it is
not a correctness fix on its own. (It is valuable as Layer 3 polish *once*
Layers 1–2 guarantee correctness.)

---

## Side effects / regressions to watch (with mitigations)

| Risk | Mitigation |
|---|---|
| Bottom-marker text appears in normal output (false positive) | Require the marker within the last *K* non-empty lines (the live prompt is always at the very bottom) **and** a structural neighbor (numbered options / `❯`) just above |
| Reading upward merges two adjacent prompts | Stop at the nearest top boundary / chrome separator; cap captured block height |
| Generous sizing interacts badly with grouped sessions | Set size at **creation** via API (never mutate a live server); test grouped sessions explicitly (crash history) |
| Backstop notification too chatty | Only fire when a marker is clearly present but extraction failed for *N* consecutive polls |
| Scrollback walk-up (if added) shows stale off-screen selection | Keep optional, display-only, anchored to live bottom |
| Capturing scrollback every poll is heavier | Pull scrollback lazily — only when a bottom marker is seen and the top isn't in the visible region |
| Test suite encodes top-anchored contract | Update `tests/ccbot/handlers/test_status_polling.py` and `terminal_parser` tests; add tall-prompt cases (top off-screen) that must still detect |

---

## Implementation sketch

1. **`src/ccbot/terminal_parser.py` — `_try_extract` / `UIPattern`:**
   make the bottom marker the primary anchor. When a bottom marker matches
   within the last *K* non-empty lines, detection succeeds; capture upward to
   the best-effort top boundary (or the top of the captured buffer). Keep the
   existing top markers as optional trim hints. Add a "tall prompt, top
   off-screen" fixture to the tests.

2. **`src/ccbot/handlers/status_polling.py` — backstop:**
   when `extract_interactive_content` returns `None` but a bottom marker is
   present for *N* consecutive polls, enqueue a degraded notification (visible
   slice + "open session to see the full prompt") instead of staying silent.

3. **`src/ccbot/tmux_manager.py` — sizing at creation:**
   create windows with a generous height/width via the tmux API; re-apply on
   startup re-resolution. Add the optional safe interim resize of existing
   windows (per-window, not a live global mutation).

4. **No change** to the answer path (`CB_ASK_*` keystroke delivery) or to the
   pane-only UIs (permission/settings/restore/bash/feedback) — they already work
   and are short/bottom-anchored.

## Verification gates

1. `uv run ruff check src/ tests/` clean
2. `uv run ruff format --check src/ tests/` clean
3. `uv run pyright src/ccbot/` 0 errors
4. `uv run python -m pytest` all green
5. Manual: trigger a tall `AskUserQuestion` (top off-screen) in an 80×24 window
   → confirm it is detected and delivered to Telegram (the incident repro).
6. Manual: confirm short prompts (permission, settings) and normal output do
   **not** false-trigger the bottom-marker detector.

---

## Related operational lessons (2026-05-27 incident)

These bit us during diagnosis and are worth fixing/avoiding independently:

1. **Live tmux mutation can crash the server.** `set -g window-size manual` +
   `resize-window` on a running grouped session is the credible cause of the
   `server exited unexpectedly` crash. Prefer creation-time sizing.
2. **`claude`-in-a-bot-window hijacks `session_map`.** Any `claude` process
   started inside a bot-managed tmux window fires the `SessionStart` hook, which
   keys `session_map.json` by `tmux_session:window_id` and **overwrites that
   window's entry**. During diagnosis a research agent ran `claude -p` inside
   window `@3`, stole its mapping (to a throwaway `/private/tmp` session), and
   the monitor dropped delivery for the real session. Consider having the hook
   ignore sessions whose `cwd`/parentage doesn't match the bound window, or
   otherwise guard against transient `claude` invocations clobbering live
   mappings.
