# Stress-test developer navigation

Use this test when a command or closed-variant family crosses several production owners. It complements reachability and correctness checks by asking whether the surviving design can be followed and changed without reconstructing parallel routing systems.

## Trace one supported value

Choose a representative command or variant with a real production entry point and effect. Follow it from input through validation, decisions, conversions, effects, and presentation. Repeat for another family only when it has a distinct routing shape, such as state mutation versus read projection or direct versus borrowed authority; stop adding traces when a new shape exposes no new owner category. Record each production site a developer must discover, in order, and classify why it exists:

- boundary validation or conversion;
- product or authorization decision;
- effect or persistence;
- presentation;
- same-meaning pass-through or remap.

Names, imports, and direct calls should make the next owner predictable. A pass-through site is a cleanup candidate when it introduces another discriminator, record, conversion table, or exhaustive branch without changing meaning or owning one of the first four responsibilities.

## Generate candidates mechanically

Search the complete production surface for these signals, then verify each with the trace and sibling simulation rather than treating the signal as proof:

- a route, tag, or enum immediately remapped to a correspondingly named typed value;
- duplicate or near-duplicate exhaustive branches over the same family;
- an exhaustive branch whose arms all read the same common field or invoke the same operation;
- repeated `match`, `isinstance`, ternary, or membership tests over the same closed family along one use-case path;
- a closed family handled through `else`, `case _`, mapping defaults, inherited behavior, optional callbacks, or a default registry handler;
- separate vocabularies with identical members and consumers that appear to assign the same meaning.

Before closing this pass, sweep the complete production surface for several shapes that ordinary reachability counts systematically miss:

- methods or functions whose body only returns one attribute, reapplies a constructor, or delegates unchanged arguments;
- the same effect, transition, or presentation choice selected independently by neighboring modes or routes;
- a test-only public root and the private helper chain that exists only beneath it;
- base protocols plus composite protocols whose required members overlap without distinct substitution consumers;
- defaults, version labels, compatibility names, and plural capability names that describe a predecessor rather than the supported product.

For each shape, record the search or analyzer, scope, candidate selectors or zero result, and disposition in the compact ledger. A representative trace can validate a design, but one trace is not evidence that these other shapes were searched.

The last signal needs extra care. Identical spelling does not prove identical product identity. Retain nominally separate vocabularies when they prevent cross-family assignment, have different transition rules or consumers, or may evolve independently as distinct product concepts. Collapse them only when they name the same fact and all current owners interpret that fact identically.

## Simulate one sibling change

Choose a plausible sibling that has the same boundary and lifecycle shape as the representative value. Without implementing it, list the files and branches that would need edits. Separate sites that define new behavior from sites that merely repeat routing knowledge.

Keep an exhaustive site when that owner must distinguish the sibling to validate it, decide differently, convert an independent external or persisted shape, apply a different effect, or present a different result. Fold a site when the sibling was already selected and the site only maps one name or same-shaped record to another.

Do not optimize the edit count by replacing a legitimate closed decision with reflection, a registry, callbacks, inheritance, or open runtime dispatch. Dynamic dispatch is not itself residue when the caller should not distinguish implementations or the extension family is genuinely open. In that case, require one visible wiring owner, an explicit required surface, and explicit failure for unsupported implementations; inherited behavior, optional handlers, mapping defaults, and catch-all branches must not silently accept a missing case.

Judge each surviving dispatch on four separate axes:

- navigability: can a developer predict and reach the implementation from the call site;
- refactoring resilience: does adding or renaming a closed alternative force every required owner to change without falling through a default;
- boilerplate: how many sites merely restate the same selected fact;
- runtime reliability: where invalid input, missing registration, and incomplete implementation fail.

Prefer exhaustive matching at a closed boundary, product decision, effect, persistence conversion, or presentation owner. Prefer a direct call, required protocol, or callback inside a cohesive module or at a deliberately open library boundary when the caller has no alternative-specific decision. Prefer a plain mapping only for genuinely data-driven lookup, with missing keys failing explicitly; do not disguise behavior dispatch as data.

This stress test produces cleanup candidates, not automatic deletion authority. Preserve a site when the trace exposes an independent observation, nominal distinction, boundary representation, policy, effect, or presentation responsibility that the superficial code shape concealed.

## Mock example: CLI leaf routing

Suppose one command currently travels through:

```text
argparse leaf
  -> CliRoute enum
  -> route-to-command decoder match
  -> typed command union
  -> exhaustive command-family router
  -> handler
```

Simulating a sibling command shows that `CliRoute` and the route-to-command match repeat the parser leaf's selection. They own no additional validation or product decision. Let an ordinary parser leaf carry inert metadata naming its exact command model. Keep a small explicit decoder branch only for leaves whose coupled options genuinely select between multiple command records. Retain the typed command union and the exhaustive command-family router: the union prevents invalid handler inputs, and the router still owns a real closed dispatch decision. Do not attach executable decoder callbacks to parser state; that hides the construction path behind dynamic behavior and makes static navigation worse.

The same test can justify superficially repeated work. Selecting an advertised capability before decoding its variant-specific payload and validating it again under a write lock are different observations when concurrency may intervene. Retain both, name the distinction clearly, and remove only remapping that does not own it.

## Evidence and stop condition

Report the before-and-after traces, sibling edit sites, retained exhaustive sites with their owned distinction, semantic sweep receipts, and deleted pass-through sites. Stop when fresh traces for each distinct routing shape find no same-meaning routing fact repeated outside a boundary, decision, effect, or presentation owner, the sibling simulations expose no ceremonial edit site in scope, and every sweep above has a concrete zero-result or disposition receipt.
