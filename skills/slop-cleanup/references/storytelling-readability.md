# Proof-read code as a product story

Use this optional pass only after the human chooses it. Its outcome is not “more comments” or one preferred architecture. Its outcome is a supported flow whose overview and implementation let a computer-literate newcomer tell the same accurate story.

## Establish the reading contract

Choose one representative supported path and the smallest set of current documentation that claims to explain it. Identify which sources own the current product story and which files are generated projections. Do not treat an old plan, test description, architecture fashion, or generated page as truth merely because it is easier to read.

Agree on the reader: someone who understands ordinary software ideas but may not know the implementation language, type system, framework conventions, or repository history. The reader may follow names and calls, but should not need type inference to discover whether a step reads, validates, decides, writes, refreshes, or presents.

Keep this evidence private unless the human requests a durable report. Record the selected entry point, overview, trace, unresolved questions, candidate repairs, and stop condition compactly.

## Get an uncoached reading

Have a fresh reader start with the overview, then follow the representative code path. Do not explain the intended architecture first. Ask the reader to retell:

- what entered the system and when it became an exact request;
- what information was merely observed;
- which caller-supplied claims were resolved against that observation;
- where current state became authoritative for the change;
- where legality was decided and how an expected rejection leaves;
- where an accepted decision became a durable effect;
- which later work is replaceable or repairable;
- what is finally presented to the caller.

At each call, ask four plain questions: What verb describes this step? Where did this value come from? What can end normally here? Which owner should I read next? Capture uncertainty rather than teaching around it. A guide that supplies answers absent from the code is evidence of a missing-manual problem, not a successful reading.

## Use a stage grammar as a probe

For a changing command, this sequence is a useful probe:

```text
decode exact input
  → observe context
  → resolve supplied claims into a requested change
  → reread locked current state
  → decide
  → project the accepted change
  → commit
  → refresh replaceable views
  → present committed state
```

Do not force these names or stages onto the repository. A different architecture may combine them, order them differently, or have no views or transaction. Translate the probe into the product's actual responsibilities, then make absent and combined stages explicit. The important test is whether one story explains both code and documentation without hiding effects.

Pay special attention to pairs that code often blurs:

- decoded input versus a domain request;
- observed context versus locked current state;
- caller-supplied authority versus resolved authority;
- validation of shape versus a decision using current state;
- accepted decision versus committed effect;
- authoritative storage versus generated projection;
- expected rejection versus infrastructure failure;
- committed state versus the value finally rendered.

## Compare documentation and code in both directions

Write the overview's stages and the code's stages side by side. For each stage, record its verb, input provenance, owner, effect, expected exits, and next stage. A mismatch is real when either side invents, omits, reorders, or renames a meaningful responsibility.

Do not assume documentation wins. Correct the overview when it describes an obsolete or idealized flow. Correct code when its names or composition conceal current behavior. Correct both when they share the same shape only after expert interpretation. Generated documentation changes at its source and is regenerated; it is never patched as the authority.

Do not convert narrative surprise into a correctness claim. Check accepted requirements, observable behavior tests, and current consumers before recommending a semantic change. An unusual time sample, authorization label, commit abstraction, or latest-state presentation may be deliberate even when it is poorly explained. When product intent is unresolved, record the question and offer behavior-preserving naming, composition, or documentation options; ask the human before changing behavior.

## Repair the earliest misleading owner

Prefer the smallest repair that makes the next step predictable:

1. Rename effectful helpers with verbs and concrete product objects.
2. Give values provenance names such as `observed_state`, `requested_change`, `locked_state`, `accepted_decision`, `focused_mutation`, and `committed_state` when those distinctions exist.
3. Use named arguments where several same-shaped values otherwise require positional decoding.
4. Recompose a long function when another arrangement makes the ordered product story or next owner clearer. Extract only functions that own meaningful inputs, effects, and exits; inline or regroup helpers when their separation creates a scavenger hunt.
5. Move a responsibility when the reading exposes genuine mixed ownership; explain the proposed boundary and obtain the human's decision before expanding cleanup scope.
6. Update the current-story overview to the same vocabulary and order.
7. Add durable contributor guidance only when the method should govern future delivery and no earlier executable owner can enforce it.

Do not add a pipeline abstraction, registry, callback system, inheritance hierarchy, or layer vocabulary merely to make the diagram symmetrical. Symmetry means comparable responsibilities read comparably. It does not mean unrelated paths must acquire identical machinery.

Function length, lint smell categories, duplication reports, and split-by-default style are weak evidence. Use them to ask a question, not to decide the structure. Prefer one longer coherent owner over several shorter functions that scatter one decision or effect. Preserve duplicated-looking code when the copies belong to independent owners; consolidate it only when the same behavior or decision genuinely needs one owner.

## Stress-test the family, not one polished example

Trace at least one sibling operation that shares the same responsibilities. It should use the same verb grammar and provenance distinctions without duplicating a routing fact in every layer. When the flow is a command or closed family, reuse the [developer-navigation lens](../../pinboard/references/developer-navigation.md) and, for an exhaustive cleanup audit, the [developer-navigation stress test](dx-stress-test.md).

Simulate adding one sibling. Count the owners a developer must discover and edit. Keep a site when it owns input decoding, validation, policy, representation conversion, an effect, or presentation. Recommend folding a site when it only repeats a fact already selected elsewhere. A low edit count is evidence of navigation cost, not proof of readable responsibility.

## Let the human choose the durable rule

Present the observed reading failure, the smallest repair, any responsibility-level refactor, and the tradeoff of making the method durable. Ask the human which recurring standard they want future implementers and reviewers to follow. Place the accepted rule at the earliest stable owner: executable structure where possible, project design principles for a reusable method, a product overview for the current story, and a thin contributor route when automatic consultation is needed.

Do not turn one repository's layer names or stage vocabulary into a universal rule. Preserve the reason, the reader test, and the stop condition rather than a frozen implementation recipe.

## Validate the outcome

Run behavior, expected-rejection, transaction, concurrency, generation, link, and metadata checks appropriate to the changed surface. Do not add unit tests that assert prose wording. Regenerate authoritative documentation projections and inspect meaningful visual output rather than relying only on a generator exit code.

Repeat the fresh reading after the changes. Finish when the reader can tell one accurate ordered story from overview through code, sibling paths are symmetric where their responsibilities match, authoritative and repairable effects remain distinct, and another refactor would only rename already clear work or introduce ceremony.

When validating this cookbook itself, compare two context-isolated readers on the same historical repository snapshot and identical task. Give the control reader the existing cleanup guidance without this cookbook and give the other reader the revised skill. Compare whether they recover the intended sequence, documentation mismatches, effect boundaries, architecture-neutral advice, and bounded next changes. Report only the demonstrated scenario; one successful comparison does not prove universal effectiveness.
