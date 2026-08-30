from enum import Enum

from pinboard.domain.errors import DecisionFailureCode


class ActionQueryError(RuntimeError):
    code: DecisionFailureCode

    def __init__(self, code: DecisionFailureCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class QueryErrorCode(Enum):
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    PARALLEL_SELECTION_INVALID = "PARALLEL_SELECTION_INVALID"
    PARALLEL_TIME_INVALID = "PARALLEL_TIME_INVALID"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


class QueryError(RuntimeError):
    code: QueryErrorCode

    def __init__(self, code: QueryErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class DispatchErrorCode(Enum):
    DISPATCH_ACTION_INVALID = "DISPATCH_ACTION_INVALID"
    DISPATCH_ACTION_UNAVAILABLE = "DISPATCH_ACTION_UNAVAILABLE"
    DISPATCH_ATTEMPT_NOT_ACTIVE = "DISPATCH_ATTEMPT_NOT_ACTIVE"
    DISPATCH_AUTHORITY_STALE = "DISPATCH_AUTHORITY_STALE"
    DISPATCH_AUTHORITY_UNREADABLE = "DISPATCH_AUTHORITY_UNREADABLE"
    DISPATCH_BRANCH_MISMATCH = "DISPATCH_BRANCH_MISMATCH"
    DISPATCH_BRIEF_INVALID = "DISPATCH_BRIEF_INVALID"
    DISPATCH_BRIEF_MISSING = "DISPATCH_BRIEF_MISSING"
    DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID = "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID"
    DISPATCH_BRIEF_REVIEW_COLLISION = "DISPATCH_BRIEF_REVIEW_COLLISION"
    DISPATCH_BRIEF_REVIEW_INVALID = "DISPATCH_BRIEF_REVIEW_INVALID"
    DISPATCH_BRIEF_REVIEW_MISSING = "DISPATCH_BRIEF_REVIEW_MISSING"
    DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT = "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT"
    DISPATCH_BRIEF_REVIEW_NOT_READY = "DISPATCH_BRIEF_REVIEW_NOT_READY"
    DISPATCH_BRIEF_REVIEW_STALE = "DISPATCH_BRIEF_REVIEW_STALE"
    DISPATCH_CHECKOUT_MISSING = "DISPATCH_CHECKOUT_MISSING"
    DISPATCH_CHECKOUT_MISMATCH = "DISPATCH_CHECKOUT_MISMATCH"
    DISPATCH_CHECKPOINT_MISSING = "DISPATCH_CHECKPOINT_MISSING"
    DISPATCH_ENVIRONMENT_INVALID = "DISPATCH_ENVIRONMENT_INVALID"
    DISPATCH_ENVIRONMENT_UNREADABLE = "DISPATCH_ENVIRONMENT_UNREADABLE"
    DISPATCH_PROMPT_NOT_CANONICAL = "DISPATCH_PROMPT_NOT_CANONICAL"
    DISPATCH_PROMPT_UNREADABLE = "DISPATCH_PROMPT_UNREADABLE"
    STALE_ACTION = "STALE_ACTION"


type DispatchFailureCode = DispatchErrorCode | DecisionFailureCode


class DispatchError(RuntimeError):
    code: DispatchFailureCode

    def __init__(self, code: DispatchFailureCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class PortableCopyErrorCode(Enum):
    PORTABLE_COPY_DESTINATION_EXISTS = "PORTABLE_COPY_DESTINATION_EXISTS"
    PORTABLE_COPY_DESTINATION_INVALID = "PORTABLE_COPY_DESTINATION_INVALID"
    PORTABLE_COPY_SOURCE_NOT_QUIESCENT = "PORTABLE_COPY_SOURCE_NOT_QUIESCENT"
    STORAGE_INVARIANT_VIOLATION = "STORAGE_INVARIANT_VIOLATION"
    STORAGE_IO_ERROR = "STORAGE_IO_ERROR"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


class PortableCopyError(RuntimeError):
    code: PortableCopyErrorCode

    def __init__(self, code: PortableCopyErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class MutationContractErrorCode(Enum):
    CHECKPOINT_ARTIFACTS_INVALID = "MUTATION_CHECKPOINT_ARTIFACTS_INVALID"
    RECEIPT_MISMATCH = "MUTATION_RECEIPT_MISMATCH"


class MutationContractError(ValueError):
    code: MutationContractErrorCode

    def __init__(self, code: MutationContractErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")
