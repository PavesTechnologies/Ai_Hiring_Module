import re
from dataclasses import dataclass

from app.services.pii.pii_types import PIIType


@dataclass(frozen=True)
class PatternSpec:
    """
    One compiled pattern contributing to a PIIType's detection. is_url_like
    controls whether a match gets trailing-punctuation trimmed (so
    "(linkedin.com/in/john)." doesn't swallow the closing paren/period).
    requires_context restricts a pattern to only fire when a contextual
    keyword (see PORTFOLIO_CONTEXT_KEYWORDS) appears shortly before the
    match — used to keep the generic bare-domain PORTFOLIO pattern from
    flagging every company/product URL mentioned in work history.
    """

    pattern: re.Pattern
    is_url_like: bool = False
    requires_context: bool = False


_EMAIL_PATTERNS = [
    PatternSpec(re.compile(
        r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}"
    )),
]

_PHONE_PATTERNS = [
    # +91 9876543210, +1 (555) 123-4567, +1-555-123-4567 x123
    PatternSpec(re.compile(
        r"(?<!\d)\+\d{1,3}[\s.-]?\(?\d{2,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?:\s?(?:ext\.?|x)\s?\d{1,6})?(?!\d)",
        re.IGNORECASE,
    )),
    # (555) 123-4567
    PatternSpec(re.compile(r"(?<!\d)\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\d)")),
    # 555-123-4567 / 555.123.4567 / 555 123 4567
    PatternSpec(re.compile(r"(?<!\d)\d{3,4}[\s.-]\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{1,4})?(?!\d)")),
    # bare digit run: 5551234567, 919876543210
    PatternSpec(re.compile(r"(?<!\d)\d{9,13}(?!\d)")),
]

_LINKEDIN_PATTERNS = [
    PatternSpec(re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/\S+", re.IGNORECASE), is_url_like=True),
]

_GITHUB_PATTERNS = [
    PatternSpec(re.compile(r"(?:https?://)?(?:www\.)?github\.com/\S+", re.IGNORECASE), is_url_like=True),
]

PORTFOLIO_CONTEXT_KEYWORDS = (
    "portfolio", "website", "personal site", "personal website", "blog", "site:", "web:",
)
PORTFOLIO_CONTEXT_WINDOW = 30

_PORTFOLIO_PATTERNS = [
    # explicit scheme or www. -- always redacted, no surrounding keyword needed
    PatternSpec(
        re.compile(r"(?:https?://\S+|www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\S*)", re.IGNORECASE),
        is_url_like=True,
    ),
    # bare domain (no protocol/www) -- only near a contextual keyword, to
    # avoid flagging every company/product URL mentioned in work history.
    PatternSpec(
        re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z]{2,}(?:\.[A-Za-z]{2,})?(?:/\S*)?", re.IGNORECASE),
        is_url_like=True,
        requires_context=True,
    ),
]

# Priority order matters: earlier types claim their character spans first, so
# a later type's pattern (e.g. PORTFOLIO's generic bare-domain match) can
# never re-claim text already matched as EMAIL/LINKEDIN/GITHUB. Extending
# detection = append a PatternSpec to the relevant list below, or add a new
# PIIType + registry entry -- PIIDetectionService's scan loop never changes.
PATTERN_REGISTRY: dict[PIIType, list[PatternSpec]] = {
    PIIType.EMAIL: _EMAIL_PATTERNS,
    PIIType.PHONE: _PHONE_PATTERNS,
    PIIType.LINKEDIN: _LINKEDIN_PATTERNS,
    PIIType.GITHUB: _GITHUB_PATTERNS,
    PIIType.PORTFOLIO: _PORTFOLIO_PATTERNS,
}

DETECTION_ORDER = (PIIType.EMAIL, PIIType.PHONE, PIIType.LINKEDIN, PIIType.GITHUB, PIIType.PORTFOLIO)

TRAILING_STRIP_CHARS = ")]}>'\".,;:!?"
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
