"""The golden vote cache's natural keys: the same frozen votes answer on any database (skills by name, not id)."""
import json
from types import SimpleNamespace
from uuid import uuid4

from scripts.golden import natural_keys
from scripts.golden import run as runner


class _Names(natural_keys.SkillNames):
    """SkillNames over a {name: id} catalogue, without a database."""

    def __init__(self, catalogue: dict):
        self._name = {str(i): n for n, i in catalogue.items()}
        self._id = {n.lower(): str(i) for n, i in catalogue.items()}


def _two_databases():
    names = ["Python", "REST APIs", "ServiceNow"]
    return _Names({n: uuid4() for n in names}), _Names({n: uuid4() for n in names})


def _core(names: _Names, skill, aliases=(), sense=None):
    return SimpleNamespace(canonical_skill_id=names.id_of(skill), sense=sense, sense_basis=None,
                           alias_skill_ids=[names.id_of(a) for a in aliases])


def test_the_natural_core_hash_is_the_same_on_two_databases_and_the_id_based_one_is_not():
    from app.services.skills.core_skill_confirmation_service import core_set_hash
    dev, fresh = _two_databases()
    dev_set = [_core(dev, "Python"), _core(dev, "ServiceNow", aliases=["REST APIs"], sense="ITSM")]
    fresh_set = [_core(fresh, "ServiceNow", aliases=["REST APIs"], sense="ITSM"), _core(fresh, "Python")]
    assert natural_keys.natural_core_set_hash(dev_set, dev) == natural_keys.natural_core_set_hash(fresh_set, fresh)
    assert core_set_hash(dev_set) != core_set_hash(fresh_set)
    assert natural_keys.natural_core_set_hash(dev_set, dev) != natural_keys.natural_core_set_hash(dev_set[:1], dev)


def test_the_natural_proposals_hash_is_the_same_on_two_databases():
    dev, fresh = _two_databases()

    def proposals(names):
        return [SimpleNamespace(canonical_skill_id=names.id_of("REST APIs"), line="Built REST endpoints in Flask")]
    assert (natural_keys.natural_proposals_hash(proposals(dev), dev)
            == natural_keys.natural_proposals_hash(proposals(fresh), fresh))


def test_a_cached_entry_gets_this_databases_ids_by_name():
    dev, fresh = _two_databases()
    entry = {"results": [{"name": "Python", "canonical_skill_id": dev.id_of("Python"), "status": "CONFIRMED"},
                         {"name": "Retired Skill", "canonical_skill_id": "old-id"}]}
    local = fresh.with_local_ids(entry)
    assert local["results"][0]["canonical_skill_id"] == fresh.id_of("Python")
    assert local["results"][1]["canonical_skill_id"] == "old-id"                   # unknown name: kept
    assert entry["results"][0]["canonical_skill_id"] == dev.id_of("Python")        # the cache isn't changed


def test_an_entry_cached_under_its_id_based_key_is_found_once_and_moved_to_its_natural_key():
    dev, _ = _two_databases()
    old = {"results": [{"name": "Python", "canonical_skill_id": dev.id_of("Python")}], "model": "m"}
    votes = runner.GoldenVotes(None, {"F1|old-key": old}, dev_cache=False, names=dev,
                               legacy={"natural-key": ["other-old-key", "old-key"]})
    votes.case_id = "F1"
    hit = votes.get_confirmation("natural-key")
    assert hit is not None and hit.results[0]["canonical_skill_id"] == dev.id_of("Python")
    assert votes.new == {"F1|natural-key": old} and votes.retired == ["F1|old-key"]
    assert votes.get_confirmation("unknown-key") is None


def test_accepting_a_run_retires_the_moved_keys(tmp_path, monkeypatch):
    cache = tmp_path / "vote_cache.json"
    cache.write_text(json.dumps({"F1|old-key": {"results": []}, "kept": {"results": []}}), encoding="utf-8")
    monkeypatch.setattr(runner, "VOTE_CACHE", cache)
    monkeypatch.setattr(runner, "accepted_path", lambda no_llm: tmp_path / "accepted.json")
    runner.accept({"run_at": "2026-11-03T11:00:00", "fingerprint": "f", "llm": {}, "cases": {},
                   "new_vote_cache_entries": {"F1|natural-key": {"results": []}},
                   "retired_vote_cache_keys": ["F1|old-key"]})
    assert sorted(json.loads(cache.read_text(encoding="utf-8"))) == ["F1|natural-key", "kept"]
