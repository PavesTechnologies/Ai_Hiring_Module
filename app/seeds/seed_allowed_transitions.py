import uuid

from app.db.session import SessionLocal
from app.models.pipeline import AllowedTransition, PipelineStage

db = SessionLocal()

# Epic 3 (M05-E03) Phase C0 fraud-review edges (C5/C7) plus the M07-E03
# rejection-handling edges - StageTransitionService.transition_to_rejected
# checks this table before ever moving a candidate to REJECTED, so without
# those rows every deterministic/semantic/AI rejection would hit the
# "abort" branch. The rest of the "normal" pipeline graph
# (UPLOADED->SCREENING etc.) is deliberately not seeded: nothing drives
# those transitions today either.
_TRANSITIONS = [
    {
        "from_stage": PipelineStage.UPLOADED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated fraud-pattern detection (near-duplicate / keyword-stuffed) flags a freshly uploaded resume (M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated fraud-pattern detection flags a resume already in screening (M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN confirms a fraud flag and rejects the candidate (M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN clears a false-positive fraud flag, returning the candidate to screening (M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER"],
        "requires_reason": False,
        "notes": "Hard rejection from the deterministic/semantic/AI screening layers (M07-E03).",
    },
    {
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN override of a deterministic rejection, re-entering the candidate into the pipeline (M07-E03 S04).",
    },
    # Epic 3 (M05-E03) Phase C5 — "update resume" resubmission re-trigger.
    # Deliberately not seeded: SELECTED/REJECTED/FRAUD_REVIEW -> UPLOADED —
    # resubmitting for a candidate already selected, rejected, or under
    # fraud review is a different, not-yet-defined business process, not a
    # straight "update resume."
    {
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER"],
        "requires_reason": False,
        "notes": "Resume update before SHORTLISTED — straight re-trigger, no extra confirmation gate (M05-E03 S03).",
    },
    {
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "Resume update once SHORTLISTED — requires HR_ADMIN confirmation (M05-E03 S03).",
    },
    {
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "Resume update once on HOLD — requires HR_ADMIN confirmation (M05-E03 S03).",
    },
    {
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "Resume update once in HM_REVIEW — requires HR_ADMIN confirmation (M05-E03 S03).",
    },
    {
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "Resume update once in INTERVIEW — requires HR_ADMIN confirmation (M05-E03 S03).",
    },
]

try:
    for transition in _TRANSITIONS:
        existing = (
            db.query(AllowedTransition)
            .filter(
                AllowedTransition.from_stage == transition["from_stage"],
                AllowedTransition.to_stage == transition["to_stage"],
            )
            .first()
        )
        if existing:
            print(f"Transition already exists: {transition['from_stage'].value} -> {transition['to_stage'].value}")
            continue

        db.add(AllowedTransition(id=uuid.uuid4(), **transition))
        print(f"Added transition: {transition['from_stage'].value} -> {transition['to_stage'].value}")

    db.commit()
    print("\nAllowed transitions seeded successfully")

except Exception as e:
    db.rollback()
    print(f"Error seeding allowed transitions: {e}")
    raise

finally:
    db.close()
