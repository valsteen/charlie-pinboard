---
name: pinboard-intake
description: Preserve one newly discovered project finding as a visible intake candidate on the pinboard. Use when the user explicitly asks to add, queue, intake, preserve, or send a prerequisite, bug, cleanup, feature, contradiction, or clarification for later coordination. Do not use merely because a conversation explores an idea.
---

# Add to the pinboard

Convert one explicit finding into immutable proposal facts and a same-identity visible intake candidate. Do not claim that intake made it ready, active, or current work.

Intake may be standalone or embedded in ongoing Pinboard coordination. Standalone intake may end after its persistence receipt. Before embedded intake, retain a compact continuation anchor containing the pre-intake objective, the next promised action, and the exact durable owner selector. Intake changes visible queue state but preserves focus and active attempts, so return control to that anchor after persistence and optional notification handling.

## Preconditions

1. Resolve this plugin's executable relative to this file as `../../scripts/pinboard`.
2. Run `pinboard status --json` from the repository checkout.
3. Require a valid current authority root. Intake does not require a coordination lease and must not wait for a master chat.
4. If the workflow or executable is unavailable, stop. Do not infer shared state from titles, recency, nearby tasks, branches, or old audit files.
5. Determine the current source task identity from trusted task context. If the environment does not expose it, ask the human for the exact task ID rather than inventing one.

## Prepare one proposal

Create a bounded JSON proposal containing:

- `schema`: `pinboard-proposal/v1`;
- unique kebab-case `proposal_id`;
- `created_at`;
- exact `source_task_id`;
- recognizable `user_label`;
- concrete `trigger`;
- bounded `evidence` selectors;
- `why_it_matters`;
- `relation.kind`: `independent`, `prerequisite`, `follow-up`, `duplicate`, `contradiction`, or `clarification`;
- related item identity or `null`;
- current product or repository `effect`;
- exact `unlock`;
- observed `urgency_evidence`, never an invented priority;
- freshness-sensitive assumptions in `freshness_assumptions`;
- optional one-based `position`; omit it to place the candidate at the back of live work.

Use `follow-up` when the new candidate depends on the related item. Use `prerequisite` when the live related item depends on the new candidate; persistence advances that target item's immutable definition history as well as its relational dependency projection. Use `duplicate`, `contradiction`, or `clarification` to expose a review flag rather than inventing a dependency. `independent` and `clarification` omit the related item; the other relations require it. Every new proposal also creates definition revision 1 from its immutable facts, so do not add parallel semantic prose after intake.

Do not create work merely because a question was asked. Require an explicit request to preserve or submit the finding.

Before creating a proposal, distinguish exact prior coverage from a merely related theme. If the exact observation and consequence already exist in a known canonical item or proposal, do not create a duplicate merely to produce a receipt. Report `already recorded` with the exact durable selector and current state. If only a broader item exists, treat the exact finding as unrecorded.

## Persist, then deliver

1. Write the proposal to a temporary file outside canonical work state.
2. Run `pinboard proposal --file <path>`.
3. Treat `OK PROPOSAL_CREATED <proposal-id> position=<n> state=intake` as proof that both the proposal facts and visible intake candidate persisted.
4. Read `references/codex-transport.md` only when Codex task messaging is available and a useful active coordination holder exists.
5. Optionally notify that holder with the proposal ID and shared work root. Repository persistence, not messaging, is the correctness boundary.
6. Report delivery only when the user requested it or when its outcome changes confidence, current work, or the next action.

For embedded intake, resume the invoking coordinator before the surrounding turn ends. If context compaction obscured the conversation, re-read the anchor's active or paused item, attempt, proposal, or exact selector rather than inventing continuation state. Complete the promised action when it remains in scope; otherwise surface its exact blocker or durably defer it at an exact owner.

If transport is unavailable, no coordination lease exists, or delivery fails, retain the visible intake candidate. Keep optional delivery state silent by default. Any later chat can discover it through overview or status, so never ask the human to relay it or authorize lease revocation merely to reduce notification latency.

## Result language

Keep the active work as the main topic and lead with the practical outcome:

- After `OK PROPOSAL_CREATED`, say `Saved for later — <finding> is now visible as <proposal-id> at intake position <n>; current work <continues | is blocked by it>.`
- For exact prior coverage, say `Saved for later — <finding> was already recorded at <selector and state>; current work <continues | is blocked by it>.`
- When the user explicitly dismisses the finding, say `Finding dismissed — <finding> was not saved at your request; no follow-up remains.`

Use `now` only after `OK PROPOSAL_CREATED`; it means this turn before the update. Notification delivery never upgrades persistence into admission or priority. If persistence happened in response to the user's question, say that directly instead of implying the exact finding was present earlier. When delivery is user-requested or materially affects the result, report it after the durable outcome without implying that optional transport changes persistence.

When proposal creation fails, `not recorded` is an unresolved state, not a terminal receipt. Give one compact formal announcement containing:

- `Cause`: the exact failure classification;
- `Durable state`: not saved and no owner;
- `Current work`: blocked or continuing;
- `Next owner`: this task for a safe retry, or the human for one named decision.

Treat a stale proposal view or a coordination-holder change during optional delivery as expected concurrency. Retry once when doing so needs no new authority. If delivery remains unavailable after persistence, stop notification work with no human action because the ledger is authoritative; this does not end an embedded caller's surrounding turn. If retry needs new authority, changes scope, or overrides another owner, ask exactly one concrete approval question. If persistence was never authorized, ask whether to preserve or dismiss the finding. Never tell the human to contact or notify the coordinating chat.

When transport detail is material, distinguish these precise lifecycle outcomes:

- proposal prepared but not persisted;
- proposal persisted, delivery unavailable;
- proposal persisted and notification delivered;
- visible proposal later accepted in place as ready, blocked, deferred, or retained intake;
- proposal later merged into an existing item;
- proposal returned for evidence;
- proposal rejected.

Only a chat holding the current coordination lease may report the latter four after applying the matching transition.
