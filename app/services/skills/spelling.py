"""
British -> American spelling normalisation for skill matching (scoring v2).

Applied word by word to both sides of every comparison (catalogue terms and
resume text), so "Programme Management", "Change Management for
organisations" or "Data Modelling" match the catalogue's American spellings
exactly. Only for matching keys - the text shown to people is never changed.

Rules are deliberately conservative: suffix rules carry exception lists for
English words that merely end the same way (expertise, enterprise, advise,
selling, hour, ...), and the irregular spellings are an explicit list.
"""
import re

# Whole words.
_WORDS = {
    "programme": "program", "programmes": "programs",
    "catalogue": "catalog", "catalogues": "catalogs", "dialogue": "dialog", "analogue": "analog",
    "centre": "center", "centres": "centers", "metre": "meter", "fibre": "fiber", "litre": "liter",
    "theatre": "theater",
    "licence": "license", "licences": "licenses", "defence": "defense", "offence": "offense",
    "grey": "gray", "cheque": "check", "cheques": "checks", "enrolment": "enrollment",
    "fulfil": "fulfill", "fulfilment": "fulfillment", "judgement": "judgment", "ageing": "aging",
    "aluminium": "aluminum", "artefact": "artifact", "artefacts": "artifacts",
    # -our -> -or (never four/hour/your/tour/pour/flour/contour/detour)
    "behaviour": "behavior", "behaviours": "behaviors", "behavioural": "behavioral",
    "colour": "color", "colours": "colors", "favour": "favor", "favourite": "favorite",
    "labour": "labor", "honour": "honor", "harbour": "harbor", "neighbour": "neighbor",
    "flavour": "flavor", "humour": "humor", "endeavour": "endeavor", "rumour": "rumor", "vapour": "vapor",
    # doubled l (never selling/telling/spelling/filling/billing/rolling/polling/controlling/installing)
    "modelling": "modeling", "modelled": "modeled", "modeller": "modeler",
    "labelling": "labeling", "labelled": "labeled", "travelling": "traveling", "travelled": "traveled",
    "cancelling": "canceling", "cancelled": "canceled", "levelling": "leveling", "levelled": "leveled",
    "signalling": "signaling", "signalled": "signaled", "fuelling": "fueling", "counselling": "counseling",
    "tunnelling": "tunneling", "channelling": "channeling", "marshalling": "marshaling",
}

# -ise/-ised/-ising/-iser/-ises and -isation forms -> -ize, except these.
_ISE_EXCEPTIONS = {
    "expertise", "enterprise", "enterprises", "advise", "advised", "advising", "adviser", "advises",
    "exercise", "exercised", "exercising", "exercises", "premise", "premises", "franchise", "franchises",
    "franchised", "promise", "promised", "promising", "promises", "revise", "revised", "revising",
    "supervise", "supervised", "supervising", "supervises", "devise", "devised", "devising",
    "compromise", "compromised", "surprise", "surprised", "merchandise", "merchandising", "otherwise",
    "likewise", "clockwise", "concise", "precise", "wise", "rise", "risen", "rising", "arise", "arising",
    "raise", "raised", "raising", "noise", "cruise", "praise", "disguise", "televise", "televised",
    "improvise", "improvised", "comprise", "comprised", "comprising", "comprises", "apprise", "demise",
    "excise", "incise", "treatise", "paradise", "poise", "poised", "guise", "chastise", "despise",
    "circumcise", "reprise", "anise", "valise", "porpoise", "tortoise", "mortise",
}
_ISE_SUFFIXES = (("isation", "ization"), ("isations", "izations"), ("ising", "izing"), ("ised", "ized"),
                 ("iser", "izer"), ("isers", "izers"), ("ises", "izes"), ("ise", "ize"))
_YSE = re.compile(r"(anal|paral|catal|dial|electrol|hydrol)ys(e|ed|es|ing|er|ers)$")
_MIN_ISE_STEM = 3


def normalise_word(word: str) -> str:
    """American spelling of one lower-case word (unchanged when no rule applies)."""
    if word in _WORDS:
        return _WORDS[word]
    if _YSE.search(word):
        return _YSE.sub(lambda m: f"{m.group(1)}yz{m.group(2)}", word)
    if word in _ISE_EXCEPTIONS:
        return word
    if "isation" in word:  # organisational, optimisations, ... - no English word keeps "-isation"
        return word.replace("isation", "ization")
    for british, american in _ISE_SUFFIXES:
        if word.endswith(british) and len(word) - len(british) >= _MIN_ISE_STEM:
            return word[: -len(british)] + american
    return word
