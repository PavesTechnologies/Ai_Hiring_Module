"""A core skill's requirement text: suggested from the JD's wording, saved and versioned in calibration."""
from app.schemas.jd.calibration import JDCalibrationSaveRequest
from app.services.jd.jd_requirement_text import MAX_LENGTH, suggest_requirement_text
from tests.services.jd.test_jd_calibration_service import _request, _setup

SENIOR_JD = """Head of Data & AI Platforms
About the role
We are looking for a senior leader to own our data and AI platforms, feeding analytics and machine learning.
Must-have skills
- Data Engineering (batch and streaming pipelines, lakehouse or warehouse design)
- Machine Learning (taking models from prototype to production)
- MLOps (model deployment, monitoring, retraining, feature stores)
Good to have
- Databricks or Apache Spark at scale
"""


# --- suggestion ------------------------------------------------------------------

def test_the_must_have_bullet_naming_the_skill_is_suggested_without_its_marker():
    assert suggest_requirement_text(SENIOR_JD, "Machine Learning") == (
        "Machine Learning (taking models from prototype to production)")
    assert suggest_requirement_text(SENIOR_JD, "Data Engineering") == (
        "Data Engineering (batch and streaming pipelines, lakehouse or warehouse design)")


def test_a_must_have_bullet_wins_over_an_earlier_mention_and_a_good_to_have_one():
    text = "About the role\nMLOps maturity matters here.\nGood to have\n- MLOps tooling\nRequirements\n- MLOps at scale\n"
    assert suggest_requirement_text(text, "MLOps") == "MLOps at scale"


def test_an_alias_finds_the_line_and_a_short_capitals_alias_matches_only_in_capitals():
    text = "Requirements\n- ML platform ownership\n- html and xml tooling\n"
    assert suggest_requirement_text(text, "Machine Learning", ["ML"]) == "ML platform ownership"
    assert suggest_requirement_text("Requirements\n- an ml-ish thing\n", "Machine Learning", ["ML"]) is None


def test_no_mention_no_suggestion_and_long_lines_are_capped():
    assert suggest_requirement_text(SENIOR_JD, "Cloud Architecture") is None
    assert suggest_requirement_text(None, "Python") is None
    long_line = "Requirements\n- Python " + "x" * 900
    assert len(suggest_requirement_text(long_line, "Python")) == MAX_LENGTH


# --- calibration -----------------------------------------------------------------

def _with_text(request: JDCalibrationSaveRequest, **texts) -> JDCalibrationSaveRequest:
    skills = [s.model_copy(update={"requirement_text": texts.get(str(s.jd_skill_id))}) for s in request.skills]
    return request.model_copy(update={"skills": skills})


def test_the_view_offers_the_jds_line_and_the_saved_text():
    service, repo, _, jd, change, *_ = _setup(
        raw_text="Requirements\n- Change Management for a 40-store retail rollout (adoption, training)\n")
    view = service.get_view(jd.id, can_calibrate=True)
    by_name = {s.name: s for s in view.skills}
    assert by_name["Change Management"].suggested_requirement_text == (
        "Change Management for a 40-store retail rollout (adoption, training)")
    assert by_name["Change Management"].requirement_text is None


def test_save_keeps_the_text_on_core_skills_only_and_versions_the_change():
    service, repo, audit, jd, change, python, aws = _setup()
    request = _with_text(_request(change, python, aws), **{
        str(change.id): "  Lead adoption for the retail rollout  ", str(python.id): "Scripting"})
    service.save(jd.id, request, actor_id="hr-1", actor_role="HR_ADMIN")
    assert change.requirement_text == "Lead adoption for the retail rollout"
    assert python.requirement_text is None                            # supporting: never kept
    assert repo.versions[-1].snapshot["skills"][str(change.id)]["requirement_text"] == (
        "Lead adoption for the retail rollout")

    edited = _with_text(_request(change, python, aws), **{str(change.id): "Lead adoption across 40 stores"})
    result = service.save(jd.id, edited, actor_id="hr-1", actor_role="HR_ADMIN")
    assert result.changed and repo.versions[-1].changes["skills"] == [{
        "skill": "Change Management", "field": "requirement_text",
        "before": "Lead adoption for the retail rollout", "after": "Lead adoption across 40 stores"}]

    cleared = _with_text(_request(change, python, aws), **{str(change.id): "   "})
    service.save(jd.id, cleared, actor_id="hr-1", actor_role="HR_ADMIN")
    assert change.requirement_text is None


def test_a_version_saved_before_the_text_existed_is_not_a_change():
    service, repo, _, jd, change, python, aws = _setup()
    service.save(jd.id, _request(change, python, aws), actor_id="hr-1", actor_role="HR_ADMIN")
    for skill in repo.versions[-1].snapshot["skills"].values():
        skill.pop("requirement_text")                                 # as an older version recorded it
    assert service._changes(repo.versions[-1].snapshot,
                            service._snapshot(jd, {s.id: (s, n) for s, n in repo.skills})) == {}


# --- schema ----------------------------------------------------------------------

def test_the_column_is_nullable_text_added_by_an_additive_migration():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.models.skills import JDSkill
    column = JDSkill.__table__.c.requirement_text
    assert column.nullable and type(column.type).__name__ == "Text"
    root = Path(__file__).resolve().parents[3]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(script.get_heads()) == 1
    revision = script.get_revision("d2f4a6c8e0b1")
    assert revision.down_revision == "c9e1a3b5d7f2"
    source = Path(revision.path).read_text(encoding="utf-8")
    assert 'op.add_column("jd_skills", sa.Column("requirement_text", sa.Text(), nullable=True))' in source
