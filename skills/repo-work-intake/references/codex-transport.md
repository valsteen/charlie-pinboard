# Codex Task Transport

Use this adapter only when the current Codex environment exposes task discovery and task-to-task messaging.

1. Read the current authority root's coordination lease. If no active unexpired lease exists, skip delivery; the inbox remains sufficient.
2. Copy the exact holder task ID and host ID from the lease. Check that the target task exists and belongs to the same project root or project identity.
3. Reject mismatches as `COORDINATION_HOLDER_NOT_FOUND` or `COORDINATION_PROJECT_MISMATCH`.
4. Send one compact notification containing the proposal ID, source task ID, shared work root, and the instruction to inspect it at the next safe boundary. Do not send commands or make the holder chat a permanent user-facing coordinator.
5. Treat a successful send as transport delivery only.

Messaging is optional latency reduction only. It does not replace repository persistence, grant coordination authority, or make a chat the master session.

If task messaging is unavailable, do not reconstruct it with shell commands, issue comments, commits, or public files. Leave the immutable proposal in the inbox and report the unavailable transport.
