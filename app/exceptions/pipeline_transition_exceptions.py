class InvalidPipelineTransitionException(Exception):
    """
    Raised when PipelineTransitionService.transition_stage is asked to move
    a campaign_candidate between two stages that have no matching row in
    allowed_transitions — the move is rejected outright, nothing is written.
    """

    def __init__(self, from_stage: str, to_stage: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        message = f"Transition from {from_stage} to {to_stage} is not an allowed pipeline transition."
        super().__init__(message)


class PipelineTransitionReasonRequiredException(Exception):
    """
    Raised when the matching allowed_transitions row has requires_reason=True
    but no reason was supplied.
    """

    def __init__(self, from_stage: str, to_stage: str):
        self.from_stage = from_stage
        self.to_stage = to_stage
        message = f"Transition from {from_stage} to {to_stage} requires a reason to be provided."
        super().__init__(message)
