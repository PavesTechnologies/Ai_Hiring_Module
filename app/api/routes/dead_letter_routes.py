from fastapi import APIRouter, Depends, Path, Security, status

from app.dependencies.dead_letter import get_dead_letter_cleanup_service
from app.enums.constants import UserRole
from app.middleware.rbac import TokenUser, require_roles
from app.schemas.response import APIResponse
from app.services.document_processing.dead_letter_cleanup_service import DeadLetterCleanupService

router = APIRouter(
    prefix="/dead-letter-queue",
    tags=["Dead Letter Queue"],
)


@router.delete(
    "/{task_id}",
    response_model=APIResponse[None],
    status_code=status.HTTP_200_OK,
)
def purge_dead_letter_task(
    task_id: str = Path(...),
    service: DeadLetterCleanupService = Depends(get_dead_letter_cleanup_service),
    user: TokenUser = Security(require_roles(UserRole.HR_ADMIN)),
):
    """
    Permanently deletes a failed task's retry-tracking trail
    (stage-execution history, checkpoint, and failure-log rows), plus
    whichever of dead_letter_queue / celery_task_log actually applies:

    - Dead-lettered task (has a dead_letter_queue row): that row is
      deleted, and for JD tasks only, the uploaded document that was never
      persisted. celery_task_log is left untouched, so it still shows in
      upload history as FAILURE.
    - Any other failed task (celery_task_log.status == FAILURE with no
      dead_letter_queue row - e.g. a duplicate-JD rejection, or a failure
      that never went through the retry/dead-letter path): celery_task_log
      itself is deleted too, since nothing else can ever clean it up.

    A task that isn't in a failed state (QUEUED/RUNNING/SUCCESS/RETRY/DEAD)
    is refused (409). No undo. Resume/candidate business records are never
    touched either way.
    """
    service.purge(task_id)
    return APIResponse.ok(message=f"Failed task '{task_id}' and its records permanently deleted.")
