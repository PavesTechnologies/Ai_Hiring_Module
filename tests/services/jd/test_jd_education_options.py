from types import SimpleNamespace
from unittest.mock import MagicMock

from app.repositories.jd_repository import JDRepository
from app.services.jd.jd_service import JDService


def test_service_returns_distinct_degrees_and_fields_from_repository():
    repository = MagicMock()
    repository.get_distinct_education_values.side_effect = lambda key, search, limit: {
        "degree": ["Bachelor's degree", "B.Tech"],
        "field": ["Computer Science"],
    }[key]
    service = JDService(repository, MagicMock(), MagicMock(), MagicMock(), MagicMock())

    result = service.get_education_options(search="b", limit=20)

    assert result.degrees == ["Bachelor's degree", "B.Tech"]
    assert result.fields == ["Computer Science"]
    repository.get_distinct_education_values.assert_any_call("degree", "b", 20)
    repository.get_distinct_education_values.assert_any_call("field", "b", 20)


def test_repository_escapes_like_wildcards_and_passes_limit():
    db = MagicMock()
    db.execute.return_value.all.return_value = [SimpleNamespace(value="B.Tech")]

    values = JDRepository(db).get_distinct_education_values("degree", " 100%_x ", 5)

    assert values == ["B.Tech"]
    params = db.execute.call_args.args[1]
    assert params == {"pattern": "%100\\%\\_x%", "limit": 5}


def test_repository_without_search_sends_null_pattern():
    db = MagicMock()
    db.execute.return_value.all.return_value = []

    JDRepository(db).get_distinct_education_values("field", None, 50)

    assert db.execute.call_args.args[1] == {"pattern": None, "limit": 50}
