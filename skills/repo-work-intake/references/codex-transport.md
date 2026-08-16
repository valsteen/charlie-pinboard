# Codex Task Transport

Use this adapter only when the current Codex environment exposes task discovery and task-to-task messaging.

1. Read `.codex/work/coordinator.json` and copy the exact task ID and host ID.
2. Check that the target task exists and belongs to the same project root or project identity.
3. Reject mismatches as `COORDINATOR_NOT_FOUND` or `COORDINATOR_PROJECT_MISMATCH`.
4. Send one compact notification containing the proposal ID, source task ID, shared work root, and the instruction to inspect it at the next safe boundary.
5. Treat a successful send as transport delivery only.

An explicitly supplied coordinator task ID may help discover a missing registration, but it does not replace or bypass ownership. Register or transfer the coordinator explicitly before persisting intake.

If task messaging is unavailable, do not reconstruct it with shell commands, issue comments, commits, or public files. Leave the immutable proposal in the inbox and report the unavailable transport.
