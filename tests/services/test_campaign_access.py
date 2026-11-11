"""The shared campaign access rule (app/services/campaign_access.py) - review items 9-12 and A-H5."""
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign_access import (
    CampaignAccess, accessible_campaigns_filter, can_access_campaign, sees_all_campaigns,
)
from tests.support.campaign_access_world import access_world

CAMPAIGN = SimpleNamespace(id="c-1", hiring_manager_id="hm-1", recruiter_id="rec-1", created_by="rec-c")


def user(user_id, *roles, admitted=None):
    return SimpleNamespace(user_id=user_id, roles=list(roles), authorised_roles=tuple(admitted or ()))


@pytest.mark.parametrize("who, allowed", [
    (user("hr-1", "HR_ADMIN"), True),
    (user("sa-1", "SUPER_ADMIN"), True),
    (user("hm-1", "HIRING_MANAGER"), True),         # owns it
    (user("hm-2", "HIRING_MANAGER"), False),
    (user("rec-1", "RECRUITER"), True),             # assigned recruiter
    (user("rec-c", "RECRUITER"), True),             # created it
    (user("rec-2", "RECRUITER"), False),
    (user("hm-1", "General"), False),                # an org label grants nothing
    (user("rec-1", "HIRING_MANAGER"), False),       # recruiter of it, but here only as a hiring manager
])
def test_the_rule(who, allowed):
    assert can_access_campaign(who, CAMPAIGN) is allowed


def test_the_roles_the_route_admitted_are_what_counts():
    # hm-1 owns the campaign as a hiring manager, but on a recruiter-only route acts as a recruiter.
    both = user("hm-1", "HIRING_MANAGER", "RECRUITER", admitted=("RECRUITER",))
    assert can_access_campaign(both, CAMPAIGN) is False
    assert can_access_campaign(user("hm-1", "HIRING_MANAGER", "RECRUITER", admitted=("HIRING_MANAGER",)), CAMPAIGN)
    # an HR admin acting on a route that doesn't admit HR_ADMIN is not all-seeing there
    assert sees_all_campaigns(user("x", "HR_ADMIN", "RECRUITER", admitted=("RECRUITER",))) is False


def test_the_list_filter_is_the_same_rule_in_sql():
    assert accessible_campaigns_filter(user("hr-1", "HR_ADMIN")) is None
    sql = str(accessible_campaigns_filter(user("u-1", "HIRING_MANAGER", "RECRUITER")).compile(
        dialect=postgresql.dialect())).lower()
    assert "hiring_campaigns.hiring_manager_id" in sql
    assert "hiring_campaigns.recruiter_id" in sql and "hiring_campaigns.created_by" in sql
    assert str(accessible_campaigns_filter(user("u-1", "General"))).lower() == "false"


def _raises(status, fn, *args):
    with pytest.raises(CampaignException) as exc:
        fn(*args)
    assert exc.value.status_code == status
    return exc.value


def test_every_resource_reaches_its_campaign():
    with access_world() as w:
        access = CampaignAccess(db=None)
        owner, outsider = user("hm-1", "HIRING_MANAGER"), user("hm-2", "HIRING_MANAGER")
        checks = [
            (access.require_campaign, w.campaign_a.id, w.campaign_b.id),
            (access.require_campaign_candidate, w.cc_a, w.cc_b),
            (access.require_bulk_job, w.job_a, w.job_b),
            (access.require_note, w.note_a, w.note_b),
            (access.require_upload_decision, w.decision_a, w.decision_b),
            (access.require_resume, w.resume_a, w.resume_b),
            (access.require_candidate, w.candidate_a, w.candidate_b),
            (access.require_dead_letter, w.dlq_a, w.dlq_b),
        ]
        for check, mine, theirs in checks:
            check(owner, mine)
            message = _raises(403, check, owner, theirs).message
            assert message == "You do not have access to this campaign."
            _raises(403, check, outsider, mine)


def test_a_missing_resource_is_404_before_access_is_judged():
    from uuid import uuid4
    with access_world():
        access = CampaignAccess(db=None)
        hr = user("hr-1", "HR_ADMIN")
        for check in (access.require_campaign, access.require_campaign_candidate, access.require_resume,
                      access.require_bulk_job, access.require_note):
            _raises(404, check, hr, uuid4())


def test_a_candidate_must_be_in_the_campaign_the_route_names():
    with access_world() as w:
        access = CampaignAccess(db=None)
        hr = user("hr-1", "HR_ADMIN")
        access.require_candidate_in_campaign(hr, w.campaign_a.id, w.cc_a)
        _raises(404, access.require_candidate_in_campaign, hr, w.campaign_a.id, w.cc_b)


def test_many_candidates_all_or_nothing():
    with access_world() as w:
        access = CampaignAccess(db=None)
        owner = user("hm-1", "HIRING_MANAGER")
        access.require_campaign_candidates(owner, [w.cc_a, w.cc_a])
        _raises(403, access.require_campaign_candidates, owner, [w.cc_a, w.cc_b])
        access.require_campaign_candidates(user("hr-1", "HR_ADMIN"), [w.cc_a, w.cc_b])


def test_a_shared_resume_is_visible_from_any_of_its_campaigns_and_an_unlinked_one_to_its_uploader():
    with access_world() as w:
        access = CampaignAccess(db=None)
        w.resume_campaigns[w.resume_b].append(w.campaign_a.id)           # shared into campaign A
        access.require_resume(user("rec-1", "RECRUITER"), w.resume_b)
        orphan = w.objects[w.resume_a]
        w.resume_campaigns[orphan.id] = []
        orphan.uploaded_by = "rec-2"
        access.require_resume(user("rec-2", "RECRUITER"), orphan.id)
        _raises(403, access.require_resume, user("rec-1", "RECRUITER"), orphan.id)


def test_deleting_a_shared_resume_needs_every_campaign_it_is_in():
    with access_world() as w:
        access = CampaignAccess(db=None)
        w.resume_campaigns[w.resume_a].append(w.campaign_b.id)          # shared with someone else's campaign
        recruiter = user("rec-1", "RECRUITER")
        access.require_resume(recruiter, w.resume_a)                   # retry/replay: one campaign is enough
        _raises(403, lambda: access.require_resume(recruiter, w.resume_a, every_campaign=True))
        access.require_resume(user("hr-1", "HR_ADMIN"), w.resume_a, every_campaign=True)
