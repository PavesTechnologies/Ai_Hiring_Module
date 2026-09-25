from unittest.mock import MagicMock
from uuid import uuid4

from app.core.cache_invalidation import CacheInvalidator
from app.core.cache_keys import (
    campaign_key,
    campaign_list_prefix,
    campaign_scoring_key,
    candidate_list_prefix,
    jd_key,
    jd_list_prefix,
    jd_search_prefix,
    resume_key,
    resume_list_prefix,
    skill_catalog_key,
    skill_prefix,
)


def _deleted(cache):
    keys = {key for call in cache.delete.call_args_list for key in call.args}
    prefixes = {call.args[0] for call in cache.delete_by_prefix.call_args_list}
    return keys, prefixes


def test_none_cache_makes_every_method_a_no_op():
    invalidator = CacheInvalidator(None)
    invalidator.jd(uuid4())
    invalidator.jds([uuid4()])
    invalidator.resumes([uuid4()])
    invalidator.campaign_candidates(uuid4(), [uuid4()])
    invalidator.skills(uuid4())


def test_campaign_candidates_drops_campaign_and_resume_views():
    cache = MagicMock()
    campaign_id, resume_id = uuid4(), uuid4()

    CacheInvalidator(cache).campaign_candidates(campaign_id, [resume_id])

    keys, prefixes = _deleted(cache)
    assert {campaign_key(campaign_id), campaign_scoring_key(campaign_id), resume_key(resume_id)} <= keys
    assert prefixes == {campaign_list_prefix(), resume_list_prefix(), candidate_list_prefix()}


def test_jds_drops_each_detail_and_the_lists():
    cache = MagicMock()
    a, b = uuid4(), uuid4()

    CacheInvalidator(cache).jds([a, b])

    keys, prefixes = _deleted(cache)
    assert keys == {jd_key(a), jd_key(b)}
    assert prefixes == {jd_list_prefix(), jd_search_prefix()}


def test_jds_with_no_ids_does_nothing():
    cache = MagicMock()

    CacheInvalidator(cache).jds([])

    cache.delete.assert_not_called()
    cache.delete_by_prefix.assert_not_called()


def test_skills_drops_the_normalization_catalog():
    cache = MagicMock()

    CacheInvalidator(cache).skills()

    keys, prefixes = _deleted(cache)
    assert skill_catalog_key() in keys
    assert prefixes == {skill_prefix()}
