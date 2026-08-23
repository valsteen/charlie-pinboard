# Architecture

## System overview

Charlie Pinboard is a local coordination system for one repository-owned work ledger. The installed `pinboard` command finds the repository, reads and validates its work state, computes legal lifecycle and resource decisions, and applies serialized changes atomically. The current production runtime still uses the Markdown ledger and filesystem transaction engine that preceded the planned SQLite authority.

The Python package is `charlie_pinboard`. Its physical layout makes the current dependency strata visible: pure rules live in `domain`, use-case contracts live in `application`, concrete environment integration lives in `adapters`, user-facing decoding and presentation live in `interfaces`, and the temporary Markdown runtime lives in `legacy`.

## Dependency direction

The stable direction is inward:

```text
interfaces ────────> application ────────> domain
     │                    ▲                  ▲
     ├────────────> adapters ────────────────┘
     │
     └────────────> legacy ─────────────────┘
```

`domain` imports the Python standard library plus `msgspec` for exact declared canonical records. `application` depends on domain values and decisions, not on concrete storage. Adapters may satisfy application needs and translate environment facts, while interfaces compose those pieces and turn command-line or JSON values into typed values.

The downward call from `interfaces` to `legacy` is the one deliberate transitional path. The CLI still invokes the Markdown authority, filesystem locks, and custom file transaction machinery. External transition input is decoded by an interface-owned decoder injected into that legacy transaction, so the legacy package does not import the interface package and domain signatures never receive JSON representations.

## Current package map

Only identity and composition entry files sit at the package root.

| Location | Current ownership | Deliberate exclusions |
| --- | --- | --- |
| `charlie_pinboard/__init__.py` | Installed distribution version lookup | Lifecycle rules, storage, and command behavior |
| `charlie_pinboard/identity.py` | Distribution and primary program names | Serialized protocol compatibility names |
| `charlie_pinboard/__main__.py` | `python -m charlie_pinboard` composition route to the CLI | Argument parsing and use-case behavior |

### Domain

`domain` contains immutable values and pure decisions. It has no dependency on application, adapters, interfaces, filesystems, SQL, Markdown, CLI parsing, or legacy owners.

Expected lifecycle, planning, resource, and history rejection over constructed domain models is part of each operation's return type. Operations return their accepted value or `DecisionFailure`; validators without an accepted value return `DecisionFailure | None`. `LedgerSnapshot` owns storage-independent read-only lookup and reports absence as `None`, leaving each decision operation to produce the failure that is meaningful for that call. Serialized history payloads decode directly into exact frozen `msgspec` records; field constraints, unknown-member rejection, and record post-init invariants run during that instantiation, while canonical sorted-byte comparison remains a codec check. Decode failures raise `HistoryOutcomeError`; only operations over constructed domain models use returned failures. Interfaces and transitional legacy owners may translate returned failures or boundary exceptions into their existing presentation or transaction error contract.

| Module | Current ownership |
| --- | --- |
| `identifiers.py` | Distinct opaque identifier types |
| `errors.py` | Typed decision failures and their closed error codes |
| `model.py` | Immutable ledger, lifecycle, resource, decoded transition-payload, and snapshot values |
| `decisions.py` | Lifecycle and action-legality decisions |
| `planning_decisions.py` | Pure planning-impact checks for item and attempt changes |
| `authority_decisions.py` | Pure coordination, attempt, and task-use authority lifecycle and fencing decisions |
| `proposal_decisions.py`, `scope_decisions.py`, `resource_definition_decisions.py` | Pure proposal intake, item-scope replacement, and portable resource-definition decisions |
| `resource_decisions.py` | Pure resource reservation, claim, use-lease, and mutation-intent decisions |
| `history.py` | Declared canonical scope and history records, direct typed codecs, digests, and receipt relationships |

### Application

The application layer is the home for top-level use-case sequencing below user-facing entry points. It coordinates domain decisions through abstract capabilities and transactions. It does not decide lifecycle legality, decode JSON, render Markdown, know filesystem paths, issue SQL, or construct concrete adapters.

| Module | Current ownership |
| --- | --- |
| `stored_state.py` | Complete immutable persistence aggregate, organized into lifecycle, proposal, planning, artifact, authority, resource, history, and focus records |
| `mutations.py` | Closed typed persistence contract over lifecycle decisions, pure planning and resource decisions, and exact before/after carriers accepted by existing legality owners |
| `decision_projection.py` | Pure projection from complete stored state into the narrower `LedgerSnapshot` consumed by domain decisions |
| `ports.py` | `WorkStore` and `WorkTransaction` protocols over complete `StoredWorkState` reads and one closed accepted-mutation commit boundary |
| `service.py` | Locked application orchestration that reselects typed authority, invokes the focused lifecycle, planning, or resource-intent decision owner, and commits one accepted mutation through `WorkStore` |

A port is an application-owned description of a capability the use case needs; a concrete file or SQLite store implements that capability from outside the application layer. `StoredWorkState` contains no SQL rows, filesystem paths, adapter exceptions, or active-record behavior. `LedgerSnapshot` remains domain-owned and storage-independent rather than becoming a lossy persistence contract. The current production CLI does not yet run through these ports because the Markdown implementation remains inside the temporary legacy path.

### Adapters

Adapters own concrete filesystem and database mechanics without deciding lifecycle legality or presenting commands.

| Module | Current ownership |
| --- | --- |
| `files/root.py` | Git-backed project-root discovery and the conventional `.codex/work` location |
| `files/file_io.py` | Verified durable-root creation, immutable single-file publication, and same-directory atomic replacement |
| `sqlite/schema.sql` | The exact current `charlie-pinboard` / `sqlite-v1` relational schema |
| `sqlite/database.py` | Raw SQLite connection configuration, exact current-schema verification, transactions, backup, synchronization, and stable storage errors |
| `sqlite/store.py` | Relational `WorkStore` persistence for complete `StoredWorkState` reads and exhaustive accepted-mutation commits with one revision and history receipt |

These SQLite owners are independently buildable primitives. The current production CLI does not compose them yet and continues to use the Markdown authority through `legacy`.

Pure lifecycle, planning, and resource modules continue to decide legality. The application mutation contract derives the exact relational delta for their accepted outputs, while proposal creation, dependency and requirement edits, authority changes, and reservation or task-use changes use typed before/after values bounded to their named record families. Every stored-state mutation carries the complete accepted history-receipt identity. Carrier-only variants add no policy; application orchestration constructs them only after current action and operation legality accepts the exact records. SQLite applies the closed union without importing raw input, Markdown, paths, or application orchestration.

### Interfaces

| Module | Current ownership |
| --- | --- |
| `cli.py` | Command definitions, argument decoding, diagnostics and JSON presentation, and composition of current operations |
| `transition_input.py` | Strict external transition-payload schemas and conversion into typed payload values |
| `transitions.py` | The external mutation boundary that binds an advertised action and decoded payload into one closed command variant before entering the current legacy transaction |

Interfaces may call application use cases, adapters, and the temporary legacy runtime. They do not add domain legality or expose raw external representations in domain signatures.

### Legacy

`legacy` is the explicit predecessor boundary for the current Markdown authority. Its modules are grouped here because they still read serialized Markdown, use filesystem paths and locks, or participate in the custom file transaction. A use-case-like name inside this package describes current behavior, not its desired future owner.

| Modules | Current ownership |
| --- | --- |
| `authority.py`, `migration.py`, `registration.py` | v1/v2 authority selection, the existing v1-to-v2 cutover, and filesystem ledger initialization |
| `markdown.py`, `coordinator.py`, `diagnostics.py`, `validate.py` | Markdown parsing and rendering, legacy coordinator records, validation diagnostics, and whole-ledger validation |
| `atomic.py`, `storage_layout.py`, `transaction_store.py` | Locking, confined legacy paths, change sets, commit journals, atomic writes, and recovery |
| `leases.py`, `resources.py` | Attempt, coordination, and resource leases persisted in the current filesystem authority, plus temporary names for their independently callable store-backed application operations |
| `actions.py`, `revisions.py` | Available-action projection and stale-subject or ledger revision calculation over current files |
| `transition.py`, `transition_plan.py` | Filesystem-coupled mutation sequencing and translation of pure decisions into legacy file changes; `transition.py` also exposes the temporary store-backed transition operation without changing CLI composition |
| `proposals.py`, `overview.py`, `parallel.py`, `dispatch.py` | Filesystem-backed proposal intake, read models, concurrency previews, and worker-launch preparation, including reviewed-authority and immutable brief-review validation for cross-boundary checkpoints; `proposals.py` also exposes the temporary store-backed intake operation |

The four mutation-facing legacy modules expose temporary, domain-typed names for the corresponding application-service operations so callers can adopt `WorkStore` without duplicating policy. Those names are direct aliases: the legacy modules add no adapter logic, generation arithmetic, mutation construction, or error remapping. The installed CLI continues to call the existing Markdown functions. The legacy package does not define the future SQLite model and is not a source for new steady-state SQLite behavior.

## Representative flows

### Mutation

The installed `pinboard transition` command enters `interfaces.cli`, which resolves the project through `adapters.files.root` and loads an advertised action from the legacy view. `interfaces.transitions` decodes the strict JSON payload and binds it to that action as one closed dataclass command variant. While holding the current authority transaction, the legacy mutation verifies lease, resource, and stale-revision tokens before accepting the bound command. Pure lifecycle decisions and `legacy.transition_plan` exhaustively match that command union, so an action discriminator and an incompatible payload cannot circulate as separate internal values. `legacy.transaction_store` validates and commits the resulting file change set atomically, then the interface renders the new revision or a typed failure.

This route proves behavior through the production command while keeping weak JSON values out of the domain. It remains transitional because the transaction and use-case sequencing still belong to the legacy filesystem implementation rather than an application service over a store port.

### Read or query

An installed status, overview, actions, or validation command enters `interfaces.cli` and resolves the project root through the file adapter. The relevant legacy reader resolves the active authority, parses the Markdown files, validates their agreement, and constructs typed domain records or a read model. The CLI converts that result into human-readable text or stable JSON. Reads do not acquire permanent coordination ownership and do not make a derived view authoritative.

### Worker dispatch

The installed `pinboard dispatch` command enters `interfaces.cli` with one current dispatch action, an exact checkpoint heading, and a typed execution environment. `legacy.dispatch` revalidates that action and the active attempt under the existing authority transaction. Local checkpoints continue directly to canonical prompt rendering.

For a cross-boundary checkpoint, `legacy.dispatch` parses the Contract, reviewed-authority, authoritative-coverage, and lifecycle records into concrete values. It resolves each selected authority against the canonical project, verifies its selected-byte digest, and binds the exact checkpoint and reviewed-authority-table bytes to immutable ready evidence under `attempts/<attempt>/brief-reviews/`. Optional `--brief-review` and `--review-id` inputs let the same command validate candidate evidence before creating that digest-named path under the existing dispatch lock. Exact bytes reuse the existing file; a differing collision preserves the later evidence under `brief-reviews/rejected/` without overwriting either artifact, then rejects. That evidence is an independent planning-review dependency, not a new lifecycle state. Missing, stale, incomplete, non-ready, noncanonical-task, or same-owner evidence rejects before prompt rendering. Local dispatch does not accept publication inputs. The resulting launch prompt remains only a pointer to the canonical brief and execution environment; it does not duplicate those semantics.

### Nonterminal checkpoint acceptance

`complete` remains the terminal transition for an accepted whole-item outcome. A coordinator may instead apply `accept-checkpoint` to an attempt in review. The domain decision pauses the same item and attempt, fences the attempt lease, revokes task-use authority, retains host-local reservations, and records the typed checkpoint identity, candidate, evidence, and acceptance time without creating terminal item history.

The legacy transition adapter archives the exact top-level `result.md` and `review.md` bytes beneath `attempts/<attempt>/checkpoints/<checkpoint>/`, writes a schema-v2 receipt with their digests, removes the top-level evidence names, clears focus, and marks each retained resource claim `reserved` in the same filesystem transaction. Reserved claims reject competing attempts and parallel launch previews until the original attempt resumes and reacquires task-use authority; a legal terminal close or completion instead releases them atomically so another live attempt can claim them. Duplicate checkpoint identities and missing evidence reject before mutation. The coordinator then replaces and validates the canonical checkpoint section in the same `attempt.md` before using the existing resume and dispatch flow.

Proposal intake remains a scheduling-neutral write to the immutable inbox. Conversation-level continuation after embedded intake is owned by the Pinboard skills through a compact anchor to existing durable state; it adds no scheduler, coordinator-ownership, or recovery record.

## Temporary compatibility and deletion condition

`pinboard` is the primary command. The installed `repo-work` command is a migration-scoped alias to the same `charlie_pinboard.interfaces.cli` entry point because known local tasks may still invoke it before ledger migration. Existing `.codex/work` locations and `repo-work/*` schemas, journals, and protocol identifiers remain the serialized vocabulary of the known Markdown ledgers; they are not a second Python package identity.

The separately tracked ledger-migration follow-up owns migrating both known ledgers and then deleting the command alias, old serialized readers and fixtures, temporary migration tools, and this legacy package boundary. Immutable history and attempt receipts remain historical evidence rather than being rewritten for naming consistency.

The remaining legacy-name inventory has these dispositions:

| Retained form | Where it remains | Disposition |
| --- | --- | --- |
| `repo-work` command | Packaging metadata, `scripts/repo-work`, metadata validation, tests, and compatibility prose | Migration-scoped alias to the same `charlie_pinboard` CLI; deleted by the known-ledger migration follow-up |
| `repo-work/*` and related `repo-work-…/v1` strings | Legacy readers and renderers, interface output contracts, skills that describe the current ledger, and compatibility fixtures | Serialized protocol needed to validate and migrate the current Markdown ledgers; renamed only by an explicit ledger migration |
| `.repo-work-…` staging, lock, journal, and prospective names | Legacy filesystem transaction code and its recovery tests | Private filesystem protocol of the current Markdown transaction engine; deleted with that engine |
| `.codex/work` | Root discovery, CLI help, documentation, tests, and both known repositories | Stable current ledger location; this checkpoint does not migrate or rename it |
| Old names inside `.codex/work` receipts or preserved topic evidence | Immutable local history and execution evidence | Historical evidence; never rewritten merely for naming consistency |

There is no `repo_work` source directory, import alias, package metadata target, or compatibility module. No current tracked source or descriptive document uses “Charlie Board” as a product name.

## Current and future structure

This document maps implemented owners. Today the package includes the current SQLite schema, raw database boundary, relational store, durable single-file primitives, and the SQLite-backed application mutation service for lifecycle, proposal, scope, authority, planning, reservation, resource-definition, and mutation-intent operations. It does not yet include the artifact service, query service, or interface composition for those owners. The Markdown-backed CLI remains authoritative. Later checkpoints add queries, artifact and view adapters, and interface composition under the same dependency direction. Future modules become part of this map only when implementation exists; empty directories and diagram-only packages are intentionally absent.
