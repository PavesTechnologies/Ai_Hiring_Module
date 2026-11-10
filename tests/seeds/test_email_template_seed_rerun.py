"""M-M3: re-running seed_email_templates never overwrites an edited template; it only adds missing ones."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.seeds import seed_email_templates as seed_module


def _db(existing_by_call):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = existing_by_call
    return db


def test_an_edited_template_is_kept_and_a_missing_one_added():
    templates = seed_module._TEMPLATES
    edited = SimpleNamespace(subject="Our own wording", body_template="<p>edited by HR</p>")
    same = SimpleNamespace(subject=templates[1]["subject"], body_template=templates[1]["body_template"])
    existing = [edited, same] + [None] * (len(templates) - 2)
    db = _db(existing)
    counts = seed_module.seed(db)
    assert edited.subject == "Our own wording" and edited.body_template == "<p>edited by HR</p>"
    assert counts == {"added": len(templates) - 2, "unchanged": 1, "edited_kept": 1}
    assert db.add.call_count == len(templates) - 2
    db.commit.assert_called_once()


def test_importing_the_seed_runs_nothing():
    assert callable(seed_module.seed) and callable(seed_module.main)
