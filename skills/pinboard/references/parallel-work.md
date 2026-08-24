# Parallel Work

Use this workflow for requests such as:

- “What can run independently right now?”
- “Let me choose a batch from the safe work.”
- “Launch all safe work in parallel.”

Do not use it merely because more than one item exists. Ordinary next-work selection stays in the main skill.

## Build the preview

1. Require authority `sqlite-v1` and identify the host on which host-local resources would be used.
2. Run `pinboard parallel preview --host-id <host> --json` without a coordination lease.
3. Present the result in three compact groups:
   - **Ready together:** the unambiguous all-safe set.
   - **Choose one:** candidates that structurally qualify alone but conflict over a host-local resource.
   - **Not ready:** excluded items with the command's exact reason translated into ordinary language.
4. Add one execution-form recommendation to every launchable item:
   - **Visible task** when the item is ready but has no accepted attempt, its brief is incomplete, or design, refinement, new authority, live-application work, external writes, or likely user input remain.
   - **Subagent** only when an active attempt has a complete accepted brief, an independently buildable checkpoint, already-authorized permissions, and no expected user decision or input.
5. Explain the recommendation in one short phrase. Do not infer that structurally independent means semantically autonomous.

If the request was only to list or preview, stop after presenting the groups. A read-only preview is not launch permission.

## Resolve batch authority

Treat either of these as explicit launch authority:

- the user names an exact subset from the preview;
- the user says to launch all safe work.

“All safe” means only the preview's **Ready together** group. It never includes **Choose one** candidates. Ask one concrete selection question if the all-safe set is empty but resource-conflicting candidates remain.

Before creating anything, rerun one selected preview containing every authorized item:

```text
pinboard parallel preview --host-id <host> --item <first> --item <second> --json
```

Proceed only when `safe` is true. Preserve its revision as the batch observation. If it is false, show the changed reason and ask only for the decision that the new state requires.

## Launch each outcome

Work through the authorized items in the presented order. Before each external creation, rerun the selected preview for that item and every not-yet-created item. Stop the remaining batch if structural safety changed; tasks already created remain real and must be reported.

For a **Visible task**:

1. Use the environment's native user-visible task creation capability. This is authorized by the user's exact batch request.
2. Give it the repository root, item identity, fresh preview revision, and an instruction to use the pinboard to inspect the item and borrow coordination only for its own legal transition.
3. Keep design and authorization questions in that visible task. Do not replace it with a subagent when visible-task creation is unavailable.

For a **Subagent**:

1. Follow the main skill's delegated-attempt procedure, including the canonical attempt brief and exact dispatch prompt.
2. Launch it through the environment's subagent capability only after the dispatch check succeeds.
3. Do not convert an incomplete, ambiguous, or interactive item into a bounded prompt merely to include it in the batch.

Task creation is an external effect, not a ledger transaction. Do not claim atomic launch or try to roll back a successfully created task because a later creation failed.

## Report the batch

Keep the report compact and exact:

| Item | Form | Result |
| --- | --- | --- |
| `<item>` | visible task or subagent | created with task identifier, or not created with exact cause |

Say `batch launched` only when every authorized item was created. Otherwise say `partial launch`, identify what exists, name the first changed-state or transport failure, and state whether retry needs user action. Never count a prepared prompt, retained proposal, or attempted message as a created task.
