import unittest
from dataclasses import replace
from datetime import timedelta

from pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from pinboard.domain import authority_models
from pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_coordination_authority,
)
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import HostId, LeaseId, TaskId
from tests.support import SQLITE_NOW, complete_sqlite_state


class AuthorityDecisionTest(unittest.TestCase):
    def assert_failure(self, value: object) -> None:
        self.assertIsInstance(value, DecisionFailure)

    def test_inactive_attempt_proof_requires_inactive_retained_authority(self) -> None:
        state = complete_sqlite_state()
        attempt = state.lifecycle.attempts[0].attempt_id
        self.assertIsInstance(
            project_inactive_attempt_authority(state, attempt, SQLITE_NOW + timedelta(seconds=1)),
            DecisionFailure,
        )
        recovered = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, state=authority_models.AttemptLeaseStatus.RELEASED)
                    for value in state.authority.attempt_leases
                ),
            ),
        )
        proof = project_inactive_attempt_authority(recovered, attempt, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(proof, DecisionFailure)
        expired = replace(
            recovered,
            authority=replace(
                recovered.authority,
                attempt_leases=tuple(
                    replace(value, state=authority_models.AttemptLeaseStatus.ACTIVE, expires_at=SQLITE_NOW)
                    for value in recovered.authority.attempt_leases
                ),
            ),
        )
        self.assertNotIsInstance(
            project_inactive_attempt_authority(expired, attempt, SQLITE_NOW + timedelta(seconds=1)),
            DecisionFailure,
        )

    def test_coordination_authority_lifecycle_is_closed_and_fenced(self) -> None:
        acquired = decide_coordination_authority(
            None,
            authority_models.AcquireCoordinationAuthority(
                2,
                TaskId("coordinator-new"),
                HostId("host-a"),
                LeaseId("coord-new"),
                SQLITE_NOW,
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)
        assert not isinstance(acquired, DecisionFailure)
        current = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW).coordination_authority
        assert current is not None
        token = replace(
            current,
            task_id=acquired.after.task_id,
            lease_id=acquired.after.lease_id,
            generation=acquired.after.generation,
            expires_at=acquired.after.expires_at,
        )
        renewed = decide_coordination_authority(
            acquired.after,
            authority_models.RenewCoordinationAuthority(
                token,
                SQLITE_NOW + timedelta(seconds=10),
                SQLITE_NOW + timedelta(minutes=3),
            ),
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        released = decide_coordination_authority(
            acquired.after,
            authority_models.ReleaseCoordinationAuthority(token, SQLITE_NOW + timedelta(seconds=20)),
        )
        self.assertNotIsInstance(released, DecisionFailure)
        revoked = decide_coordination_authority(
            released.after,
            authority_models.RevokeCoordinationAuthority(
                token.lease_id, token.generation, SQLITE_NOW + timedelta(seconds=20)
            ),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        stale = decide_coordination_authority(
            acquired.after,
            authority_models.RenewCoordinationAuthority(
                replace(token, generation=token.generation + 1),
                SQLITE_NOW + timedelta(seconds=10),
                SQLITE_NOW + timedelta(minutes=3),
            ),
        )
        self.assertIsInstance(stale, DecisionFailure)

    def test_coordination_authority_rejects_busy_missing_expired_and_invalid_operations(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        retained = snapshot.coordination_lease
        token = snapshot.coordination_authority
        assert retained is not None
        assert token is not None
        acquire = authority_models.AcquireCoordinationAuthority(
            retained.host_epoch,
            TaskId("coordinator-next"),
            retained.host_id,
            LeaseId("coord-next"),
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=2),
        )
        self.assert_failure(decide_coordination_authority(None, replace(acquire, expires_at=SQLITE_NOW)))
        self.assert_failure(decide_coordination_authority(retained, acquire))
        self.assert_failure(
            decide_coordination_authority(
                None,
                authority_models.RenewCoordinationAuthority(token, SQLITE_NOW, SQLITE_NOW + timedelta(minutes=2)),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                None,
                authority_models.ReleaseCoordinationAuthority(token, SQLITE_NOW),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                None,
                authority_models.RevokeCoordinationAuthority(token.lease_id, token.generation, SQLITE_NOW),
            )
        )
        expired = replace(retained, expires_at=SQLITE_NOW)
        self.assert_failure(
            decide_coordination_authority(
                expired,
                authority_models.RenewCoordinationAuthority(
                    replace(token, expires_at=SQLITE_NOW), SQLITE_NOW, SQLITE_NOW + timedelta(minutes=2)
                ),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                retained,
                authority_models.RenewCoordinationAuthority(token, SQLITE_NOW, retained.expires_at),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                expired,
                authority_models.ReleaseCoordinationAuthority(replace(token, expires_at=SQLITE_NOW), SQLITE_NOW),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                retained,
                authority_models.RevokeCoordinationAuthority(token.lease_id, token.generation + 1, SQLITE_NOW),
            )
        )

    def test_attempt_authority_lifecycle_covers_transfer_release_and_revocation(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        command = snapshot.command_attempt_authorities[0]
        coordination = snapshot.coordination_authority
        assert coordination is not None
        retained = authority_models.AttemptLeaseAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            command.lease_id,
            command.generation,
            SQLITE_NOW,
            command.expires_at,
            authority_models.AttemptLeaseStatus.ACTIVE,
        )
        initial = decide_attempt_authority(
            None,
            0,
            authority_models.AcquireInitialAttemptAuthority(
                command.host_epoch,
                command.attempt,
                command.item,
                command.task_id,
                command.host_id,
                LeaseId("initial"),
                SQLITE_NOW,
                SQLITE_NOW + timedelta(minutes=1),
            ),
            snapshot.coordination_lease,
            live_attempt=(command.attempt, command.item),
            project_host_epoch=command.host_epoch,
        )
        self.assertNotIsInstance(initial, DecisionFailure)
        released_retained = replace(
            retained,
            state=authority_models.AttemptLeaseStatus.RELEASED,
            expires_at=SQLITE_NOW + timedelta(seconds=1),
        )
        inactive = authority_models.InactiveAttemptAuthority(
            released_retained.host_epoch,
            released_retained.attempt,
            released_retained.item,
            released_retained.task_id,
            released_retained.host_id,
            released_retained.lease_id,
            released_retained.generation,
            released_retained.expires_at,
            authority_models.AttemptLeaseStatus.RELEASED,
        )
        transfer = decide_attempt_authority(
            released_retained,
            command.generation,
            authority_models.TransferAttemptAuthority(
                inactive,
                coordination,
                TaskId("worker-next"),
                HostId("host-a"),
                LeaseId("attempt-next"),
                SQLITE_NOW + timedelta(seconds=1),
                SQLITE_NOW + timedelta(minutes=2),
            ),
            snapshot.coordination_lease,
            transferable_attempt=(command.attempt, command.item),
        )
        self.assertNotIsInstance(transfer, DecisionFailure)
        renewed = decide_attempt_authority(
            retained,
            command.generation,
            authority_models.RenewAttemptAuthority(
                command, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)
            ),
            snapshot.coordination_lease,
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        released = decide_attempt_authority(
            retained,
            command.generation,
            authority_models.ReleaseAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1)),
            snapshot.coordination_lease,
        )
        self.assertNotIsInstance(released, DecisionFailure)
        revoked = decide_attempt_authority(
            retained,
            command.generation,
            authority_models.RevokeAttemptAuthority(
                command.attempt,
                command.lease_id,
                command.generation,
                coordination,
                SQLITE_NOW + timedelta(seconds=1),
            ),
            snapshot.coordination_lease,
        )
        self.assertNotIsInstance(revoked, DecisionFailure)

    def test_attempt_authority_rejects_stale_cross_wired_and_unresolved_changes(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        command = snapshot.command_attempt_authorities[0]
        coordination = snapshot.coordination_authority
        retained_coordination = snapshot.coordination_lease
        assert coordination is not None
        assert retained_coordination is not None
        retained = authority_models.AttemptLeaseAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            command.lease_id,
            command.generation,
            SQLITE_NOW,
            command.expires_at,
            authority_models.AttemptLeaseStatus.ACTIVE,
        )
        initial = authority_models.AcquireInitialAttemptAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            LeaseId("initial-new"),
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=1),
        )
        self.assert_failure(decide_attempt_authority(retained, command.generation, initial, retained_coordination))
        self.assert_failure(
            decide_attempt_authority(None, 0, replace(initial, expires_at=SQLITE_NOW), retained_coordination)
        )
        transfer = authority_models.TransferAttemptAuthority(
            authority_models.InactiveAttemptAuthority(
                retained.host_epoch,
                retained.attempt,
                retained.item,
                retained.task_id,
                retained.host_id,
                retained.lease_id,
                retained.generation,
                retained.expires_at,
                authority_models.AttemptLeaseStatus.EXPIRED,
            ),
            coordination,
            TaskId("worker-next"),
            HostId("host-a"),
            LeaseId("attempt-next"),
            SQLITE_NOW + timedelta(seconds=1),
            SQLITE_NOW + timedelta(minutes=2),
        )
        self.assert_failure(decide_attempt_authority(retained, command.generation, transfer, None))
        active_transfer = decide_attempt_authority(
            retained,
            command.generation,
            transfer,
            retained_coordination,
        )
        self.assertIsInstance(active_transfer, DecisionFailure)
        assert isinstance(active_transfer, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, active_transfer.code)
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                replace(transfer, expires_at=transfer.acquired_at),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                None,
                command.generation,
                authority_models.RenewAttemptAuthority(
                    command, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)
                ),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                replace(retained, state=authority_models.AttemptLeaseStatus.RELEASED),
                command.generation,
                authority_models.RenewAttemptAuthority(
                    command, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)
                ),
                retained_coordination,
            )
        )
        expired = replace(retained, expires_at=SQLITE_NOW)
        self.assert_failure(
            decide_attempt_authority(
                expired,
                command.generation,
                authority_models.RenewAttemptAuthority(
                    replace(command, expires_at=SQLITE_NOW), SQLITE_NOW, SQLITE_NOW + timedelta(minutes=6)
                ),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                authority_models.RenewAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1), command.expires_at),
                retained_coordination,
            )
        )
        revoke = authority_models.RevokeAttemptAuthority(
            command.attempt,
            command.lease_id,
            command.generation,
            coordination,
            SQLITE_NOW + timedelta(seconds=1),
        )
        self.assert_failure(decide_attempt_authority(retained, command.generation, revoke, None))
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                replace(revoke, generation=command.generation + 1),
                retained_coordination,
            )
        )


if __name__ == "__main__":
    unittest.main()
