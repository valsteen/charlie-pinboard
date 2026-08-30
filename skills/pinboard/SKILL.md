---
name: pinboard
description: Coordinate one pinboard for live status, next-work selection, safe parallel work, admission, lifecycle changes, review acceptance, recovery, or long-running continuity. Use when a user asks what should happen next or wants shared work state changed. Do not use to implement one already accepted attempt; use $pinboard-deliver instead.
---

# Coordinate with the pinboard

Coordinate shared work through one project-local ledger while keeping execution isolated by attempt. Support one chat and concurrent chats through the same lease protocol; never require a permanent master chat.

## Start from executable state

1. Resolve this plugin's executable relative to this file as `../../scripts/pinboard`.
2. For ordinary orientation, run `pinboard overview --json` from the repository checkout and treat its revision-stamped result as the complete default input. When the user has already supplied an exact mutation and its semantic inputs, do not add an overview preflight merely because this skill triggered; use the narrow canonical command or action query directly.
3. If the default `.codex/pinboard` root is absent, report `WORKFLOW_UNAVAILABLE`. Initialize it with `pinboard init` only when the user explicitly requests coordinated-work setup. An explicit `--work-root` remains an exact user-selected exception. Do not ask for a permanent coordinator identity.
4. Require the returned authority to be exactly `sqlite-v1`. If validation fails or another authority is reported, stop state-consuming work and use the recovery procedure in `references/state-and-recovery.md`; never infer a fallback from generated views or archived files.
5. Inspecting SQLite state needs no lease. For one graph-wide transition, prepare its semantic payload first, using `pinboard input-contract <action-kind> --json` when the fields are not already known. Then use `pinboard coordination apply --task-id <current-task> --host-id <current-host> --action-id <kind:subject> --payload <file>`; it validates the payload before borrowing coordination, uses a 60-second lease by default, applies one exact legal action, and releases authority before returning. The `close` convenience command has the same borrow-and-release shape for terminal human decisions.
6. Acquire coordination manually only when an immediate sequence genuinely requires the same authority. Prepare every input first, perform no exploratory reads while holding the lease, query only the needed action with `pinboard actions --role coordinator --lease-id <coordination-lease> --generation <generation> --action-id <kind:subject>`, and release immediately after the atomic change. Before an attempt-local change, use that attempt's lease instead.

Tell the user how to proceed from the chat they are using. In one-chat use, that chat borrows coordination and owns its attempt. In multi-chat use, recommend one chat per distinct outcome. If coordination or an attempt is busy, name the current holder and expiry, explain what can continue offline, and ask about revocation only when waiting is unsuitable.

Do not infer work from arbitrary Markdown, historical plans, unchecked boxes, branch names, or transcript memory. Do not directly edit canonical lifecycle fields when the executable can perform the transition.

## Match detail to the question

Keep Pinboard operationally invisible when it is working normally. User-facing progress and receipts describe the user's tasks, decisions, confidence, blockers, and next actions. Do not volunteer the storage backend, command mechanics, generated views, revisions, leases, routing, transport, validation steps, or unchanged coordination state. Include an implementation or process detail only when it changes the result, confidence, risk, requested action, or next step, or when the user asks to inspect or troubleshoot Pinboard itself.

Default “where do we stand?”, “what remains?”, “quick status”, and equivalent orientation questions to the single `overview` result. Report the current focus, active attempts, ordered live items, eligibility, dependency reasons, review flags, and immediate choices. Intake candidates already appear in that order; there is no separate hidden inbox to inspect. Retain the revision stamp privately as freshness evidence; show it only when the user asks for it or when it materially explains stale or conflicting state. Do not read history, project notes, GitHub, branches, CI, delivery state, or old decisions for this default answer. Do not acquire a lease merely to explain the live picture.

When continuing work from another Codex task, extract every named Pinboard item for which that task claims terminal completion and verify each claim with `pinboard item status --item-id <item-id> --json` before treating it as complete. Then use `overview` only to identify remaining live work. Absence from `overview` means only that the item is not currently live; never use that absence to report an item as missing, unrecorded, or unfinished without the exact item-status lookup. Exact item status proves the recorded item and attempt state, not acceptance of a nonterminal checkpoint.

Make the user's next move frictionless at a genuine decision edge. When a response leaves an unresolved item or coordination state with two to four useful ways to continue, end with one natural sentence that recommends the best-supported next step when evidence supports one and offers the alternatives. Follow it with a shortcut line such as `Quick reply: 1 investigate freshness · 2 admit it · 3 leave it unchanged`. Use this pattern especially after item-detail answers, priority or admission choices, and blockers that require user direction. Put the recommendation first when practical. Accept the visible number, a short phrase, or an ordinary sentence as the same intent.

Keep the conversational sentence primary and the shortcut line secondary; do not present bare option names as a robotic command menu. The numbers belong only to the visible offer and are never global application commands. A state-changing option must name its practical effect clearly, and selecting it authorizes only that described effect; resolve current action availability and gather any missing semantic input before executing it. Omit shortcuts when the result is terminal with no follow-up, the user's exact requested action has completed, only one sensible next move exists, or extra options would be filler.

End every compact overview with one short, explicit offer to expand the useful layers: recommendation and item context; completed decisions; delivery and CI; or full history. Phrase the offer as a natural continuation of the conversation, such as “If useful, I can explain why X should come next, recap decisions we already closed, check what shipped and whether CI is green, or reconstruct the full project history.” Follow it with `Quick reply: 1 why next · 2 decisions · 3 delivery/CI · 4 full history`.

Do not invent slash commands for these follow-ups. `$pinboard` is an explicit entry point for invoking the skill in a new or unrelated context, where terse prompts such as `$pinboard quick status` or `$pinboard full history` are valid. It is not a required subcommand inside an ongoing pinboard conversation.

Fetch only the selected expansion:

- For a recommendation or deeper rationale, use the overview itself when it contains enough evidence. Otherwise read only the exact live item records that are plausible choices. Apply the ordering below only then.
- For completed decisions, read terminal item history only. Do not add GitHub or delivery checks.
- For delivery or CI, inspect Git and GitHub only. Do not reconstruct ledger history.
- For full history, say that the answer will be slower, then read the complete relevant ledger and accepted artifact context. Add delivery state only if the user includes it.

When an expansion needs several known project files or headings, inspect their selected sizes with `pinboard brief-sources` before loading their bodies, then read each non-overlapping emitted batch once. For other known facts, use a bounded batch when the available tool supports it. If output truncates, continue at the first unread boundary without replaying returned content. Do not turn one requested layer into an exploratory command-per-item walk. If the user explicitly asks for a zero-tool or last-known answer, reuse the most recent overview from the current conversation and label it as not refreshed. Never present it as current. A requested expansion is not permission to preload the other layers.

This proportional behavior applies to mutation as well as status. After a successful command returns a revision-stamped receipt, do not run a redundant overview merely to confirm what the command already proved. Run broader verification only when the operation lacks a sufficient postcondition or the user asks for it.

## Ownership

- Let `state.sqlite3` own lifecycle, focus, dependencies, attempts, leases, proposals, history, and accepted artifact references.
- Let `views/` remain generated human-readable output. Never edit it as authority.
- Let accepted brief and evidence artifacts own execution semantics and review receipts; resolve them through their SQLite artifact references.
- Let immutable proposal rows preserve discovery facts while their same-identity work items own visible intake state and queue position.
- Let public project documentation own stable architecture and domain truth.

One current coordination lease may authorize graph-wide transitions. Disjoint attempt leases may authorize item-local work concurrently. Expiry, release, revocation, and higher fencing generations invalidate retained actions.

## Coordinate review responsibility and checkout use

Route frozen candidates by explicit current responsibility, not conversational ancestry. The task carrying the attempt normally commissions one fresh-context, candidate-read-only review subagent after the candidate is stable, then processes that review's complete accept-or-correct verdict. Keep the reviewer independent, commission one reviewer by default, and do not duplicate the full review or add reviewers without a concrete risk.

Use another visible Codex task for review only when the user explicitly requests that task or it remains the active user-facing coordinator for the same live workflow. Dispatching work, clarifying or widening scope, sending a prior message, or once holding coordination does not establish current responsibility. A task that merely receives a result remains status-only unless one of the explicit responsibility conditions applies; do not wake it or let it adopt coordination from the message alone.

Before a task that began with a read-only request starts repository writes, inspect whether the exact target checkout or worktree is occupied. When it is, say so and name the owning Codex task with its real title when available. Make a bounded merge-risk assessment from:

- target-checkout or worktree occupancy;
- the intended write paths;
- currently changed filenames; and
- at most the active brief's named owners or source selectors.

Separate direct observations from uncertainty and limitations. Recommend waiting when the same checkout is occupied or known overlap or unresolved uncertainty makes that safest. The human may knowingly choose an isolated worktree instead; state that isolation prevents competing writes in one checkout but does not remove later integration or merge-conflict risk.

Stop after the bounded recommendation unless further evidence is necessary for the user's decision. Do not turn this assessment into a full-diff review, history walk, test run, open-ended investigation, persistent checkout registry, or generic resource-lock subsystem without concrete evidence that the bounded path fails.

## Coordinate delegated attempts

When a coordinating chat launches a worker for an active attempt:

1. Compile one strict `pinboard-work-brief/v2` JSON candidate containing the accepted scope, checkpoint, named source selectors, criteria, and exact checks, then run `pinboard brief publish --file <candidate> --json`. The accepted `.json` artifact is the sole semantic brief. Its generated Markdown attempt view is read-only convenience output and must never be edited or parsed as authority.
2. Give every checkpoint a stable kebab-case ID, separate human title, and one typed `local` or `cross-boundary` variant. Use `cross-boundary` when the checkpoint changes one contract across multiple production owners or required consumers. Local checkpoints retain their lightweight path.
3. Give every checkpoint exactly one tagged architecture-impact value. Use `none` when ownership and dependency direction are unchanged, `read-only` when implementation must conform to a named project-relative authority, and `update-required` when the same candidate must update that authority. Do not defer an architecture update to a separate work item.
4. Before launching a cross-boundary checkpoint, read [references/brief-preservation.md](references/brief-preservation.md) completely. Plan the complete authority read with `pinboard brief-sources` before loading source bodies, then compile the reviewed-authority inventory, authoritative coverage, authorization basis for every Contract row and mandatory Verification entry, and lifecycle disposition from the exact named sources. Commission one fresh-context read-only reviewer to test every cheapest counterexample, including the declared architecture impact, the semantic role of every source-derived authorization basis, and whether each mandatory check's tool, threshold, platform, compatibility obligation, or hardening target is supported by accepted scope or selected source bytes. Publish digest-bound ready evidence. When coverage or authorization is missing or ambiguous, correct the brief before implementation; do not file a code defect for code that does not exist.
   When the accepted item or brief explicitly selects the optional engineering-health baseline, also read [references/engineering-health-baseline.md](references/engineering-health-baseline.md) and preserve its representative contract proof in the brief's authorized contracts and verification. Do not load it because work merely appears complex or cross-boundary, and do not turn it into a default requirement.
5. Select the exact `dispatch:<attempt>` action. Record only `schema`, `checkout`, `branch`, `starting_revision`, and `permissions` in a `pinboard-dispatch/v1` environment JSON file. Permission values are `repository-read`, `repository-write`, `network`, `external-write`, and `live-application`.
6. Run `pinboard dispatch` with the action tokens, stable checkpoint ID, and environment file. Launch the worker with the rendered prompt unchanged. Use `--prompt` to verify a prompt received through another transport before launch.
7. Release coordination after dispatch preparation. Retain the worker identifier and its attempt lease. Do not edit the worker's checkout or review candidate while it is still changing.
8. Wait until the worker finishes or needs attention. A wait timeout, progress update, or successful launch is not a terminal result.
9. After completion, read `result.md` or `blocker.md`, inspect the stable candidate, and follow the current-responsibility review route above.

The dispatch prompt identifies the canonical brief, checkpoint, and execution environment. It must not repeat acceptance semantics, checks, deferrals, or source-reading instructions. Change the attempt first when its contract is wrong.

Do not end the coordinating turn merely because implementation started. End with a running worker only when the user explicitly requested background execution; report that independent review remains pending and perform it before any acceptance transition.

The responsible task reviews the frozen candidate without coordination. Only after reaching a complete verdict does it use one-shot coordination for the review outcome. Use `complete:<attempt>` only when the entire work-item outcome is accepted. When an independently buildable checkpoint is accepted and the canonical brief records remaining work, use `accept-checkpoint:<attempt>` with the stable checkpoint ID, exact candidate identity, and nonempty review evidence. That transition archives the exact current `result.md` and `review.md`, pauses the same item and attempt, and fences worker authority. Publish the revised canonical JSON brief at its next artifact revision, then use the ordinary `resume:<item>` with that accepted reference before dispatch. When the protected candidate is accepted but work should continue within the same active attempt, use `accept-review-and-continue:<attempt>` with that exact candidate identity and nonempty acceptance evidence. This distinct transition returns the item and attempt to active, clears the protected candidate, records the accepted candidate and evidence without a checkpoint, sets the next action to continue, and fences the previous worker authority before a worker reacquires it. When review finds an actionable defect, use `return-for-correction:<attempt>` with one concise reason that identifies the durable review evidence. The transition preserves the same attempt and evidence, fences its previous worker authority, and leaves it ready for an explicitly selected worker to reacquire.

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

Use `pinboard parallel preview` as read-only structural evidence. It does not authorize task creation or decide product priority. Keep these outcomes distinct:

- listing or previewing creates no tasks;
- an exact selected subset or “all safe work” is launch authority for that batch only;
- each external creation receives a fresh structural check and an explainable visible-task or subagent recommendation.

Never describe a partial external launch as complete. Report one result per requested item and retain the current repository work as the main topic.

## Apply a transition

1. Identify one action as `<kind>:<subject>`. Read its returned `use_case`, `effect`, `permitted_roles`, `subject_kind`, `lifecycle_precondition`, and `practical_result` instead of inferring from the verb. Treat the exact action record's `authorization` as the runtime authority to forward; the kind-level permitted roles do not replace it. When semantic input is not already settled, run `pinboard input-contract <kind> --json` and prepare the payload before borrowing authority. `report-blocker:<attempt>` is a worker advisory with no mutation payload; `block:<attempt>` is the coordinator transition for an active attempt; `block-item:<item>` is the coordinator transition for an unstarted intake item.
2. For one graph-wide change, prefer `pinboard coordination apply`. Its revision-stamped result records the exact post-commit SQLite revision; coordination is then released before the receipt returns. Do not add an action-list preflight or a confirming overview.
3. For an attempt-local change or an exceptional manual coordination sequence, query only the exact action with `pinboard actions --role <role> --lease-id <lease> --generation <generation> --action-id <kind:subject> --json`. Treat the returned action record as one opaque capability receipt. Forward its `action_id`, `expected_revision`, `coordinator_generation`, `authorization`, and every non-empty `subject_revision` and `lease_id` value verbatim. Do not infer authorization from the current role, reuse fields from another action, or reconstruct a partial token from prose.
4. Run `pinboard transition` with those exact token fields and the prepared payload file.
5. If either path returns `ACTION_NOT_AVAILABLE`, do not retry the same command, switch roles, or skip to another lifecycle state. Validate once and refresh only the same exact action under fresh authority. If it disappeared, explain the intervening state change. If it remains available with fresh tokens, report an executable contradiction, preserve current attempt evidence, and stop transition work until the command is corrected.
6. If a task is interrupted before a receipt is visible, never replay its retained action. Follow the interrupted-transition recovery in `references/state-and-recovery.md`; authoritative state distinguishes no change from one complete committed transition, and a fresh lease generation fences the abandoned action.
7. Report the practical effect of the returned transition. Keep its internal revision and command receipt private unless they materially affect confidence or the user's next action. Run `pinboard validate` immediately after any non-tool edit to supporting attempt artifacts.

The executable rejects stale subject scopes, expired or replaced lease holders, illegal states, invalid dependencies, and inconsistent references. It does not decide whether evidence is true, whether work is valuable, or what product behavior should mean.

For an explicit terminal human decision about non-active live work, use one `pinboard close <item> --outcome <done|dropped> --reason <text> --task-id <current-task> --host-id <current-host>` invocation instead of manufacturing intake, ready, active, or attempt states. The command borrows and releases SQLite coordination internally. Do not precede this exact command with overview or action-list reads. Use `done` when the recorded decision or outcome is complete and may satisfy dependents. Use `dropped` only when the work is intentionally abandoned and has no live dependents. Never use `close` to bypass review for active or review work; keep the normal evidence-backed completion transition there. The returned revision is the durable receipt, so do not create a temporary payload file or re-read status after success.

## Safe boundaries

Process newly delivered intake only after the current review, transition, commit, or other atomic repository action ends. Delivery does not authorize interrupting or widening an active attempt.

Intake persists immutable proposal facts and a same-identity visible work item. It appends at the back by default or uses an explicitly requested one-based position, and relation semantics may add a dependency or review flag. It does not mark the candidate ready, create or activate an attempt, pause current work, change focus, or complete anything. When intake is embedded in ongoing coordination, keep a compact continuation anchor in the current task context before invoking it:

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

- For `already recorded`, say `Saved for later — <finding> is already in <exact owner and state>; current work <continues | is blocked by it>.` For `recorded now`, say `Saved for later — <finding> is now in <exact owner and state>; current work <continues | is blocked by it>.`
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
3. report the advisory `report-blocker:<attempt>` from the worker context, then have coordination select the exact active-attempt transition: use `block:<attempt>` when recording named dependencies or `pause:<attempt>` when preserving work without a dependency condition; never use the intake-only `block-item:<item>` action for active work;
4. implement the accepted prerequisite independently from the current integration base;
5. resume the preserved attempt only after its freshness check passes.

Never absorb the prerequisite silently into the current attempt.

## Review and completion

Treat worker completion as a review request, not acceptance. Compare the attempt brief, diff or commit, result receipt, and fresh proportionate verification. Check the architecture declaration against the final owner and dependency-direction diff: `none` must be truthful, `read-only` must conform to its named authority, and `update-required` must include a coherent same-candidate authority change. For a reviewed cross-boundary brief, reuse its compiled authority map and account for every acceptance criterion, Contract row and authorization basis, coverage row, and lifecycle sibling row. Every blocking finding must cite an accepted Contract row, criterion, reviewed authority, or applicable repository rule. Classify it as an implementation defect, brief omission, authority contradiction, product decision, or new capability. A correction review may reuse exact unchanged selector digests and owners, but it must re-read changed owners and sweep every changed or neighboring row before returning one complete correction package. Complete the item only when every acceptance criterion is satisfied and current knowledge owners are reconciled.

For an accepted intermediate checkpoint, use the checkpoint acceptance path above. Its paused item and attempt remain nonterminal, checkpoint evidence remains immutable under the same attempt, and terminal item history remains absent until a later full-outcome completion. For an accepted intermediate review inside an unfinished checkpoint, use `accept-review-and-continue:<attempt>` instead; it records the passing review without accepting the checkpoint and leaves the fenced attempt ready to continue after a worker reacquires it.

Use `return-for-correction:<attempt>` only for an implementation defect against an accepted owner. For a brief omission, authority contradiction, unresolved product decision, or new capability, preserve the review evidence and stop before any lifecycle transition until the existing upstream owner resolves it; do not turn the finding into implementation scope or invent a state, transition, or alternate acceptance path. For an implementation defect, record the exact correction reason instead of completing the item, manufacturing a new attempt, or editing lifecycle files. Report the outcome compactly: `Returned for correction — <reason>; the same attempt and review evidence are preserved and ready for a named worker to reacquire. No acceptance occurred.` If the executable does not offer that action for an implementation defect in review, preserve the review evidence and report the workflow blocker; do not bypass the missing transition.

Move terminal scheduling state out of the live queue in the same transition that preserves its history receipt. Do not manufacture follow-up work when the honest result is no follow-up.

Read `references/state-and-recovery.md` only for lease revocation, invalid SQLite state, or interrupted transitions.
