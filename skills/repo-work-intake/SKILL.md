---
name: repo-work-intake
description: Propose newly discovered repository work from any project task and deliver it to the exact registered coordinator. Use when a user says to add, queue, intake, preserve, or send a finding for later; when an investigation discovers a prerequisite, bug, cleanup, feature, or contradiction; or when a side task should notify the coordinator at its next safe boundary. Do not use merely because a conversation explores an idea without requesting shared work state.
---

# Repository Work Intake

Convert one explicit finding into an immutable proposal. Do not edit the canonical queue or claim admission.

## Preconditions

1. Resolve this plugin's executable relative to this file as `../../scripts/repo-work`.
2. Run `repo-work status --json` from the repository checkout.
3. Require an existing valid `.codex/work/coordinator.json` with an exact task ID, host ID, and generation.
4. If the workflow, executable, or coordinator registration is unavailable, stop. Do not infer a coordinator from titles, recency, nearby tasks, branches, or old audit files.
5. Determine the current source task identity from trusted task context. If the environment does not expose it, ask the human for the exact task ID rather than inventing one.

## Prepare one proposal

Create a bounded JSON proposal containing:

- `schema`: `repo-work/v1`;
- unique kebab-case `proposal_id`;
- `created_at`;
- exact `source_task_id`;
- recognizable `user_label`;
- concrete `trigger`;
- bounded `evidence` selectors;
- `why_it_matters`;
- `relation.kind`: `independent`, `prerequisite`, `follow-up`, `duplicate`, or `contradiction`;
- related item identity or `null`;
- current product or repository `effect`;
- exact `unlock`;
- observed `urgency_evidence`, never an invented priority;
- freshness-sensitive assumptions in `freshness_assumptions`.

Do not create work merely because a question was asked. Require an explicit request to preserve or submit the finding.

Before creating a proposal, distinguish exact prior coverage from a merely related theme. If the exact observation and consequence already exist in a known canonical item or proposal, do not create a duplicate merely to produce a receipt. Report `already recorded` with the exact durable selector and current state. If only a broader item exists, treat the exact finding as unrecorded.

## Persist, then deliver

1. Write the proposal to a temporary file outside canonical work state.
2. Run `repo-work proposal --file <path>`.
3. Treat `OK PROPOSAL_CREATED` as persistence only.
4. Read `references/codex-transport.md` when Codex task messaging is available.
5. Deliver a compact notification naming the proposal ID and shared work root to the exact registered coordinator.
6. Report persistence and delivery separately.

If transport is unavailable or fails, retain the validated proposal in the inbox and report `INTAKE_TRANSPORT_UNAVAILABLE`. Do not claim the coordinator saw it. The coordinator can discover it through status at its next continuation.

## Result language

Keep the active work as the main topic. State the exact finding and consequence in the normal update, then give a compact **Durable finding** receipt on one line by default:

`Durable finding — <already recorded | recorded now | not recorded> in <exact owner and state>; notification <delivered | unavailable | not applicable>; current work <blocked | not blocked>.`

Use `recorded now` only after `OK PROPOSAL_CREATED`; it means this turn before the update. For `already recorded`, include the earlier durable selector or timestamp. Use `not recorded` when proposal creation fails and name `no owner`. Omit `notification not applicable` when an existing admitted item was updated directly. Expand the receipt into separate labeled fields only when persistence failed, ownership is ambiguous, the finding blocks current work, or the user asks for detail. Notification delivery never upgrades persistence into admission or priority. If persistence happened in response to the user's question, say that directly instead of implying the exact finding was present earlier.

Then use one of these precise lifecycle outcomes:

- proposal prepared but not persisted;
- proposal persisted, delivery unavailable;
- proposal persisted and notification delivered;
- proposal later admitted as a new item;
- proposal later merged into an existing item;
- proposal returned for evidence;
- proposal rejected.

Only the registered coordinator may report the latter four after applying the matching transition.
