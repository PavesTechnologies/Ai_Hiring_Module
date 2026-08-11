class InvalidPipelineTransitionException(Exception):
    """
    Raised when PipelineTransitionService.transition_stage is asked to move
    a campaign_candidate between two stages that have no matching row in
    allowed_transitions — the move is rejected outright, nothing is written.

    Message carries a fixed "INVALID_TRANSITION:" prefix (E02) so a client
    can reliably split on ':' even though there's no structured error-code
    field anywhere in this codebase - same convention applied to every
    pipeline-transition exception in this file.
    """

    def __init__(self, from_stage: str, to_stage: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        message = f"INVALID_TRANSITION: No transition exists from {from_stage} to {to_stage}."
        super().__init__(message)


class PipelineTransitionReasonRequiredException(Exception):
    """
    Raised when the matching allowed_transitions row has requires_reason=True
    but no reason was supplied.
    """

    def __init__(self, from_stage: str, to_stage: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        message = f"REASON_REQUIRED: A reason is required to move from {from_stage} to {to_stage}."
        super().__init__(message)


class ForbiddenPipelineRoleException(Exception):
    """
    E02: raised when the actor's role is not in allowed_transitions.
    allowed_roles for the requested (from_stage, to_stage).
    """

    def __init__(self, from_stage: str, to_stage: str, role: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        self.role = role
        message = f"FORBIDDEN_ROLE: {role} is not permitted to perform this transition from {from_stage} to {to_stage}."
        super().__init__(message)


class PipelineStageConflictException(Exception):
    """
    E02: raised when the candidate's live pipeline_stage no longer matches
    the expected from_stage at write time - lost a race to a concurrent
    transition.
    """

    def __init__(self, expected_stage: str):
        self.expected_stage = expected_stage
        message = f"STAGE_CONFLICT: Candidate is no longer at {expected_stage} — a concurrent update occurred."
        super().__init__(message)
