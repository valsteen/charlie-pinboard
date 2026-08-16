from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from repo_work import __version__
from repo_work.actions import Action, ActionError, actions_for, state_revision
from repo_work.markdown import parse_current, parse_queue
from repo_work.proposals import ProposalError, create_proposal
from repo_work.registration import RegistrationError, initialize_work_state, transfer_coordinator
from repo_work.root import RootError, resolve_project_root
from repo_work.transition import TransitionError, apply_action
from repo_work.validate import ValidationReport, validate_work_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-work",
        description="Inspect and transition one repository work ledger.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("root", help="Resolve the shared project and work roots.")

    validate = commands.add_parser("validate", help="Validate work state without modifying it.")
    validate.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="Show bounded current work facts.")
    status.add_argument("--json", action="store_true")

    actions = commands.add_parser("actions", help="List the legal contextual actions.")
    actions.add_argument("--role", choices=("coordinator", "worker", "observer"), required=True)
    actions.add_argument("--json", action="store_true")

    initialize = commands.add_parser("init", help="Create an empty ledger and register its first coordinator.")
    initialize.add_argument("--coordinator-task-id", required=True)
    initialize.add_argument("--host-id", required=True)

    proposal = commands.add_parser("proposal", help="Create one immutable inbox proposal.")
    proposal.add_argument("--file", type=Path, required=True)

    transition = commands.add_parser("transition", help="Apply one action returned by the actions command.")
    transition.add_argument("--action-id", required=True)
    transition.add_argument("--expected-revision", required=True)
    transition.add_argument("--generation", required=True, type=int)
    transition.add_argument("--subject-revision")
    transition.add_argument("--payload", required=True, type=Path)
    return parser


def _roots(arguments: argparse.Namespace) -> tuple[Path, Path]:
    project = arguments.project_root.resolve() if arguments.project_root else resolve_project_root(Path.cwd())
    work = arguments.work_root.resolve() if arguments.work_root else project / ".codex" / "work"
    return project, work


def _diagnostic_json(report: ValidationReport) -> dict[str, object]:
    return {
        "valid": report.valid,
        "diagnostics": [
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity.value,
                "path": str(diagnostic.path),
                "message": diagnostic.message,
                "hint": diagnostic.hint,
            }
            for diagnostic in report.diagnostics
        ],
    }


def _status(work: Path, project: Path) -> dict[str, object]:
    report = validate_work_state(work, project)
    if not report.valid:
        raise ActionError("WORK_STATE_INVALID", report.render())
    queue = parse_queue(work / "queue.md")
    current = parse_current(work / "current.md")
    coordinator = json.loads((work / "coordinator.json").read_text(encoding="utf-8"))
    return {
        "valid": True,
        "project_root": str(project),
        "work_root": str(work),
        "revision": state_revision(work),
        "focus_item": current.focus_item,
        "focus_attempt": current.focus_attempt,
        "active_attempts": [item.attempt for item in queue.items if item.state.value == "active"],
        "next_action": current.next_action,
        "counts": dict(Counter(item.state.value for item in queue.items)),
        "inbox_count": len(list((work / "inbox").glob("*.json"))),
        "coordinator": {
            "task_id": coordinator["task_id"],
            "host_id": coordinator.get("host_id"),
            "generation": coordinator["generation"],
        },
    }


def _action_from_arguments(arguments: argparse.Namespace) -> Action:
    if ":" not in arguments.action_id:
        raise TransitionError("ACTION_ID_INVALID", "Action identity must be 'kind:subject'.")
    kind, subject = arguments.action_id.split(":", 1)
    return Action(
        action_id=arguments.action_id,
        kind=kind,
        subject=subject,
        label=arguments.action_id,
        expected_revision=arguments.expected_revision,
        coordinator_generation=arguments.generation,
        subject_revision=arguments.subject_revision,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        project, work = _roots(arguments)
        if arguments.command == "root":
            print(json.dumps({"project_root": str(project), "work_root": str(work)}, sort_keys=True))
            return 0
        if arguments.command == "init":
            initialized = initialize_work_state(project, arguments.coordinator_task_id, arguments.host_id, work)
            print(f"OK WORK_STATE_INITIALIZED {initialized}")
            return 0
        if arguments.command == "validate":
            report = validate_work_state(work, project)
            print(json.dumps(_diagnostic_json(report), indent=2, sort_keys=True) if arguments.json else report.render())
            return 0 if report.valid else 10
        if arguments.command == "status":
            value = _status(work, project)
            if arguments.json:
                print(json.dumps(value, indent=2, sort_keys=True))
            else:
                print(f"OK WORK_STATE_VALID revision={value['revision']}")
                print(f"focus_item={value['focus_item'] or 'none'} focus_attempt={value['focus_attempt'] or 'none'}")
                print(f"next_action={value['next_action']} inbox={value['inbox_count']}")
            return 0
        if arguments.command == "actions":
            actions = actions_for(work, project, arguments.role)
            if arguments.json:
                print(json.dumps({"actions": [action.as_dict() for action in actions]}, indent=2, sort_keys=True))
            elif not actions:
                print("OK NO_ACTIONS_AVAILABLE")
            else:
                for action in actions:
                    print(f"{action.action_id}\t{action.label}")
            return 0
        if arguments.command == "proposal":
            value = json.loads(arguments.file.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ProposalError("PROPOSAL_INVALID", "Proposal file root must be an object.")
            path = create_proposal(work, project, value)
            print(f"OK PROPOSAL_CREATED {path}")
            return 0
        if arguments.command == "transition":
            action = _action_from_arguments(arguments)
            payload = json.loads(arguments.payload.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TransitionError("TRANSITION_INPUT_INVALID", "Payload root must be an object.")
            if action.kind == "transfer-coordinator":
                if action.expected_revision != state_revision(work):
                    raise TransitionError("STATE_REVISION_STALE", "Repository work state changed after this action was issued.")
                task_id = payload.get("task_id")
                host_id = payload.get("host_id")
                if not isinstance(task_id, str) or not isinstance(host_id, str):
                    raise TransitionError("TRANSITION_INPUT_REQUIRED", "task_id and host_id are required strings.")
                transfer_coordinator(work, project, action.coordinator_generation, task_id, host_id)
            else:
                apply_action(work, project, action, payload)
            print(f"OK TRANSITION_APPLIED {action.action_id}")
            return 0
        raise AssertionError(f"unhandled command {arguments.command}")
    except (RootError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ActionError, TransitionError) as error:
        print(str(error), file=sys.stderr)
        return 11
    except RegistrationError as error:
        print(str(error), file=sys.stderr)
        return 12
    except ProposalError as error:
        print(str(error), file=sys.stderr)
        return 13
