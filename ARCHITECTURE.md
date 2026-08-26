# Architecture

## System overview

Charlie Pinboard is a local coordination system for one repository-owned work ledger. The installed `pinboard` command discovers the project, opens its work root, reads and validates current state, computes legal actions, and applies accepted changes atomically.

SQLite is the authority for the repository work ledger.

One work root has three durable roles:

```text
.codex/work/
  state.sqlite3   # authoritative lifecycle, dependency, authority, and history state
  artifacts/      # immutable long-form bytes referenced by SQLite
  views/          # repairable Markdown projections generated from SQLite
```

The architecture map is semantic rather than an exhaustive file tree. It names the owners and dependency boundaries a contributor needs to preserve; leaf modules that do not change those relationships need not be listed individually.

## Dependency direction

Pure decisions remain at the center:

```text
interfaces ────────> application ────────> domain
     │                    ▲                  ▲
     └────────────> adapters ───────────────┘
```

`domain` depends only on the Python standard library and `msgspec` for exact canonical records. `application` normally depends on domain values and capability protocols rather than concrete storage. `adapters` implement those capabilities with SQLite and the filesystem. `interfaces` decode external values, compose the installed command, and present results.

Two application modules currently compose concrete adapters for workflows that span them: `application.validation` verifies SQLite plus referenced artifacts and derived views, while `application.transfer` creates a portable copy from the SQLite backup, artifact repository, and view builder. They are current composition seams, not permission for domain decisions or ordinary application services to import infrastructure.

## Runtime ownership

### Package root

Only distribution version lookup and command composition live at the package root.

| Location | Ownership |
| --- | --- |
| `charlie_pinboard/__init__.py` | Installed distribution version lookup |
| `charlie_pinboard/__main__.py` | `python -m charlie_pinboard` route to the CLI |

### Domain

`domain` owns immutable identifiers, ledger values, canonical history records, and pure decisions. It does not read files, issue SQL, parse command-line or JSON input, render views, or coordinate transactions.

| Owner group | Responsibility |
| --- | --- |
| `work_models.py`, `ledger.py`, `identifiers.py`, `errors.py` | Work-ledger values, read-only snapshot behavior, opaque identifiers, and expected decision failures |
| `decision_models.py`, `decisions.py` | Closed item and attempt commands plus lifecycle, focus, dependency, requirement, and review legality |
| `authority_models.py`, `authority_decisions.py` | Closed authority operations plus coordination and attempt authority lifecycle and fencing |
| `proposal_models.py`, `proposal_decisions.py` | Closed proposal intake values and immutable inbox intake decisions |
| `history.py` | Exact history scope records, canonical codecs, digests, and receipt relationships |

Expected rejection over constructed domain values is returned as a typed `DecisionFailure`. Boundary decoding and infrastructure failures remain typed exceptions until input has become a valid domain value.

### Application

`application` owns use-case sequencing, persistence contracts, read models, and cross-capability workflows. It converts complete stored state into the narrower domain snapshots used by decisions and commits only closed accepted mutations.

| Owner group | Responsibility |
| --- | --- |
| `stored_state.py`, `mutation_models.py`, `mutations.py`, `ports.py` | Complete persistence aggregate, closed mutation records, mutation projection, and transactional store capabilities |
| `decision_projection.py`, `service.py` | Projection into domain decisions and locked mutation orchestration |
| `actions.py`, `query_models.py`, `queries.py` | Legal-action discovery plus current overview and parallel-preview records and queries |
| `artifacts.py`, `dispatch_models.py`, `dispatch.py` | Immutable artifact references, dispatch contracts, accepted evidence publication, dispatch eligibility, and prompt preparation |
| `errors.py` | Exact application exception families and their code enums |
| `validation.py`, `transfer.py` | Whole-work-root validation and portable-copy workflow |

SQLite rows are not active domain objects. `StoredWorkState` contains exact typed records without SQL handles or filesystem paths, and `LedgerSnapshot` remains the storage-independent decision input.

### Adapters

Adapters own concrete persistence and filesystem mechanics without deciding product legality or presenting commands.

| Owner group | Responsibility |
| --- | --- |
| `files/root.py`, `files/file_io.py`, `files/models.py`, `files/errors.py` | Git-backed project discovery, durable-root resolution, file-operation records, exact failure families, directory creation, and atomic file publication |
| `files/artifacts.py` | Immutable artifact naming, publication, digest verification, and reference resolution |
| `files/views.py` | Revision-stamped queue, focus, item, attempt, and history projections |
| `sqlite/schema.sql`, `sqlite/database.py`, `sqlite/models.py`, `sqlite/errors.py` | Exact current schema, connection configuration, schema verification, connection records, transactions, backup, synchronization, and exact storage failures |
| `sqlite/store.py` | Complete `StoredWorkState` loading and exhaustive accepted-mutation persistence |
| `sqlite/registration.py` | Fresh initialization, safe reopen of SQLite, and initial view generation |

### Interfaces

Interfaces own user-facing boundaries. They may depend on application use cases, concrete adapters for composition, and domain identifiers needed to construct typed input. They do not own lifecycle legality or persistence policy.

| Owner group | Responsibility |
| --- | --- |
| `cli_models.py`, `cli.py` | Command and presentation records plus the current command surface, argument decoding, authority-token comparison, SQLite composition, JSON/text conversion, and generated-view refresh |
| `transition_models.py`, `transition_input.py` | Strict external transition payload records and conversion to typed command input |
| `brief_source_models.py`, `brief_sources.py` | Strict source manifests, shared project-relative file and Markdown-heading selection, deterministic digests, overlap rejection, and context-bounded batch planning |
| `dispatch_brief_models.py`, `dispatch_brief.py` | Closed brief record vocabulary plus canonical checkpoint parsing, architecture-impact validation, reviewed-authority and brief-review verification through shared source selection, and canonical launch prompt rendering |
| `proposal_models.py`, `proposals.py` | Strict proposal-file records and decoding into current SQLite intake input |
| `errors.py` | Exact interface exception families and their code enums |

## Storage boundaries

### Authoritative SQLite state

`.codex/work/state.sqlite3` owns project revision and host epoch; item, attempt, focus, dependency, requirement, and proposal state; coordination and attempt authority; accepted artifact references; and transition history. A mutation opens a write transaction, reselects current state and authority, applies one accepted closed mutation, and advances the revision with its history receipt. A rejected or failed transition leaves the previous valid ledger intact, and stale actions or fencing tokens are rejected.

### Immutable artifacts

Accepted requirements, briefs, results, reviews, and other evidence are immutable files below `.codex/work/artifacts/`. SQLite stores their kind, selector, revision, digest, size, and semantic relationships. Readers resolve artifacts through those accepted references and verify their bytes. The files do not independently own lifecycle state.

### Generated views

`.codex/work/views/` is human-readable output derived entirely from SQLite. A successful SQLite commit is authoritative before refresh begins. If view refresh fails, the command reports repair guidance without rolling back the accepted transition. `pinboard views rebuild` recreates the full projection, and validation distinguishes an authoritative defect from stale or missing generated output.

### Private topic evidence

`.codex/topics/` organizes ignored local working notes. It is not authoritative work state, is not a production dependency, and is not included in the package. Durable execution semantics enter the runtime only through accepted immutable artifact references.

## Representative flows

### Initialization and reopen

`pinboard init` resolves the durable roots and creates the SQLite schema when `state.sqlite3` is absent. When the database exists, it verifies and reopens that exact schema, ensures the artifact directories exist, and rebuilds views.

### Reads and validation

Status, overview, action discovery, and parallel preview open one `StoredWorkState` snapshot through `SQLiteWorkStore`, then build application-owned read models. They never parse generated Markdown. Validation verifies the database and every accepted artifact reference, then reports generated-view drift separately.

### Brief source planning

The installed `pinboard brief-sources` command reads a strict source manifest without opening work state. It resolves every project-relative whole-file or Markdown-heading selector before emitting content, rejects overlapping line spans, reports normalized selected-source digests, and assigns every selected UTF-8 byte to one consecutive segment and batch. Its heading selection is also used by dispatch when validating reviewed-authority digests. Planning is read-only and does not acquire authority or write project state.

### Mutations and proposal intake

The CLI decodes command input into exact typed values and reselects the advertised action. `application.service` rechecks authority and legality inside the store transaction, then commits one closed mutation. Proposal intake follows the same SQLite transaction boundary: the proposal file is decoded at the interface, the immutable inbox decision rejects duplicates, and the accepted row is stored without changing scheduling state.

### Worker dispatch and review publication

Dispatch reselects the current action, active attempt, and accepted brief reference from SQLite. `application.dispatch` passes the selected attempt's item and accepted scope revision into brief validation. The artifact adapter verifies the canonical brief bytes. `interfaces.dispatch_brief` validates the checkpoint boundary and architecture-impact declaration before any prompt is rendered. A cross-boundary checkpoint additionally verifies its seven-column contract, authorization-basis syntax and reference integrity, reviewed authority digests, coverage, lifecycle disposition, and independent READY review. Accepted-scope authorization must match the reselected attempt; source-derived authorization must name one exact reviewed authority family. The independent brief reviewer, not the parser, verifies that each source truthfully serves its claimed product-authority, repository-policy, or existing-consumer role.

Review evidence is published as an immutable artifact and accepted through the application-owned SQLite workflow. The launch prompt only points the worker to the canonical brief and names the execution environment; it does not duplicate or reinterpret the task contract.

### Checkpoint and terminal acceptance

An accepted nonterminal checkpoint preserves exact result and review evidence, pauses the same attempt, and fences its worker authority. Terminal completion records accepted evidence and removes the item from live work in the same SQLite transition. Review return keeps the same attempt and evidence while fencing the rejected worker lease.

### Portable copy

Portable copy requires a quiescent source. It backs up `state.sqlite3`, copies and verifies every referenced artifact, advances the destination revision and host epoch, neutralizes host-local leases, rebuilds views, synchronizes the staged tree, and atomically publishes the relocated work root. The source remains unchanged. Project-local source authorities named by accepted briefs are outside the portable work root and must be supplied by the relocated project when dispatch needs them.

## Stored formats

The `.codex/work` path is current. Proposal JSON uses `pinboard-proposal/v1`; accepted work briefs use `pinboard-work-brief/v1`; independent review evidence uses `pinboard-work-brief-review/v1`; dispatch environments use `pinboard-dispatch/v1`; brief-source manifests and plans use `pinboard-brief-sources/v1` and `pinboard-brief-source-plan/v1`; and read projections use `pinboard-overview/v1` and `pinboard-parallel-preview/v1`. Atomic file publication uses private `.pinboard-stage-*` names.

## Keeping this map current

This document describes implemented ownership and dependency direction. Every implementation checkpoint declares its architecture impact before dispatch. A checkpoint that changes an owner or dependency direction names this file as `update-required` and includes the coherent documentation change in the same candidate. A `read-only` checkpoint names the authority it must conform to; `none` records why no architecture change occurs. Parser validation enforces the declaration shape, while brief and implementation review verify that the declaration is true for the sources and final diff.

Future behavior belongs here only when its implementation is present. Delivery history, speculative modules, and deferred redesigns remain in private planning evidence until they change the current architecture.
