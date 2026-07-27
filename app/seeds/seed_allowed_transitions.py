import uuid

from app.db.session import SessionLocal
from app.models.pipeline import AllowedTransition, PipelineStage

db = SessionLocal()

# M07-E03 S02 T01: allowed_transitions had zero rows in the live database -
# StageTransitionService.transition_to_rejected checks this table before
# ever moving a candidate to REJECTED, so without this row every
# deterministic/semantic/AI rejection would hit the "abort" branch.
_TRANSITIONS = [
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
