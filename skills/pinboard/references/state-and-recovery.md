# State and recovery

## Contents

- [Authoritative state](#authoritative-state)
- [Leases and revocation](#leases-and-revocation)
- [Invalid state](#invalid-state)
- [Interrupted tasks](#interrupted-tasks)

## Authoritative state

The project-local work root contains one SQLite authority and generated or immutable supporting artifacts:

```text
.codex/work/
  state.sqlite3
  artifacts/
  views/
.codex/topics/
```

Resolve the project through Git's shared common directory. A linked worktree therefore uses the primary checkout's `.codex/work`, not a competing ignored root.

Require `authority: sqlite-v1` from the executable. `state.sqlite3` owns lifecycle, focus, attempts, leases, resources, proposals, history, and accepted artifact references. `views/` is replaceable output and never a fallback authority. Archived predecessor files are evidence only; do not parse, edit, or consult them to reconstruct current state.

Use these nonterminal states:

- `intake`: admitted but not yet ready for selection;
- `ready`: eligible for activation;
- `active`: one current execution attempt for that item; several disjoint items may be active concurrently;
- `paused`: preserved attempt intentionally preempted;
- `blocked`: waiting for named work, evidence, or a decision;
- `deferred`: deliberately unscheduled with a concrete reopen condition;
- `review`: a frozen attempt awaiting independent acceptance.

Terminal state remains queryable as history and does not appear as live work.

## Leases and revocation

Coordination is a short-lived exclusive SQLite lease, not a permanent task role. Acquire it only for a graph-wide atomic change, use its lease identity and generation in the action, then release it. Another chat may acquire it after release or expiry.

Attempt ownership is renewable and fenced. A worker presents its current attempt lease for item-local transitions. A resource capability binds the exact attempt-lease identity and generation, so replacing the attempt owner also fences retained mutation authority.

Use forced revocation only with explicit user authority when the recorded holder cannot release or has demonstrably abandoned the lease. Revocation increments the fencing generation. Never infer ownership from task titles, pinning, recency, or semantic similarity.

## Invalid state

When `pinboard validate --json` fails, report a compact recovery packet:

- the diagnostic codes and exact affected SQLite or artifact paths;
- whether the failure is authority, accepted-artifact, or generated-view state;
- the safest supported action that does not guess intent;
- one human question only when intent, scope, or product meaning is missing.

A `VIEW_REFRESH_REQUIRED` warning does not invalidate SQLite authority. Rebuild generated views only when the user authorizes that write or the active attempt requires it.

Do not edit SQLite directly, recreate a predecessor root, weaken validation, or use archived files as current authority. Recovery beyond initialization from absent `state.sqlite3` is not a general supported command. Preserve the observed failure and return it to the owning implementation when the executable cannot reopen the current database atomically.

## Interrupted tasks

A missing chat receipt is not evidence that a transition failed or succeeded. Never replay a retained action token.

For a task interrupted around a mutation:

1. Run `pinboard validate --json`.
2. Run one `pinboard status --json` or `pinboard overview --json` read. If the intended item still has its prior state, no transition committed. If it has the intended next state, the SQLite transition committed completely even if the task stopped before reporting it.
3. If coordination is still active for the interrupted task, wait for its recorded expiry when practical. One-shot coordinated transitions default to 60 seconds and perform best-effort release.
4. Use forced revocation only with explicit user authority when waiting is unsuitable.
5. Acquire fresh coordination only after release, expiry, or authorized revocation. Its higher generation fences actions retained by the interrupted task.
6. Resume from the authoritative item and attempt state. Never reconstruct ownership from the stopped task's prose, generated views, archived files, or temporary payloads.

The observable transaction contract remains binary: the previous valid revision or one complete new revision. Report any counterexample as a current SQLite defect rather than routing around it.
