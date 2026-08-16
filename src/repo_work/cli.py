import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from repo_work import __version__
from repo_work.actions import Action, ActionError, actions_for, state_revision
from repo_work.coordinator import read_coordinator
from repo_work.json_values import JsonObjectError, read_json_object
from repo_work.markdown import parse_current, parse_queue
from repo_work.proposals import ProposalError, create_proposal
from repo_work.registration import RegistrationError, initialize_work_state
from repo_work.root import RootError, resolve_project_root
from repo_work.transition import TransitionError, apply_action
from repo_work.validate import ValidationReport, validate_work_state


@dataclass(frozen=True, slots=True)
class CommandContext:
    arguments: argparse.Namespace
    project: Path
    work: Path


type CommandHandler = Callable[[CommandContext], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-work", description="Inspect and transition one repository work ledger.")
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
    project_argument = getattr(arguments, "project_root", None)
    project = project_argument.resolve() if isinstance(project_argument, Path) else resolve_project_root(Path.cwd())
    work_argument = getattr(arguments, "work_root", None)
    work = work_argument.resolve() if isinstance(work_argument, Path) else project / ".codex" / "work"
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


def _status_value(work: Path, project: Path) -> dict[str, object]:
    report = validate_work_state(work, project)
    if not report.valid:
        raise ActionError("WORK_STATE_INVALID", report.render())
    queue = parse_queue(work / "queue.md")
    current = parse_current(work / "current.md")
    coordinator = read_coordinator(work / "coordinator.json")
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
            "task_id": coordinator.task_id,
            "host_id": coordinator.host_id,
            "generation": coordinator.generation,
        },
    }


def _action_from_arguments(arguments: argparse.Namespace) -> Action:
    action_id = getattr(arguments, "action_id", None)
    if not isinstance(action_id, str) or ":" not in action_id:
        raise TransitionError("ACTION_ID_INVALID", "Action identity must be 'kind:subject'.")
    kind, subject = action_id.split(":", 1)
    expected_revision = getattr(arguments, "expected_revision", None)
    generation = getattr(arguments, "generation", None)
    subject_revision = getattr(arguments, "subject_revision", None)
    if not isinstance(expected_revision, str) or not isinstance(generation, int):
        raise TransitionError("TRANSITION_INPUT_INVALID", "Transition action tokens are invalid.")
    if subject_revision is not None and not isinstance(subject_revision, str):
        raise TransitionError("TRANSITION_INPUT_INVALID", "Subject revision must be a string.")
    return Action(action_id, kind, subject, action_id, expected_revision, generation, subject_revision)


def _root(context: CommandContext) -> int:
    print(json.dumps({"project_root": str(context.project), "work_root": str(context.work)}, sort_keys=True))
    return 0


def _validate(context: CommandContext) -> int:
    report = validate_work_state(context.work, context.project)
    if bool(getattr(context.arguments, "json", False)):
        print(json.dumps(_diagnostic_json(report), indent=2, sort_keys=True))
    else:
        print(report.render())
    return 0 if report.valid else 10


def _status(context: CommandContext) -> int:
    value = _status_value(context.work, context.project)
    if bool(getattr(context.arguments, "json", False)):
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(f"OK WORK_STATE_VALID revision={value['revision']}")
        print(f"focus_item={value['focus_item'] or 'none'} focus_attempt={value['focus_attempt'] or 'none'}")
        print(f"next_action={value['next_action']} inbox={value['inbox_count']}")
    return 0


def _actions(context: CommandContext) -> int:
    role = getattr(context.arguments, "role", None)
    if not isinstance(role, str):
        raise ActionError("ROLE_INVALID", "A role is required.")
    available = actions_for(context.work, context.project, role)
    if bool(getattr(context.arguments, "json", False)):
        print(json.dumps({"actions": [action.as_dict() for action in available]}, indent=2, sort_keys=True))
    elif not available:
        print("OK NO_ACTIONS_AVAILABLE")
    else:
        for action in available:
            print(f"{action.action_id}\t{action.label}")
    return 0


def _initialize(context: CommandContext) -> int:
    task_id = getattr(context.arguments, "coordinator_task_id", None)
    host_id = getattr(context.arguments, "host_id", None)
    if not isinstance(task_id, str) or not isinstance(host_id, str):
        raise RegistrationError("COORDINATOR_IDENTITY_INVALID", "Coordinator task and host identities are required.")
    initialized = initialize_work_state(context.project, task_id, host_id, context.work)
    print(f"OK WORK_STATE_INITIALIZED {initialized}")
    return 0


def _proposal(context: CommandContext) -> int:
    path = getattr(context.arguments, "file", None)
    if not isinstance(path, Path):
        raise ProposalError("PROPOSAL_INVALID", "A proposal file is required.")
    value = read_json_object(path, code="PROPOSAL_INVALID", subject="proposal")
    created = create_proposal(context.work, context.project, value)
    print(f"OK PROPOSAL_CREATED {created}")
    return 0


def _transition(context: CommandContext) -> int:
    payload_path = getattr(context.arguments, "payload", None)
    if not isinstance(payload_path, Path):
        raise TransitionError("TRANSITION_INPUT_INVALID", "A transition payload file is required.")
    action = _action_from_arguments(context.arguments)
    payload = read_json_object(payload_path, code="TRANSITION_INPUT_INVALID", subject="transition payload")
    apply_action(context.work, context.project, action, payload)
    print(f"OK TRANSITION_APPLIED {action.action_id}")
    return 0


COMMANDS: dict[str, CommandHandler] = {
    "root": _root,
    "validate": _validate,
    "status": _status,
    "actions": _actions,
    "init": _initialize,
    "proposal": _proposal,
    "transition": _transition,
}


def _dispatch(arguments: argparse.Namespace) -> int:
    command = getattr(arguments, "command", None)
    if not isinstance(command, str) or command not in COMMANDS:
        raise AssertionError(f"unhandled command {command}")
    project, work = _roots(arguments)
    return COMMANDS[command](CommandContext(arguments, project, work))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return _dispatch(arguments)
    except (RootError, OSError, JsonObjectError) as error:
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
