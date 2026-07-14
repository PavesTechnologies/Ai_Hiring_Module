from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.models.skills import SkillOntology, UnknownSkill
from app.repositories.skill_repository import SkillRepository


def test_upsert_unknown_skill_falls_back_to_existing_row_on_integrity_error():
    """
    Simulates two concurrent JDs racing to insert the same never-before-seen
    raw_text: this call's own INSERT loses the race (raw_text is unique) and
    must fall back to the concurrent winner's row rather than propagating
    the IntegrityError and failing this JD's entire persistence.
    """
    db = MagicMock()
    repo = SkillRepository(db)

    winner_row = UnknownSkill(id=uuid4(), raw_text="Kubernetes Operators", frequency=1)

    # 1st query: pre-insert check finds nothing. 2nd query: after losing the
    # race, the concurrent winner's row is now visible.
    db.query.return_value.filter.return_value.first.side_effect = [None, winner_row]
    db.begin_nested.return_value.__enter__.return_value = None
    db.begin_nested.return_value.__exit__.return_value = False  # let the exception propagate out of the `with`
    db.flush.side_effect = [IntegrityError("insert", {}, Exception("duplicate key")), None]

    unknown_skill, was_created = repo.upsert_unknown_skill("Kubernetes Operators")

    assert unknown_skill is winner_row
    assert was_created is False
    assert winner_row.frequency == 2  # bumped once, not duplicated


def test_upsert_unknown_skill_reports_created_on_the_happy_path():
    db = MagicMock()
    repo = SkillRepository(db)
    db.query.return_value.filter.return_value.first.return_value = None
    db.begin_nested.return_value.__enter__.return_value = None
    db.begin_nested.return_value.__exit__.return_value = False

    unknown_skill, was_created = repo.upsert_unknown_skill("Brand New Skill")

    assert was_created is True
    assert unknown_skill.raw_text == "Brand New Skill"


def test_upsert_unknown_skill_reports_not_created_on_a_plain_frequency_bump():
    db = MagicMock()
    repo = SkillRepository(db)
    existing_row = UnknownSkill(id=uuid4(), raw_text="Docker Compose", frequency=1)
    db.query.return_value.filter.return_value.first.return_value = existing_row

    unknown_skill, was_created = repo.upsert_unknown_skill("Docker Compose")

    assert was_created is False
    assert unknown_skill.frequency == 2


def test_find_skill_by_name_or_alias_is_case_insensitive_for_aliases():
    """
    Regression guard: the canonical-name branch already lowercased, but the
    alias branch compared raw text — a case-variant of an existing alias
    (e.g. 'reactjs' vs the stored alias 'ReactJS') must still be found.
    """
    db = MagicMock()
    repo = SkillRepository(db)
    react_skill = SkillOntology(id=uuid4(), canonical_name="React", aliases=["ReactJS"])
    db.query.return_value.all.return_value = [react_skill]

    found = repo.find_skill_by_name_or_alias("reactjs")

    assert found is react_skill


def test_find_skill_by_name_or_alias_is_case_insensitive_for_canonical_name():
    db = MagicMock()
    repo = SkillRepository(db)
    python_skill = SkillOntology(id=uuid4(), canonical_name="Python", aliases=[])
    db.query.return_value.all.return_value = [python_skill]

    found = repo.find_skill_by_name_or_alias("PYTHON")

    assert found is python_skill


def test_find_skill_by_name_or_alias_returns_none_when_no_match():
    db = MagicMock()
    repo = SkillRepository(db)
    db.query.return_value.all.return_value = [
        SkillOntology(id=uuid4(), canonical_name="Go", aliases=["Golang"])
    ]

    assert repo.find_skill_by_name_or_alias("Rust") is None


def test_acquire_alias_lock_issues_advisory_lock_keyed_by_lowered_alias():
    db = MagicMock()
    repo = SkillRepository(db)

    repo.acquire_alias_lock("ReactJS")

    db.execute.assert_called_once()
    (_, params) = db.execute.call_args.args
    assert params == {"key": "reactjs"}
