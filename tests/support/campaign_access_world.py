"""
A small in-memory world for campaign access tests: one campaign the test users own, one they don't,
and the things inside each. CampaignAccess's loaders are replaced, so no database is needed.

    with access_world() as world:
        world.campaign_a / world.cc_a / world.resume_a ...   (the users' campaign)
        world.campaign_b / world.cc_b / ...                    (someone else's)

Users: hm-1 owns campaign A, rec-1 is its assigned recruiter, rec-c created it; hm-2 / rec-2 have
nothing to do with it. Campaign B belongs to hm-9 / rec-9.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.services.campaign_access import CampaignAccess, can_access_campaign, sees_all_campaigns


class World:
    def __init__(self):
        self.campaigns = {}
        self.candidate_campaign = {}
        self.objects = {}
        self.resume_campaigns = {}
        self.candidate_campaigns = {}
        self.tasks = {}
        self.campaign_a = self._campaign("hm-1", "rec-1", "rec-c")
        self.campaign_b = self._campaign("hm-9", "rec-9", "rec-9")
        for side, campaign in (("a", self.campaign_a), ("b", self.campaign_b)):
            cc = uuid4()
            self.candidate_campaign[cc] = campaign.id
            resume = SimpleNamespace(id=uuid4(), uploaded_by="someone", bulk_upload_job_id=None)
            candidate = SimpleNamespace(id=uuid4(), source_campaign_id=campaign.id)
            job = SimpleNamespace(id=uuid4(), campaign_id=campaign.id)
            note = SimpleNamespace(id=uuid4(), campaign_candidate_id=cc)
            interview = SimpleNamespace(id=uuid4(), campaign_candidate_id=cc)
            decision = SimpleNamespace(id=uuid4(), campaign_id=campaign.id)
            dlq = SimpleNamespace(id=uuid4(), campaign_candidate_id=cc, resume_id=None)
            task_id = str(uuid4())
            self.tasks[task_id] = SimpleNamespace(task_id=task_id, campaign_candidate_id=None, resume_id=resume.id,
                                                  bulk_upload_job_id=None, created_by="someone")
            for obj in (resume, candidate, job, note, interview, decision, dlq):
                self.objects[obj.id] = obj
            self.resume_campaigns[resume.id] = [campaign.id]
            self.candidate_campaigns[candidate.id] = [campaign.id]
            for name, value in (("cc", cc), ("resume", resume.id), ("candidate", candidate.id), ("job", job.id),
                                ("note", note.id), ("interview", interview.id), ("decision", decision.id),
                                ("dlq", dlq.id), ("task", task_id)):
                setattr(self, f"{name}_{side}", value)

    def _campaign(self, hm, recruiter, creator):
        campaign = SimpleNamespace(id=uuid4(), hiring_manager_id=hm, recruiter_id=recruiter, created_by=creator)
        self.campaigns[campaign.id] = campaign
        return campaign

    def ids(self, side: str) -> dict:
        """Path-parameter values for one side's resources."""
        campaign = self.campaign_a if side == "a" else self.campaign_b
        return {"campaign_id": campaign.id, "campaign_candidate_id": getattr(self, f"cc_{side}"),
                "resume_id": getattr(self, f"resume_{side}"), "candidate_id": getattr(self, f"candidate_{side}"),
                "bulk_upload_job_id": getattr(self, f"job_{side}"), "note_id": getattr(self, f"note_{side}"),
                "interview_id": getattr(self, f"interview_{side}"), "decision_id": getattr(self, f"decision_{side}"),
                "dlq_id": getattr(self, f"dlq_{side}"), "task_id": getattr(self, f"task_{side}")}


@contextmanager
def access_world():
    world = World()

    def _get(self, model, entity_id):
        if model.__name__ == "HiringCampaign":
            return world.campaigns.get(entity_id)
        return world.objects.get(entity_id)

    loaders = {
        "_get": _get,
        "_campaigns": lambda self, ids: [world.campaigns[i] for i in ids if i in world.campaigns],
        "_candidate_campaign_ids": lambda self, ids: {
            i: world.candidate_campaign[i] for i in ids if i in world.candidate_campaign},
        "_resume_campaign_ids": lambda self, resume: list(world.resume_campaigns.get(resume.id, [])),
        "_candidate_campaigns_of": lambda self, candidate: list(world.candidate_campaigns.get(candidate.id, [])),
        "_task_log": lambda self, task_id: world.tasks.get(str(task_id)),
        "visible_campaign_ids": lambda self, user: (
            None if sees_all_campaigns(user)
            else sorted((c.id for c in world.campaigns.values() if can_access_campaign(user, c)), key=str)),
        "_resumes_in_campaigns": lambda self, resume_ids, _select: {
            r for r in resume_ids if any(
                c == world.campaign_a.id for c in world.resume_campaigns.get(r, []))},
    }
    patchers = [patch.object(CampaignAccess, name, fn) for name, fn in loaders.items()]
    for p in patchers:
        p.start()
    try:
        yield world
    finally:
        for p in patchers:
            p.stop()


@contextmanager
def campaign_access_granted():
    """
    For route tests about something else (role forwarding, request bodies) that use made-up ids
    over a mocked database: every CampaignAccess.require_* passes. The access check itself is
    tested in tests/api/test_campaign_access_routes.py and tests/services/test_campaign_access.py.
    """
    names = [n for n in vars(CampaignAccess) if n.startswith("require_")]
    patchers = [patch.object(CampaignAccess, n, lambda *a, **k: None) for n in names]
    for p in patchers:
        p.start()
    try:
        yield
    finally:
        for p in patchers:
            p.stop()
