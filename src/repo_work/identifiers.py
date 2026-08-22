from typing import NewType

ActionId = NewType("ActionId", str)
AttemptId = NewType("AttemptId", str)
HostId = NewType("HostId", str)
ItemId = NewType("ItemId", str)
LeaseId = NewType("LeaseId", str)
LedgerId = NewType("LedgerId", str)
PlanningImpactId = NewType("PlanningImpactId", str)
ProposalId = NewType("ProposalId", str)
ReservationId = NewType("ReservationId", str)
ResourceId = NewType("ResourceId", str)
ResourceInstanceId = NewType("ResourceInstanceId", str)
TaskId = NewType("TaskId", str)

type SubjectId = ItemId | AttemptId | ProposalId | LedgerId
