"""
The golden vote cache keyed by natural keys - skills by canonical name, not database id - so the frozen
votes answer on any database built from the same catalogue: dev, a fresh one, production.

The app's confirmation cache key (core_skill_confirmation_service.confirmation_cache_key) depends on skill
ids twice: the core skills are hashed by id (core_set_hash), and the paraphrase round's text hash covers
its proposals by skill id (paraphrase_proposal_service.proposals_hash). Each cached result also names its
skill by id. A fresh database gives every skill a new id, so none of dev's votes would ever match. During
a golden run:

- natural_cache_keys() swaps both hashes for natural ones (canonical names; senses and alias names for
  the core set) - the cache key becomes the natural key - and remembers each key's older id-based forms,
  so an entry stored before this change is found once and moved to its natural key (see GoldenVotes);
- SkillNames.with_local_ids() gives a cached entry's results this database's ids, by the name each
  result carries.

The app's own cache (core_skill_confirmations) is not affected.
"""
import contextlib
import copy
import hashlib
from unittest.mock import patch

NATURAL_PREFIX = "natural:"


class SkillNames:
    """Canonical name <-> id for the connected database's catalogue (an active skill wins a shared name)."""

    def __init__(self, db):
        from sqlalchemy import select
        from app.models.skills import SkillOntology
        rows = db.execute(select(SkillOntology.id, SkillOntology.canonical_name, SkillOntology.is_active)).all()
        self._name = {str(sid): name for sid, name, _ in rows}
        self._id: dict[str, str] = {}
        for sid, name, _active in sorted(rows, key=lambda r: bool(r[2])):     # active last: it wins
            self._id[name.lower()] = str(sid)

    def name_of(self, skill_id) -> str:
        return self._name.get(str(skill_id), str(skill_id))

    def id_of(self, name: str | None) -> str | None:
        return self._id.get((name or "").lower())

    def with_local_ids(self, entry: dict) -> dict:
        """A cached entry whose results name this database's skill ids (matched by the result's name)."""
        out = copy.deepcopy(entry)
        for result in out.get("results") or []:
            local = self.id_of(result.get("name"))
            if local is not None:
                result["canonical_skill_id"] = local
        return out


def natural_core_set_hash(core_skills, names: SkillNames) -> str:
    """core_set_hash with each skill (and its aliases) by canonical name instead of id."""
    keys = sorted(
        f"{names.name_of(s.canonical_skill_id)}:{s.sense or ''}:{s.sense_basis or ''}:"
        f"{','.join(sorted(names.name_of(a) for a in s.alias_skill_ids))}"
        for s in core_skills
    )
    return NATURAL_PREFIX + hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()


def natural_proposals_hash(proposals, names: SkillNames) -> str:
    """proposals_hash with each proposal's skill by canonical name instead of id."""
    raw = "|".join(sorted(f"{names.name_of(p.canonical_skill_id)}:{p.line}" for p in proposals))
    return "n" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:15]


@contextlib.contextmanager
def natural_cache_keys(confirmation_module, scoring_module, names: SkillNames, legacy: dict):
    """
    While active, the confirmation service's cache keys are natural ones; legacy maps each natural key to
    its older id-based forms (entries cached before natural keys): id-based core set and/or id-based
    paraphrase proposals.
    """
    original_hash, original_key = confirmation_module.core_set_hash, confirmation_module.confirmation_cache_key
    original_text_hash = scoring_module.paraphrase_text_hash
    id_based_core: dict[str, str] = {}
    id_based_text: dict[str, str] = {}

    def core_hash(core_skills):
        natural = natural_core_set_hash(core_skills, names)
        id_based_core[natural] = original_hash(core_skills)
        return natural

    def paraphrase_text_hash(text_hash, proposals):
        natural = hashlib.sha256(f"{text_hash}:paraphrase:{natural_proposals_hash(proposals, names)}"
                                 .encode("utf-8")).hexdigest()
        id_based_text[natural] = original_text_hash(text_hash, proposals)
        return natural

    def cache_key(text_hash, core_hash_value, provider, model):
        key = original_key(text_hash, core_hash_value, provider, model)
        texts = dict.fromkeys([text_hash, id_based_text.get(text_hash, text_hash)])
        cores = dict.fromkeys([core_hash_value, id_based_core.get(core_hash_value, core_hash_value)])
        older = [original_key(t, c, provider, model) for t in texts for c in cores]
        legacy[key] = [k for k in older if k != key]
        return key

    with patch.object(confirmation_module, "core_set_hash", core_hash), \
            patch.object(confirmation_module, "confirmation_cache_key", cache_key), \
            patch.object(scoring_module, "paraphrase_text_hash", paraphrase_text_hash):
        yield
