# Design principles

This document describes how Pinboard keeps decisions visible while moving repetitive boundary work out of their way. `ARCHITECTURE.md` owns the concrete package map and runtime responsibilities. This document owns the method used to shape and evolve that map.

## Optimize for visible decisions

The primary reader is a person trying to answer: what can happen here, under which condition, and with which effect? Put those branches together in one explicit owner. Move representation conversion and mechanical persistence details aside only when their contract remains obvious from the call site.

File size is a signal, not the objective. A smaller orchestration file is useful when it concentrates the real alternatives. A lower total line count is useful when it removes repetition without hiding control flow. Measure those outcomes separately.

## Separate decisions, conversions, and effects

A decision determines whether an operation is legal and what accepted change it describes. It should not read files, issue SQL, obtain time, or depend on a concrete adapter.

A conversion changes representation without changing meaning. Prefer a plain typed function beside the boundary model or in the outer module that knows both representations. It must not acquire resources or perform unrelated work.

An effect reads or changes an explicitly supplied resource. Its signature names that resource and every value that influences the effect. A thematic SQLite function, for example, receives an existing connection and supplied time; it does not secretly open a database or read the clock.

Do not combine these roles merely to save a call. Do not separate them when the new boundary would add more translation machinery than the distinction removes.

## Make effect contracts locally complete

The caller should be able to determine whether a function can mutate state, perform external I/O, end a transaction, obtain ambient values, invoke caller-supplied behavior, or exit normally with an expected rejection.

Prefer these properties:

- resources and time are explicit parameters;
- transaction opening, commit, rollback, and close remain with one visible owner;
- cross-boundary functions return data instead of invoking callbacks;
- pure mapping functions depend only on their arguments;
- module-level contract text states the allowed effects and the effects it deliberately does not own;
- infrastructure and invariant failures are not silently normalized into ordinary outcomes.

A helper that takes bread and cheese may return a sandwich. It must not also collect the mail, call another service, or decide whether the meal was authorized.

## Put glue at the outer owner

Dependencies point toward policy. Domain code owns product vocabulary and pure legality. Application code sequences use cases through storage-independent capabilities. Adapters implement those capabilities. Interfaces decode external input, compose concrete implementations, and present results.

When an operation genuinely needs two layers, the outer layer that already knows both owns the conversion or composition. Do not make both inner layers import each other, and do not create a shared module that makes every participant own the cross-dependency.

## Constrain composition fan-out

Being outermost permits a dependency direction; it does not justify collecting unrelated work. Keep the process entry point as a small composition root that owns only complete input decoding, one exhaustive route, and final result presentation. Put each cross-layer workflow in a thematic outer module named for the use case it composes.

Judge fan-out per module as well as per layer. A thematic composition module may know several concrete collaborators when all of them serve one visible operation. It becomes an architectural octopus when it owns unrelated command families, conversions, resource lifetimes, or presentation rules merely because it is allowed to import them.

Keep composition modules acyclic. Prefer direct module-qualified calls so navigation reveals the exact owner. Mechanically constrain the entry point's outward dependencies and the interface package's cycles; do not rely on directory placement as proof that the graph is clear.

Keep the production dependency graph mechanically checkable. Move uninstalled experiments to test-only prototypes rather than granting production code a reverse dependency for a hypothetical consumer.

## Make expected exits explicit

Use a typed result when a caller can reasonably act on an expected outcome. Examples include rejected command, proposal, and dispatch requests; unavailable actions; missing selected work; stale compare-and-set writes; and rejected lifecycle transitions.

A low-level decoder may raise while data is still an untyped external representation. When invalid input is an advertised outcome of an installed use case, its boundary owner catches that exact parser failure and returns the use case's typed failure. Do not give a nominal command, proposal, dispatch, or domain rejection exception status merely because a parser first observed it.

Let failures remain exceptions when execution cannot proceed as designed. Examples include unreadable accepted internal files outside an advertised rejection contract, SQLite I/O and locking failures, corrupt persisted relationships, and violated programming contracts.

Do not catch every exception and convert it to a result. Catch only the exact boundary exceptions owned by an advertised failure contract. Do not use a decorator or framework to hide that conversion. Return and propagate expected failures in ordinary typed control flow; let genuine infrastructure and programming failures remain visibly exceptional. The transaction owner rolls back both categories.

## Make impossible states unrepresentable

Supported typed code must not represent impossible states through exceptions, result variants, sentinels, fallback branches, placeholder initializers, optional-parameter coupling, or tests that fabricate malformed typed values. Encode the valid combinations in concrete records, nominal identifiers, closed unions, and exhaustive matching so an invalid construction or call is rejected statically or has no callable surface.

A clean strict type check is evidence for invariants expressed by those types. Do not add runtime guards or malformed-object tests solely to execute a state that supported typed code cannot construct. Runtime validation and failure surfaces remain necessary where the fact is genuinely runtime-owned: deserialization and other external input, `Any` or cast boundaries, persisted relational state, filesystem and database effects, concurrency and staleness, and infrastructure failure.

Apply this rule to signatures as well as implementations. Removing a defensive branch is incomplete while an optional parameter, broad input union, general authorization value, or fabricated test helper still admits the invalid combination. Preserve product distinctions with separate closed variants when their required data differs; do not recover the same distinction later with nullable fields or repeated validation.

## Prefer closed, direct Python

For a closed family, use concrete records or flat unions, exhaustive `match` statements, and direct function calls. The source should reveal which variant calls which effect.

When a module owns a coherent vocabulary, import the module and qualify its members at use sites. This keeps the owner visible and prevents long member-import lists from disguising cross-module coupling.

Do not replace a closed branch with a handler registry, reflective attribute access, inheritance-based dispatch, or a callback pipeline. Those tools are appropriate only when supported behavior is genuinely open at runtime. Generics and protocols are useful when they preserve exact types across a real reusable boundary; they are not reasons to create one.

## Split by theme and preserve useful symmetry

Group models, conversions, reads, and effects by the product concept they serve. Corresponding layers should use corresponding themes when the responsibilities genuinely match, so a reader can predict where lifecycle, proposal, artifact, or authority behavior lives.

Symmetry is a navigation aid, not a quota. A layer may keep a specialized module when only that layer has the responsibility. Do not create empty or ceremonial counterparts merely to make directory listings align.

Avoid generic `utils`, `writer`, `manager`, and `handlers` modules. A module earns its name from the concept and contract it owns.

## Collapse to a fixed point

Use a reversible pilot before applying a new decomposition broadly:

1. Identify the decision surface and one repetitive family around it.
2. State the proposed function and module contract, including hidden effects it forbids.
3. Move the smallest representative family and preserve observable behavior.
4. Measure decision visibility, result paths, dependency edges, conversions, and total source separately.
5. Stop if the extraction duplicates legality, adds dynamic indirection, or makes the effect contract harder to see.
6. If it succeeds, apply the same ownership rule to every matching family.
7. Recompute imports, callers, dead variants, tests, and documentation after each collapse.
8. Repeat until a fresh pass finds no matching residue.

Stop when another fold would erase a product distinction, scatter one exhaustive decision, create a generic dumping ground, or add more conversion machinery than repeated ownership it removes.

## Evaluate the result on independent axes

Do not use one metric as a proxy for architecture quality. Report at least:

- where the core decisions are and how many places own them;
- which expected outcomes are explicit result paths;
- which boundary conversions remain and where they live;
- production dependency direction and import fan-out;
- source lines by thematic owner and in total;
- observable behavior, concurrency, rollback, and fresh-reload evidence.

A successful change may reduce the orchestration file while increasing explicit result propagation. That trade is acceptable only when the new lines make control flow or ownership clearer and no smaller plain-Python expression preserves the same guarantees.
