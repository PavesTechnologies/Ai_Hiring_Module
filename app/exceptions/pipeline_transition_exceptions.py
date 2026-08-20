from app.exceptions.campaign_exceptions import CampaignException


class InvalidPipelineTransitionException(CampaignException):
    """
    Raised when PipelineTransitionService.transition_stage is asked to move
    a campaign_candidate between two stages that have no matching row in
    allowed_transitions — the move is rejected outright, nothing is written.

    Message carries a fixed "INVALID_TRANSITION:" prefix (E02) so a client
    can reliably split on ':' even though there's no structured error-code
    field anywhere in this codebase - same convention applied to every
    pipeline-transition exception in this file. Subclasses CampaignException
    (status_code=409: the request is well-formed, it's the candidate's
    current stage that makes it invalid) so the existing
    campaign_exception_handler turns this into a proper 4xx instead of an
    unhandled 500.
    """

    def __init__(self, from_stage: str, to_stage: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        message = f"INVALID_TRANSITION: No transition exists from {from_stage} to {to_stage}."
        super().__init__(message, status_code=409)


class PipelineTransitionReasonRequiredException(CampaignException):
    """
    Raised when the matching allowed_transitions row has requires_reason=True
    but no reason was supplied.
    """

    def __init__(self, from_stage: str, to_stage: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        message = f"REASON_REQUIRED: A reason is required to move from {from_stage} to {to_stage}."
        super().__init__(message, status_code=422)


class ForbiddenPipelineRoleException(CampaignException):
    """
    E02: raised when none of the actor's roles are in allowed_transitions.
    allowed_roles for the requested (from_stage, to_stage). Takes the
    actor's full role list (not a single role) so the message shows
    everything the caller held, not one possibly-misleading role.
    """

    def __init__(self, from_stage: str, to_stage: str, roles: list[str]):
        self.from_stage = from_stage
        self.to_stage = to_stage
        self.roles = roles
        message = (
            f"FORBIDDEN_ROLE: none of {roles} is permitted to perform this "
            f"transition from {from_stage} to {to_stage}."
        )
        super().__init__(message, status_code=403)


class PipelineStageConflictException(CampaignException):
    """
    E02: raised when the candidate's live pipeline_stage no longer matches
    the expected from_stage at write time - lost a race to a concurrent
    transition.
    """

    def __init__(self, expected_stage: str):
        self.expected_stage = expected_stage
        message = f"STAGE_CONFLICT: Candidate is no longer at {expected_stage} — a concurrent update occurred."
        super().__init__(message, status_code=409)
