import msgspec

from pinboard.domain.errors import DecisionFailureCode
from pinboard.interfaces.errors import ProposalError
from pinboard.interfaces.proposal_models import Proposal


def parse_proposal(data: bytes | str) -> Proposal:
    try:
        return msgspec.json.decode(data, type=Proposal)
    except msgspec.DecodeError as error:
        raise ProposalError(DecisionFailureCode.PROPOSAL_INVALID, f"Cannot decode proposal JSON: {error}") from error
