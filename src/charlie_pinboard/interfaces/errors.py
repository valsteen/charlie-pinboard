class InterfaceError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ActionError(InterfaceError):
    pass


class LeaseError(InterfaceError):
    pass


class ProposalError(InterfaceError):
    pass


class TransitionError(InterfaceError):
    pass
