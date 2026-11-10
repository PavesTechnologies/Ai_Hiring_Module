"""M-M2: re-running seed_prompt_templates never adds a second active prompt beside an edited one."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.seeds import seed_prompt_templates as seed_module


def test_an_edited_prompt_gets_no_duplicate_and_a_new_one_is_added():
    templates = seed_module._TEMPLATES
    # Per template the seed asks: same text? then same task type + name?
    answers = []
    answers += [SimpleNamespace(id="identical")]              # 1st: identical text exists
    answers += [None, SimpleNamespace(id="edited")]           # 2nd: text differs, same name exists
    answers += [None, None] * (len(templates) - 2)            # the rest: new
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = answers
    counts = seed_module.seed(db)
    assert counts == {"added": len(templates) - 2, "identical": 1, "edited_kept": 1}
    added = [c.args[0].name for c in db.add.call_args_list]
    assert templates[1]["name"] not in added and templates[0]["name"] not in added
    db.commit.assert_called_once()
