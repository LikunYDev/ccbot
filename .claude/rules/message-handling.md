# Message Handling

## Message Queue Architecture

Per-user message queues + worker pattern for all send tasks:
- Messages are sent in receive order (FIFO)
- Status messages always follow content messages
- Multi-user concurrent processing without interference

**Message merging**: The worker automatically merges consecutive mergeable content messages on dequeue:
- Content messages for the same window can be merged (including text, thinking)
- tool_use breaks the merge chain and is sent separately (message ID recorded for later editing)
- tool_result breaks the merge chain and is edited into the tool_use message (preventing order confusion)
- Merging stops when combined length exceeds 3800 characters (to avoid pagination)

## Status Message Handling

**Conversion**: The status message is edited into the first content message, reducing message count:
- When a status message exists, the first content message updates it via edit
- Subsequent content messages are sent as new messages

**Polling**: Background task polls terminal status for all active windows at 1-second intervals. Send-layer rate limiting ensures flood control is not triggered.

**Deduplication + timer tick**: Status updates whose action word is unchanged (only the timer/stats parenthetical moved) are normally skipped, reducing API calls. The elapsed-time display still ticks: once `STATUS_TICK_INTERVAL` (10s) has passed since the last status send/edit, a stats-only change is edited through anyway — at most one edit per topic per interval. Identical raw text always skips.

**Turn-end statusline footer**: The poller distinguishes a live working status from Claude Code's static turn-end summary line ("Cogitated for 1m 12s", `is_turn_end_status`). When a working status has been seen and the pane then stays idle for 3 consecutive polls with an empty queue (debouncing the 1s pane poll vs 2s JSONL monitor race), the terminal's statusline (the line(s) below the input box, `parse_chrome_footer`, captured verbatim — no shape assumed) is appended as code to the turn's final content message via a `turn_end_footer` task, and any leftover spinner status message is deleted. The footer task is ephemeral like status tasks: dropped under flood control, never retried, at most one append per turn.

## Rate Limiting

- `AIORateLimiter(max_retries=5)` on the Application (30/s global)
- On 429, AIORateLimiter pauses all concurrent requests (`_retry_after_event`) and retries after the ban
- On restart, the global bucket is pre-filled (`_level=max_rate`) to avoid burst against Telegram's persisted server-side counter
- Status polling interval: 1 second (skips enqueue when queue is non-empty)
- Per-queue `RetryAfter` handling: a `content`/`interactive_ui` task hitting `RetryAfter` is retried **in place** (same queued item) up to `MAX_CONTENT_RETRY_ATTEMPTS` (5) times, sleeping the required seconds between attempts — nothing else runs on that topic's queue meanwhile, so FIFO order holds. A long ban (`retry_after > FLOOD_CONTROL_MAX_WAIT`) also records `_flood_until` so producers skip enqueuing fresh status updates while banned. `status_update`/`status_clear` tasks are ephemeral and are dropped (after waiting out a short ban) instead of retried. A `content` task dropped after exhausting retries (or on any non-`RetryAfter` exception) gets a best-effort plain-text failure notice sent to the topic, so silence never means "delivered".

## Performance Optimizations

**mtime cache**: The monitoring loop maintains an in-memory file mtime cache, skipping reads for unchanged files.

**Byte offset incremental reads**: Each tracked session records `last_byte_offset`, reading only new content. File truncation (offset > file_size) is detected and offset is auto-reset. Delivery contract: **read → dispatch → observe success → commit**. A batch's offset is only persisted once every message in it has been handed to the message callback without raising; while a batch awaits that outcome the session is held in-flight, which also backpressures further reads for it (no session ever has two dispatch batches racing each other). A callback failure re-reads and re-dispatches the same batch next poll cycle instead of losing it — this is an at-least-once contract (duplicates are possible on a mid-batch failure, preferred over silent loss). A poison batch is bounded to 3 consecutive delivery attempts, after which the offset is committed anyway and the drop is logged.

## No Message Truncation

Historical messages (tool_use summaries, tool_result text, user/assistant messages, thinking) are always kept in full — no character-level truncation at the parsing or formatting layer. Long text is handled exclusively at the send layer: tool summaries/errors carry their full content via an expandable blockquote (budget-limited only at the MarkdownV2 render step, not dropped); user and assistant text — including thinking — paginate through the same `split_message` path instead of being cut off at a fixed character count. Real-time messages get `[1/N]` text suffixes, history pages get inline keyboard navigation.
