"""
The invariants every allowed_transitions change must keep (settings phase 2, agreed 2026-11-12).

Checked on the table as it would be after the whole batch of changes, so a batch either keeps them all or is
refused with every violation listed:

  1. Roles: only HR_ADMIN, RECRUITER and HIRING_MANAGER can be granted, and only where a backend route accepts
     that role for the move (ROUTES_FOR_ROLE). SYSTEM is code-owned: the automated-move list stays in code
     (StageTransitionService._SYSTEM_MOVES), so the editor never adds or removes SYSTEM. A row always keeps
     at least one role.
  2. Reachable: every stage stays reachable from UPLOADED.
  3. Terminal: no new edges out of SELECTED or REJECTED (existing ones can be edited, not multiplied).
  4. Protected: rows the system relies on can't be removed - every SYSTEM row (automated moves), the
     REJECTED -> SCREENING rows (re-parse) and the rows into UPLOADED (re-upload / resubmission).
"""
from collections import defaultdict

from app.models.pipeline import PipelineStage as S

SYSTEM = "SYSTEM"
HUMAN_ROLES = ("HR_ADMIN", "RECRUITER", "HIRING_MANAGER")
# The backend routes through which each role makes a stage move (app/api/routes/candidate_actions_routes.py).
# tests/services/settings/test_transition_rules.py checks these against the routes' require_roles.
ROUTES_FOR_ROLE = {
    "HR_ADMIN": {"bulk-stage-move": None, "reject": S.REJECTED},       # None: any target stage
    "RECRUITER": {"stage-move": None, "reject": S.REJECTED},
    "HIRING_MANAGER": {"stage-move": None},
}
TERMINAL_STAGES = frozenset({S.SELECTED, S.REJECTED})
START = S.UPLOADED


def _stage(value):
    return value if isinstance(value, S) or value is None else S(value)


def protected_reason(row: dict) -> str | None:
    """Why this row can't be removed, or None."""
    if SYSTEM in row["allowed_roles"]:
        return "Automated scoring makes this move."
    if row["from_stage"] == S.REJECTED and row["to_stage"] == S.SCREENING:
        return "Re-parsing a rejected candidate's resume uses this move."
    if row["to_stage"] == S.UPLOADED:
        return "Re-uploading a candidate's resume uses this move."
    return None


def role_allowed(role: str, to_stage) -> bool:
    routes = ROUTES_FOR_ROLE.get(role)
    return bool(routes) and any(target is None or target == to_stage for target in routes.values())


def violations(rows: list[dict], *, added_keys=frozenset()) -> list[str]:
    """Every invariant the table (rows as dicts) breaks. added_keys: (prev, from, to) of rows added now."""
    problems = []
    seen = set()
    for row in rows:
        key = (row["previous_stage"], row["from_stage"], row["to_stage"])
        label = describe(row)
        if key in seen:
            problems.append(f"{label}: this move already exists.")
        seen.add(key)
        roles = row["allowed_roles"]
        if not roles:
            problems.append(f"{label}: give at least one role, or remove the move.")
        for role in roles:
            if role == SYSTEM:
                continue
            if role not in HUMAN_ROLES:
                problems.append(f"{label}: {role} isn't a role that can move candidates.")
            elif not role_allowed(role, row["to_stage"]):
                problems.append(f"{label}: no screen lets a {role.replace('_', ' ').lower()} make this move.")
        if key in added_keys and row["from_stage"] in TERMINAL_STAGES:
            problems.append(f"{label}: {row['from_stage'].value.title()} is a final stage - no new moves out of it.")

    reachable, frontier = {START}, [START]
    edges = defaultdict(set)
    for row in rows:
        if row["allowed_roles"]:
            edges[row["from_stage"]].add(row["to_stage"])
    while frontier:
        for nxt in edges[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    for stage in S:
        if stage not in reachable:
            problems.append(f"{stage.value.replace('_', ' ').title()} could no longer be reached from Uploaded.")
    return list(dict.fromkeys(problems))


def describe(row: dict) -> str:
    prev = row["previous_stage"]
    base = f"{row['from_stage'].value} -> {row['to_stage'].value}"
    return f"{base} (arrived from {prev.value})" if prev is not None else base
