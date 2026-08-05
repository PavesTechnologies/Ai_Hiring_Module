from app.services.jd.jd_embedding_text_builder import build_jd_embedding_text


def test_builds_expected_format_with_skills_and_raw_text():
    text = build_jd_embedding_text(
        title="Python Developer (Fresher)",
        raw_text="We are looking for a motivated Python fresher.",
        mandatory_skill_names=["Python", "SQL", "REST APIs"],
        preferred_skill_names=["FastAPI", "Django"],
        max_chars=2000,
    )

    assert text == (
        "Python Developer (Fresher).\n"
        "Required skills: Python, SQL, REST APIs.\n"
        "Preferred skills: FastAPI, Django.\n"
        "We are looking for a motivated Python fresher."
    )


def test_truncates_only_raw_text_never_title_or_skills():
    long_raw_text = "x" * 5000
    text = build_jd_embedding_text(
        title="Senior Engineer",
        raw_text=long_raw_text,
        mandatory_skill_names=["Python"],
        preferred_skill_names=[],
        max_chars=100,
    )

    lines = text.split("\n")
    assert lines[0] == "Senior Engineer."
    assert lines[1] == "Required skills: Python."
    assert lines[2] == "Preferred skills: ."
    assert lines[3] == "x" * 100
    assert len(lines[3]) == 100


def test_handles_empty_skill_lists():
    text = build_jd_embedding_text(
        title="Generalist Role",
        raw_text="Some description.",
        mandatory_skill_names=[],
        preferred_skill_names=[],
        max_chars=2000,
    )

    assert "Required skills: ." in text
    assert "Preferred skills: ." in text


def test_handles_none_raw_text_gracefully():
    text = build_jd_embedding_text(
        title="Role",
        raw_text=None,
        mandatory_skill_names=["Python"],
        preferred_skill_names=[],
        max_chars=100,
    )

    assert text.endswith("\n")
    assert "Role." in text


def test_never_includes_candidate_or_resume_terminology():
    """Sanity check: the builder's inputs are JD-only; nothing here reads resume/candidate data."""
    text = build_jd_embedding_text(
        title="Backend Engineer",
        raw_text="Build APIs.",
        mandatory_skill_names=["Python"],
        preferred_skill_names=["Docker"],
        max_chars=2000,
    )
    assert "candidate" not in text.lower()
    assert "resume" not in text.lower()
