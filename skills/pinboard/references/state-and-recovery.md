# State and recovery

## Contents

- [Authoritative state](#authoritative-state)
- [Leases and revocation](#leases-and-revocation)
- [Invalid state](#invalid-state)
- [Interrupted tasks](#interrupted-tasks)

## Authoritative state

The project-local work root contains one SQLite authority and generated or immutable supporting artifacts:

```text
.codex/pinboard/
  state.sqlite3
  artifacts/
  views/
```

Resolve the project through Git's shared common directory. A linked worktree therefore uses the primary checkout's `.codex/pinboard`, not a competing ignored root. Default initialization adds only the anchored `/.codex/pinboard/` entry to that shared repository's local Git exclude file; it does not edit committed ignore files or hide unrelated `.codex` content. An explicit `--work-root` remains at the exact selected path.

Require `authority: sqlite-v3` from the executable. `state.sqlite3` owns lifecycle, focus, dependencies, attempts, leases, proposals, history, and accepted artifact references. `views/` is replaceable output and never a fallback authority. Do not reconstruct state from other files.

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

Coordination is a short-lived exclusive SQLite lease, not a permanent task role. Prefer the one-shot command that borrows it for one exact mutation and releases it before returning. Its 60-second default is an upper recovery bound, not permission to retain authority between steps. Another task may acquire it after release or expiry.

For the exceptional manual sequence, prepare every stable value and the exact command first. After acquisition, do only the preselected current-state read that an immediate preparation-start mutation genuinely requires, apply that mutation, and release. Never use a manual coordination lease while discovering commands, inspecting schemas or raw SQLite rows, broadly troubleshooting, waiting, or asking for input. If a supported read does not expose a required value, release and preserve the missing read or one-shot mutation as a product defect.

The ledger remains transactionally safe during ordinary contention: an exact transition commits completely or the prior revision remains. Short contention is therefore silent retry control flow. A manual lease retained for minutes can still block timely preparation and make visible item state lag actual work; treat that delay as a flow defect even when the ledger remains valid.

Attempt ownership is renewable and fenced. A worker presents its current attempt lease for item-local transitions. Replacing the attempt owner fences actions retained by the previous owner.

Use forced revocation only with explicit user authority when the recorded holder cannot release or has demonstrably abandoned the lease. Revocation increments the fencing generation. Never infer ownership from task titles, pinning, recency, or semantic similarity.

## Invalid state

When `pinboard validate --json` fails, report a compact recovery packet:

- the diagnostic codes and exact affected SQLite or artifact paths;
- whether the failure is authority, accepted-artifact, or generated-view state;
- the safest supported action that does not guess intent;
- one human question only when intent, scope, or product meaning is missing.

A `VIEW_REFRESH_REQUIRED` warning does not invalidate SQLite authority. Rebuild generated views only when the user authorizes that write or the active attempt requires it.

Do not edit SQLite directly or weaken validation. Recovery beyond initialization from absent `state.sqlite3` is not a general supported command. Preserve the observed failure and return it to the owning implementation when the executable cannot reopen the current database atomically.

## Interrupted tasks

A missing chat receipt is not evidence that a transition failed or succeeded. Never replay a retained action token.

For a task interrupted around a mutation:

1. Run `pinboard validate --json`.
2. Run one `pinboard status --json` or `pinboard overview --json` read. If the intended item still has its prior state, no transition committed. If it has the intended next state, the SQLite transition committed completely even if the task stopped before reporting it.
3. If coordination is still active for the interrupted task, retry silently only within the bounded short-recovery window. One-shot coordinated transitions default to 60 seconds and perform best-effort release; do not interpret that upper bound as a normal planned wait.
4. Use forced revocation only with explicit user authority when waiting is unsuitable.
5. Acquire fresh coordination only after release, expiry, or authorized revocation. Its higher generation fences actions retained by the interrupted task.
6. Resume from the authoritative item and attempt state. Never reconstruct ownership from the stopped task's prose, generated views, archived files, or temporary payloads.

The observable transaction contract remains binary: the previous valid revision or one complete new revision. Report any counterexample as a current SQLite defect rather than routing around it.
