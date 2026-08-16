# State And Recovery

## Contents

- [State layout](#state-layout)
- [Coordinator transfer](#coordinator-transfer)
- [Recovery](#recovery)
- [Legacy migration](#legacy-migration)

## State layout

Schema version 1 uses one physical nonterminal queue:

```text
.codex/work/
  current.md
  queue.md
  coordinator.json
  inbox/
  items/
  attempts/
  history/
.codex/topics/
```

Resolve the project through Git's shared common directory. A linked worktree must therefore use the primary checkout's `.codex/work`, not create a competing ignored root.

Use these nonterminal states:

- `intake`: admitted but not yet ready for selection;
- `ready`: eligible for activation;
- `active`: one current execution attempt;
- `paused`: preserved attempt intentionally preempted;
- `blocked`: waiting for named work, evidence, or a decision;
- `deferred`: deliberately unscheduled with a concrete reopen condition.

Keep `done`, `superseded`, and `dropped` in history, never in the live queue.

## Coordinator transfer

Transfer only at a safe boundary. Obtain the `transfer-coordinator:ledger` action, name the exact replacement task and host, and apply it through the executable. The generation increments once. Any action issued under the old generation becomes invalid.

Task titles, pinning, recency, and semantic similarity are human affordances, not ownership evidence.

## Recovery

When validation fails, report a compact recovery packet:

- observed facts;
- exact conflicting artifacts or diagnostics;
- the safest action that does not guess intent;
- one human question when intent, scope, or product meaning is missing.

Repair the earliest authoritative owner. Use the executable for lifecycle transitions. Use a direct file repair only when malformed state prevents the executable from operating, obtain explicit human confirmation, preserve the pre-repair bytes in ignored recovery evidence, and validate immediately afterward.

Do not search history merely to avoid asking for intent. Do not weaken validation to admit the current files.

## Legacy migration

Use a shadow root before changing authority:

1. inventory every nonterminal row from all prior queues and indexes;
2. reconcile contradictions using current hot state, accepted receipts, and repository evidence;
3. copy each live candidate exactly once into the shadow ledger;
4. preserve completed and superseded material as cold history without reviving it;
5. validate representative intake, duplicate, prerequisite, shelving, selection, and concurrency scenarios;
6. stop if the model needs a topic-owned queue, transcript reconstruction, or a competing worktree root;
7. reach a coordinator safe boundary;
8. perform one authority cutover;
9. register and verify the coordinator generation;
10. tombstone the old index and thematic queues as non-authoritative.

Never leave two roots claiming current work authority.
