"""
The candidate's name is taken out of every text sent to the core-skill confirmation and
paraphrase votes: scoring never needs it, and a name can carry gender or community. The
resume parse's full_name (the name as written in the resume) is replaced by "[candidate]":

- the full name, in any case and spacing ("Priya Raman", "PRIYA  RAMAN");
- each name part of 3+ letters, written as a name ("Priya", "RAMAN", "Raman's") - not in
  lower case, so a surname like "Price" leaves "price optimisation" alone;
- never a part that is a catalogue skill term on its own ("Ruby" in "Ruby Sharma" stays
  where it stands alone; the full name is still masked).

Contact details are already redacted from the resume text before this (PII redaction).
"""
import re
from dataclasses import dataclass

from app.services.skills.resume_skill_scanner import term_key

PLACEHOLDER = "[candidate]"
_MIN_PART = 3
_REPEATED = re.compile(r"\[candidate\](?:\s+\[candidate\])+")


@dataclass(frozen=True)
class NameMask:
    patterns: tuple = ()

    @classmethod
    def build(cls, names, catalog_terms=()) -> "NameMask":
        protected = {term_key(t) for t in catalog_terms if t}
        fulls, parts = set(), set()
        for name in names or ():
            clean = " ".join(str(name or "").split())
            if not clean:
                continue
            fulls.add(clean)
            for part in re.split(r"[\s.\-'’]+", clean):
                if len(part) >= _MIN_PART and part.isalpha() and term_key(part) not in protected:
                    parts.add(part)
        patterns = []
        for full in sorted(fulls, key=len, reverse=True):
            spaced = r"\s+".join(re.escape(w) for w in full.split())
            patterns.append(re.compile(rf"(?<!\w){spaced}(?!\w)", re.IGNORECASE))
        for part in sorted(parts, key=len, reverse=True):
            # written as a name: Title case or upper case, never lower case
            forms = {part[:1].upper() + part[1:].lower(), part.upper(), part}
            alternatives = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
            patterns.append(re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)"))
        return cls(tuple(patterns))

    @classmethod
    def for_resume(cls, resume, catalog_terms=()) -> "NameMask":
        parsed = getattr(resume, "parsed_json", None) or {}
        name = parsed.get("full_name") if isinstance(parsed, dict) else getattr(parsed, "full_name", None)
        return cls.build([name] if isinstance(name, str) and name.strip() else [], catalog_terms)

    def apply(self, text: str | None) -> str | None:
        if not text or not self.patterns:
            return text
        for pattern in self.patterns:
            text = pattern.sub(PLACEHOLDER, text)
        return _REPEATED.sub(PLACEHOLDER, text)


NO_MASK = NameMask()
