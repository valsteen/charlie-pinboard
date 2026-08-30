"""Read-only installed work-inspection composition and presentation.

Functions in this module may read an explicitly selected SQLite work root and
write command output. They never mutate the ledger, publish artifacts, change
authority, refresh generated views, obtain a lease, or own a transaction.
"""

import sys
from collections import Counter
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import actions as action_queries
from pinboard.application import queries, query_models
from pinboard.domain import decision_models, work_models
from pinboard.domain import errors as domain_errors
from pinboard.interfaces import cli_commands, errors, transition_input, work_inspection_models
from pinboard.interfaces.cli_output import write_json


def overview_item_view(item: query_models.OverviewItem) -> work_inspection_models.OverviewItemView:
    return work_inspection_models.OverviewItemView(
        item.item_id,
        item.label,
        item.state.value,
        item.position,
        item.eligible,
        item.timing,
        item.depends_on,
        tuple(
            work_inspection_models.DependencyReasonView(value.item_id, value.reason)
            for value in item.dependency_reasons
        ),
        tuple(
            work_inspection_models.ReviewFlagView(value.kind.value, value.related_item, value.reason)
            for value in item.review_flags
        ),
        item.attempt_id,
        item.next_action,
        item.notes,
    )


def overview_view(overview: query_models.WorkOverview) -> work_inspection_models.OverviewView:
    return work_inspection_models.OverviewView(
        overview.schema,
        overview.authority,
        overview.revision,
        overview.focus_item,
        overview.focus_attempt,
        overview.active_attempts,
        tuple(overview_item_view(item) for item in overview.items),
        overview.immediate_options,
    )


def action_semantics_view(
    semantics: decision_models.ActionSemantics,
) -> work_inspection_models.ActionSemanticsView:
    return work_inspection_models.ActionSemanticsView(
        semantics.use_case,
        semantics.effect.value,
        tuple(role.value for role in semantics.permitted_roles),
        semantics.subject_kind.value,
        semantics.lifecycle_precondition.value,
        semantics.practical_result,
    )


def input_contract_view(
    kind: decision_models.ActionKind,
) -> errors.TransitionInputResult[work_inspection_models.InputContractView]:
    semantics = decision_models.action_semantics(kind)
    if semantics.effect == decision_models.ActionEffect.ADVISORY:
        payload_schema = None
    else:
        encoded_schema = transition_input.encoded_transition_input_schema(kind)
        if isinstance(encoded_schema, errors.TransitionInputFailure):
            return encoded_schema
        payload_schema = msgspec.Raw(encoded_schema)
    return work_inspection_models.InputContractView(kind.value, action_semantics_view(semantics), payload_schema)


def action_view(
    action: decision_models.Action,
    *,
    include_input_contract: bool = False,
) -> errors.TransitionInputResult[work_inspection_models.ActionView]:
    capability = action.capability
    input_contract: work_inspection_models.InputContractView | None = None
    if include_input_contract:
        contract = input_contract_view(action.kind)
        if isinstance(contract, errors.TransitionInputFailure):
            return contract
        input_contract = contract
    return work_inspection_models.ActionView(
        action_id=decision_models.action_id(action),
        kind=action.kind.value,
        subject=capability.subject,
        label=capability.label,
        expected_revision=capability.expected_revision,
        coordinator_generation=capability.coordinator_generation,
        subject_revision=capability.subject_revision or "",
        authorization="observer" if capability.authorization is None else capability.authorization.value,
        lease_id=capability.lease_id or "",
        semantics=action_semantics_view(decision_models.action_semantics(action.kind)),
        input_contract=input_contract,
    )


def parallel_preview_view(
    preview: query_models.ParallelPreview,
) -> work_inspection_models.ParallelPreviewView:
    launchable: list[work_inspection_models.ParallelItemView] = []
    excluded: list[work_inspection_models.ParallelItemView] = []
    for item in preview.items:
        match item:
            case query_models.LaunchableParallelItem():
                launchable.append(
                    work_inspection_models.ParallelItemView(
                        item.item_id,
                        item.label,
                        item.state.value,
                        item.attempt_id,
                        "launchable",
                        (),
                    )
                )
            case query_models.ExcludedParallelItem(reasons=reasons):
                excluded.append(
                    work_inspection_models.ParallelItemView(
                        item.item_id,
                        item.label,
                        item.state.value,
                        item.attempt_id,
                        "excluded",
                        tuple(
                            work_inspection_models.ParallelReasonView(reason.code.value, reason.message)
                            for reason in reasons
                        ),
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)
    return work_inspection_models.ParallelPreviewView(
        preview.schema,
        preview.revision,
        preview.selection.value,
        preview.safe,
        tuple(launchable),
        tuple(excluded),
    )


def status_view(work: Path, source_checkout: Path, shared_repository: Path) -> work_inspection_models.StatusView:
    state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
    overview_value = queries.overview_from_state(state)
    coordinator = state.authority.coordination
    return work_inspection_models.StatusView(
        valid=True,
        source_checkout_root=str(source_checkout),
        shared_repository_root=str(shared_repository),
        work_root=str(work),
        revision=str(state.lifecycle.project.revision),
        focus_item=overview_value.focus_item,
        focus_attempt=overview_value.focus_attempt,
        active_attempts=overview_value.active_attempts,
        next_action=state.focus.next_action,
        counts=dict(Counter(item.state.value for item in state.lifecycle.work_items)),
        visible_candidate_count=sum(1 for item in overview_value.items if item.state == work_models.WorkState.INTAKE),
        coordinator=(
            work_inspection_models.CoordinatorView(
                str(coordinator.task_id),
                str(coordinator.host_id),
                coordinator.generation,
                str(coordinator.lease_id),
                coordinator.expires_at.isoformat(),
                coordinator.state.value,
            )
            if coordinator is not None
            else None
        ),
        authority="sqlite-v1",
    )


def status(roots: cli_commands.ResolvedRoots, command: cli_commands.StatusCommand) -> int:
    value = status_view(roots.work, roots.source_checkout, roots.shared_repository)
    if command.json:
        write_json(value)
    else:
        print(f"OK WORK_STATE_VALID revision={value.revision}")
        print(f"focus_item={value.focus_item or 'none'} focus_attempt={value.focus_attempt or 'none'}")
        print(f"next_action={value.next_action} visible_candidates={value.visible_candidate_count}")
    return 0


def overview(roots: cli_commands.ResolvedRoots, command: cli_commands.OverviewCommand) -> int:
    value = queries.overview_from_state(SQLiteWorkStore(roots.work / "state.sqlite3").snapshot())
    if command.json:
        write_json(overview_view(value))
        return 0
    print(f"OK WORK_OVERVIEW revision={value.revision} authority={value.authority}")
    if not value.items:
        print("live_work=none")
    for item in value.items:
        attempt = f" attempt={item.attempt_id}" if item.attempt_id is not None else ""
        next_action = item.next_action or "none"
        print(
            f"{item.position}\t{item.item_id}\t{item.state.value}\teligible={str(item.eligible).lower()}"
            f"\tnext={next_action}{attempt}\t{item.label}"
        )
    print(
        f"visible_candidates={sum(1 for item in value.items if item.state == work_models.WorkState.INTAKE)} "
        f"immediate_options={len(value.immediate_options)}"
    )
    return 0


def item_status(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ItemStatusCommand,
) -> errors.CommandResult[int]:
    value = queries.item_status(SQLiteWorkStore(roots.work / "state.sqlite3"), command.item_id)
    if isinstance(value, domain_errors.DecisionFailure):
        return errors.CommandFailure(value.code, value.message)
    if command.json:
        write_json(value)
        return 0
    print(
        f"OK ITEM_STATUS item={value.item_id} state={value.state.value} "
        f"revision={value.revision} authority={value.authority}"
    )
    print(
        f"label={value.label} timing={value.timing.value if value.timing is not None else 'none'} "
        f"next_action={value.next_action or 'none'}"
    )
    print(f"outcome_evidence={value.outcome_evidence or 'none'} notes={value.notes or 'none'}")
    if not value.attempts:
        print("attempts=none")
    for attempt in value.attempts:
        print(
            f"attempt={attempt.attempt_id} state={attempt.state.value} candidate={attempt.candidate_revision or 'none'}"
        )
    return 0


def actions(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ActionsCommand | cli_commands.LeasedActionsCommand,
) -> errors.CommandResult[int]:
    match command:
        case cli_commands.ActionsCommand():
            lease_id = None
            generation = None
        case cli_commands.LeasedActionsCommand(lease_id=lease_id, generation=generation):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    available = action_queries.discover_actions(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        command.role,
        lease_id=lease_id,
        generation=generation,
    )
    if isinstance(available, domain_errors.DecisionFailure):
        return errors.CommandFailure(available.code, available.message)
    exact_action_id = command.action_id
    if exact_action_id is not None:
        available = tuple(action for action in available if decision_models.action_id(action) == exact_action_id)
        if not available:
            return errors.CommandFailure(
                domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Action '{exact_action_id}' is not currently legal for this role and lease.",
            )
    if command.json:
        action_views: list[work_inspection_models.ActionView] = []
        for action in available:
            view = action_view(action, include_input_contract=exact_action_id is not None)
            if isinstance(view, errors.TransitionInputFailure):
                return errors.CommandFailure(view.code, view.message)
            action_views.append(view)
        write_json(work_inspection_models.ActionsView(tuple(action_views)))
    elif not available:
        print("OK NO_ACTIONS_AVAILABLE")
    else:
        for action in available:
            print(f"{decision_models.action_id(action)}\t{action.capability.label}")
    return 0


def input_contract(
    _roots: cli_commands.ResolvedRoots,
    command: cli_commands.InputContractCommand,
) -> errors.CommandResult[int]:
    value = input_contract_view(command.action_kind)
    if isinstance(value, errors.TransitionInputFailure):
        return errors.CommandFailure(value.code, value.message)
    if command.json:
        write_json(value)
    else:
        print(f"OK INPUT_CONTRACT action_kind={value.action_kind}")
        print(f"use_case={value.semantics.use_case}")
        print(
            f"effect={value.semantics.effect} permitted_roles={','.join(value.semantics.permitted_roles)} "
            f"subject_kind={value.semantics.subject_kind} "
            f"lifecycle_precondition={value.semantics.lifecycle_precondition}"
        )
        print(f"practical_result={value.semantics.practical_result}")
        if value.payload_schema is None:
            print("payload_schema=none")
        else:
            sys.stdout.write(msgspec.json.format(bytes(value.payload_schema), indent=2).decode() + "\n")
    return 0


def _print_parallel_group(title: str, items: tuple[work_inspection_models.ParallelItemView, ...]) -> None:
    print(f"{title}:")
    if not items:
        print("- none")
        return
    for item in items:
        detail = "; ".join(reason.message for reason in item.reasons)
        attempt = f", attempt {item.attempt_id}" if item.attempt_id is not None else ""
        suffix = f" — {detail}" if detail else ""
        print(f"- {item.item_id} ({item.state}{attempt}){suffix}")


def parallel(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ParallelPreviewCommand,
) -> errors.CommandResult[int]:
    preview = queries.preview_parallel(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        selected=tuple(command.item),
    )
    if isinstance(preview, query_models.QueryFailure):
        match preview.code:
            case query_models.QueryRejectionCode.PARALLEL_SELECTION_INVALID:
                code = errors.CommandErrorCode.PARALLEL_SELECTION_INVALID
            case query_models.QueryRejectionCode.PARALLEL_TIME_INVALID:
                code = errors.CommandErrorCode.PARALLEL_TIME_INVALID
            case _ as unreachable:
                assert_never(unreachable)
        return errors.CommandFailure(code, preview.message)
    view = parallel_preview_view(preview)
    if command.json:
        write_json(view)
    else:
        print(
            f"OK PARALLEL_PREVIEW revision={preview.revision} selection={preview.selection.value} "
            f"safe={'yes' if preview.safe else 'no'}"
        )
        _print_parallel_group("Ready to launch together", view.launchable)
        _print_parallel_group("Not launchable", view.excluded)
    return 0
