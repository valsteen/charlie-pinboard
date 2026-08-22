import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from repo_work.actions import Action, ActionError, actions_for
from repo_work.cli import main
from repo_work.decisions import ActionKind, AuthorizationKind, DecisionError, decide
from repo_work.leases import acquire_attempt, acquire_coordination, release_coordination
from repo_work.markdown import parse_attempt, parse_current, parse_header, parse_queue
from repo_work.migration import migrate_to_v2
from repo_work.model import (
    AttemptAuthority,
    AttemptRecord,
    AttemptState,
    LedgerSnapshot,
    QueueItem,
    ReservationState,
    ResourceReservation,
    ResourceUseLease,
    UseLeaseState,
    WorkState,
)
from repo_work.overview import read_overview
from repo_work.resources import (
    ResourceError,
    claim_resource,
    declare_resource,
    read_resource_claim,
    require_resource,
)
from repo_work.transaction_store import CommitFailpoint, FileChange, journal_path_for
from repo_work.transition import TransitionError, apply_action
from repo_work.transition_input import ReasonInput
from repo_work.validate import validate_work_state

from .support import create_state


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
    def test_decision_fences_attempt_and_resource_authority_as_one_effect(self) -> None:
        item = QueueItem("reveal-core", WorkState.REVIEW, None, (), "reveal-core-1", "design", "complete", "")
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
        action = Action(
            "return-for-correction:reveal-core-1",
            ActionKind.RETURN_FOR_CORRECTION,
            "reveal-core-1",
            "Return reveal-core for correction",
            "revision",
            11,
            authorization=AuthorizationKind.COORDINATION,
        )

        decision = decide(snapshot, action, ReasonInput("review.md: authority mismatch"), datetime(2026, 8, 22, tzinfo=UTC))

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
        self.assertEqual(("reveal-core", "reveal-core-1", "reacquire-and-continue"), (current.focus_item, current.focus_attempt, current.next_action))
        self.assertEqual((before_attempt.branch, before_attempt.base_revision, before_attempt.provenance), (attempt.branch, attempt.base_revision, attempt.provenance))
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

    def test_invalid_and_interrupted_returns_preserve_one_valid_state(self) -> None:
        authority_fixture = _review_fixture()
        authority_action, authority_lease_id, authority_generation = _return_action(authority_fixture)
        before_authority_failures = _snapshot(authority_fixture.root)
        invalid_authorities = (
            replace(authority_action, lease_id="wrong"),
            replace(authority_action, coordinator_generation=authority_generation + 1),
        )
        for invalid in invalid_authorities:
            with self.subTest(authority=(invalid.lease_id, invalid.coordinator_generation)), self.assertRaisesRegex(
                TransitionError,
                "LEASE_FENCED",
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
            with self.subTest(action=invalid.action_id), self.assertRaisesRegex(
                TransitionError,
                "STATE_REVISION_STALE|ACTION_NOT_AVAILABLE|LEASE_FENCED",
            ):
                apply_action(fixture.work, fixture.project, invalid, '{"reason":"repeat"}')
            self.assertEqual(after, _snapshot(fixture.root))
        release_coordination(fixture.work, lease_id, generation)


if __name__ == "__main__":
    unittest.main()
