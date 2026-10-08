"""Quote checks shared by every LLM step that must cite the resume (inferred skills, core-skill confirmation)."""
import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Evidence is compared on its opening words - long quotes get paraphrased or
# truncated at the end more often than at the start.
_EVIDENCE_PREFIX_CHARS = 60


def _squash(text: str | None) -> str:
    return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def evidence_found_in(evidence: str | None, resume_text: str | None) -> bool:
    """
    True when an inferred skill's evidence really appears in the resume -
    case/punctuation/whitespace-insensitive. LLMs will "quote" plausible
    evidence that isn't there (observed: the prompt's own example sentences
    returned as a candidate's evidence), so an inference only counts when its
    quote can be found.
    """
    # Whole words only, up to ~60 characters - cutting mid-word would make
    # the whole-word comparison below fail on a genuine quote.
    words, length = [], 0
    for word in _squash(evidence).split():
        if words and length + 1 + len(word) > _EVIDENCE_PREFIX_CHARS:
            break
        words.append(word)
        length += len(word) + (1 if len(words) > 1 else 0)
    needle = " ".join(words)
    if len(needle) < 3:
        return False
    return f" {needle} " in f" {_squash(resume_text)} "
