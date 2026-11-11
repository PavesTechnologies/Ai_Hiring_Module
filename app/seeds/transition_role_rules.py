"""
Roles every allowed_transitions row must hold beyond its owner (2026-11-11, settings phase 1).

- HR_ADMIN on every RECRUITER-owned move: the bulk stage move (HR_ADMIN only) and the reject route
  (HR_ADMIN or RECRUITER) check the edge's roles, and no row held HR_ADMIN, so both always refused an
  HR admin. Hiring-manager-owned decisions stay the hiring manager's.
- SYSTEM on every move automated scoring makes (StageTransitionService._SYSTEM_MOVES and the full-scoring
  moves off a fallback HOLD). Those paths don't check roles today; the rows say so, and phase 2's
  transition editor protects SYSTEM edges from removal.

Roles are only ever added, never removed: an edited row keeps whatever else it holds.
"""
from app.models.pipeline import PipelineStage

HR_ADMIN = "HR_ADMIN"
RECRUITER = "RECRUITER"
SYSTEM = "SYSTEM"

# (from_stage, to_stage) -> previous stages whose rows the system uses (None = the wildcard row).
SYSTEM_EDGES: dict[tuple[PipelineStage, PipelineStage], frozenset] = {
    (PipelineStage.UPLOADED, PipelineStage.SCREENING): frozenset({None}),
    (PipelineStage.SCREENING, PipelineStage.REJECTED): frozenset({None}),
    (PipelineStage.SCREENING, PipelineStage.SHORTLISTED): frozenset({None}),
    (PipelineStage.SCREENING, PipelineStage.HOLD): frozenset({None}),
    # Full scoring finishes a candidate the no-LLM fallback held; that HOLD was entered from SCREENING.
    (PipelineStage.HOLD, PipelineStage.REJECTED): frozenset({None, PipelineStage.SCREENING}),
    (PipelineStage.HOLD, PipelineStage.SHORTLISTED): frozenset({None, PipelineStage.SCREENING}),
}


def required_roles(previous_stage, from_stage, to_stage, roles) -> list[str]:
    """The row's roles with the required ones added (order kept, additions appended)."""
    result = list(roles or [])
    if RECRUITER in result and HR_ADMIN not in result:
        result.append(HR_ADMIN)
    if previous_stage in SYSTEM_EDGES.get((from_stage, to_stage), frozenset()) and SYSTEM not in result:
        result.append(SYSTEM)
    return result


def reconcile_existing(db) -> list[tuple[str, str, str, list[str]]]:
    """Add the required roles to rows already in the table. Returns (prev, from, to, added) per changed row."""
    from app.models.pipeline import AllowedTransition
    changed = []
    for row in db.query(AllowedTransition).all():
        wanted = required_roles(row.previous_stage, row.from_stage, row.to_stage, row.allowed_roles)
        if wanted != list(row.allowed_roles or []):
            added = [r for r in wanted if r not in (row.allowed_roles or [])]
            row.allowed_roles = wanted
            changed.append((row.previous_stage.value if row.previous_stage else "ANY",
                            row.from_stage.value, row.to_stage.value, added))
    return changed
