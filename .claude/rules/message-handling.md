# Message Handling

## Message Queue

Per-user FIFO queue + worker for all send tasks. Receive order preserved; status messages always follow content; multi-user concurrent processing without interference.

Task types (`ContentTask`, `StatusTask`, `ToolResultTask`) live in `message_task.py` — a dependency-free sum type imported by `message_queue.py`, `tool_batch.py`, and `status_bubble.py` without circular imports. Inbound routing (SessionMonitor → Telegram topics) lives in `message_routing.py`.

## Message Merging

Worker merges consecutive mergeable content messages on dequeue:

- Same-window content messages can merge (text, thinking).
- `tool_use` breaks the merge chain (sent separately; message ID recorded for later editing).
- `tool_result` breaks the merge chain (edits into the `tool_use` message to prevent order confusion).
- Merging stops at 3800 chars combined to avoid pagination.

## Status Messages

The status message is edited into the first content message, reducing message count. Subsequent content messages are sent as new messages.

Polling: 1s interval for all active windows. Send layer rate-limits to avoid flood.

Dedup: worker compares `last_text` on status updates; identical content skips the edit.

## Rate Limiting

- 1.1s min between messages per user.
- Status polling: 1s (send layer protects against floods).
- Automated outbound (queue worker, status updates) goes through `rate_limit_send()`.

## Performance

- mtime cache: monitoring loop maintains in-memory mtime cache, skips reads for unchanged files.
- Byte offsets: each tracked session records `last_byte_offset`, reads only new content. File truncation (offset > size) detected; offset auto-resets.

## Crash Recovery (delivery-committed offsets)

Each tracked session carries two cursors: `last_byte_offset` (in-memory read cursor) and `delivered_byte_offset` (crash-safe). Only the delivered cursor is persisted to `monitor_state.json` (under the existing `last_byte_offset` key — schema unchanged both directions). A batch's bytes are committed when its messages reach a delivery terminal state — sent, consciously dropped, or failed-after-retry — approximated by the serving users' outbound queues being fully drained (`is_session_delivery_drained` in `message_queue.py`, wired into the monitor via `set_delivery_drained_callback` in bootstrap; join-accounting covers the task the worker currently holds). Batches producing no messages commit immediately. A crash between read and send restarts the reader at the delivered cursor and replays the lost batch — delivery is at-least-once: a crash mid-batch may re-send already-delivered messages from that batch. `SessionMonitor.stop()` runs a final commit so clean restarts don't replay.

## No Truncation

Historical messages (tool_use summaries, tool_result text, user/assistant messages) are kept in full at the parse layer. Long text is handled only at send: `split_message` splits by 4096 limit; real-time messages get `[1/N]` suffixes, history pages get inline keyboard navigation.
