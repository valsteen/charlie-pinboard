---
name: bounded-implementer
description: Execute exactly one accepted repository work attempt from its project-local attempt brief. Use when an item is already active, a branch or worktree and acceptance criteria are recorded, and implementation must avoid scope drift, role drift, or backlog mutation. Do not use for intake, portfolio selection, broad audits, migration design, or coordinator review.
---

# Bounded Implementer

Implement one accepted attempt, verify it, and leave a result the registered coordinator can review.

## Establish the attempt

1. Resolve the repository-work executable relative to the installed plugin as `../../scripts/repo-work`.
2. Run `repo-work status --json` and `repo-work actions --role worker --json`.
3. Require one active item and one active attempt. Stop if state is invalid, idle, or names a different attempt than the user supplied.
4. Read the active `attempt.md` fully, then read only the project guidance, item context, accepted knowledge, and source authorities it names.
5. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

Restate the objective, non-goals, affected boundaries, testing mode, acceptance criteria, and completion checks briefly. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Do not edit `queue.md`, `current.md`, or `coordinator.json`.
- Do not accept or complete your own item.

If additional work is useful but not required, invoke `$repo-work-intake` only when the user explicitly wants it preserved. Otherwise mention it in the result without creating shared state.

If a discovered problem blocks the attempt:

1. stop widening the implementation;
2. preserve the current commit/worktree and verification;
3. write `blocker.md` in the active attempt directory with the observation, affected criterion, completed work, and safest next action;
4. use `$repo-work-intake` to propose a prerequisite when explicitly requested;
5. report the blocker so the coordinator can choose the available block or pause transition.

## Implement and verify

Use the repository's selected testing mode. Prefer the smallest evidence that can disprove the important failure, then run the broader changed-surface gate required by the attempt.

Before review:

1. finish the coherent implementation batch;
2. run the required focused checks and formatter/linter gates;
3. inspect the final diff;
4. identify the stable candidate by commit or working-tree fingerprint;
5. map every acceptance criterion to code, test, or evidence;
6. write `result.md` in the attempt directory.

The result must record:

- candidate identity and changed files;
- concise implementation result;
- acceptance-criterion evidence;
- verification commands and outcomes;
- preserved unrelated changes;
- new findings or exact unknowns;
- whether the attempt is ready for review or blocked.

Finish by reporting that the result was submitted for coordinator review. Do not claim canonical completion until the coordinator applies the completion transition.
