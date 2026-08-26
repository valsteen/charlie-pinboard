from enum import Enum


class CommandErrorCode(Enum):
    ACTION_ID_INVALID = "ACTION_ID_INVALID"
    ACTION_NOT_AVAILABLE = "ACTION_NOT_AVAILABLE"
    ATTEMPT_AUTHORITY_REQUIRED = "ATTEMPT_AUTHORITY_REQUIRED"
    ATTEMPT_LEASE_EXPIRED = "ATTEMPT_LEASE_EXPIRED"
    ATTEMPT_LEASE_REQUIRED = "ATTEMPT_LEASE_REQUIRED"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    COORDINATION_IDENTITY_REQUIRED = "COORDINATION_IDENTITY_REQUIRED"
    COORDINATION_LEASE_BUSY = "COORDINATION_LEASE_BUSY"
    COORDINATION_LEASE_REQUIRED = "COORDINATION_LEASE_REQUIRED"
    DEPENDENCY_NOT_SATISFIED = "DEPENDENCY_NOT_SATISFIED"
    HISTORY_RECORD_EXISTS = "HISTORY_RECORD_EXISTS"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    ITEM_ALREADY_EXISTS = "ITEM_ALREADY_EXISTS"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    ITEM_SCOPE_INVALID = "ITEM_SCOPE_INVALID"
    ITEM_SCOPE_STALE = "ITEM_SCOPE_STALE"
    LEASE_FENCED = "LEASE_FENCED"
    LIVE_DEPENDENTS = "LIVE_DEPENDENTS"
    PROPOSAL_ALREADY_EXISTS = "PROPOSAL_ALREADY_EXISTS"
    PROPOSAL_IDENTITY_INVALID = "PROPOSAL_IDENTITY_INVALID"
    PROPOSAL_INVALID = "PROPOSAL_INVALID"
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    STALE_ACTION = "STALE_ACTION"
    TRANSITION_INPUT_INVALID = "TRANSITION_INPUT_INVALID"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


class CommandError(RuntimeError):
    code: CommandErrorCode

    def __init__(self, code: CommandErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class ProposalErrorCode(Enum):
    ACTION_NOT_AVAILABLE = "ACTION_NOT_AVAILABLE"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    PROPOSAL_ALREADY_EXISTS = "PROPOSAL_ALREADY_EXISTS"
    PROPOSAL_IDENTITY_INVALID = "PROPOSAL_IDENTITY_INVALID"
    PROPOSAL_INVALID = "PROPOSAL_INVALID"


class ProposalError(RuntimeError):
    code: ProposalErrorCode

    def __init__(self, code: ProposalErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class TransitionInputErrorCode(Enum):
    ACTION_NOT_MUTATING = "ACTION_NOT_MUTATING"
    TRANSITION_INPUT_INVALID = "TRANSITION_INPUT_INVALID"


class TransitionInputError(ValueError):
    code: TransitionInputErrorCode

    def __init__(self, code: TransitionInputErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class BriefSourceErrorCode(Enum):
    BATCH_NOT_FOUND = "BRIEF_SOURCE_BATCH_NOT_FOUND"
    LINE_TOO_LARGE = "BRIEF_SOURCE_LINE_TOO_LARGE"
    MANIFEST_INVALID = "BRIEF_SOURCE_MANIFEST_INVALID"
    SELECTOR_INVALID = "BRIEF_SOURCE_SELECTOR_INVALID"
    SELECTOR_OVERLAP = "BRIEF_SOURCE_SELECTOR_OVERLAP"
    SOURCE_NOT_UTF8 = "BRIEF_SOURCE_NOT_UTF8"
    SOURCE_UNREADABLE = "BRIEF_SOURCE_UNREADABLE"


class BriefSourceError(ValueError):
    code: BriefSourceErrorCode
    message: str

    def __init__(self, code: BriefSourceErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class HeaderErrorCode(Enum):
    FIELD_DUPLICATE = "HEADER_FIELD_DUPLICATE"
    FIELD_INVALID = "HEADER_FIELD_INVALID"
    MISSING = "HEADER_MISSING"
    UNTERMINATED = "HEADER_UNTERMINATED"


class HeaderError(ValueError):
    code: HeaderErrorCode

    def __init__(self, code: HeaderErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")
