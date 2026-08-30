# Cross-boundary brief preservation

Use this procedure only for a typed `cross-boundary` checkpoint. It projects named architecture, plans, and accepted evidence into a reviewable execution contract before implementation. A `local` checkpoint does not add contracts, reviewed-authority coverage, lifecycle declarations, or an independent brief review.

## Plan authority loading

Resolve and measure the complete authority set before loading source bodies. Create a temporary strict `pinboard-brief-sources/v1` JSON manifest with one row per exact selector:

```json
{
  "schema": "pinboard-brief-sources/v1",
  "sources": [
    {
      "authority_id": "architecture",
      "selector": "ARCHITECTURE.md#Dependency direction",
      "families": ["ownership", "dependencies"]
    }
  ]
}
```

Use one selector with several families instead of repeating or nesting the same selection. Run `pinboard brief-sources --file <manifest> --json` before reading any selected body. Correct overlap errors, inspect selected byte counts, spans, digests, and batches, then emit each batch once in ascending order.

Preserve each selector and selected digest as its read receipt. Across corrections, reuse exact unchanged receipts and reread only changed owners plus neighboring records whose meaning depends on them. If output truncates, continue from the first unread boundary without replaying returned content.

If the complete authority set plus working headroom cannot fit, stop before compiling or reviewing the brief. Narrow selectors, split only a semantically independent checkpoint, or move mechanical comparison into validated tooling. Do not omit an accepted requirement, prohibition, lifecycle sibling, or consumer to fit context.

## Compile canonical JSON

Prepare a strict `pinboard-work-brief/v2` JSON candidate. Pinboard decodes it directly into frozen records with unknown fields forbidden, validates its cross-references, canonicalizes it with sorted object keys and one final LF, and publishes the immutable accepted `.json` artifact through `pinboard brief publish --file <candidate> --json`. This JSON artifact is the sole semantic brief. The generated Markdown attempt view is read-only output and must not be edited or parsed as input.

The root record contains:

- schema and artifact identity: `schema`, positive `artifact_revision`, `attempt_id`, `item_id`, `branch`, `base_revision`, and `owner_task_id`;
- accepted scope: positive `revision` and lowercase SHA-256 `digest`;
- human context: `title`, `outcome`, `supported_production_roots`, `product_decision_and_provenance`, `testing_strategy`, `scope`, `bootstrap`, `compatibility`, `non_goals`, and `remaining_work`;
- one tagged `checkpoint` record.

Use one built-in design lens while compiling the brief: prefer existing canonical typed values and direct composition. Introduce a helper, wrapper, protocol, projection, or conversion only when it owns a current semantic boundary, meaningful complexity, reuse, or genuine substitution. Do not recreate an existing concept as a parallel tuple, dictionary, or field-by-field mirror. This lens shapes the proposed implementation; make it a mandatory contract or verification only when accepted scope or reviewed repository policy authorizes it.

Every checkpoint has a stable kebab-case `checkpoint_id`, separate human `title`, `outcome_description`, architecture impact, nonempty acceptance criteria, nonempty mandatory verification, and explicit deferrals. Its `boundary` tag selects one closed shape:

- `local` contains the common fields only;
- `cross-boundary` additionally requires `outcome: independently-buildable`, nonempty contracts, reviewed authorities, authoritative coverage, and one lifecycle partition.

When a checkpoint owns structural cleanup, its typed scope and acceptance criteria must name the readable typed consolidation, the direct orphan families to recurse through, and the evidence from one fresh fixed-point pass. Reference the repository's recursive-cleanup procedure for the method instead of duplicating it here; the generated Markdown projection remains non-authoritative.

When that cleanup collapses a closed classification, the contract must identify every duplicated encoding, equivalent alternative-handling branch, proposed canonical owner, and independently required boundary shape. It must distinguish label-only vocabularies, data-bearing alternatives, and context-dependent decisions; require equivalent branches to share one handler without erasing alternatives that another consumer distinguishes; and require exhaustive conversion at retained wire, storage, or presentation boundaries. Acceptance evidence must report core decision points, explicit boundary conversion, dependency volume, and source-size change separately, then name the fixed-point pass and the product distinctions deliberately retained. Reject a brief that prescribes one representation category for every closed family or treats smaller source or dependency counts as proof of a simpler decision model.

Architecture impact is tagged by `kind`:

- `none` records a reason ownership and dependency direction are unchanged;
- `read-only` records one project-relative authority selector and conformance reason;
- `update-required` records the authority selector that must change in the same candidate and why.

Each contract records `invariant`, `authority`, `consumer`, `failure`, `verification`, `revalidation`, and a tagged `authorization_basis`. Each mandatory verification record has an `obligation` and the same authorization basis. Use exactly one basis:

- `accepted-scope` with the current `item_id` and positive `scope_revision`;
- `authority`, `repository-policy`, or `existing-consumer` with an exact reviewed `authority_id` and `family`.

Dispatch checks basis reference integrity against the current SQLite attempt. The independent reviewer owns semantic truth: reject a syntactically valid source used under the wrong role, such as code cited as repository policy, a validator cited as a production consumer, or newly written prose cited as product authority. Verify that every mandatory tool, threshold, platform, compatibility obligation, and hardening check is required by accepted scope or selected authority bytes. Keep proportionate exploratory checks outside the mandatory list.

Each reviewed authority records a unique kebab-case `authority_id`, exact `selector`, selected-byte `reviewed_sha256`, and one or more unique kebab-case `families`. A selector is a project-relative file, optionally followed by `#` and one literal unique Markdown H1–H6 heading. Whole-file digests use unchanged bytes. Heading digests use the heading through the line before the next heading of equal or higher level, with LF line endings and one final LF.

Give every reviewed authority family exactly one coverage record. It names `authority_id`, `family`, `distinction`, `consumer`, `counterexample`, and one tagged `owner` disposition:

- `contract` with an exact contract invariant;
- `acceptance` with an exact criterion number;
- `deferred` with an exact deferral ID;
- `not-applicable` with a concrete reason.

Never defer or mark not applicable an in-scope prohibition. Missing coverage is a brief defect, not implementation discretion.

Lifecycle partition is tagged by `kind`. Use `not-applicable` with a reason when the checkpoint changes no related lifecycle operations. Use `required` with one record per related operation when adjacent operations consume related states. Each record names `operation`, `source_state`, `authority`, `evidence`, `effects`, and one cheapest `illegal_sibling`.

## Review the compiled contract

Commission one read-only reviewer in fresh context after compiling the checkpoint and before implementation. The reviewer task identity must differ from the attempt owner. Give the reviewer the canonical checkpoint and the same source plan. It must:

- inspect every selected source exactly once;
- verify every contract, criterion, mandatory verification basis, architecture declaration, and semantic source role;
- trace every reviewed family to exactly one coverage owner;
- test each cheapest counterexample and every lifecycle sibling;
- reject unsupported, absent, ambiguous, or contradictory coverage rather than asking the implementer to infer it.

Correct the JSON candidate when coverage is incomplete, then produce a new digest-bound review. Reuse unchanged source receipts across corrections, but reread changed owners and sweep neighboring records.

## Publish review evidence

Canonical encoding uses the application JSON codec with sorted object keys. The brief artifact adds one final LF. Digest inputs do not add presentation bytes:

- `checkpoint_sha256` is SHA-256 of the canonical encoded checkpoint record;
- `reviewed_authority_set_sha256` is SHA-256 of the canonical encoded ordered tuple of reviewed-authority records;
- each `reviewed_sha256` remains the digest of the selected source bytes.

Prepare strict `pinboard-work-brief-review/v2` JSON with `attempt_id`, stable `checkpoint_id`, both digests, independent `reviewer_task_id`, `status: complete`, `verdict: ready`, and one coverage result per brief coverage record. Each result repeats the exact `authority_id`, `family`, and tagged owner, records `verdict: covered`, and states the concrete `counterexample_result`.

Pass the candidate to `pinboard dispatch` with `--brief-review <candidate-file> --review-id <kebab-case-review-id>`. Dispatch validates and canonicalizes it before application-owned publication. It creates the immutable artifact once, reuses byte-identical evidence, and preserves differing collisions as rejected evidence. Omit both publication arguments when exact accepted ready evidence already exists. Publication arguments are cross-boundary-only and never change the canonical prompt.

Dispatch reselects the current action and accepted brief, verifies the stable checkpoint ID, canonical checkpoint and ordered-authority digests, selected-source digests, reviewer independence, exact coverage, and immutable evidence before returning the launch prompt.

## Reuse during implementation review

Use the compiled map again against the frozen candidate. Account for every criterion, contract and authorization basis, mandatory verification entry, coverage record, and lifecycle sibling. Every piece of blocking review feedback must cite one accepted owner or applicable reviewed repository rule. Classify anything else as a brief omission, authority contradiction, unresolved product decision, or new capability. Compare the architecture declaration with the final diff, requiring the named authority change in the same candidate when it is `update-required`.
