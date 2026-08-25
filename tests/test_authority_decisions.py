import unittest
from dataclasses import replace
from datetime import timedelta

from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from charlie_pinboard.application.stored_state import AttemptLeaseState
from charlie_pinboard.domain.authority_decisions import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    InactiveAttemptAuthority,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    TransferAttemptAuthority,
    decide_attempt_authority,
    decide_coordination_authority,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import HostId, LeaseId, TaskId
from charlie_pinboard.domain.model import UseLeaseState
from tests.support import SQLITE_NOW, complete_sqlite_state


class AuthorityDecisionTest(unittest.TestCase):
    def assert_failure(self, value: object) -> None:
        self.assertIsInstance(value, DecisionFailure)

    def test_inactive_attempt_proof_requires_recovered_retained_authority(self) -> None:
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
                    replace(value, state=AttemptLeaseState.RELEASED) for value in state.authority.attempt_leases
                ),
            ),
            resources=replace(
                state.resources,
                use_leases=tuple(replace(value, state=UseLeaseState.RELEASED) for value in state.resources.use_leases),
                mutation_intents=(),
            ),
        )
        proof = project_inactive_attempt_authority(recovered, attempt, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(proof, DecisionFailure)
        expired = replace(
            recovered,
            authority=replace(
                recovered.authority,
                attempt_leases=tuple(
                    replace(value, state=AttemptLeaseState.ACTIVE, expires_at=SQLITE_NOW)
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
            AcquireCoordinationAuthority(
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
        current = project_decision_snapshot(complete_sqlite_state()).coordination_authority
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
            RenewCoordinationAuthority(
                token,
                SQLITE_NOW + timedelta(seconds=10),
                SQLITE_NOW + timedelta(minutes=3),
            ),
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        released = decide_coordination_authority(
            acquired.after,
            ReleaseCoordinationAuthority(token, SQLITE_NOW + timedelta(seconds=20)),
        )
        self.assertNotIsInstance(released, DecisionFailure)
        revoked = decide_coordination_authority(
            released.after,
            RevokeCoordinationAuthority(token.lease_id, token.generation, SQLITE_NOW + timedelta(seconds=20)),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        stale = decide_coordination_authority(
            acquired.after,
            RenewCoordinationAuthority(
                replace(token, generation=token.generation + 1),
                SQLITE_NOW + timedelta(seconds=10),
                SQLITE_NOW + timedelta(minutes=3),
            ),
        )
        self.assertIsInstance(stale, DecisionFailure)

    def test_coordination_authority_rejects_busy_missing_expired_and_invalid_operations(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())
        retained = snapshot.coordination_lease
        token = snapshot.coordination_authority
        assert retained is not None
        assert token is not None
        acquire = AcquireCoordinationAuthority(
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
                RenewCoordinationAuthority(token, SQLITE_NOW, SQLITE_NOW + timedelta(minutes=2)),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                None,
                ReleaseCoordinationAuthority(token, SQLITE_NOW),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                None,
                RevokeCoordinationAuthority(token.lease_id, token.generation, SQLITE_NOW),
            )
        )
        expired = replace(retained, expires_at=SQLITE_NOW)
        self.assert_failure(
            decide_coordination_authority(
                expired,
                RenewCoordinationAuthority(
                    replace(token, expires_at=SQLITE_NOW), SQLITE_NOW, SQLITE_NOW + timedelta(minutes=2)
                ),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                retained,
                RenewCoordinationAuthority(token, SQLITE_NOW, retained.expires_at),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                expired,
                ReleaseCoordinationAuthority(replace(token, expires_at=SQLITE_NOW), SQLITE_NOW),
            )
        )
        self.assert_failure(
            decide_coordination_authority(
                retained,
                RevokeCoordinationAuthority(token.lease_id, token.generation + 1, SQLITE_NOW),
            )
        )

    def test_attempt_authority_lifecycle_covers_transfer_release_and_revocation(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())
        command = snapshot.command_attempt_authorities[0]
        coordination = snapshot.coordination_authority
        assert coordination is not None
        retained = AttemptLeaseAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            command.lease_id,
            command.generation,
            SQLITE_NOW,
            command.expires_at,
            AttemptLeaseStatus.ACTIVE,
        )
        initial = decide_attempt_authority(
            None,
            0,
            (),
            AcquireInitialAttemptAuthority(
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
            state=AttemptLeaseStatus.RELEASED,
            expires_at=SQLITE_NOW + timedelta(seconds=1),
        )
        inactive = InactiveAttemptAuthority(
            released_retained.host_epoch,
            released_retained.attempt,
            released_retained.item,
            released_retained.task_id,
            released_retained.host_id,
            released_retained.lease_id,
            released_retained.generation,
            released_retained.expires_at,
            AttemptLeaseStatus.RELEASED,
        )
        transfer = decide_attempt_authority(
            released_retained,
            command.generation,
            (),
            TransferAttemptAuthority(
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
            snapshot.attempt_task_uses,
            RenewAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)),
            snapshot.coordination_lease,
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        released = decide_attempt_authority(
            retained,
            command.generation,
            snapshot.attempt_task_uses,
            ReleaseAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1)),
            snapshot.coordination_lease,
        )
        self.assertNotIsInstance(released, DecisionFailure)
        revoked = decide_attempt_authority(
            retained,
            command.generation,
            snapshot.attempt_task_uses,
            RevokeAttemptAuthority(
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
        snapshot = project_decision_snapshot(complete_sqlite_state())
        command = snapshot.command_attempt_authorities[0]
        coordination = snapshot.coordination_authority
        retained_coordination = snapshot.coordination_lease
        assert coordination is not None
        assert retained_coordination is not None
        retained = AttemptLeaseAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            command.lease_id,
            command.generation,
            SQLITE_NOW,
            command.expires_at,
            AttemptLeaseStatus.ACTIVE,
        )
        initial = AcquireInitialAttemptAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            LeaseId("initial-new"),
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=1),
        )
        self.assert_failure(decide_attempt_authority(retained, command.generation, (), initial, retained_coordination))
        self.assert_failure(
            decide_attempt_authority(None, 0, (), replace(initial, expires_at=SQLITE_NOW), retained_coordination)
        )
        transfer = TransferAttemptAuthority(
            InactiveAttemptAuthority(
                retained.host_epoch,
                retained.attempt,
                retained.item,
                retained.task_id,
                retained.host_id,
                retained.lease_id,
                retained.generation,
                retained.expires_at,
                AttemptLeaseStatus.EXPIRED,
            ),
            coordination,
            TaskId("worker-next"),
            HostId("host-a"),
            LeaseId("attempt-next"),
            SQLITE_NOW + timedelta(seconds=1),
            SQLITE_NOW + timedelta(minutes=2),
        )
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                (),
                transfer,
                retained_coordination,
                (command.attempt,),
            )
        )
        self.assert_failure(decide_attempt_authority(retained, command.generation, (), transfer, None))
        active_transfer = decide_attempt_authority(
            retained,
            command.generation,
            (),
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
                (),
                replace(transfer, expires_at=transfer.acquired_at),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                None,
                command.generation,
                (),
                RenewAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                replace(retained, state=AttemptLeaseStatus.RELEASED),
                command.generation,
                (),
                RenewAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)),
                retained_coordination,
            )
        )
        expired = replace(retained, expires_at=SQLITE_NOW)
        self.assert_failure(
            decide_attempt_authority(
                expired,
                command.generation,
                (),
                RenewAttemptAuthority(
                    replace(command, expires_at=SQLITE_NOW), SQLITE_NOW, SQLITE_NOW + timedelta(minutes=6)
                ),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                (),
                RenewAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1), command.expires_at),
                retained_coordination,
            )
        )
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                (),
                ReleaseAttemptAuthority(command, SQLITE_NOW + timedelta(seconds=1)),
                retained_coordination,
                (command.attempt,),
            )
        )
        revoke = RevokeAttemptAuthority(
            command.attempt,
            command.lease_id,
            command.generation,
            coordination,
            SQLITE_NOW + timedelta(seconds=1),
        )
        self.assert_failure(decide_attempt_authority(retained, command.generation, (), revoke, None))
        self.assert_failure(
            decide_attempt_authority(
                retained,
                command.generation,
                (),
                replace(revoke, generation=command.generation + 1),
                retained_coordination,
            )
        )


if __name__ == "__main__":
    unittest.main()
