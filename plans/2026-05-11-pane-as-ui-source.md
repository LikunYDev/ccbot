# Pane as the single source of truth for interactive UIs

## Problem

`AskUserQuestion` / `ExitPlanMode` prompts can silently fail to reach Telegram.
Reproduced this session (window @48, `job-application`):

```
15:15:20.250  session_monitor: [complete] AskUserQuestion tool_use         (JSONL fires)
15:15:20.273  bot.py: set_interactive_mode
15:15:21.307  bot.py retry 1: "No interactive UI detected" (pane still empty)
15:15:22.091  bot.py retry 2: "No interactive UI detected"
15:15:23.158  bot.py retry 3: "No interactive UI detected"
15:15:23.159  bot.py: clear_interactive_mode (retry budget exhausted)
15:15:35.251  status_polling: pane FINALLY renders UI, sets mode (no send)
… 64 min of silence …
```

The pane took **~15 s** to render after the JSONL `tool_use` entry was written.
`bot.py`'s 2 s retry loop gave up first. `status_polling` then refused to send
because `content.name in INTERACTIVE_TOOL_NAMES` (`src/ccbot/handlers/status_polling.py:91-94`):
the rescue path is explicitly disabled for these UIs.

The same failure mode is reachable from other paths:
- Bot restart with `last_byte_offset` past the `tool_use` entry → JSONL trigger
  never fires again. We hit this last session.
- `claude --resume` opens a new `session_id` while messages continue under the
  old one → JSONL trigger fires for the wrong file.
- JSONL writer lag, file truncation, mismatched `session_map.json` keys.

## Why retry-and-timeout is a kludge

The current design treats two signals as both authoritative:

| Signal | What it tells us | When |
|---|---|---|
| **JSONL `tool_use` event** | "Claude decided to ask" | Written ~instant |
| **Pane content** | "User can see the UI" | Rendered seconds later |

`bot.py` listens on JSONL, then races against the pane render with a fixed
retry budget. `status_polling` watches the pane but defers to JSONL via
`INTERACTIVE_TOOL_NAMES`. The coordination has four failure modes (above)
and any one of them produces silent loss.

A wall-clock timeout (e.g. "after 5 s, status_polling takes over") shifts the
threshold but keeps the dual-signal design. Tomorrow's slow render is 6 s.

## First principles

For `AskUserQuestion` / `ExitPlanMode` we don't render anything from JSONL — we
capture the pane. The only fact that matters for delivery is **"is the UI
visible in the pane right now?"**. That's a pane property. JSONL's `tool_use`
event tells us roughly when to *expect* it, but it's strictly redundant with
the pane.

Use the pane as the single source of truth. Drop the JSONL trigger for
interactive UIs.

## Proposed architecture

### State

Per `(user_id, thread_id_or_0)`:

```
        pane shows UI &              worker
       !already enqueued             dispatches
None ─────────────────────▶ enqueued ───────▶ sent
  ▲                                             │
  └─────── pane no longer shows UI ─────────────┘
```

Four flags, all keyed by `ikey = (user_id, thread_id or 0)`:

- `_interactive_enqueued: set[ikey]` — task is in the message queue, worker
  hasn't dispatched yet.
- `_interactive_msgs: dict[ikey, int]` — Telegram `message_id` of the sent UI
  (existing).
- `_interactive_mode: dict[ikey, window_id]` — same role as today, used by
  status_polling to skip status-line updates while UI is active (existing).
- `_interactive_last_name: dict[ikey, str]` — name of the UI most recently
  delivered (e.g. `"ExitPlanMode"`, `"PermissionPrompt"`). Lets status_polling
  detect a pane *morph* (UI type changed in place) and re-enqueue so the
  Telegram message gets updated. Set by `handle_interactive_ui` on success;
  cleared in `clear_interactive_msg`.

### Flow

1. `status_poll_loop` runs every 1 s for each `(user_id, thread_id) → window_id`
   binding.
2. Inside `update_status_message`, after `extract_interactive_content(pane_text)`:
   - **UI visible**:
     - If `ikey` already in `_interactive_enqueued` → no-op (worker will
       dispatch it).
     - Else if `msg_id` is set AND `last_name == content.name` → no-op (same
       UI still showing, already delivered).
     - Else → set `_interactive_mode`, add to `_interactive_enqueued`, push an
       `interactive_ui` task onto the per-user `message_queue`. The
       `msg_id None` case handles first delivery; the `last_name !=
       content.name` case handles in-place pane morph (e.g. ExitPlanMode
       → PermissionPrompt) by re-enqueuing so the worker can edit the
       Telegram message.
   - **UI not visible**:
     - If `msg_id` set → `clear_interactive_msg` (deletes Telegram message,
       clears all three flags). Existing.
3. Message queue worker dequeues the `interactive_ui` task in FIFO order
   (after any preceding text/thinking), discards `ikey` from
   `_interactive_enqueued`, then calls `handle_interactive_ui`. `handle_interactive_ui`
   captures the pane *again at dispatch time* (already does), sends, sets
   `_interactive_msgs[ikey]`.
4. If `handle_interactive_ui` returns `False` (pane no longer shows UI, capture
   failed): just discard the task. Next poll re-detects and re-enqueues.

### Ordering

The per-user `message_queue` is FIFO. JSONL still writes text/thinking from
the same session_monitor pass and enqueues them via `enqueue_content_message`.
In the common case — pane renders the UI after the JSONL `tool_use` is
written *and* after session_monitor's 2 s poll picks up the preceding
text/thinking — those messages are already ahead of the `interactive_ui`
task in the queue.

There is a narrow window where status_polling (1 s cadence) can see a
rendered UI before session_monitor (2 s cadence) has read the preceding
JSONL bytes. In that case the UI lands first and the text/thinking follows.
This is a best-effort ordering, not a hard guarantee — same as today's
behavior (today's `bot.py` retry path had the same gap). Worth knowing, not
worth fixing in this change.

### Idempotency / no double-send

`_interactive_enqueued` blocks duplicate enqueues during the worker's busy
window. After the worker dispatches, it clears the flag *and* sets
`_interactive_msgs[ikey]`. Either guard alone is sufficient on subsequent
polls.

## Concrete change list

### 1. `src/ccbot/handlers/interactive_ui.py`

- Add `_interactive_enqueued: set[tuple[int, int]]`.
- Add module-level helpers: `mark_interactive_enqueued(user_id, thread_id)`,
  `clear_interactive_enqueued(user_id, thread_id)`,
  `is_interactive_enqueued(user_id, thread_id)`.
- Extend `clear_interactive_msg` to also discard from `_interactive_enqueued`.
- No change to `handle_interactive_ui` body — it's already idempotent (checks
  `_interactive_msgs.get(ikey)` and edits if set).

### 2. `src/ccbot/handlers/message_queue.py`

- Extend `MessageTask.task_type` Literal to include `"interactive_ui"`.
- Add `enqueue_interactive_ui(bot, user_id, window_id, thread_id)` helper that
  creates a `MessageTask(task_type="interactive_ui", ...)` and `put_nowait`s it.
- In `_message_queue_worker`, add a branch:
  ```python
  elif task.task_type == "interactive_ui":
      await _process_interactive_ui_task(bot, user_id, task)
  ```
- New `_process_interactive_ui_task`:
  ```python
  ikey = (user_id, task.thread_id or 0)
  clear_interactive_enqueued(user_id, task.thread_id)
  await handle_interactive_ui(bot, user_id, task.window_id, task.thread_id)
  ```
  Imports `handle_interactive_ui` and `clear_interactive_enqueued` from
  `.interactive_ui` (already an import from the same package, no cycle).

### 3. `src/ccbot/handlers/status_polling.py`

Rewrite the interactive-UI branch in `update_status_message`:

```python
content = extract_interactive_content(pane_text)
interactive_window = get_interactive_window(user_id, thread_id)

if content is not None:
    if content.name == "Feedback":
        # auto-dismiss (unchanged)
        await tmux_manager.send_keys(window_id, "0", enter=False, literal=False)
        return

    if interactive_window is not None and interactive_window != window_id:
        # mode set for another window — stale, clean up
        await clear_interactive_msg(user_id, bot, thread_id)

    if get_interactive_msg_id(user_id, thread_id) is None \
            and not is_interactive_enqueued(user_id, thread_id):
        set_interactive_mode(user_id, window_id, thread_id)
        mark_interactive_enqueued(user_id, thread_id)
        await enqueue_interactive_ui(bot, user_id, window_id, thread_id=thread_id)
    return  # skip status update while UI is active

# No UI in pane
if interactive_window == window_id:
    await clear_interactive_msg(user_id, bot, thread_id)
# fall through to status line check
```

Delete:
- The `content.name not in INTERACTIVE_TOOL_NAMES` rescue exclusion.
- The `content.name in INTERACTIVE_TOOL_NAMES and session_id` "JSONL is sole
  sender" branch (the deferral that caused the bug).

### 4. `src/ccbot/bot.py`

In `handle_new_message`, replace the `INTERACTIVE_TOOL_NAMES` special case
(lines 1773-1802):

```python
if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
    # Pane is the source of truth for these UIs — status_polling handles
    # detection + delivery. Just suppress the JSONL tool_use entry so it
    # doesn't get sent as a regular text message, and advance the read offset.
    session = await session_manager.resolve_session_for_window(wid)
    if session and session.file_path:
        try:
            file_size = Path(session.file_path).stat().st_size
            session_manager.update_user_window_offset(user_id, wid, file_size)
        except OSError:
            pass
    continue
```

The "clear UI on any non-interactive message" branch at line 1804 stays — it's
still useful as a fast-path cleanup when Claude moves past the UI before the
1 s poll notices.

### 5. Tests

Files to update:
- `tests/ccbot/handlers/test_status_polling.py`
- `tests/ccbot/handlers/test_message_queue.py` (new tests for the
  `interactive_ui` task type)
- Possibly `tests/ccbot/test_bot_interactive.py` if it asserts the old retry loop

Existing tests that will need adjustment (they encode the JSONL-sole-sender
contract):
- `TestStatusPollerExitPlanDetection.test_exit_plan_with_session_defers_to_jsonl`
  — assertion flips: ExitPlanMode in pane now triggers an enqueue, not a
  silent `set_interactive_mode`.
- `TestStickyInteractiveModeRescue.test_exit_plan_then_still_exit_plan_stays_silent`
  — assertion flips: the second poll *should* be a no-op, but the first poll
  now enqueues a task (the test needs to assert "enqueue called once, second
  cycle skipped because enqueued/msg flag set").
- `TestStickyInteractiveModeRescue.test_exit_plan_then_permission_transition_sends`
  — still valid; first cycle enqueues ExitPlanMode UI, second cycle (pane
  morphs to PermissionPrompt) is a clear → new enqueue. Update assertions
  accordingly.

New tests:
- `test_pane_ui_enqueues_once`: pane shows AskUserQuestion across 3 polls →
  exactly 1 `enqueue_interactive_ui` call (idempotency).
- `test_pane_ui_rescues_after_simulated_bot_restart`: `_interactive_msgs` and
  `_interactive_enqueued` empty, pane shows UI → enqueue fires (regression
  for last session's byte-offset bug).
- `test_worker_processes_interactive_ui_task`: enqueue task, run worker once,
  assert `handle_interactive_ui` called with correct args and
  `_interactive_enqueued` cleared.
- `test_ordering_text_before_ui`: enqueue content task then interactive_ui
  task → assert worker processes content first.
- `test_pane_ui_gone_clears_state`: msg_id set + pane has no UI → both
  `_interactive_msgs` and `_interactive_enqueued` cleared.

## Migration / behavior changes

1. **No in-place edit on `tool_result` for interactive tools.** Today
   `bot.py`'s `INTERACTIVE_TOOL_NAMES` branch never set `_tool_msg_ids` for the
   `tool_use_id` (it `continue`s out before reaching the recorder at line 421
   of `message_queue.py`). So today the `tool_result` ("User answered Claude's
   questions: …") already lands as a fresh message. **No regression.**

2. **Detection latency.** Up to 1 s (the status poll interval) plus message
   queue worker latency (typically < 100 ms). Today's "fast" path was
   theoretically instant but in practice waited 1+ s for the pane anyway.
   Net: same or better.

3. **Bot restart with active UI in pane.** None of the four interactive-UI
   state dicts (`_interactive_msgs`, `_interactive_mode`,
   `_interactive_enqueued`, `_interactive_last_name`) are persisted to
   `state.json`. They live as module-level globals and reset on every
   process start. After restart, status_polling re-detects, re-enqueues,
   sends a second notification. The old Telegram message remains in the
   chat. **Known minor regression vs. ideal**; acceptable as the
   alternative is silent loss. Out of scope for this change.

4. **`set_interactive_mode` no longer set by bot.py.** Now exclusively set by
   status_polling. This concentrates the state machine in one place — easier
   to reason about.

5. **`INTERACTIVE_TOOL_NAMES` is still meaningful** — it's the contract that
   tells status_polling "these names = capture UI from pane" vs. the other
   pane patterns (PermissionPrompt, Settings, RestoreCheckpoint, BashApproval)
   which take the same code path. The constant doesn't need to move.

## Out of scope

- Persisting `_interactive_msgs` to `state.json` across restarts. Separate
  follow-up if duplicate-on-restart becomes annoying.
- Reducing the 1 s poll interval. The current cadence is intentional for
  rate-limit safety.
- Updating the Telegram message when the user navigates options *in the
  pane* (vs. via Telegram buttons). Pre-existing limitation, not introduced.
- The session_map / byte-offset clean-up tooling we needed last session.
  Already shipped (commit-ready in `session.py`/`session_monitor.py`/`hook.py`).

## Implementation method: TDD

Each module change follows red → green → refactor. Tests are written first,
must fail for the right reason, then code is changed until they pass. No
production change lands without a test that would have caught the bug.

**Order of implementation** (each step is one commit-sized unit):

1. **`interactive_ui.py` — `_interactive_enqueued` set + helpers.**
   - Red: write `test_mark_clear_is_idempotent`, `test_clear_interactive_msg_clears_enqueued_flag` (new tests for the new helpers).
   - Green: add the set, helpers, extend `clear_interactive_msg`.

2. **`message_queue.py` — `"interactive_ui"` task type + worker branch.**
   - Red: `test_worker_processes_interactive_ui_task`, `test_ordering_text_before_ui` (assert worker dispatches in FIFO order).
   - Green: extend `MessageTask.task_type` literal, add `enqueue_interactive_ui`, add `_process_interactive_ui_task`, wire into `_message_queue_worker`.

3. **`status_polling.py` — pane-driven enqueue.**
   - Red: `test_pane_ui_enqueues_once` (3 polls → 1 enqueue), `test_pane_ui_rescues_after_simulated_bot_restart` (the original bug), `test_pane_ui_gone_clears_state`. **Flip** assertions on `test_exit_plan_with_session_defers_to_jsonl` and `test_exit_plan_then_still_exit_plan_stays_silent` to match new contract.
   - Green: rewrite the UI branch as described in §Concrete change list.

4. **`bot.py` — suppress INTERACTIVE_TOOL_NAMES tool_use.**
   - Red: `test_jsonl_interactive_tool_use_does_not_call_handle_ui`, `test_jsonl_interactive_tool_use_advances_offset` (replace any existing retry-loop tests).
   - Green: replace the retry loop with the suppress-and-advance branch.

## Verification gates

Run after each step (red commit + green commit), and a final pass before merge:

1. `uv run ruff check src/ tests/` clean
2. `uv run ruff format --check src/ tests/` clean
3. `uv run pyright src/ccbot/` 0 errors
4. `uv run python -m pytest` all green
5. **Code review subagent**: spawn a `general-purpose` agent with a focused
   reviewer prompt — give it the plan, the diff vs. `main`, and ask for an
   independent second opinion on (a) correctness of the state-machine
   transitions under concurrent polls + worker dispatch, (b) any ordering
   regression vs. today, (c) test coverage gaps, (d) ruff/pyright signals it
   would expect to catch. Address any high-severity findings before live
   verification.
6. Restart bot, confirm the pending "Eval harness data" `AskUserQuestion` in
   window @48 is delivered to Telegram.
7. Trigger a fresh `AskUserQuestion` in any window and confirm normal
   text/thinking still arrives before the UI keyboard.
