---
name: pinboard-deliver
description: Deliver exactly one active pinboard attempt from its accepted brief and renewable lease. Use when the item, checkout, scope, acceptance criteria, and verification are already recorded. Do not use for intake, portfolio selection, broad audits, design exploration, or acceptance review.
---

# Deliver from the pinboard

Implement one accepted attempt, verify it, leave a durable result, and return it accurately for independent coordination review.

## Establish the attempt

1. Resolve the pinboard executable relative to the installed plugin as `../../scripts/pinboard`.
2. Run `pinboard status --json` and require authority `sqlite-v1`. Stop if validation fails or another authority is reported; never infer current state from generated views or archived files. Acquire or validate the user-supplied attempt lease, then run `pinboard actions --role worker` with its lease identity and fencing generation.
3. Require the user-supplied attempt to be present and active. Other disjoint attempts may also be active. Stop if state is invalid, the supplied attempt is absent, its item and attempt records disagree, or another unexpired owner holds it. Report that owner and expiry instead of guessing or silently revoking it.
4. Read the attempt's accepted canonical `.json` brief fully. Its generated Markdown view is inspection convenience, not an editable or parseable contract. Then read only the project guidance, item context, accepted knowledge, and source authorities the JSON record names. Before loading several or potentially large named authorities, create a temporary `pinboard-brief-sources/v1` manifest and run `pinboard brief-sources --file <manifest> --json` to measure the complete set. Read each emitted batch once in order. Preserve exact selectors and selected digests; reuse unchanged receipts and reread only changed owners. If output truncates, continue at the first unread batch or line without replaying returned content.
5. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

Confirm the attempt identity, stable checkpoint ID, and execution environment without rewriting its semantics. The canonical JSON brief remains the sole source for scope, ordering, deferrals, and verification. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

When reacquiring an attempt returned from review, read the durable correction reason and `review.md` before editing. Keep the same accepted brief, branch, evidence, and attempt identity. Treat the earlier `result.md` as preserved history, not a current readiness claim; refresh it only after the corrected candidate is stable and all required checks pass.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout. Disjoint attempts may proceed concurrently in separate checkouts.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Do not edit generated views, SQLite authority, coordination leases, or another item's lifecycle state outside the executable workflow.
- Do not accept or complete your own item.
- Prepare the stable candidate for independent review, then return it using the confirmed-delivery rules below. A later chat may borrow coordination to review and accept it; no permanent coordinator chat is required. Do not invoke a generic reviewer-dispatch workflow or launch another reviewer from the worker role.

Worker diff inspection, requirement mapping, and fresh verification are pre-review evidence. They do not replace independent review under a current coordination lease.

For a cross-boundary attempt, map every observable behavior and behavior-defining test to an authorized Contract row before adding it. The row's `Authorization basis` must resolve to the current accepted scope or to an exact reviewed authority family. Ordinary internal choices that preserve supported behavior—such as helper names, local refactors, and equivalent algorithms—need no separate provenance record.

Run every basis-bearing entry in the accepted `Verification` section as a mandatory check. Do not add a tool, threshold, platform promise, compatibility obligation, or hardening check to that mandatory list unless the brief gives it an accepted basis. Proportionate exploratory checks remain available when they help implementation, but they do not become acceptance obligations merely because they were run or suggested during review.

When a useful addition has no authorized row, stop widening that part of the implementation and report the unsupported addition. Continue independent in-scope work when possible. Create an intake proposal only when the user explicitly authorizes preservation; proposal creation does not authorize implementation. Do not invent a ledger state or transition to represent the discovery.

If additional work is useful but not required, invoke `$pinboard-intake` only when the user explicitly wants it preserved. Otherwise mention it in the result without creating shared state.

When authorized intake is nested inside delivery, retain the attempt ID, current accepted objective, and next promised implementation or verification action as a continuation anchor. After the proposal is persisted and optional delivery handling ends, return to that action. Intake alone does not pause or reprioritize the attempt. Before the final result, account for every announced pending action as completed, durably deferred at an exact owner, or blocked by one exact decision.

If a discovered problem blocks the attempt:

1. stop widening the implementation;
2. preserve the current commit/worktree and verification;
3. write `blocker.md` in the active attempt directory with the observation, affected criterion, completed work, and safest next action;
4. use `$pinboard-intake` to propose a prerequisite when explicitly requested;
5. report the blocker so any chat can borrow coordination and choose the available block or pause transition.

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

For an independently buildable checkpoint, report the exact candidate and remaining-work boundary without claiming that the whole item is complete. Checkpoint acceptance belongs to an independent coordinator after review; the worker does not archive its own checkpoint evidence or resume the next checkpoint.

## Return the candidate for review

`result.md` makes the candidate durably ready for review. It does not by itself notify another task.

1. When the launch includes an exact `source_thread_id` and task messaging is available, send that task a concise review request containing the attempt ID, candidate identity, and absolute `result.md` path. Treat delivery as successful only after the messaging tool confirms it.
2. If the launch has no exact return task, messaging is unavailable, or delivery fails, do not guess a destination or create a reviewer. Report `ready for coordination review` and state plainly that no task was notified. The durable receipt remains sufficient for any later chat to borrow coordination and review the candidate.
3. Say `submitted for coordination review` only after confirmed delivery to the exact return task. A written receipt without confirmed delivery is `ready for coordination review`, not submitted.

Do not claim canonical completion until a current coordination lease authorizes the completion transition.

If the attempt was returned for correction, report the new candidate normally. Do not present the return itself as a new finding or imply that the earlier review was accepted. The compact human outcome is: `Correction ready — <candidate>; the same attempt has been resubmitted for independent review.`
