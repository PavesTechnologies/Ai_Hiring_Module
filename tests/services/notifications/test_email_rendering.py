"""Email templates: plain {name} placeholders the sender supplies, rendered without str.format."""
import re
from pathlib import Path

import pytest

from app.models.email import EmailTriggerEvent as E
from app.services.notifications.email_rendering import (
    NOT_SENT, PLACEHOLDERS, SAMPLE_VALUES, TemplateRenderError, render, template_problems,
)

SEED = Path(__file__).resolve().parents[3] / "app" / "seeds" / "seed_email_templates.py"


def test_every_trigger_event_is_either_sendable_or_marked_not_sent():
    assert set(PLACEHOLDERS) | NOT_SENT == set(E) and not (set(PLACEHOLDERS) & NOT_SENT)


def test_every_placeholder_has_a_preview_sample():
    assert {p for names in PLACEHOLDERS.values() for p in names} <= set(SAMPLE_VALUES)


def test_the_seeded_templates_use_only_what_their_sender_supplies():
    from app.seeds import seed_email_templates
    for template in seed_email_templates._TEMPLATES:
        if template["trigger_event"] in NOT_SENT:
            continue
        allowed = PLACEHOLDERS[template["trigger_event"]]
        assert template_problems(template["subject"], allowed) == [], template["trigger_event"]
        assert template_problems(template["body_template"], allowed) == [], template["trigger_event"]


@pytest.mark.parametrize("text, problem", [
    ("Hi {candidate_name.__class__}", "only plain names"),
    ("Hi {0}", "only plain names"),
    ("Hi {candidate_name[0]}", "only plain names"),
    ("Hi {candidate_name:>20}", "formatting after the name"),
    ("Hi {candidate_name!r}", "formatting after the name"),
    ("Hi {salary}", "isn't supplied"),
    ("Hi {candidate_name", "Unbalanced braces"),
])
def test_unsafe_or_unknown_placeholders_are_refused(text, problem):
    problems = template_problems(text, PLACEHOLDERS[E.CANDIDATE_REJECTED])
    assert problems and problem in problems[0]


def test_render_fills_plain_names_and_keeps_literal_braces():
    assert render("Hi {candidate_name}, {{not a field}}", {"candidate_name": "Alex"}) == "Hi Alex, {not a field}"
    assert render("{suspended_note}", {"suspended_note": None}) == ""


@pytest.mark.parametrize("text", ["{candidate_name.__class__}", "{0}", "{candidate_name:>9}", "{missing}"])
def test_render_refuses_what_str_format_would_have_evaluated(text):
    with pytest.raises(TemplateRenderError):
        render(text, {"candidate_name": "Alex"})


def test_the_send_task_no_longer_calls_str_format_on_templates():
    source = (SEED.parents[1] / "tasks" / "email_tasks.py").read_text(encoding="utf-8")
    assert not re.search(r"(subject|body_template)\.format\(", source)
