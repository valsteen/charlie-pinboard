# Cross-boundary brief preservation

Use this procedure only for a checkpoint declared `Checkpoint boundary: cross-boundary`. It makes projection from named architecture, plans, and accepted evidence into the execution brief reviewable before implementation. Local checkpoints do not add these tables, lifecycle declarations, or brief reviews.

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

Use one selector with several families instead of repeating or nesting the same selection. Run `pinboard brief-sources --file <manifest> --json` before reading any selected body. Correct `BRIEF_SOURCE_SELECTOR_OVERLAP` in the manifest. Inspect the returned selected byte counts, line spans, digests, and batches, then run `pinboard brief-sources --file <manifest> --emit-batch <index>` once for each batch in ascending order.

Preserve each selector and selected digest as its read receipt. Across corrections, reuse exact unchanged receipts and reread only changed owners plus neighboring rows whose meaning depends on them. If output truncates, continue from the first unread boundary without replaying returned content.

If the complete authority set plus working headroom cannot fit, stop before compiling or reviewing the brief. Narrow selectors, split only a semantically independent checkpoint, or move mechanical comparison into validated tooling. Do not omit an accepted requirement, prohibition, lifecycle sibling, or consumer to fit context.

## Compile the brief

The canonical `attempt.md` front matter uses `kind: work-attempt` and `schema: pinboard-work-brief/v1`. Dispatch rejects missing, arbitrary, or stale brief format tags before interpreting either a local or cross-boundary checkpoint.

1. Read the planned batches for the exact sources that own the checkpoint's relevant semantics. Keep semantic truth in those sources; the brief records durable selectors and the executable subset rather than copying source prose.
2. Verify the checkpoint's one architecture declaration against those sources. `none` must explain why ownership and dependency direction are unchanged. `read-only` names the project-relative architecture authority implementation must conform to. `update-required` names the authority that must change in the same candidate; a later documentation task is not a valid substitute.
3. Record `Checkpoint outcome: independently-buildable` and this seven-column `Contract table`:

   | Invariant | Authority / owner | Required consumer or production observation | Failure classification | Exact verification | Preflight / final revalidation | Authorization basis |
   | --- | --- | --- | --- | --- | --- | --- |

   Give every row exactly one authorization basis. Use `accepted-scope:<item>@<positive-scope-revision>` when the current accepted item owns the behavior. Use `authority:<authority-id>#<family>` for a reviewed product authority, `repository-policy:<authority-id>#<family>` for an applicable reviewed repository constraint, or `existing-consumer:<authority-id>#<family>` for a named reviewed production consumer. The latter three forms must reference an exact family in the `Reviewed authorities` table.

   Dispatch checks syntax and reference integrity. For `accepted-scope`, it compares the token with the item and accepted scope revision reselected from the current SQLite attempt rather than trusting brief front matter. The independent reviewer owns semantic truth: it must reject a syntactically valid source used under the wrong role, such as code cited as repository policy, a validator cited as a production consumer, or newly written prose cited as product authority.
4. Add one `Reviewed authorities` table:

   | Authority ID | Selector | Reviewed SHA-256 | In-scope families |
   | --- | --- | --- | --- |

   Use a unique kebab-case authority ID. A selector is a project-relative file, optionally followed by `#` and one literal unique Markdown H1–H6 heading. The first `#` separates the path; later `#` characters are literal heading text. Hash whole-file bytes unchanged. For a heading selector, hash the heading through the line before the next heading of equal or higher level, serialized with LF line endings and one final LF. List one or more unique comma-separated kebab-case families per authority.
5. Add one `Authoritative coverage` table:

   | Authority / invariant family | Required distinction | Required consumer / production observation | Disposition | Brief owner | Cheapest counterexample |
   | --- | --- | --- | --- | --- | --- |

   Give every declared family exactly one `authority:<id>#<family>` row. Name the distinction that projection could lose, every consumer that must observe it, and the cheapest case that would expose the loss. Use one of these dispositions and owners:

   - `contract` with `contract:<exact Contract-row invariant>`;
   - `acceptance` with `criterion:<exact acceptance-criterion number>`;
   - `deferred` with `deferral:<label>`, where the checkpoint also has `Deferral: <label> — <reason> Reopen when: <condition>`;
   - `not-applicable` with `reason:<nonempty explanation>`.

   Do not defer or mark an in-scope prohibition not applicable.
6. Declare the lifecycle partition exactly once. Use `Lifecycle partition: not-applicable — <reason>` when the checkpoint does not add or change related lifecycle operations. When adjacent operations consume related states, use `Lifecycle partition: required` and add this table:

   | Operation | Allowed source state | Required authority | Required observation / evidence | State and fencing effects | Nearest illegal sibling / stable rejection |
   | --- | --- | --- | --- | --- | --- |

   Use one concrete unique kebab-case row per operation. Partition the relevant source-state classes so no neighboring operation silently owns the same class. The table stays bounded to the named lifecycle authority; do not expand it into a field Cartesian product.

## Review before dispatch

Commission one read-only reviewer in fresh context after the checkpoint is compiled and before implementation begins. The reviewer task identity must differ from the attempt owner, and both task identities must be canonical values without surrounding whitespace.

Give the reviewer the canonical checkpoint and the same manifest or its complete `brief-sources --json` plan. The reviewer must:

- recompute every selected-source digest;
- compare each coverage distinction and owner with its named source;
- verify every Contract authorization basis against its selected bytes, including wrong-role counterexamples for product authority, repository policy, and existing consumer claims;
- test the architecture declaration against the named authority and reject any hidden ownership or dependency-direction change;
- test the cheapest counterexample for every coverage row;
- verify a required lifecycle table partitions every relevant source-state class and rejects its nearest sibling;
- return one complete correction package rather than stopping after the first missing or ambiguous row.

Missing coverage is a brief defect. Correct `attempt.md`, create a new digest-bound review, and do not ask the implementer to infer the omitted rule. A correction review may reuse exact unchanged selector digests and owners, but it must re-read changed owners and sweep every changed or neighboring row.

## Publish review evidence

Normalize the exact checkpoint section with LF line endings and one final LF, then compute its SHA-256. Compute the reviewed-authority-set SHA-256 over the exact LF-normalized `Reviewed authorities` table bytes, from its header row through its last data row, in source order and with one final LF.

Prepare a complete ready verdict for the canonical path:

```text
attempts/<attempt>/brief-reviews/<checkpoint-sha256>.md
```

Use this front matter:

```yaml
---
kind: work-brief-review
schema: pinboard-work-brief-review/v1
attempt: <attempt>
checkpoint: <exact checkpoint heading>
checkpoint_sha256: <checkpoint sha256>
reviewed_authority_set_sha256: <reviewed-authority-set sha256>
reviewer_task_id: <independent reviewer task>
status: complete
verdict: ready
---
```

Its body contains exactly one result row per coverage row:

| Authority / invariant family | Brief owner | Verdict | Cheapest counterexample result |
| --- | --- | --- | --- |

Copy the coverage reference and owner exactly, use `covered`, and record the concrete counterexample result. Keep the candidate outside the canonical ready path, then pass it to the existing dispatch command with `--brief-review <candidate-file> --review-id <kebab-case-review-id>`. Dispatch validates the candidate before publication through the SQLite artifact workflow. It creates the canonical artifact once, reuses byte-identical evidence, and never overwrites differing evidence. A differing ready collision is preserved as rejected evidence, then dispatch rejects with `DISPATCH_BRIEF_REVIEW_COLLISION`. Reusing that rejected identity is safe only for identical bytes.

Preserve incomplete or rejected work directly under the rejected directory; never offer it as the ready candidate. A corrected checkpoint has a new digest and never overwrites prior evidence. Omit both publication arguments when validated ready evidence already exists. Publication arguments are cross-boundary-only and never change the canonical prompt.

`pinboard dispatch` recomputes the checkpoint, authority-table, and selected-source digests and revalidates its SQLite action before returning. It rejects absent, stale, mismatched, incomplete, non-ready, or same-owner review evidence before rendering the canonical prompt.

## Reuse during implementation review

Use the compiled map again when reviewing the frozen implementation. Account for every acceptance criterion, Contract row and its authorization basis, coverage row, and lifecycle sibling row. Every blocking finding must cite one of those accepted owners or an applicable reviewed repository rule. Classify anything else as a brief omission, authority contradiction, unresolved product decision, or new capability rather than turning it into an implementation defect. Compare the architecture declaration with the final diff, and require the named authority change in the same candidate when the declaration is `update-required`. Reuse exact unchanged selector hashes across correction rounds; re-read changed owners and sweep their neighboring rows. This review checks implementation against the already-compiled contract instead of rediscovering requirements from architecture.
