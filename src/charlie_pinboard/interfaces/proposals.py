import msgspec

from charlie_pinboard.domain.errors import DecisionFailureCode
from charlie_pinboard.interfaces.errors import ProposalError
from charlie_pinboard.interfaces.proposal_models import Proposal


def parse_proposal(data: bytes | str) -> Proposal:
    try:
        return msgspec.json.decode(data, type=Proposal)
    except msgspec.DecodeError as error:
        raise ProposalError(DecisionFailureCode.PROPOSAL_INVALID, f"Cannot decode proposal JSON: {error}") from error
