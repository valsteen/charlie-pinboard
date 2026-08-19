# State And Recovery

## Contents

- [State layout](#state-layout)
- [Leases and revocation](#leases-and-revocation)
- [Recovery](#recovery)
- [Legacy migration](#legacy-migration)

## State layout

Schema version 2 selects one current authority root:

```text
.codex/work/
  authority.json
  v2/
    current.md
    queue.md
    inbox/
    items/
    attempts/
    resources/
    leases/
    history/
.codex/topics/
```

Resolve the project through Git's shared common directory. A linked worktree must therefore use the primary checkout's `.codex/work`, not create a competing ignored root.

Each live item Markdown file owns its lifecycle fields. `queue.md` is a generated overview, not a second writer. Use these nonterminal states:

- `intake`: admitted but not yet ready for selection;
- `ready`: eligible for activation;
- `active`: one current execution attempt for that item; several disjoint items may be active concurrently;
- `paused`: preserved attempt intentionally preempted;
- `blocked`: waiting for named work, evidence, or a decision;
- `deferred`: deliberately unscheduled with a concrete reopen condition.
- `review`: a frozen attempt awaiting independent acceptance.

Keep `done`, `superseded`, and `dropped` in history, never in the live queue.

## Leases and revocation

Coordination is a short-lived exclusive lease, not a permanent task role. Acquire it only for a graph-wide atomic change, use its lease identity and generation in the action, then release it. Another chat may acquire it after release or expiry.

Attempt ownership is also renewable and fenced. A worker must present its current attempt lease for item-local transitions. A resource claim records the exact attempt-lease identity and generation, so replacing the attempt owner also fences retained live-resource claims.

Use forced revocation only with explicit user authority when the recorded holder cannot release or has demonstrably abandoned the lease. Revocation increments the fencing generation. Never infer ownership from task titles, pinning, recency, or semantic similarity.

## Recovery

When validation fails, report a compact recovery packet:

- observed facts;
- exact conflicting artifacts or diagnostics;
- the safest action that does not guess intent;
- one human question when intent, scope, or product meaning is missing.

Repair the earliest authoritative owner. Use the executable for lifecycle transitions. Use a direct file repair only when malformed state prevents the executable from operating, obtain explicit human confirmation, preserve the pre-repair bytes in ignored recovery evidence, and validate immediately afterward.

Do not search history merely to avoid asking for intent. Do not weaken validation to admit the current files.

## Legacy migration

Lease and resource commands exist only in schema v2. If status reports v1, run `repo-work migrate --to v2` before giving lease instructions. When the current request does not authorize migration, return `MIGRATION_REQUIRED` and that exact command instead of treating permanent v1 ownership as a lease.

Use a shadow root before changing authority:

1. inventory every nonterminal row from all prior queues and indexes;
2. reconcile contradictions using current hot state, accepted receipts, and repository evidence;
3. copy each live candidate exactly once into the shadow ledger;
4. preserve completed and superseded material as cold history without reviving it;
5. validate representative intake, duplicate, prerequisite, shelving, selection, and concurrency scenarios;
6. stop if the model needs a topic-owned queue, transcript reconstruction, or a competing worktree root;
7. reach a safe atomic boundary;
8. perform one authority cutover;
9. verify the selected v2 root without consulting v1 as current authority;
10. preserve v1 files as cold readable history.

Never leave two roots claiming current work authority.
