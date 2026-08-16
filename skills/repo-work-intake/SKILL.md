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

## Persist, then deliver

1. Write the proposal to a temporary file outside canonical work state.
2. Run `repo-work proposal --file <path>`.
3. Treat `OK PROPOSAL_CREATED` as persistence only.
4. Read `references/codex-transport.md` when Codex task messaging is available.
5. Deliver a compact notification naming the proposal ID and shared work root to the exact registered coordinator.
6. Report persistence and delivery separately.

If transport is unavailable or fails, retain the validated proposal in the inbox and report `INTAKE_TRANSPORT_UNAVAILABLE`. Do not claim the coordinator saw it. The coordinator can discover it through status at its next continuation.

## Result language

Use one of these precise outcomes:

- proposal prepared but not persisted;
- proposal persisted, delivery unavailable;
- proposal persisted and notification delivered;
- proposal later admitted as a new item;
- proposal later merged into an existing item;
- proposal returned for evidence;
- proposal rejected.

Only the registered coordinator may report the latter four after applying the matching transition.
