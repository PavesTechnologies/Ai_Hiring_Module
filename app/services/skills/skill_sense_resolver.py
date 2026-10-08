"""
Scoring v2: which sense of an ambiguous skill an occurrence means, from cue
words near it (skill_senses rows). Change Management next to "incident",
"CAB" or "ITIL" is the ITSM sense; next to "adoption" or "stakeholders" it
is the organisational one.

Resume side: the sense with the most cue hits in the window wins; none or
a tie leaves it unresolved (sense None) - never guessed.
JD side (resolve_jd_sense): cue words in the JD text, then in the role
title/domain; if still undecided, the skill's default sense marked
"assumed" so the shadow report can show it.
"""
from dataclasses import dataclass
from uuid import UUID

from app.services.skills.resume_skill_scanner import squash_words, term_key


@dataclass(frozen=True)
class SenseDef:
    skill_id: UUID
    sense_key: str
    label: str
    description: str
    cue_words: tuple[str, ...]
    is_default: bool = False
    window_lines: int = 1


@dataclass(frozen=True)
class SenseChoice:
    sense: str | None
    resolved: bool
    # "cue" (decided by cue words), "title" (JD role title/domain),
    # "assumed" (JD default), None (unresolved).
    basis: str | None = None


SENSE_BASIS_CUE = "cue"
SENSE_BASIS_TITLE = "title"
SENSE_BASIS_ASSUMED = "assumed"


class SkillSenseResolver:
    def __init__(self, senses: list[SenseDef]):
        self.by_skill: dict[UUID, list[SenseDef]] = {}
        for sense in senses:
            self.by_skill.setdefault(sense.skill_id, []).append(sense)

    def has_senses(self, skill_id: UUID) -> bool:
        return len(self.by_skill.get(skill_id, ())) >= 2

    def senses_for(self, skill_id: UUID) -> list[SenseDef]:
        return self.by_skill.get(skill_id, [])

    @staticmethod
    def _count(sense: SenseDef, window: str, own_term: str) -> int:
        own = squash_words(own_term)
        hits = 0
        for cue in sense.cue_words:
            key = term_key(cue)
            if key and f" {key} " in window and f" {key} " not in own:
                hits += 1
        return hits

    def _decide(self, skill_id: UUID, window_text: str, own_term: str) -> str | None:
        window = squash_words(window_text)
        scores = sorted(
            ((self._count(s, window, own_term), s.sense_key) for s in self.senses_for(skill_id)), reverse=True,
        )
        if not scores or scores[0][0] == 0 or (len(scores) > 1 and scores[0][0] == scores[1][0]):
            return None
        return scores[0][1]

    def resolve_occurrence(self, skill_id: UUID, lines: list[str], line_no: int, own_term: str) -> SenseChoice:
        """Sense of one resume occurrence, from the lines around it."""
        if not self.has_senses(skill_id):
            return SenseChoice(None, True)
        width = max((s.window_lines for s in self.senses_for(skill_id)), default=1)
        window = " ".join(lines[max(0, line_no - width): line_no + width + 1])
        sense = self._decide(skill_id, window, own_term)
        return SenseChoice(sense, sense is not None, SENSE_BASIS_CUE if sense else None)

    def resolve_snippet(self, skill_id: UUID, snippet: str, own_term: str) -> SenseChoice:
        if not self.has_senses(skill_id):
            return SenseChoice(None, True)
        sense = self._decide(skill_id, snippet, own_term)
        return SenseChoice(sense, sense is not None, SENSE_BASIS_CUE if sense else None)

    def resolve_jd_sense(self, skill_id: UUID, jd_text: str, own_term: str, title_text: str = "") -> SenseChoice:
        """JD side: cues in the JD text, then title/domain, then the default sense (assumed)."""
        if not self.has_senses(skill_id):
            return SenseChoice(None, True)
        sense = self._decide(skill_id, jd_text or "", own_term)
        if sense:
            return SenseChoice(sense, True, SENSE_BASIS_CUE)
        sense = self._decide(skill_id, title_text or "", own_term)
        if sense:
            return SenseChoice(sense, True, SENSE_BASIS_TITLE)
        default = next((s.sense_key for s in self.senses_for(skill_id) if s.is_default), None)
        return SenseChoice(default, default is not None, SENSE_BASIS_ASSUMED if default else None)
