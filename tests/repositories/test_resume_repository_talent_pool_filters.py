from unittest.mock import MagicMock

from app.repositories.resume_repository import ResumeRepository

"""GET /talentpoolfilters - ResumeRepository distinct-value queries backing
the Talent Pool Normal Search UI's location/designation/education filter
options. Each query is pushed to Postgres (GROUP BY / DISTINCT), never
computed by loading every resume into Python."""


def _make_repo(rows=None):
    db = MagicMock()
    db.execute.return_value.all.return_value = rows or []
    return ResumeRepository(db), db


def test_get_distinct_locations_issues_one_query():
    repo, db = _make_repo()

    repo.get_distinct_locations()

    db.execute.assert_called_once()


def test_get_distinct_locations_returns_value_count_pairs():
    row = MagicMock(value="Hyderabad", cnt=3)
    repo, db = _make_repo([row])

    result = repo.get_distinct_locations()

    assert result == [("Hyderabad", 3)]


def test_get_distinct_designations_issues_one_query():
    repo, db = _make_repo()

    repo.get_distinct_designations()

    db.execute.assert_called_once()


def test_get_distinct_designations_returns_value_count_pairs():
    row = MagicMock(value="Software Engineer", cnt=5)
    repo, db = _make_repo([row])

    result = repo.get_distinct_designations()

    assert result == [("Software Engineer", 5)]


def test_get_distinct_education_degree_levels_issues_one_query():
    repo, db = _make_repo()

    repo.get_distinct_education_degree_levels()

    db.execute.assert_called_once()


def test_get_distinct_education_degree_levels_returns_values():
    rows = [MagicMock(value="BACHELOR"), MagicMock(value="MASTER")]
    repo, db = _make_repo(rows)

    result = repo.get_distinct_education_degree_levels()

    assert result == ["BACHELOR", "MASTER"]


def test_get_distinct_education_fields_issues_one_query():
    repo, db = _make_repo()

    repo.get_distinct_education_fields()

    db.execute.assert_called_once()


def test_get_distinct_education_fields_returns_values():
    rows = [MagicMock(value="COMPUTER_SCIENCE")]
    repo, db = _make_repo(rows)

    result = repo.get_distinct_education_fields()

    assert result == ["COMPUTER_SCIENCE"]
