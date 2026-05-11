# Fix: Bot silently drops state for windows under grouped tmux sessions

## Problem

When the user runs multiple tmux clients via grouped sessions (e.g. two iTerm
windows, one attached to `ccbot`, another to `ccbot-2` created with
`tmux new-session -t ccbot -s ccbot-2`), the bot's session_map reconciler
silently discards entries written by Claude Code's `SessionStart` hook when
the hook happens to resolve the pane's session name to anything other than
the literal `config.tmux_session_name`.

## Evidence (from the current deployment)

`~/.ccbot/session_map.json` mixes both prefixes:

```json
{
  "ccbot:@22":   { "session_id": "df75283a-…", "window_name": "obsidian" },
  "ccbot:@29":   { "session_id": "df75283a-…", "window_name": "obsidian" },
  "ccbot-2:@18": { "session_id": "f852297e-…", "window_name": "ccbot" },
  "ccbot-2:@28": { "session_id": "8fd8e985-…", "window_name": "marrige-proposal" },
  "ccbot-2:@30": { "session_id": "483c8a3a-…", "window_name": "CV" }
}
```

`ccbot.stderr.log` shows the consequence — 40+ repetitions over ~10 hours of:

```
ccbot.session - INFO - Removing stale window_state: @28
```

## Root cause

1. `SessionStart` hook in `src/ccbot/hook.py:196-220` resolves the tmux key by
   running `tmux display-message -t $TMUX_PANE -p '#{session_name}:#{window_id}:#{window_name}'`.
   For a pane whose window is shared by grouped sessions, tmux returns the
   session name of *whichever* client's context `display-message` resolves
   through — non-deterministic from the bot's perspective.
2. Multiple `session_map` readers hard-code the filter to the literal
   `config.tmux_session_name:` prefix:
   - `src/ccbot/session.py:load_session_map`
   - `src/ccbot/session.py:wait_for_session_map_entry`
   - `src/ccbot/session.py` cleanup helpers for stale / old-format keys
   - `src/ccbot/session_monitor.py:_load_current_session_map`
   Anything under `ccbot-2:` is skipped entirely.
3. `src/ccbot/session.py:load_session_map` then treats any `window_states`
   entry *not* seen in that filtered view as stale and deletes it.

Downstream effects:

- `SessionMonitor` builds its active-session set from its own filtered view of
  `session_map`. So even if `window_states` were preserved, grouped-prefix
  sessions still would not be tailed until the monitor path is fixed too.
  Result: **inbound messages from Claude stop reaching Telegram** for that
  topic.
- `maybe_restart_for_upgrade` calls `get_window_state(window_id)`, which
  auto-creates an empty `WindowState`. Backfill then re-runs, re-pins the
  launch version, and the next reconciler pass wipes it again — a persistent
  flapping state.
- `wait_for_session_map_entry` only polls for `f"{config.tmux_session_name}:{window_id}"`,
  so grouped-prefix hook writes are misclassified as hook timeouts during
  window creation / resume.
- Fixed the auto-restart false-failure separately (see
  `src/ccbot/update_watcher.py` changes on 2026-04-24). That fix addresses the
  `pane_current_command='zsh'` health-check bug, but leaves the grouped-
  session wipe in place.

## Proposed fix

Make every `session_map` reader / cleanup path accept *any* tmux session in
the same group as `config.tmux_session_name`, not just the literal name.

### Approach

1. Add `TmuxManager.list_group_session_names()` (new helper):
   - Runs `tmux list-sessions -F '#{session_name}|#{session_group}'`.
   - Returns `{name}` plus every other session sharing the configured
     session's group.
   - Graceful fallback to `{name}` on any tmux query failure (keeps current
     behavior for users without grouped sessions).
   - Important edge case: when the configured session is **ungrouped**,
     tmux reports an empty `session_group`. In that case the helper must
     return only `{name}`; otherwise it would incorrectly include every other
     ungrouped session on the server.

2. Use that helper everywhere `session_map` is consumed:
   - `SessionManager.load_session_map`
   - `SessionManager.wait_for_session_map_entry`
   - `SessionManager._cleanup_stale_session_map_entries`
   - `SessionManager._cleanup_old_format_session_map_keys`
   - `SessionMonitor._load_current_session_map`
   Each path should share the same fallback behavior: if the tmux query fails,
   fall back to the literal configured session name.

### Why session-group is the right scope

tmux guarantees that grouped sessions share the same underlying windows.
So any window_id seen under any session name in the group *is* a valid
reference to one of our windows. Accepting entries from those prefixes is
semantically correct, not a heuristic.

Sessions outside the group (e.g. the stale `"1:@2"` entry in the current
`session_map.json`) still correctly get ignored.

### Non-goals

- **Don't** change the hook. It has no reliable way to discover the canonical
  session name from inside a pane, and adding tmux queries there risks hook
  timeouts (the hook is given only 5s).
- **Don't** collapse window_ids across tmux *servers* — window_id uniqueness
  is only within one server.

## Tests (TDD, failing-first)

In `tests/ccbot/test_session.py`:

1. `test_load_session_map_accepts_grouped_session_prefix`
   - Seed `session_map.json` with keys under both `ccbot:` and `ccbot-2:`.
   - Mock `TmuxManager.list_group_session_names()` → `{"ccbot", "ccbot-2"}`.
   - Assert both entries end up in `window_states` (not wiped).

2. `test_load_session_map_ignores_unrelated_sessions`
   - session_map contains `ccbot:@5`, `other:@7`.
   - `list_group_session_names()` → `{"ccbot"}`.
   - `@7` is *not* added; `@5` is.

3. `test_load_session_map_survives_tmux_query_failure`
   - `list_group_session_names()` raises / returns empty.
   - Behavior falls back to literal-name matching (today's behavior).

4. `test_load_session_map_does_not_wipe_state_from_grouped_prefix`
   - Pre-populate `window_states["@28"]`.
   - session_map only has `ccbot-2:@28`.
   - After reconcile, `@28` is still in `window_states` (regression guard
     for the current "Removing stale window_state: @28" behavior).

5. `test_wait_for_session_map_entry_accepts_grouped_session_prefix`
   - session_map only has `ccbot-2:@28`.
   - grouped-session lookup returns `{"ccbot", "ccbot-2"}`.
   - `wait_for_session_map_entry("@28")` succeeds instead of timing out.

In `tests/ccbot/test_session_monitor.py`:

1. `test_load_current_session_map_accepts_grouped_session_prefix`
   - grouped-prefix session_map entries are returned to the monitor.

2. `test_cleanup_all_stale_sessions_keeps_grouped_prefix_session`
   - tracked session remains active when the only session_map entry uses
     `ccbot-2:`.

In `tests/ccbot/test_tmux_command.py`:

1. Unit tests for `list_group_session_names` parsing `list-sessions` output.
2. Explicit regression test for the **multiple ungrouped sessions** case:
   if `ccbot` is ungrouped and `other` is also ungrouped, the helper must
   return only `{"ccbot"}`, not both.

## Verification against the live deployment

After fix + restart:

- `tail -f ~/.ccbot/ccbot.stderr.log | grep "Removing stale"` should be
  silent (no more flapping).
- Send a message in the marrige-proposal topic. Claude's reply must arrive
  back in Telegram (confirms SessionMonitor is now tailing the @28 session
  via the grouped-prefix entry).
- Re-run the auto-restart scenario (next claude upgrade) — with both fixes
  in place, restart succeeds cleanly with no ⚠️ warning AND the post-restart
  state is not wiped.

## Scope / rollout

- Pure server-side change; no state migration needed.
- Compatible with ungrouped deployments (single `ccbot` session): the helper
  returns `{"ccbot"}` and the existing behavior is preserved.
- No user action required after deploy.

## Related prior fix

The auto-restart health check was fixed on 2026-04-24 to detect claude as a
descendant of the `window_shell` wrapper (src/ccbot/update_watcher.py
`_has_claude_descendant`, `_find_version_descendant`). That change stops
false "⚠️ Auto-restart failed" warnings and lets the backfill recover the
running version for wrapper-shell panes. This plan addresses the independent
grouped-session wipe that makes the auto-restart fix *stick* for windows
whose hook happens to register under a grouped prefix.
