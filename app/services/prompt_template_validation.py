from typing import Callable, Optional
from uuid import UUID

from app.exception_handler.exceptions import UnprocessableError
from app.models.prompt_template import PromptTemplate, PromptTemplateStatus
from app.repositories.prompt_template_repository import PromptTemplateRepository

_TASK_TYPE_LABELS = {
    "JD_PARSE": "JD Parsing",
    "RESUME_PARSE": "Resume Parsing",
    "AI_EVALUATE": "AI Evaluation",
}


def validate_prompt_template_selection(
    prompt_template_id: Optional[UUID],
    *,
    expected_task_type: str,
    repository: PromptTemplateRepository,
    exception_factory: Callable[[str], Exception] = UnprocessableError,
) -> PromptTemplate:
    """
    Shared by JDService/CampaignService: a selected prompt_template_id must
    reference an existing, ACTIVE prompt template whose task_type matches
    what the calling entity (Job Description -> JD_PARSE, Hiring Campaign ->
    RESUME_PARSE) actually needs.

    exception_factory lets each caller raise its own module's established
    error type with this same message/lookup logic (JDService's generic
    UnprocessableError vs CampaignService's CampaignException) instead of
    forcing one exception class on both.
    """
    if prompt_template_id is None:
        raise exception_factory("Prompt Template is required.")

    prompt = repository.get_by_id(prompt_template_id)
    if not prompt:
        raise exception_factory("Selected Prompt Template does not exist.")

    if prompt.status != PromptTemplateStatus.ACTIVE:
        raise exception_factory("Selected Prompt Template is inactive.")

    if prompt.task_type != expected_task_type:
        label = _TASK_TYPE_LABELS.get(expected_task_type, expected_task_type)
        raise exception_factory(f"Selected Prompt Template is not a {label} prompt.")

    return prompt
