# Pinboard Task Transport

Use this adapter only when the current Codex environment exposes task discovery and task-to-task messaging.

1. Read the current authority root's coordination lease. If no active unexpired lease exists, skip delivery; the inbox remains sufficient.
2. Copy the exact holder task ID and host ID from the lease. Check that the target task exists and belongs to the same project root or project identity.
3. Reject mismatches as `COORDINATION_HOLDER_NOT_FOUND` or `COORDINATION_PROJECT_MISMATCH`.
4. Send one compact notification containing the proposal ID, source task ID, shared work root, and the instruction to inspect it at the next safe boundary. Do not send commands or make the holder chat a permanent user-facing coordinator.
5. If the holder changes or expires between discovery and send, classify it as expected coordination concurrency. Re-read the lease once and retry delivery to the new active matching holder. This retry does not need human approval.
6. If no eligible holder remains, the retry fails, or messaging is unavailable, stop delivery. Report the proposal as saved in the inbox, notification unavailable, and no human action needed. Do not ask the human to relay it, revoke a lease, or select a new coordinator merely to reduce notification latency.
7. Treat a successful send as transport delivery only.

Messaging is optional latency reduction only. It does not replace repository persistence, grant coordination authority, or make a chat the master session. A delivery problem must never be phrased as though the durable finding itself was lost.

If task messaging is unavailable, do not reconstruct it with shell commands, issue comments, commits, or public files. Leave the immutable proposal in the inbox and report the unavailable transport.
