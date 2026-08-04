from unittest.mock import patch
from uuid import uuid4

import pytest

from app.services.embedding_queue_service import (
    EmbeddingQueueService,
    JDEmbeddingQueueError,
)

TASKS_MODULE = "app.services.embedding_queue_service"


def test_queue_jd_embedding_dispatches_with_generated_task_id():
    jd_id = uuid4()
    with patch(f"{TASKS_MODULE}.generate_jd_embedding") as task_mock:
        returned_task_id = EmbeddingQueueService().queue_jd_embedding(jd_id)

        task_mock.apply_async.assert_called_once()
        call_kwargs = task_mock.apply_async.call_args.kwargs
        assert call_kwargs["kwargs"]["jd_id"] == str(jd_id)
        assert call_kwargs["kwargs"]["force_regenerate"] is False
        assert call_kwargs["kwargs"]["task_id"] == str(returned_task_id)
        assert call_kwargs["task_id"] == str(returned_task_id)


def test_queue_jd_embedding_passes_force_regenerate_through():
    jd_id = uuid4()
    with patch(f"{TASKS_MODULE}.generate_jd_embedding") as task_mock:
        EmbeddingQueueService().queue_jd_embedding(jd_id, force_regenerate=True)

        call_kwargs = task_mock.apply_async.call_args.kwargs
        assert call_kwargs["kwargs"]["force_regenerate"] is True


def test_queue_jd_embedding_raises_jd_embedding_queue_error_on_apply_async_failure():
    jd_id = uuid4()
    with patch(f"{TASKS_MODULE}.generate_jd_embedding") as task_mock:
        task_mock.apply_async.side_effect = Exception("broker unreachable")

        with pytest.raises(JDEmbeddingQueueError) as exc_info:
            EmbeddingQueueService().queue_jd_embedding(jd_id)

        assert exc_info.value.jd_id == jd_id
