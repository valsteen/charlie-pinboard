---
name: pinboard
description: Coordinate one pinboard for live status, next-work selection, safe parallel work, admission, lifecycle changes, review acceptance, recovery, or long-running continuity. Use when a user asks what should happen next or wants shared work state changed. Do not use to implement one already accepted attempt; use $pinboard-deliver instead.
---

# Coordinate with the pinboard

Coordinate shared work through one project-local ledger while keeping knowledge organized independently by topic and execution isolated by attempt. Support one chat and concurrent chats through the same lease protocol; never require a permanent master chat.

## Start from executable state

1. Resolve this plugin's executable relative to this file as `../../scripts/pinboard`.
2. For ordinary orientation, run `pinboard overview --json` from the repository checkout and treat its revision-stamped result as the complete default input. When the user has already supplied an exact mutation and its semantic inputs, do not add an overview preflight merely because this skill triggered; use the narrow canonical command or action query directly.
3. If `.codex/work` is absent, report `WORKFLOW_UNAVAILABLE`. Initialize it with `pinboard init` only when the user explicitly requests coordinated-work setup. Do not ask for a permanent coordinator identity.
4. Require the returned authority to be exactly `sqlite-v1`. If validation fails or another authority is reported, stop state-consuming work and use the recovery procedure in `references/state-and-recovery.md`; never infer a fallback from generated views or archived files.
5. Inspecting SQLite state needs no lease. For one graph-wide transition, prepare its semantic payload first, using `pinboard input-contract <action-kind> --json` when the fields are not already known. Then use `pinboard coordination apply --task-id <current-task> --host-id <current-host> --action-id <kind:subject> --payload <file>`; it validates the payload before borrowing coordination, uses a 60-second lease by default, applies one exact legal action, and releases authority before returning. The `close` convenience command has the same borrow-and-release shape for terminal human decisions.
6. Acquire coordination manually only when an immediate sequence genuinely requires the same authority. Prepare every input first, perform no exploratory reads while holding the lease, query only the needed action with `pinboard actions --role coordinator --lease-id <coordination-lease> --generation <generation> --action-id <kind:subject>`, and release immediately after the atomic change. Before an attempt-local change, use that attempt's lease instead.

Tell the user how to proceed from the chat they are using. In one-chat use, that chat borrows coordination and owns its attempt. In multi-chat use, recommend one chat per distinct outcome. If coordination or a resource is busy, name the current holder and expiry, explain what can continue offline, and ask about revocation only when waiting is unsuitable.

Do not infer work from arbitrary Markdown, historical plans, topic folders, unchecked boxes, branch names, or transcript memory. Do not directly edit canonical lifecycle fields when the executable can perform the transition.

## Match detail to the question

Default “where do we stand?”, “what remains?”, “quick status”, and equivalent orientation questions to the single `overview` result. Report the current focus, active attempts, live items, inbox, immediate choices, and the short revision stamp. Do not read history, topic context, GitHub, branches, CI, delivery state, or old decisions for this default answer. Do not acquire a lease merely to explain the live picture.

Keep chat usable without GUI affordances. End every compact overview with one short, explicit offer to expand the useful layers: recommendation and item context; completed decisions; delivery and CI; or full history. Phrase the offer as a natural continuation of the conversation, such as “If useful, I can explain why X should come next, recap decisions we already closed, check what shipped and whether CI is green, or reconstruct the full project history.” Follow it with the optional shortcut `Quick reply: 1 why next · 2 decisions · 3 delivery/CI · 4 full history`. Accept the visible number, a short phrase such as `decisions`, or an ordinary sentence as the same intent. The numbers belong only to that visible offer; do not imply that they are global application commands.

Do not present bare option names as a robotic command menu. Keep the conversational sentence primary and the shortcut line optional. Do not invent slash commands for these expansions. `$pinboard` is an explicit entry point for invoking the skill in a new or unrelated context, where terse prompts such as `$pinboard quick status` or `$pinboard full history` are valid. It is not a required subcommand inside an ongoing pinboard conversation.

Fetch only the selected expansion:

- For a recommendation or deeper rationale, use the overview itself when it contains enough evidence. Otherwise read only the exact live item records that are plausible choices. Apply the ordering below only then.
- For completed decisions, read terminal item history only. Do not add GitHub or delivery checks.
- For delivery or CI, inspect Git and GitHub only. Do not reconstruct ledger history.
- For full history, say that the answer will be slower, then read the complete relevant ledger and topic context. Add delivery state only if the user includes it.

When an expansion needs several known files or facts, fetch them in one bounded batch when the available tool supports it. Do not turn one requested layer into a serial command-per-item walk. If the user explicitly asks for a zero-tool or last-known answer, reuse the most recent overview from the current conversation and label it with its revision and the fact that it was not refreshed. Never present it as current. A requested expansion is not permission to preload the other layers.

This proportional behavior applies to mutation as well as status. After a successful command returns a revision-stamped receipt, do not run a redundant overview merely to confirm what the command already proved. Run broader verification only when the operation lacks a sufficient postcondition or the user asks for it.

## Ownership

- Let `state.sqlite3` own lifecycle, focus, dependencies, attempts, leases, resources, proposals, history, and accepted artifact references.
- Let `views/` remain generated human-readable output. Never edit it as authority.
- Let accepted brief and evidence artifacts own execution semantics and review receipts; resolve them through their SQLite artifact references.
- Let immutable inbox rows hold proposals that have been delivered but not admitted.
- Let `topics/` organize findings, designs, evidence, and human navigation without owning work state.
- Let public project documentation own stable architecture and domain truth.

One current coordination lease may authorize graph-wide transitions. Disjoint attempt leases may authorize item-local work concurrently. Expiry, release, revocation, and higher fencing generations invalidate retained actions.

## Coordinate delegated attempts

When a coordinating chat launches a worker for an active attempt:

1. Make `attempt.md` the only semantic execution brief. Its front matter uses `kind: work-attempt` and `schema: pinboard-work-brief/v1`. Put the accepted scope, checkpoint, named source selectors, acceptance criteria, and exact checks there.
2. Give every dispatched checkpoint one explicit `Checkpoint boundary: local` or `Checkpoint boundary: cross-boundary` line. Use `cross-boundary` when that checkpoint changes one contract across multiple production owners or required consumers. Local checkpoints retain their ordinary lightweight path.
3. Give every checkpoint exactly one architecture declaration. Use `Architecture impact: none — <reason>` when it changes no owner or dependency direction. Use `Architecture impact: read-only — \`<project-relative authority selector>\` — <reason>` when implementation must conform to architecture without changing it. Use `Architecture impact: update-required — \`<project-relative authority selector>\` — <reason>` when the same candidate must update that authority. Do not defer an architecture update to a separate work item.
4. Before launching a cross-boundary checkpoint, read [references/brief-preservation.md](references/brief-preservation.md) completely. Compile the reviewed-authority inventory, authoritative coverage, contract, and lifecycle disposition from the exact named sources. Commission one fresh-context read-only reviewer to test every cheapest counterexample, including the declared architecture impact, and publish digest-bound ready evidence. When coverage is missing or ambiguous, correct the brief before implementation; do not file a code defect for code that does not exist.
5. Select the exact `dispatch:<attempt>` action. Record only `schema`, `checkout`, `branch`, `starting_revision`, and `permissions` in a `pinboard-dispatch/v1` environment JSON file. Permission values are `repository-read`, `repository-write`, `network`, `external-write`, and `live-application`.
6. Run `pinboard dispatch` with the action tokens, exact checkpoint heading, and environment file. Launch the worker with the rendered prompt unchanged. Use `--prompt` to verify a prompt received through another transport before launch.
7. Release coordination after dispatch preparation. Retain the worker identifier and its attempt lease. Do not edit the worker's checkout or review candidate while it is still changing.
8. Wait until the worker finishes or needs attention. A wait timeout, progress update, or successful launch is not a terminal result.
9. After completion, read `result.md` or `blocker.md`, inspect the stable candidate, and perform the review described below.

The dispatch prompt identifies the canonical brief, checkpoint, and execution environment. It must not repeat acceptance semantics, checks, deferrals, or source-reading instructions. Change the attempt first when its contract is wrong.

Do not end the coordinating turn merely because implementation started. End with a running worker only when the user explicitly requested background execution; report that independent review remains pending and perform it before any acceptance transition.

The chat performing acceptance reviews the frozen candidate without coordination. Only after reaching a complete verdict does it use one-shot coordination for the review outcome. Use `complete:<attempt>` only when the entire work-item outcome is accepted. When an independently buildable checkpoint is accepted and the canonical brief records remaining work, use `accept-checkpoint:<attempt>` with the nominal checkpoint ID, exact candidate identity, and nonempty review evidence. That transition archives the exact current `result.md` and `review.md`, pauses the same item and attempt, fences worker and task-use authority, and retains host-local reservations. Replace the canonical checkpoint section in `attempt.md`, validate it, and only then use the ordinary `resume:<item>` and dispatch flow. When review finds an actionable defect, use `return-for-correction:<attempt>` with one concise reason that identifies the durable review evidence. The transition preserves the same attempt and evidence, fences its previous worker and resource authority, and leaves it ready for an explicitly selected worker to reacquire. If an additional reviewer is useful, launch it after the worker freezes and returns the candidate; the bounded worker does not recruit or substitute its own reviewer.

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

## Preview and launch independent work

When the user asks what can run in parallel, asks to choose a batch, or explicitly asks to launch independent work, read `references/parallel-work.md` and follow it completely.

Use `pinboard parallel preview` as read-only structural evidence. It does not authorize task creation, decide product priority, or select among items that share a host-local resource. Keep these outcomes distinct:

- listing or previewing creates no tasks;
- an exact selected subset or “all safe work” is launch authority for that batch only;
- items marked `requires-selection` stay out of an all-safe launch until the user chooses among them;
- each external creation receives a fresh structural check and an explainable visible-task or subagent recommendation.

Never describe a partial external launch as complete. Report one result per requested item and retain the current repository work as the main topic.

## Apply a transition

1. Identify one action as `<kind>:<subject>`. When its semantic input is not already settled, run `pinboard input-contract <kind> --json` and prepare the payload before borrowing authority.
2. For one graph-wide change, prefer `pinboard coordination apply`. Its revision-stamped result records the exact post-commit SQLite revision; coordination is then released before the receipt returns. Do not add an action-list preflight or a confirming overview.
3. For an attempt-local change or an exceptional manual coordination sequence, query only the exact action with `pinboard actions --role <role> --lease-id <lease> --generation <generation> --action-id <kind:subject> --json`. Treat the returned action record as one opaque capability receipt. Forward its `action_id`, `expected_revision`, `coordinator_generation`, `authorization`, and every non-empty `subject_revision`, `lease_id`, and `resource_claims` value verbatim. Do not infer authorization from the current role, reuse fields from another action, or reconstruct a partial token from prose.
4. Run `pinboard transition` with those exact token fields and the prepared payload file. Repeat `--resource-claim` once for every returned claim, preserving its resource, host, lease, and generation.
5. If either path returns `ACTION_NOT_AVAILABLE`, do not retry the same command, switch roles, or skip to another lifecycle state. Validate once and refresh only the same exact action under fresh authority. If it disappeared, explain the intervening state change. If it remains available with fresh tokens, report an executable contradiction, preserve current attempt evidence, and stop transition work until the command is corrected.
6. If a task is interrupted before a receipt is visible, never replay its retained action. Follow the interrupted-transition recovery in `references/state-and-recovery.md`; authoritative state distinguishes no change from one complete committed transition, and a fresh lease generation fences the abandoned action.
7. Report the returned transition result. Run `pinboard validate` immediately after any non-tool edit to supporting topic or attempt artifacts.

The executable rejects stale subject scopes, expired or replaced lease holders, illegal states, invalid dependencies, missing resource claims, and inconsistent references. It does not decide whether evidence is true, whether work is valuable, or what product behavior should mean.

For an explicit terminal human decision about non-active live work, use one `pinboard close <item> --outcome <done|dropped> --reason <text> --task-id <current-task> --host-id <current-host>` invocation instead of manufacturing intake, ready, active, or attempt states. The command borrows and releases SQLite coordination internally. Do not precede this exact command with overview or action-list reads. Use `done` when the recorded decision or outcome is complete and may satisfy dependents. Use `dropped` only when the work is intentionally abandoned and has no live dependents. Never use `close` to bypass review for active or review work; keep the normal evidence-backed completion transition there. The returned revision is the durable receipt, so do not create a temporary payload file or re-read status after success.

## Safe boundaries

Process newly delivered intake only after the current review, transition, commit, or other atomic repository action ends. Delivery does not authorize interrupting or widening an active attempt.

Intake is scheduling-neutral. It persists an immutable proposal and may change the ledger revision, but it does not pause, activate, admit, reorder, focus, or complete work. When intake is embedded in ongoing coordination, keep a compact continuation anchor in the current task context before invoking it:

- the pre-intake objective;
- the next action already promised;
- the exact active or paused item, attempt, proposal, or durable selector that owns the facts after context compaction.

After persistence and optional notification handling, return to that anchor in the same turn. Re-read the durable selector when compaction made conversational memory uncertain; do not create a scheduler or recovery record. Before the final response, reconcile every announced pending action as completed, durably deferred at an exact owner, or blocked by one exact decision.

Deliberate steering is distinct from intake. Insert a genuine prerequisite only at a safe boundary, pause or block the displaced objective through its legal transition, and name the ordinary `resume:<item>` action that restores it. Recovery is for interrupted or inconsistent state, not the normal route back to preserved work.

### Reconcile material findings before reporting them

Before presenting a discovered out-of-scope finding as planned, preserved, covered, queued, deferred, or future work, determine the disposition of that exact finding. A broader item with a compatible theme is not exact coverage.

Use one of these durable dispositions:

- `already recorded`: the exact observation and its consequence were durable before the current report;
- `recorded now`: the exact observation was absent and this turn added it to its authoritative item context or created an intake proposal;
- `not recorded`: no exact durable owner exists, persistence was not authorized, or persistence failed.

When the finding belongs to an existing admitted item but its semantic context is incomplete, add the exact observation to that item's context arc and run `pinboard validate`. When no admitted item owns it, use `$pinboard-intake` if the user has explicitly authorized preservation.

Keep current work as the main topic and lead the receipt with its practical outcome, not the internal disposition name:

- For `already recorded` or `recorded now`, say `Saved for later — <finding> <was already recorded at selector | was recorded now> in <exact owner and state>; current work <continues | is blocked by it>.`
- For an explicit dismissal, say `Finding dismissed — <finding> was not saved at your request; no follow-up remains.`
- For completed work, say `Completed — <result>; no follow-up needed.` Do not introduce completed or dismissed work as a durable finding or saved follow-up.

A material `not recorded` disposition is unresolved and must not end as a bare receipt. State the finding, exact cause, durable state, current-work impact, and next action owner. Then produce exactly one resolution:

- If preservation lacks authorization, ask one concrete preserve-or-dismiss question: `Finding needs a decision — <finding> is not saved because <cause>. Current work <impact>. Should I preserve it for later or dismiss it?`
- If a safe retry needs no new authority, announce it as expected concurrency and retry before the terminal response: `Persistence delayed — <finding> is not yet saved because <cause>. Current work <impact>. I am retrying now; no action needed.`
- If retry needs new authority, changes scope, or overrides another owner, ask one concrete approval question that names the action and consequence.
- If persistence still fails after the permitted retry, report the same four facts and ask the one decision that can actually resolve it. Never leave `not recorded` as a terminal aside.

Let `recorded now` mean this turn before the update. For `already recorded`, include the earlier durable selector or timestamp. Use separate labeled fields only when the compact outcome-first line cannot make a persistence failure, ambiguous owner, blocking impact, or required decision unmistakable.

Never use a later edit to imply earlier coverage. If a broad item existed before the report but the exact observation was added only after a question or challenge, say both facts explicitly.

When an active attempt discovers a prerequisite:

1. preserve its current result and verification;
2. use `$pinboard-intake` to propose the prerequisite;
3. block or pause the attempt through an available action;
4. implement the accepted prerequisite independently from the current integration base;
5. resume the preserved attempt only after its freshness check passes.

Never absorb the prerequisite silently into the current attempt.

## Review and completion

Treat worker completion as a review request, not acceptance. Compare the attempt brief, diff or commit, result receipt, and fresh proportionate verification. Check the architecture declaration against the final owner and dependency-direction diff: `none` must be truthful, `read-only` must conform to its named authority, and `update-required` must include a coherent same-candidate authority change. For a reviewed cross-boundary brief, reuse its compiled authority map and account for every acceptance criterion, contract row, coverage row, and lifecycle sibling row. A correction review may reuse exact unchanged selector digests and owners, but it must re-read changed owners and sweep every changed or neighboring row before returning one complete correction package. Complete the item only when every acceptance criterion is satisfied and current knowledge owners are reconciled.

For accepted intermediate work, use the checkpoint acceptance path above. Its paused item and attempt remain nonterminal, checkpoint evidence remains immutable under the same attempt, and terminal item history remains absent until a later full-outcome completion.

When review rejects the candidate, record the exact correction reason through `return-for-correction:<attempt>` instead of completing the item, manufacturing a new attempt, or editing lifecycle files. Report the outcome compactly: `Returned for correction — <reason>; the same attempt and review evidence are preserved and ready for a named worker to reacquire. No acceptance occurred.` If the executable does not offer that action for an item in review, preserve the review evidence and report the workflow blocker; do not bypass the missing transition.

Move terminal scheduling state out of the live queue in the same transition that preserves its history receipt. Do not manufacture follow-up work when the honest result is no follow-up.

Read `references/state-and-recovery.md` only for lease revocation, invalid SQLite state, or interrupted transitions.
