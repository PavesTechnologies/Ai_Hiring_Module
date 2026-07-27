import uuid

from app.db.session import SessionLocal
from app.models.pipeline import AllowedTransition, PipelineStage

db = SessionLocal()

# Epic 3 (M05-E03) Phase C0 — only the fraud-review edges C5/C7 need.
# Two rows already exist in the live table from the M07-E03 rejection-handling
# epic (SCREENING->REJECTED, REJECTED->SCREENING) - left untouched here. The
# rest of the "normal" pipeline graph (UPLOADED->SCREENING etc.) is
# deliberately not seeded: nothing drives those transitions today either.
try:
    transitions = [
        AllowedTransition(
            id=uuid.uuid4(),
            from_stage=PipelineStage.UPLOADED,
            to_stage=PipelineStage.FRAUD_REVIEW,
            allowed_roles=["SYSTEM"],
            requires_reason=False,
            notes="Automated fraud-pattern detection (near-duplicate / keyword-stuffed) flags a freshly uploaded resume (M05-E03 S06).",
        ),
        AllowedTransition(
            id=uuid.uuid4(),
            from_stage=PipelineStage.SCREENING,
            to_stage=PipelineStage.FRAUD_REVIEW,
            allowed_roles=["SYSTEM"],
            requires_reason=False,
            notes="Automated fraud-pattern detection flags a resume already in screening (M05-E03 S06).",
        ),
        AllowedTransition(
            id=uuid.uuid4(),
            from_stage=PipelineStage.FRAUD_REVIEW,
            to_stage=PipelineStage.REJECTED,
            allowed_roles=["HR_ADMIN"],
            requires_reason=True,
            notes="HR_ADMIN confirms a fraud flag and rejects the candidate (M05-E03 S06).",
        ),
        AllowedTransition(
            id=uuid.uuid4(),
            from_stage=PipelineStage.FRAUD_REVIEW,
            to_stage=PipelineStage.SCREENING,
            allowed_roles=["HR_ADMIN"],
            requires_reason=True,
            notes="HR_ADMIN clears a false-positive fraud flag, returning the candidate to screening (M05-E03 S06).",
        ),
    ]

    for transition in transitions:
        existing = (
            db.query(AllowedTransition)
            .filter(
                AllowedTransition.from_stage == transition.from_stage,
                AllowedTransition.to_stage == transition.to_stage,
            )
            .first()
        )
        if not existing:
            db.add(transition)
            print(f"Added transition: {transition.from_stage.value} -> {transition.to_stage.value}")
        else:
            print(f"Transition already exists: {transition.from_stage.value} -> {transition.to_stage.value}")

    db.commit()
    print("\nAllowed transitions seeded successfully")

except Exception as e:
    db.rollback()
    print(f"Error seeding allowed transitions: {e}")
    raise

finally:
    db.close()
