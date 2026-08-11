import uuid

from app.db.session import SessionLocal
from app.models.pipeline import AllowedTransition, PipelineStage

db = SessionLocal()

# Epic 3 (M05-E03) Phase C0 fraud-review edges (C5/C7) plus the M07-E03
# rejection-handling edges - StageTransitionService.transition_to_rejected
# checks this table before ever moving a candidate to REJECTED, so without
# those rows every deterministic/semantic/AI rejection would hit the
# "abort" branch.
#
# M12 addition: the rest of the "normal" pipeline graph
# (UPLOADED->SCREENING->SHORTLISTED->HM_REVIEW->INTERVIEW->SELECTED/REJECTED)
# plus fraud-review edges from the later stages (SHORTLISTED/HM_REVIEW/
# INTERVIEW -> FRAUD_REVIEW, and their "cleared" edges back).
_TRANSITIONS = [
    {
        "from_stage": PipelineStage.UPLOADED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated: deterministic scoring starting moves the candidate into screening (StageTransitionService.transition_to_screening).",
    },
    {
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER", "HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Automated: AI evaluation SHORTLIST recommendation (StageTransitionService.transition_on_ai_success); also reachable via manual stage override.",
    },
    {
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER", "HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Automated: AI evaluation HOLD recommendation (StageTransitionService.transition_on_ai_success); also reachable via manual stage override.",
    },
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
    # M12 — normal pipeline progression.
    {
        "from_stage": PipelineStage.UPLOADED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER"],
        "requires_reason": False,
        "notes": "Initial resume screening kickoff after upload; SYSTEM-driven in the normal flow, HR_ADMIN/RECRUITER can force it manually (M12).",
    },
    {
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER", "HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Composite scoring (M10) shortlists a candidate; HR_ADMIN/RECRUITER/HIRING_MANAGER can force it manually (M12).",
    },
    {
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["SYSTEM", "HR_ADMIN", "RECRUITER", "HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Candidate handed to hiring manager for review; HR_ADMIN/RECRUITER/HIRING_MANAGER can force it manually (M12).",
    },
    {
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER", "HR_ADMIN"],
        "requires_reason": False,
        "notes": "Hiring manager approves candidate to move to interview (M12); HR_ADMIN retains stalled-candidate override capability.",
    },
    {
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Hiring manager rejects candidate after review (M12) — terminal human decision, reason required.",
    },
    {
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["HIRING_MANAGER", "HR_ADMIN"],
        "requires_reason": False,
        "notes": "Hiring manager selects candidate after interview (M12); HR_ADMIN retains stalled-candidate override capability.",
    },
    {
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Hiring manager rejects candidate after interview (M12) — terminal human decision, reason required.",
    },
    # M12 — extend automated fraud detection to later stages, matching
    # the existing UPLOADED/SCREENING -> FRAUD_REVIEW pattern (M05-E03 S06).
    {
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated fraud-pattern detection flags a shortlisted candidate (M12 extension of M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated fraud-pattern detection flags a candidate in HM review (M12 extension of M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated fraud-pattern detection flags a candidate in interview (M12 extension of M05-E03 S06).",
    },
    {
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN clears a false-positive fraud flag, returning the candidate to SHORTLISTED (M12, mirrors FRAUD_REVIEW -> SCREENING).",
    },
    {
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN clears a false-positive fraud flag, returning the candidate to HM_REVIEW (M12, mirrors FRAUD_REVIEW -> SCREENING).",
    },
    {
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN clears a false-positive fraud flag, returning the candidate to INTERVIEW (M12, mirrors FRAUD_REVIEW -> SCREENING).",
    },
    {
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HR_ADMIN"],
        "requires_reason": True,
        "notes": "HR_ADMIN override of a deterministic/semantic/AI rejection, re-entering the candidate directly at SHORTLISTED (Epic 2 pre-work).",
    },
]

# Pipeline Board drag-and-drop - frictionless (no reason) forward/lateral
# moves among the board's 7 columns (Uploaded/Screening/Shortlisted/Hold/
# Interview/Selected/Rejected), for the same roles that can view the board
# (get_ranked_campaign_candidates' RBAC). REJECTED<->SCREENING is
# deliberately left as the existing HR_ADMIN-only, reason-required rows
# above - not duplicated or loosened here.
_BOARD_ROLES = ["HR_ADMIN", "RECRUITER", "HIRING_MANAGER"]
_BOARD_TRANSITIONS = [
    (PipelineStage.UPLOADED, PipelineStage.SCREENING),
    (PipelineStage.SCREENING, PipelineStage.SHORTLISTED),
    (PipelineStage.SCREENING, PipelineStage.HOLD),
    (PipelineStage.SCREENING, PipelineStage.INTERVIEW),
    (PipelineStage.SHORTLISTED, PipelineStage.SCREENING),
    (PipelineStage.SHORTLISTED, PipelineStage.INTERVIEW),
    (PipelineStage.SHORTLISTED, PipelineStage.HOLD),
    (PipelineStage.SHORTLISTED, PipelineStage.REJECTED),
    (PipelineStage.HOLD, PipelineStage.SCREENING),
    (PipelineStage.HOLD, PipelineStage.SHORTLISTED),
    (PipelineStage.HOLD, PipelineStage.INTERVIEW),
    (PipelineStage.INTERVIEW, PipelineStage.SHORTLISTED),
    (PipelineStage.INTERVIEW, PipelineStage.SELECTED),
    (PipelineStage.INTERVIEW, PipelineStage.HOLD),
    (PipelineStage.INTERVIEW, PipelineStage.REJECTED),
    (PipelineStage.SELECTED, PipelineStage.REJECTED),
]
for _from_stage, _to_stage in _BOARD_TRANSITIONS:
    _TRANSITIONS.append({
        "from_stage": _from_stage,
        "to_stage": _to_stage,
        "allowed_roles": _BOARD_ROLES,
        "requires_reason": False,
        "notes": f"Pipeline Board drag-and-drop: {_from_stage.value} -> {_to_stage.value}.",
    })

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
