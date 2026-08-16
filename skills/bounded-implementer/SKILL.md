---
name: bounded-implementer
description: Execute exactly one accepted repository work attempt from its project-local attempt brief. Use when an item is already active, a branch or worktree and acceptance criteria are recorded, and implementation must avoid scope drift, role drift, or backlog mutation. Do not use for intake, portfolio selection, broad audits, migration design, or coordinator review.
---

# Bounded Implementer

Implement one accepted attempt, verify it, and leave a result the registered coordinator can review.

## Establish the attempt

1. Resolve the repository-work executable relative to the installed plugin as `../../scripts/repo-work`.
2. Run `repo-work status --json` and `repo-work actions --role worker --json`.
3. Require the user-supplied attempt to be present and active. Other disjoint attempts may also be active. Stop if state is invalid, the supplied attempt is absent, or its queue and attempt records disagree.
4. Read that attempt's `attempt.md` fully, then read only the project guidance, item context, accepted knowledge, and source authorities it names. Preserve exact file and section selectors instead of broadening them into exploratory reads.
5. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

Confirm the attempt identity, checkpoint, and execution environment without rewriting its semantics. The canonical attempt remains the sole source for scope, ordering, deferrals, and verification. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Do not edit `queue.md`, `current.md`, or `coordinator.json`.
- Do not accept or complete your own item.
- Return the stable candidate to the registered coordinator for independent review. Do not invoke a generic reviewer-dispatch workflow or launch another reviewer from the worker role.

Worker diff inspection, requirement mapping, and fresh verification are pre-review evidence. They do not replace the coordinator's independent review.

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
2. run every command required by the attempt, without replacing it with a narrower package, test, formatter, or linter command;
3. inspect the final diff;
4. identify the stable candidate by commit or working-tree fingerprint;
5. map every acceptance criterion to code, test, or evidence;
6. if an existing test file shrank materially, inventory the removed behavior or test names and identify the replacement evidence;
7. when acceptance claims lifecycle wiring, prove it through the production entry point rather than only through an internal primitive;
8. write `result.md` in the attempt directory.

The result must record:

- candidate identity and changed files;
- concise implementation result;
- acceptance-criterion evidence;
- verification commands and outcomes;
- any material test removal and its replacement evidence;
- production-entry-point evidence for lifecycle claims;
- preserved unrelated changes;
- new findings or exact unknowns;
- whether the attempt is ready for review or blocked.

Finish by reporting that the result was submitted for coordinator review. Do not claim canonical completion until the coordinator applies the completion transition.
