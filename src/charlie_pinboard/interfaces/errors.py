from enum import Enum

from charlie_pinboard.domain.errors import DecisionFailureCode


class CommandErrorCode(Enum):
    ACTION_ID_INVALID = "ACTION_ID_INVALID"
    STALE_ACTION = "STALE_ACTION"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


type CommandFailureCode = CommandErrorCode | DecisionFailureCode


class CommandError(RuntimeError):
    code: CommandFailureCode

    def __init__(self, code: CommandFailureCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class ProposalError(RuntimeError):
    code: DecisionFailureCode

    def __init__(self, code: DecisionFailureCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class TransitionInputError(ValueError):
    code: DecisionFailureCode

    def __init__(self, code: DecisionFailureCode, message: str) -> None:
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


class WorkBriefErrorCode(Enum):
    BRIEF_INVALID = "WORK_BRIEF_INVALID"
    BRIEF_NOT_CANONICAL = "WORK_BRIEF_NOT_CANONICAL"
    REVIEW_INVALID = "WORK_BRIEF_REVIEW_INVALID"
    REVIEW_NOT_CANONICAL = "WORK_BRIEF_REVIEW_NOT_CANONICAL"
    REVIEW_NOT_INDEPENDENT = "WORK_BRIEF_REVIEW_NOT_INDEPENDENT"
    REVIEW_NOT_READY = "WORK_BRIEF_REVIEW_NOT_READY"
    REVIEW_STALE = "WORK_BRIEF_REVIEW_STALE"


class WorkBriefError(ValueError):
    code: WorkBriefErrorCode
    message: str

    def __init__(self, code: WorkBriefErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")
