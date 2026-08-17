---
name: repo-work
description: Coordinate one repository-owned work ledger for backlog orientation, next-work selection, admission, activation, shelving, resumption, review, migration, or long-running work continuity. Use when a user asks what to do next, wants repository work state changed, resumes coordinated work, manages prerequisites or concurrent attempts, or replaces an audit/theme-owned queue. Do not use for ordinary implementation that already has one accepted attempt.
---

# Repository Work

Coordinate shared work through one project-local ledger while keeping knowledge organized independently by topic and execution isolated by attempt.

## Start from executable state

1. Resolve this plugin's executable relative to this file as `../../scripts/repo-work`.
2. Run `repo-work status --json` from the repository checkout.
3. If `.codex/work` is absent, report `WORKFLOW_UNAVAILABLE`. Initialize it only when the user explicitly requests coordinated-work setup and supplies the exact coordinator task and host identities.
4. If validation fails, stop state-consuming work and use the recovery procedure in `references/state-and-recovery.md`.
5. Run `repo-work actions --role coordinator --json` and present only the actions it returns.

Do not infer work from arbitrary Markdown, historical plans, topic folders, unchecked boxes, branch names, or transcript memory. Do not directly edit canonical lifecycle fields when the executable can perform the transition.

## Ownership

- Let `queue.md` own every nonterminal item's state, dependencies, current attempt, source, and next action.
- Let `current.md` remain an optional validated coordinator-focus pointer, never a second queue or a limit on concurrent attempts.
- Let `items/<item>.md` own the context arc and semantic rationale without duplicating lifecycle state.
- Let `attempts/<attempt>/` own the execution brief, branch/worktree, result, blocker, and review evidence.
- Let `inbox/` hold immutable proposals that have been delivered but not admitted.
- Let `topics/` organize findings, designs, evidence, and human navigation without owning work state.
- Let public project documentation own stable architecture and domain truth.

One registered coordinator generation owns canonical transitions. Multiple tasks may create unique intake proposals. Multiple workers may execute disjoint accepted attempts in separate worktrees.

## Coordinate delegated attempts

When the coordinator launches a worker for an active attempt:

1. Make `attempt.md` the only semantic execution brief. Put the accepted scope, checkpoint, named source selectors, acceptance criteria, and exact checks there.
2. Give every dispatched checkpoint one explicit `Checkpoint boundary: local` or `Checkpoint boundary: cross-boundary` line. Use `cross-boundary` when that checkpoint changes one contract across multiple production owners or required consumers. Before launching a cross-boundary checkpoint, also record `Checkpoint outcome: independently-buildable` and a compact `Contract table` with these columns: `Invariant`; `Authority / owner`; `Required consumer or production observation`; `Failure classification`; `Exact verification`; `Preflight / final revalidation`.
3. Select the exact `dispatch:<attempt>` action. Record only `schema`, `checkout`, `branch`, `starting_revision`, and `permissions` in a `repo-work-dispatch/v1` environment JSON file. Permission values are `repository-read`, `repository-write`, `network`, `external-write`, and `live-application`.
4. Run `repo-work dispatch` with the action tokens, exact checkpoint heading, and environment file. Launch the worker with the rendered prompt unchanged. Use `--prompt` to verify a prompt received through another transport before launch.
5. Retain coordinator ownership and the worker identifier. Do not edit the worker's checkout or review candidate while it is still changing.
6. Wait until the worker finishes or needs attention. A wait timeout, progress update, or successful launch is not a terminal result.
7. After completion, read `result.md` or `blocker.md`, inspect the stable candidate, and perform the review described below.

The dispatch prompt identifies the canonical brief, checkpoint, and execution environment. It must not repeat acceptance semantics, checks, deferrals, or source-reading instructions. Change the attempt first when its contract is wrong.

Do not end the coordinator turn merely because implementation started. End with a running worker only when the user explicitly requested background execution; report that coordinator review remains pending and resume it before any acceptance transition.

The registered coordinator owns independent review. If an additional reviewer is useful, the coordinator launches it after the worker freezes and returns the candidate; the bounded worker does not recruit or substitute its own reviewer.

## Interpret the available actions

Translate executable actions into ordinary repository and product language. Retain the exact action record privately for execution.

When selecting work, explain:

1. the current product or repository trajectory;
2. what changed after the last accepted item;
3. which items are ready and why;
4. which items are blocked and the exact unresolved condition;
5. which ordering is mechanically determined by dependencies;
6. which choice genuinely requires human priority;
7. the recommended next item and why now.

Prefer, in order:

1. evidence preservation or avoidance of an irreversible transition;
2. prerequisites required by the current product objective;
3. runtime, consumer, or user-visible product evidence;
4. foundation work that is concretely cheaper now;
5. optional hardening only after its recorded reopen condition occurs.

Do not turn that ordering into a score. A recommendation remains an explanation, not an automatic product decision.

## Apply a transition

1. Select one exact action returned by `repo-work actions --json`.
2. Prepare only the semantic payload required by that action.
3. Run `repo-work transition` with the action ID, expected revision, coordinator generation, optional proposal revision, and payload file.
4. Report the returned transition result.
5. Run `repo-work validate` immediately after any non-tool edit to supporting topic or attempt artifacts.

The executable rejects stale revisions, replaced coordinators, illegal states, invalid dependencies, and inconsistent references. It does not decide whether evidence is true, whether work is valuable, or what product behavior should mean.

## Safe boundaries

Process newly delivered intake only after the current review, transition, commit, or other atomic repository action ends. Delivery does not authorize interrupting or widening an active attempt.

### Reconcile material findings before reporting them

Before presenting a discovered out-of-scope finding as planned, preserved, covered, queued, deferred, or future work, determine the disposition of that exact finding. A broader item with a compatible theme is not exact coverage.

Use one of these outcomes:

- `already recorded`: the exact observation and its consequence were durable before the current report;
- `recorded now`: the exact observation was absent and this turn added it to its authoritative item context or created an intake proposal;
- `not recorded`: no exact durable owner exists, persistence was not authorized, or persistence failed.

When the finding belongs to an existing admitted item but its semantic context is incomplete, add the exact observation to that item's context arc and run `repo-work validate`. When no admitted item owns it, use `$repo-work-intake` if the user has explicitly authorized preservation. Otherwise report `not recorded` and ask whether to preserve it; do not imply guaranteed follow-up.

Give a formal **Durable finding receipt** whenever the finding is material enough to affect later product work or likely to make the user wonder whether it will be lost. Include:

- `Finding`: the exact observation and consequence;
- `Durable status`: `already recorded`, `recorded now`, or `not recorded`;
- `Persistence timing`: the earlier durable selector or timestamp, `this turn before this receipt`, or `not persisted`;
- `Owner`: the exact item or proposal ID, durable selector, and current lifecycle state, or `none`;
- `Current work`: `blocked` or `not blocked`, with the reason.

Never use a later edit to imply earlier coverage. If a broad item existed before the report but the exact observation was added only after a question or challenge, say both facts explicitly.

When an active attempt discovers a prerequisite:

1. preserve its current result and verification;
2. use `$repo-work-intake` to propose the prerequisite;
3. block or pause the attempt through an available action;
4. implement the accepted prerequisite independently from the current integration base;
5. resume the preserved attempt only after its freshness check passes.

Never absorb the prerequisite silently into the current attempt.

## Review and completion

Treat worker completion as a review request, not acceptance. Compare the attempt brief, diff or commit, result receipt, and fresh proportionate verification. Complete the item only when every acceptance criterion is satisfied and current knowledge owners are reconciled.

Move terminal scheduling state out of the live queue in the same transition that preserves its history receipt. Do not manufacture follow-up work when the honest result is no follow-up.

Read `references/state-and-recovery.md` only for coordinator transfer, inconsistent state, schema migration, legacy-queue migration, or interrupted transitions.
