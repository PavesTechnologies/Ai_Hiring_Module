import uuid

from app.db.session import SessionLocal
from app.models.pipeline import AllowedTransition, PipelineStage
from app.seeds.transition_role_rules import reconcile_existing, required_roles

db = SessionLocal()

# Governance model (2026-08-31): replaces the old flat (from_stage, to_stage)
# edge table. Every row is now scoped by previous_stage too - None means "any
# previous_stage" (a wildcard), a specific PipelineStage means the row only
# governs a candidate arriving at from_stage from exactly that stage (e.g.
# HM_REVIEW ownership flipping to HIRING_MANAGER, or a role differing by
# where a FRAUD_REVIEW/HOLD pause was entered from).
#
# Ownership rule applied throughout: the role that owns the *current* stage
# (from_stage) owns the transition out of it. RECRUITER owns SCREENING /
# SHORTLISTED / INTERVIEW / SELECTED / HOLD / REJECTED / FRAUD_REVIEW by
# default - except wherever previous_stage = HM_REVIEW, which flips
# ownership to HIRING_MANAGER. FRAUD_REVIEW *resolution* is always
# HIRING_MANAGER regardless of previous_stage. Since 2026-11-11 HR_ADMIN is
# added to every RECRUITER-owned row and SYSTEM to every move automated
# scoring makes - see app/seeds/transition_role_rules.py; the rows below
# list each move's owner, the rule adds the rest.
#
# _TRANSITIONS entries are dicts keyed exactly like AllowedTransition's
# columns; previous_stage omitted (or None) means the wildcard row.
_TRANSITIONS = [
    # ---- UPLOADED (current) ----
    {
        "previous_stage": None,
        "from_stage": PipelineStage.UPLOADED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": False,
        "notes": "Automated: deterministic scoring starting moves the candidate into screening.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.UPLOADED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["SYSTEM"],
        "requires_reason": True,
        "notes": "Exception: automated fraud-pattern detection flags a freshly uploaded resume.",
    },

    # ---- SCREENING (current) ----
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["RECRUITER", "SYSTEM"],
        "requires_reason": False,
        "notes": "Next: composite scoring shortlists a candidate; RECRUITER can also force it manually.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER", "SYSTEM"],
        "requires_reason": True,
        "notes": "Reject: hard rejection from the deterministic/semantic/AI screening layers, or a manual RECRUITER reject.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["RECRUITER", "SYSTEM"],
        "requires_reason": True,
        "notes": "Exception: automated fraud-pattern detection, or a manual RECRUITER flag.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SCREENING,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reset: resume update before SHORTLISTED.",
    },

    # ---- SHORTLISTED (current) ----
    # Default (RECRUITER) ownership - wildcard row.
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Next - Optional: candidate handed to hiring manager for review. HM_REVIEW is optional, not every candidate passes through it.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Next - Optional: skips HM_REVIEW directly to interview.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["RECRUITER", "SYSTEM"],
        "requires_reason": True,
        "notes": "Exception: automated fraud-pattern detection, or a manual RECRUITER flag.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reset: resume update once SHORTLISTED.",
    },
    # Ownership flip: candidate's previous_stage was HM_REVIEW (sent back to
    # SHORTLISTED, now moving again) - HIRING_MANAGER owns these instead.
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next (back into review).",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SHORTLISTED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception.",
    },

    # ---- HM_REVIEW (current) - always HIRING_MANAGER ----
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    # Gap-filled: HM_REVIEW reached straight from SCREENING (skipping
    # SHORTLISTED) previously had no outbound row at all - same target set
    # as the SHORTLISTED-arrival rows above, since SCREENING is the same
    # RECRUITER-owned pre-HM_REVIEW stage one hop earlier.
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next. Gap-filled: HM_REVIEW reached directly from SCREENING previously had no outbound row.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Pause. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back / Continue.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Confirm.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject (re-reject).",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    {
        "previous_stage": PipelineStage.FRAUD_REVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next. Gap-filled: HM_REVIEW reached via a cleared FRAUD_REVIEW previously had no outbound row.",
    },
    {
        "previous_stage": PipelineStage.FRAUD_REVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.FRAUD_REVIEW,
        "from_stage": PipelineStage.HM_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject. Gap-filled.",
    },

    # ---- INTERVIEW (current) ----
    {
        "previous_stage": None,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Next.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Optional Review.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["RECRUITER", "SYSTEM"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Pause.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.INTERVIEW,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER", "SYSTEM"],
        "requires_reason": True,
        "notes": "Exception.",
    },

    # ---- SELECTED (current) ----
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SELECTED,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Cancel Selection.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SELECTED,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reconsideration: SELECTED can be walked back to HM_REVIEW, never to an earlier pipeline stage directly.",
    },
    {
        "previous_stage": None,
        "from_stage": PipelineStage.SELECTED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["RECRUITER", "SYSTEM"],
        "requires_reason": True,
        "notes": "Exception.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SELECTED,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Cancel Selection. Gap-filled: SELECTED reached via HM_REVIEW previously had no outbound row.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SELECTED,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Back. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.SELECTED,
        "to_stage": PipelineStage.FRAUD_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Exception. Gap-filled.",
    },

    # ---- HOLD (current) ----
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Resume.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reset.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Resume - Optional.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Resume - Optional.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reset.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": False,
        "notes": "Resume.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reset.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": False,
        "notes": "Resume.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.UPLOADED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reset.",
    },
    {
        "previous_stage": PipelineStage.FRAUD_REVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back. Gap-filled: HOLD reached via a cleared FRAUD_REVIEW previously had no outbound row.",
    },
    {
        "previous_stage": PipelineStage.FRAUD_REVIEW,
        "from_stage": PipelineStage.HOLD,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject. Gap-filled.",
    },

    # ---- REJECTED (current) ----
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["RECRUITER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.FRAUD_REVIEW,
        "from_stage": PipelineStage.REJECTED,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reconsider. Gap-filled: REJECTED reached via FRAUD_REVIEW previously had no outbound row.",
    },

    # ---- FRAUD_REVIEW (current) - resolution always HIRING_MANAGER ----
    {
        "previous_stage": PipelineStage.UPLOADED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Return.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.SCREENING,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Next - Optional.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Next - Optional.",
    },
    {
        "previous_stage": PipelineStage.SHORTLISTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.HM_REVIEW,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Next.",
    },
    {
        "previous_stage": PipelineStage.INTERVIEW,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SELECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back / Confirm.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.HM_REVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reconsider.",
    },
    {
        "previous_stage": PipelineStage.SELECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.HOLD,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Back. Gap-filled: FRAUD_REVIEW reached from HOLD previously had no outbound row.",
    },
    {
        "previous_stage": PipelineStage.HOLD,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SCREENING,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reconsider. Gap-filled: FRAUD_REVIEW reached from REJECTED previously had no outbound row.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.SHORTLISTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reconsider. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.INTERVIEW,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reconsider. Gap-filled.",
    },
    {
        "previous_stage": PipelineStage.REJECTED,
        "from_stage": PipelineStage.FRAUD_REVIEW,
        "to_stage": PipelineStage.REJECTED,
        "allowed_roles": ["HIRING_MANAGER"],
        "requires_reason": True,
        "notes": "Reject (re-reject). Gap-filled.",
    },
]

def _label(transition) -> str:
    previous = transition["previous_stage"].value if transition["previous_stage"] else "ANY"
    return f"{previous} -> {transition['from_stage'].value} -> {transition['to_stage'].value}"


try:
    for transition in _TRANSITIONS:
        existing = (
            db.query(AllowedTransition)
            .filter(
                AllowedTransition.previous_stage == transition["previous_stage"],
                AllowedTransition.from_stage == transition["from_stage"],
                AllowedTransition.to_stage == transition["to_stage"],
            )
            .first()
        )
        if existing:
            print(f"Transition already exists: {_label(transition)}")
            continue

        roles = required_roles(transition["previous_stage"], transition["from_stage"], transition["to_stage"],
                               transition["allowed_roles"])
        db.add(AllowedTransition(id=uuid.uuid4(), **{**transition, "allowed_roles": roles}))
        print(f"Added transition: {_label(transition)}")

    # Existing rows (an older database) get the required roles added; nothing is ever removed.
    for previous, from_stage, to_stage, added in reconcile_existing(db):
        print(f"Roles added: {previous} -> {from_stage} -> {to_stage}: {', '.join(added)}")

    db.commit()
    print("Allowed transitions seeded successfully")

except Exception as e:
    db.rollback()
    print(f"Error seeding allowed transitions: {e}")
    raise

finally:
    db.close()
