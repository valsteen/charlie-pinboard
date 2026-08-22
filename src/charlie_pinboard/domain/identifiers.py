from typing import NewType

ActionId = NewType("ActionId", str)
ArtifactRefId = NewType("ArtifactRefId", int)
AttemptId = NewType("AttemptId", str)
CandidateId = NewType("CandidateId", str)
CheckpointId = NewType("CheckpointId", str)
HostId = NewType("HostId", str)
HistoryId = NewType("HistoryId", int)
HistorySubjectId = NewType("HistorySubjectId", str)
ItemId = NewType("ItemId", str)
LeaseId = NewType("LeaseId", str)
LedgerId = NewType("LedgerId", str)
MutationIntentId = NewType("MutationIntentId", str)
PlanningImpactId = NewType("PlanningImpactId", str)
ProposalId = NewType("ProposalId", str)
ReservationId = NewType("ReservationId", str)
ResourceId = NewType("ResourceId", str)
ResourceInstanceId = NewType("ResourceInstanceId", str)
TaskId = NewType("TaskId", str)

type SubjectId = ItemId | AttemptId | ProposalId | LedgerId
