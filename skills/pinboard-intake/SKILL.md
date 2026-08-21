---
name: pinboard-intake
description: Preserve one newly discovered project finding in the pinboard's immutable inbox. Use when the user explicitly asks to add, queue, intake, preserve, or send a prerequisite, bug, cleanup, feature, or contradiction for later coordination. Do not use merely because a conversation explores an idea.
---

# Add to the pinboard

Convert one explicit finding into an immutable proposal. Do not edit the canonical queue or claim admission.

## Preconditions

1. Resolve this plugin's executable relative to this file as `../../scripts/pinboard`.
2. Run `pinboard status --json` from the repository checkout.
3. Require a valid current authority root. Intake does not require a coordination lease and must not wait for a master chat.
4. If the workflow or executable is unavailable, stop. Do not infer shared state from titles, recency, nearby tasks, branches, or old audit files.
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
2. Run `pinboard proposal --file <path>`.
3. Treat `OK PROPOSAL_CREATED` as persistence only.
4. Read `references/codex-transport.md` only when Codex task messaging is available and a useful active coordination holder exists.
5. Optionally notify that holder with the proposal ID and shared work root. Repository persistence, not messaging, is the correctness boundary.
6. Report persistence and delivery separately.

If transport is unavailable, no coordination lease exists, or delivery fails, retain the validated proposal in the inbox. Report notification as unavailable without treating that as a persistence failure. Any later chat can discover it through status, so never ask the human to relay the proposal or authorize lease revocation merely to deliver an optional notification.

## Result language

Keep the active work as the main topic and lead with the practical outcome:

- After `OK PROPOSAL_CREATED`, say `Saved for later — <finding> was recorded now as <proposal in inbox>; notification <delivered | unavailable>; current work <continues | is blocked by it>.` When notification is unavailable, add `No action needed; the inbox is authoritative.`
- For exact prior coverage, say `Saved for later — <finding> was already recorded at <selector and state>; current work <continues | is blocked by it>.`
- When the user explicitly dismisses the finding, say `Finding dismissed — <finding> was not saved at your request; no follow-up remains.`

Use `recorded now` only after `OK PROPOSAL_CREATED`; it means this turn before the update. Notification delivery never upgrades persistence into admission or priority. If persistence happened in response to the user's question, say that directly instead of implying the exact finding was present earlier.

When proposal creation fails, `not recorded` is an unresolved state, not a terminal receipt. Give one compact formal announcement containing:

- `Cause`: the exact failure classification;
- `Durable state`: not saved and no owner;
- `Current work`: blocked or continuing;
- `Next owner`: this task for a safe retry, or the human for one named decision.

Treat a stale proposal view or a coordination-holder change during optional delivery as expected concurrency. Retry once when doing so needs no new authority. If delivery remains unavailable after persistence, stop with no human action because the inbox is authoritative. If retry needs new authority, changes scope, or overrides another owner, ask exactly one concrete approval question. If persistence was never authorized, ask whether to preserve or dismiss the finding. Never tell the human to contact or notify the coordinating chat.

Then use one of these precise lifecycle outcomes:

- proposal prepared but not persisted;
- proposal persisted, delivery unavailable;
- proposal persisted and notification delivered;
- proposal later admitted as a new item;
- proposal later merged into an existing item;
- proposal returned for evidence;
- proposal rejected.

Only a chat holding the current coordination lease may report the latter four after applying the matching transition.
