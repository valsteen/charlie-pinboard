import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from charlie_pinboard.domain.decisions import ActionKind, AuthorizationKind, decide
from charlie_pinboard.domain.errors import DecisionError
from charlie_pinboard.domain.identifiers import AttemptId, CandidateId, CheckpointId, ItemId
from charlie_pinboard.domain.model import (
    AcceptCheckpointInput,
    AttemptState,
    LedgerSnapshot,
    ReservationState,
    UseLeaseState,
    WorkItem,
    WorkState,
)
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.interfaces.transition_input import ReasonInput
from charlie_pinboard.interfaces.transitions import TransitionError, apply_action
from charlie_pinboard.legacy.actions import Action, ActionError, actions_for
from charlie_pinboard.legacy.leases import acquire_attempt, acquire_coordination, release_coordination
from charlie_pinboard.legacy.markdown import parse_attempt, parse_current, parse_header, parse_queue
from charlie_pinboard.legacy.migration import migrate_to_v2
from charlie_pinboard.legacy.overview import read_overview
from charlie_pinboard.legacy.parallel import preview_parallel
from charlie_pinboard.legacy.resources import (
    ResourceError,
    claim_resource,
    declare_resource,
    read_resource_claim,
    require_resource,
)
from charlie_pinboard.legacy.transaction_store import CommitFailpoint, FileChange, journal_path_for
from charlie_pinboard.legacy.validate import validate_work_state
from tests.domain_support import (
    action as make_action,
)
from tests.domain_support import (
    attempt_authority as AttemptAuthority,
)
from tests.domain_support import (
    attempt_record as AttemptRecord,
)
from tests.domain_support import (
    replace,
)
from tests.domain_support import (
    resource_reservation as ResourceReservation,
)
from tests.domain_support import (
    resource_use_lease as ResourceUseLease,
)

from .support import JsonObject, create_state


@dataclass(frozen=True, slots=True)
class ReviewFixture:
    project: Path
    work: Path
    root: Path
    submit: Action
    result: bytes
    review: bytes
    unrelated: bytes
    resource_lease_id: str
    resource_generation: int


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _fail_at(selected: int) -> CommitFailpoint:
    def failpoint(boundary: int, _change: FileChange) -> None:
        if boundary == selected:
            raise RuntimeError("interrupted")

    return failpoint


def _proposal_payload(proposal_id: str, relation_kind: str) -> JsonObject:
    return {
        "schema": "repo-work/v1",
        "proposal_id": proposal_id,
        "created_at": "2026-08-22T12:00:00Z",
        "source_task_id": "coordinator",
        "user_label": proposal_id.replace("-", " ").title(),
        "trigger": f"{proposal_id} was discovered at a safe boundary.",
        "evidence": [f"evidence:{proposal_id}"],
        "why_it_matters": "The current objective must retain a natural return path.",
        "relation": {"kind": relation_kind, "item": "reveal-core"},
        "effect": "The finding is durable without changing live scheduling.",
        "unlock": "A coordinator may assess it without losing the current objective.",
        "urgency_evidence": "The active objective can continue until a safe boundary.",
        "freshness_assumptions": ["The current objective remains live."],
    }


def _accept_and_activate_proposal(
    work: Path,
    project: Path,
    proposal_id: str,
    attempt_id: str,
    coordination_lease_id: str,
    coordination_generation: int,
) -> None:
    accept = next(
        candidate
        for candidate in actions_for(
            work,
            project,
            "coordinator",
            lease_id=coordination_lease_id,
            generation=coordination_generation,
        )
        if candidate.action_id == f"accept-proposal:{proposal_id}"
    )
    apply_action(
        work,
        project,
        accept,
        json.dumps({"item": proposal_id, "state": "ready", "next_action": "activate", "depends_on": []}),
    )
    activate = next(
        candidate
        for candidate in actions_for(
            work,
            project,
            "coordinator",
            lease_id=coordination_lease_id,
            generation=coordination_generation,
        )
        if candidate.action_id == f"activate:{proposal_id}"
    )
    apply_action(
        work,
        project,
        activate,
        json.dumps(
            {
                "attempt": attempt_id,
                "branch": f"codex/{proposal_id}",
                "base_revision": "abc123",
                "owner": proposal_id,
            }
        ),
    )


def _review_fixture() -> ReviewFixture:
    project, work = create_state(
        ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
        focus_item="reveal-core",
        focus_attempt="reveal-core-1",
        create_active_attempt=True,
    )
    migrate_to_v2(work, project)
    root = work / "v2"
    coordination = acquire_coordination(work, "setup", "host", 300)
    declare_resource(
        work,
        "capture-rig",
        "Capture rig",
        coordination.lease_id,
        coordination.generation,
        scope="host-local",
    )
    release_coordination(work, coordination.lease_id, coordination.generation)
    attempt = acquire_attempt(work, "reveal-core-1", "worker", "host", 300)
    claim = claim_resource(
        work,
        "capture-rig",
        "reveal-core-1",
        "worker",
        "host",
        300,
        attempt.lease_id,
        attempt.generation,
    )
    item_path = root / "items" / "reveal-core.md"
    item_path.write_text(
        item_path.read_text(encoding="utf-8").replace("resources: —", "resources: capture-rig"),
        encoding="utf-8",
    )
    attempt_path = root / "attempts" / "reveal-core-1"
    result = b"candidate receipt\n"
    review = b"independent rejection\n"
    unrelated = b"keep me\n"
    attempt_path.joinpath("result.md").write_bytes(result)
    attempt_path.joinpath("review.md").write_bytes(review)
    attempt_path.joinpath("notes.txt").write_bytes(unrelated)
    submit = next(
        candidate
        for candidate in actions_for(
            work,
            project,
            "worker",
            lease_id=attempt.lease_id,
            generation=attempt.generation,
        )
        if candidate.action_id == "submit-review:reveal-core-1"
    )
    apply_action(work, project, submit, "{}")
    return ReviewFixture(
        project,
        work,
        root,
        submit,
        result,
        review,
        unrelated,
        claim.lease_id,
        claim.generation,
    )


def _return_action(fixture: ReviewFixture) -> tuple[Action, str, int]:
    coordination = acquire_coordination(fixture.work, "reviewer", "host", 300)
    action = next(
        candidate
        for candidate in actions_for(
            fixture.work,
            fixture.project,
            "coordinator",
            lease_id=coordination.lease_id,
            generation=coordination.generation,
        )
        if candidate.action_id == "return-for-correction:reveal-core-1"
    )
    return action, coordination.lease_id, coordination.generation


class ReviewReturnTest(unittest.TestCase):
    def test_checkpoint_decision_pauses_nonterminal_work_and_fences_only_task_use(self) -> None:
        item = WorkItem(
            ItemId("reveal-core"),
            WorkState.REVIEW,
            None,
            (),
            AttemptId("reveal-core-1"),
            "design",
            "complete",
            "",
        )
        authority = AttemptAuthority("reveal-core-1", "reveal-core", "attempt-lease", 4)
        held = ResourceReservation(
            "capture-rig--host",
            "capture-rig",
            "capture-rig--host",
            "reveal-core-1",
            7,
            ReservationState.ACTIVE,
        )
        use = ResourceUseLease("use-lease", held.reservation_id, "attempt-lease", 4, 7, UseLeaseState.ACTIVE)
        snapshot = LedgerSnapshot(
            "revision",
            11,
            (item,),
            attempts=(AttemptRecord("reveal-core-1", "reveal-core", AttemptState.REVIEW),),
            attempt_authorities=(authority,),
            resource_reservations=(held,),
            resource_use_leases=(use,),
        )
        action = replace(
            make_action(ActionKind.ACCEPT_CHECKPOINT, "reveal-core-1"),
            label="Accept a checkpoint for reveal-core",
            expected_revision="revision",
            coordinator_generation=11,
            authorization=AuthorizationKind.COORDINATION,
        )
        accepted_at = datetime(2026, 8, 22, tzinfo=UTC)

        decision = decide(
            snapshot,
            action,
            AcceptCheckpointInput(
                CheckpointId("design-accepted"),
                CandidateId("sha256:candidate"),
                "review.md accepted this exact candidate",
            ),
            accepted_at,
        )

        self.assertEqual(
            (WorkState.REVIEW, WorkState.PAUSED, AttemptState.REVIEW, AttemptState.PAUSED),
            (
                decision.item_change.before if decision.item_change else None,
                decision.item_change.after if decision.item_change else None,
                decision.attempt_change.before if decision.attempt_change else None,
                decision.attempt_change.after if decision.attempt_change else None,
            ),
        )
        self.assertEqual((), decision.reservation_changes)
        self.assertEqual(
            ((use, replace(use, state=UseLeaseState.REVOKED)),),
            tuple((change.before, change.after) for change in decision.resource_use_lease_changes),
        )
        self.assertEqual(
            replace(authority, lease_id=None, generation=5, resources=()),
            decision.attempt_authority_change.after if decision.attempt_authority_change else None,
        )
        self.assertEqual(
            ("design-accepted", "reveal-core-1", "sha256:candidate", accepted_at),
            (
                decision.checkpoint_acceptance_change.checkpoint,
                decision.checkpoint_acceptance_change.attempt,
                decision.checkpoint_acceptance_change.candidate,
                decision.checkpoint_acceptance_change.accepted_at,
            )
            if decision.checkpoint_acceptance_change
            else None,
        )
        self.assertNotIn(ItemId("reveal-core"), snapshot.history_items)

    def test_decision_fences_attempt_and_resource_authority_as_one_effect(self) -> None:
        item = WorkItem(
            ItemId("reveal-core"),
            WorkState.REVIEW,
            None,
            (),
            AttemptId("reveal-core-1"),
            "design",
            "complete",
            "",
        )
        authority = AttemptAuthority("reveal-core-1", "reveal-core", "attempt-lease", 4)
        held = ResourceReservation(
            "capture-rig--host",
            "capture-rig",
            "capture-rig--host",
            "reveal-core-1",
            7,
            ReservationState.ACTIVE,
        )
        unrelated = replace(held, reservation_id="other--host", attempt="other-1")
        use = ResourceUseLease("use-lease", held.reservation_id, "attempt-lease", 4, 7, UseLeaseState.ACTIVE)
        snapshot = LedgerSnapshot(
            "revision",
            11,
            (item,),
            attempts=(AttemptRecord("reveal-core-1", "reveal-core", AttemptState.REVIEW),),
            attempt_authorities=(authority,),
            resource_reservations=(held, unrelated),
            resource_use_leases=(use,),
        )
        action = replace(
            make_action(ActionKind.RETURN_FOR_CORRECTION, "reveal-core-1"),
            label="Return reveal-core for correction",
            expected_revision="revision",
            coordinator_generation=11,
            authorization=AuthorizationKind.COORDINATION,
        )

        decision = decide(
            snapshot, action, ReasonInput("review.md: authority mismatch"), datetime(2026, 8, 22, tzinfo=UTC)
        )

        self.assertIsNotNone(decision.attempt_authority_change)
        if decision.attempt_authority_change is None:
            self.fail("return decision omitted attempt-authority fencing")
        self.assertEqual(authority, decision.attempt_authority_change.before)
        self.assertEqual(
            replace(authority, lease_id=None, generation=5, resources=()),
            decision.attempt_authority_change.after,
        )
        self.assertEqual(
            ((held, replace(held, generation=8, state=ReservationState.REVOKED)),),
            tuple((change.before, change.after) for change in decision.reservation_changes),
        )
        self.assertEqual(
            ((use, replace(use, generation=8, state=UseLeaseState.REVOKED)),),
            tuple((change.before, change.after) for change in decision.resource_use_lease_changes),
        )
        with self.assertRaisesRegex(DecisionError, "ATTEMPT_AUTHORITY_REQUIRED"):
            decide(
                replace(snapshot, attempt_authorities=()),
                action,
                ReasonInput("review.md: authority mismatch"),
                datetime(2026, 8, 22, tzinfo=UTC),
            )

    def test_review_action_visibility_is_role_and_state_scoped(self) -> None:
        fixture = _review_fixture()
        action, lease_id, generation = _return_action(fixture)
        coordinator_ids = [
            candidate.action_id
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=lease_id,
                generation=generation,
            )
        ]

        self.assertEqual(1, coordinator_ids.count(action.action_id))
        self.assertIn("complete:reveal-core-1", coordinator_ids)
        self.assertIn("accept-checkpoint:reveal-core-1", coordinator_ids)
        self.assertNotIn(
            action.action_id,
            {candidate.action_id for candidate in actions_for(fixture.work, fixture.project, "observer")},
        )
        with self.assertRaisesRegex(ActionError, "ATTEMPT_LEASE_REQUIRED"):
            actions_for(
                fixture.work,
                fixture.project,
                "worker",
                lease_id=fixture.submit.lease_id,
                generation=fixture.submit.coordinator_generation,
            )

        release_coordination(fixture.work, lease_id, generation)
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        self.assertNotIn(
            "return-for-correction:reveal-core-1",
            {candidate.action_id for candidate in actions_for(work, project, "coordinator")},
        )

    def test_cli_return_preserves_evidence_fences_authority_and_allows_resubmission(self) -> None:
        fixture = _review_fixture()
        attempt_path = fixture.root / "attempts" / "reveal-core-1" / "attempt.md"
        before_attempt = parse_attempt(attempt_path)
        payload = Path(tempfile.mkdtemp()) / "return.json"
        payload.write_text(json.dumps({"reason": "review.md: authority mismatch"}), encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
                (
                    "--project-root",
                    str(fixture.project),
                    "--work-root",
                    str(fixture.work),
                    "coordination",
                    "apply",
                    "--task-id",
                    "reviewer",
                    "--host-id",
                    "host",
                    "--action-id",
                    "return-for-correction:reveal-core-1",
                    "--payload",
                    str(payload),
                )
            )

        self.assertEqual(0, result, stderr.getvalue())
        self.assertIn("COORDINATED_TRANSITION", stdout.getvalue())
        item = parse_queue(fixture.root / "queue.md").by_id()["reveal-core"]
        attempt = parse_attempt(attempt_path)
        current = parse_current(fixture.root / "current.md")
        attempt_header = parse_header(attempt_path)
        self.assertEqual((WorkState.ACTIVE, "reacquire-and-continue"), (item.state, item.next_action))
        self.assertIn("review.md: authority mismatch", item.notes)
        overview_item = read_overview(fixture.work, fixture.project).items[0]
        self.assertEqual(item.notes, overview_item.notes)
        self.assertEqual(AttemptState.ACTIVE, attempt.state)
        self.assertEqual(
            ("reveal-core", "reveal-core-1", "reacquire-and-continue"),
            (current.focus_item, current.focus_attempt, current.next_action),
        )
        self.assertEqual(
            (before_attempt.branch, before_attempt.base_revision, before_attempt.provenance),
            (attempt.branch, attempt.base_revision, attempt.provenance),
        )
        self.assertEqual("revoked", attempt_header["lease_status"])
        self.assertGreater(int(str(attempt_header["lease_generation"])), fixture.submit.coordinator_generation)
        self.assertEqual("unclaimed", attempt_header["owner_task_id"])
        directory = attempt_path.parent
        self.assertEqual(fixture.result, directory.joinpath("result.md").read_bytes())
        self.assertEqual(fixture.review, directory.joinpath("review.md").read_bytes())
        self.assertEqual(fixture.unrelated, directory.joinpath("notes.txt").read_bytes())
        claim = read_resource_claim(fixture.root, "capture-rig", "host")
        self.assertEqual("revoked", claim.status.value)
        self.assertGreater(claim.generation, fixture.resource_generation)
        with self.assertRaisesRegex(TransitionError, "LEASE_FENCED|ACTION_NOT_AVAILABLE"):
            apply_action(fixture.work, fixture.project, fixture.submit, "{}")
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            require_resource(
                fixture.root,
                "capture-rig",
                "host",
                fixture.resource_lease_id,
                fixture.resource_generation,
            )

        fresh = acquire_attempt(fixture.work, "reveal-core-1", "corrector", "host", 300)
        fresh_claim = claim_resource(
            fixture.work,
            "capture-rig",
            "reveal-core-1",
            "corrector",
            "host",
            300,
            fresh.lease_id,
            fresh.generation,
        )
        corrected_submit = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "worker",
                lease_id=fresh.lease_id,
                generation=fresh.generation,
            )
            if candidate.action_id == "submit-review:reveal-core-1"
        )
        self.assertEqual(fresh_claim.generation, corrected_submit.resource_claims[0].generation)
        apply_action(fixture.work, fixture.project, corrected_submit, "{}")
        self.assertEqual(WorkState.REVIEW, parse_queue(fixture.root / "queue.md").items[0].state)
        self.assertTrue(validate_work_state(fixture.work, fixture.project).valid)

    def test_checkpoint_lifecycle_archives_resumes_and_completes(self) -> None:  # noqa: PLR0915
        fixture = _review_fixture()
        attempt_path = fixture.root / "attempts" / "reveal-core-1" / "attempt.md"
        before_attempt_header = parse_header(attempt_path)
        before_claim = read_resource_claim(fixture.root, "capture-rig", "host")
        payload_directory = Path(tempfile.mkdtemp())
        payload = payload_directory / "checkpoint.json"
        payload.write_text(
            json.dumps(
                {
                    "checkpoint": "design-accepted",
                    "candidate": "sha256:candidate",
                    "evidence": "review.md accepted this exact candidate",
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
                (
                    "--project-root",
                    str(fixture.project),
                    "--work-root",
                    str(fixture.work),
                    "coordination",
                    "apply",
                    "--task-id",
                    "reviewer",
                    "--host-id",
                    "host",
                    "--action-id",
                    "accept-checkpoint:reveal-core-1",
                    "--payload",
                    str(payload),
                )
            )

        self.assertEqual(0, result, stderr.getvalue())
        self.assertIn("COORDINATED_TRANSITION", stdout.getvalue())
        item = parse_queue(fixture.root / "queue.md").by_id()["reveal-core"]
        attempt = parse_attempt(attempt_path)
        current = parse_current(fixture.root / "current.md")
        attempt_header = parse_header(attempt_path)
        self.assertEqual((WorkState.PAUSED, "resume"), (item.state, item.next_action))
        self.assertEqual(AttemptState.PAUSED, attempt.state)
        self.assertEqual((None, None, "select"), (current.focus_item, current.focus_attempt, current.next_action))
        self.assertFalse((fixture.root / "history" / "items" / "reveal-core.md").exists())
        self.assertEqual("revoked", attempt_header["lease_status"])
        self.assertEqual(
            int(str(before_attempt_header["lease_generation"])) + 1,
            int(str(attempt_header["lease_generation"])),
        )
        directory = attempt_path.parent
        checkpoint = directory / "checkpoints" / "design-accepted"
        self.assertFalse(directory.joinpath("result.md").exists())
        self.assertFalse(directory.joinpath("review.md").exists())
        self.assertEqual(fixture.result, checkpoint.joinpath("result.md").read_bytes())
        self.assertEqual(fixture.review, checkpoint.joinpath("review.md").read_bytes())
        receipt = parse_header(checkpoint / "receipt.md")
        self.assertEqual(
            (
                "work-checkpoint",
                "repo-work/v2",
                "design-accepted",
                "reveal-core-1",
                "sha256:candidate",
                "review.md accepted this exact candidate",
                hashlib.sha256(fixture.result).hexdigest(),
                hashlib.sha256(fixture.review).hexdigest(),
            ),
            (
                receipt["kind"],
                receipt["schema"],
                receipt["checkpoint"],
                receipt["attempt"],
                receipt["candidate"],
                receipt["evidence"],
                receipt["result_sha256"],
                receipt["review_sha256"],
            ),
        )
        retained_claim = read_resource_claim(fixture.root, "capture-rig", "host")
        self.assertEqual(
            (before_claim.attempt_id, before_claim.lease_id, before_claim.generation, "reserved"),
            (
                retained_claim.attempt_id,
                retained_claim.lease_id,
                retained_claim.generation,
                retained_claim.status.value,
            ),
        )
        with self.assertRaisesRegex(TransitionError, "STATE_REVISION_STALE|ACTION_NOT_AVAILABLE|LEASE_FENCED"):
            apply_action(fixture.work, fixture.project, fixture.submit, "{}")
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            require_resource(
                fixture.root,
                "capture-rig",
                "host",
                fixture.resource_lease_id,
                fixture.resource_generation,
            )

        scheduling_paths = (
            fixture.root / "queue.md",
            fixture.root / "current.md",
            attempt_path,
            fixture.root / "leases" / "resources" / "capture-rig--host.md",
        )
        scheduling_before_intake = tuple(path.read_bytes() for path in scheduling_paths)
        for proposal_id, relation_kind in (
            ("safe-prerequisite", "prerequisite"),
            ("later-follow-up", "follow-up"),
        ):
            proposal_path = payload_directory / f"{proposal_id}.json"
            proposal_path.write_text(
                json.dumps(_proposal_payload(proposal_id, relation_kind)),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                persisted = main(
                    (
                        "--project-root",
                        str(fixture.project),
                        "--work-root",
                        str(fixture.work),
                        "proposal",
                        "--file",
                        str(proposal_path),
                    )
                )
            self.assertEqual(0, persisted, stderr.getvalue())
            self.assertIn("PROPOSAL_CREATED", stdout.getvalue())
        self.assertEqual(scheduling_before_intake, tuple(path.read_bytes() for path in scheduling_paths))
        self.assertTrue((fixture.root / "inbox" / "safe-prerequisite.json").is_file())
        self.assertTrue((fixture.root / "inbox" / "later-follow-up.json").is_file())

        terminal_project = Path(tempfile.mkdtemp()) / "project"
        shutil.copytree(fixture.project, terminal_project)
        terminal_work = terminal_project / ".codex" / "work"
        terminal_root = terminal_work / "v2"
        terminal_coordination = acquire_coordination(terminal_work, "terminal-reviewer", "host", 300)
        _accept_and_activate_proposal(
            terminal_work,
            terminal_project,
            "later-follow-up",
            "later-follow-up-1",
            terminal_coordination.lease_id,
            terminal_coordination.generation,
        )
        close_checkpoint_owner = next(
            candidate
            for candidate in actions_for(
                terminal_work,
                terminal_project,
                "coordinator",
                lease_id=terminal_coordination.lease_id,
                generation=terminal_coordination.generation,
            )
            if candidate.action_id == "close:reveal-core"
        )
        apply_action(
            terminal_work,
            terminal_project,
            close_checkpoint_owner,
            '{"outcome":"dropped","reason":"The accepted checkpoint is the terminal outcome."}',
        )
        release_coordination(
            terminal_work,
            terminal_coordination.lease_id,
            terminal_coordination.generation,
        )
        terminal_claim = read_resource_claim(terminal_root, "capture-rig", "host")
        self.assertEqual("released", terminal_claim.status.value)
        follow_up_lease = acquire_attempt(terminal_work, "later-follow-up-1", "follow-up", "host", 300)
        follow_up_claim = claim_resource(
            terminal_work,
            "capture-rig",
            "later-follow-up-1",
            "follow-up",
            "host",
            300,
            follow_up_lease.lease_id,
            follow_up_lease.generation,
        )
        self.assertEqual("later-follow-up-1", follow_up_claim.attempt_id)
        self.assertGreater(follow_up_claim.generation, retained_claim.generation)

        archived = _snapshot(checkpoint)
        attempt_path.write_text(
            attempt_path.read_text(encoding="utf-8") + "\n## Checkpoint 2: complete the remaining accepted outcome\n",
            encoding="utf-8",
        )
        self.assertTrue(validate_work_state(fixture.work, fixture.project).valid)

        completion_project = Path(tempfile.mkdtemp()) / "project"
        shutil.copytree(fixture.project, completion_project)
        completion_work = completion_project / ".codex" / "work"
        completion_root = completion_work / "v2"
        self.assertTrue(validate_work_state(completion_work, completion_project).valid)
        completion_coordination = acquire_coordination(completion_work, "completion-reviewer", "host", 300)
        completion_resume = next(
            candidate
            for candidate in actions_for(
                completion_work,
                completion_project,
                "coordinator",
                lease_id=completion_coordination.lease_id,
                generation=completion_coordination.generation,
            )
            if candidate.action_id == "resume:reveal-core"
        )
        apply_action(completion_work, completion_project, completion_resume, "{}")
        completion = next(
            candidate
            for candidate in actions_for(
                completion_work,
                completion_project,
                "coordinator",
                lease_id=completion_coordination.lease_id,
                generation=completion_coordination.generation,
            )
            if candidate.action_id == "complete:reveal-core-1"
        )
        apply_action(
            completion_work,
            completion_project,
            completion,
            '{"evidence":"The validated checkpoint is the complete outcome."}',
        )
        _accept_and_activate_proposal(
            completion_work,
            completion_project,
            "later-follow-up",
            "later-follow-up-1",
            completion_coordination.lease_id,
            completion_coordination.generation,
        )
        release_coordination(
            completion_work,
            completion_coordination.lease_id,
            completion_coordination.generation,
        )
        completed_claim = read_resource_claim(completion_root, "capture-rig", "host")
        self.assertEqual("released", completed_claim.status.value)
        completion_follow_up_lease = acquire_attempt(
            completion_work,
            "later-follow-up-1",
            "later-follow-up",
            "host",
            300,
        )
        completion_follow_up_claim = claim_resource(
            completion_work,
            "capture-rig",
            "later-follow-up-1",
            "later-follow-up",
            "host",
            300,
            completion_follow_up_lease.lease_id,
            completion_follow_up_lease.generation,
        )
        self.assertEqual("later-follow-up-1", completion_follow_up_claim.attempt_id)
        self.assertGreater(completion_follow_up_claim.generation, retained_claim.generation)

        resume_payload = payload_directory / "resume.json"
        resume_payload.write_text("{}\n", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            resumed = main(
                (
                    "--project-root",
                    str(fixture.project),
                    "--work-root",
                    str(fixture.work),
                    "coordination",
                    "apply",
                    "--task-id",
                    "coordinator",
                    "--host-id",
                    "host",
                    "--action-id",
                    "resume:reveal-core",
                    "--payload",
                    str(resume_payload),
                )
            )
        self.assertEqual(0, resumed, stderr.getvalue())
        self.assertEqual(WorkState.ACTIVE, parse_queue(fixture.root / "queue.md").items[0].state)
        self.assertEqual(_snapshot(checkpoint), archived)
        self.assertFalse((fixture.root / "history" / "items" / "reveal-core.md").exists())

        coordination = acquire_coordination(fixture.work, "coordinator", "host", 300)
        accept_prerequisite = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "accept-proposal:safe-prerequisite"
        )
        apply_action(
            fixture.work,
            fixture.project,
            accept_prerequisite,
            '{"item":"safe-prerequisite","state":"ready","next_action":"activate","depends_on":[]}',
        )
        activate_prerequisite = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "activate:safe-prerequisite"
        )
        apply_action(
            fixture.work,
            fixture.project,
            activate_prerequisite,
            '{"attempt":"safe-prerequisite-1","branch":"codex/safe-prerequisite",'
            '"base_revision":"abc123","owner":"prerequisite"}',
        )
        prerequisite_path = fixture.root / "items" / "safe-prerequisite.md"
        prerequisite_path.write_text(
            prerequisite_path.read_text(encoding="utf-8").replace("resources: —", "resources: capture-rig"),
            encoding="utf-8",
        )
        block_main = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "block:reveal-core-1"
        )
        apply_action(
            fixture.work,
            fixture.project,
            block_main,
            '{"reason":"Complete the admitted prerequisite at this safe boundary","depends_on":["safe-prerequisite"]}',
        )
        preview = preview_parallel(
            fixture.work,
            fixture.project,
            "host",
            selected=("safe-prerequisite",),
        )
        self.assertEqual(
            {"safe-prerequisite": ("resource-busy",)},
            {
                candidate.item_id: tuple(reason.code.value for reason in candidate.reasons)
                for candidate in preview.excluded
            },
        )
        prerequisite_lease = acquire_attempt(fixture.work, "safe-prerequisite-1", "prerequisite", "host", 300)
        with self.assertRaisesRegex(ResourceError, "RESOURCE_BUSY"):
            claim_resource(
                fixture.work,
                "capture-rig",
                "safe-prerequisite-1",
                "prerequisite",
                "host",
                300,
                prerequisite_lease.lease_id,
                prerequisite_lease.generation,
            )
        complete_prerequisite = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "complete:safe-prerequisite-1"
        )
        apply_action(
            fixture.work,
            fixture.project,
            complete_prerequisite,
            '{"evidence":"The genuine prerequisite is complete"}',
        )
        resume_main = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "resume:reveal-core"
        )
        apply_action(fixture.work, fixture.project, resume_main, "{}")
        release_coordination(fixture.work, coordination.lease_id, coordination.generation)
        self.assertEqual(WorkState.ACTIVE, parse_queue(fixture.root / "queue.md").by_id()["reveal-core"].state)
        self.assertTrue((fixture.root / "history" / "items" / "safe-prerequisite.md").is_file())
        self.assertEqual(_snapshot(checkpoint), archived)

        fresh = acquire_attempt(fixture.work, "reveal-core-1", "finisher", "host", 300)
        fresh_claim = claim_resource(
            fixture.work,
            "capture-rig",
            "reveal-core-1",
            "finisher",
            "host",
            300,
            fresh.lease_id,
            fresh.generation,
        )
        self.assertGreater(fresh_claim.generation, retained_claim.generation)
        directory.joinpath("result.md").write_bytes(b"final candidate\n")
        directory.joinpath("review.md").write_bytes(b"final review\n")
        submit = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "worker",
                lease_id=fresh.lease_id,
                generation=fresh.generation,
            )
            if candidate.action_id == "submit-review:reveal-core-1"
        )
        apply_action(fixture.work, fixture.project, submit, "{}")
        coordination = acquire_coordination(fixture.work, "reviewer", "host", 300)
        actions = actions_for(
            fixture.work,
            fixture.project,
            "coordinator",
            lease_id=coordination.lease_id,
            generation=coordination.generation,
        )
        duplicate = next(candidate for candidate in actions if candidate.action_id == "accept-checkpoint:reveal-core-1")
        complete = next(candidate for candidate in actions if candidate.action_id == "complete:reveal-core-1")
        before_duplicate = _snapshot(fixture.root)
        with self.assertRaisesRegex(TransitionError, "CHECKPOINT_ALREADY_EXISTS"):
            apply_action(
                fixture.work,
                fixture.project,
                duplicate,
                json.dumps(
                    {
                        "checkpoint": "design-accepted",
                        "candidate": "sha256:final",
                        "evidence": "final review",
                    }
                ),
            )
        self.assertEqual(before_duplicate, _snapshot(fixture.root))
        apply_action(fixture.work, fixture.project, complete, '{"evidence":"full outcome accepted"}')
        release_coordination(fixture.work, coordination.lease_id, coordination.generation)
        self.assertTrue((fixture.root / "history" / "items" / "reveal-core.md").is_file())
        self.assertEqual(_snapshot(checkpoint), archived)

    def test_checkpoint_missing_evidence_and_interrupted_archive_leave_previous_state_intact(self) -> None:
        fixture = _review_fixture()
        payload = json.dumps(
            {
                "checkpoint": "design-accepted",
                "candidate": "sha256:candidate",
                "evidence": "independent review accepted",
            }
        )
        review_path = fixture.root / "attempts" / "reveal-core-1" / "review.md"
        review = review_path.read_bytes()
        review_path.unlink()
        action, lease_id, generation = _return_action(fixture)
        action = replace(action, kind=ActionKind.ACCEPT_CHECKPOINT, action_id="accept-checkpoint:reveal-core-1")
        before_missing = _snapshot(fixture.root)
        with self.assertRaisesRegex(TransitionError, "CHECKPOINT_EVIDENCE_MISSING"):
            apply_action(fixture.work, fixture.project, action, payload)
        self.assertEqual(before_missing, _snapshot(fixture.root))
        review_path.write_bytes(review)
        release_coordination(fixture.work, lease_id, generation)

        for selected_boundary in range(1, 10):
            with self.subTest(boundary=selected_boundary):
                candidate_fixture = _review_fixture()
                return_action, lease_id, generation = _return_action(candidate_fixture)
                checkpoint_action = replace(
                    return_action,
                    kind=ActionKind.ACCEPT_CHECKPOINT,
                    action_id="accept-checkpoint:reveal-core-1",
                )
                before = _snapshot(candidate_fixture.root)
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    apply_action(
                        candidate_fixture.work,
                        candidate_fixture.project,
                        checkpoint_action,
                        payload,
                        failpoint=_fail_at(selected_boundary),
                    )
                self.assertEqual(before, _snapshot(candidate_fixture.root))
                self.assertFalse(journal_path_for(candidate_fixture.root).exists())
                self.assertTrue(validate_work_state(candidate_fixture.work, candidate_fixture.project).valid)
                release_coordination(candidate_fixture.work, lease_id, generation)

    def test_invalid_and_interrupted_returns_preserve_one_valid_state(self) -> None:
        authority_fixture = _review_fixture()
        authority_action, authority_lease_id, authority_generation = _return_action(authority_fixture)
        before_authority_failures = _snapshot(authority_fixture.root)
        invalid_authorities = (
            replace(authority_action, lease_id="wrong"),
            replace(authority_action, coordinator_generation=authority_generation + 1),
        )
        for invalid in invalid_authorities:
            with (
                self.subTest(authority=(invalid.lease_id, invalid.coordinator_generation)),
                self.assertRaisesRegex(
                    TransitionError,
                    "LEASE_FENCED",
                ),
            ):
                apply_action(
                    authority_fixture.work,
                    authority_fixture.project,
                    invalid,
                    '{"reason":"review.md: correction required"}',
                )
            self.assertEqual(before_authority_failures, _snapshot(authority_fixture.root))
        release_coordination(authority_fixture.work, authority_lease_id, authority_generation)

        for selected_boundary in range(1, 6):
            with self.subTest(boundary=selected_boundary):
                fixture = _review_fixture()
                action, lease_id, generation = _return_action(fixture)
                before = _snapshot(fixture.root)

                with self.assertRaisesRegex(TransitionError, "TRANSITION_INPUT_INVALID"):
                    apply_action(fixture.work, fixture.project, action, "{}")
                self.assertEqual(before, _snapshot(fixture.root))

                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    apply_action(
                        fixture.work,
                        fixture.project,
                        action,
                        '{"reason":"review.md: correction required"}',
                        failpoint=_fail_at(selected_boundary),
                    )
                self.assertEqual(before, _snapshot(fixture.root))
                self.assertFalse(journal_path_for(fixture.root).exists())
                self.assertTrue(validate_work_state(fixture.work, fixture.project).valid)
                release_coordination(fixture.work, lease_id, generation)

        fixture = _review_fixture()
        action, lease_id, generation = _return_action(fixture)
        complete = next(
            candidate
            for candidate in actions_for(
                fixture.work,
                fixture.project,
                "coordinator",
                lease_id=lease_id,
                generation=generation,
            )
            if candidate.action_id == "complete:reveal-core-1"
        )
        apply_action(fixture.work, fixture.project, action, '{"reason":"review.md: correction required"}')
        after = _snapshot(fixture.root)
        for invalid in (action, complete):
            with (
                self.subTest(action=invalid.action_id),
                self.assertRaisesRegex(
                    TransitionError,
                    "STATE_REVISION_STALE|ACTION_NOT_AVAILABLE|LEASE_FENCED",
                ),
            ):
                apply_action(fixture.work, fixture.project, invalid, '{"reason":"repeat"}')
            self.assertEqual(after, _snapshot(fixture.root))
        release_coordination(fixture.work, lease_id, generation)


if __name__ == "__main__":
    unittest.main()
