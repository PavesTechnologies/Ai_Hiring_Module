from app.services.pii.pii_patterns import (
    DETECTION_ORDER,
    MAX_PHONE_DIGITS,
    MIN_PHONE_DIGITS,
    PATTERN_REGISTRY,
    PORTFOLIO_CONTEXT_KEYWORDS,
    PORTFOLIO_CONTEXT_WINDOW,
    TRAILING_STRIP_CHARS,
)
from app.services.pii.pii_types import PIIFinding, PIIType


class PIIDetectionService:
    """
    Deterministic (regex/URL-pattern only, no AI) PII detector for Email,
    Phone, LinkedIn, GitHub, and Portfolio/personal-site URLs. Stateless and
    thread-safe -- every compiled pattern lives in pii_patterns.py as a
    module-level constant, so instances share no mutable state and one
    instance can be reused freely across concurrent Celery workers.

    Scans in a fixed priority order (see DETECTION_ORDER) so a
    lower-priority pattern (e.g. PORTFOLIO's generic bare-domain match) can
    never re-claim a span already matched by a higher-priority type -- this
    is what keeps a LinkedIn URL from also being counted as a portfolio
    link, and what naturally dedupes overlapping matches from different
    patterns of the same type.
    """

    def detect(self, text: str) -> list[PIIFinding]:
        if not text:
            return []

        findings: list[PIIFinding] = []
        claimed: list[tuple[int, int]] = []

        for pii_type in DETECTION_ORDER:
            for spec in PATTERN_REGISTRY[pii_type]:
                for match in spec.pattern.finditer(text):
                    start, end = match.start(), match.end()

                    if spec.is_url_like:
                        end = self._trim_trailing_punctuation(text, start, end)
                        if end <= start:
                            continue

                    if self._overlaps_claimed(start, end, claimed):
                        continue

                    if pii_type == PIIType.PHONE and not self._is_plausible_phone(text[start:end]):
                        continue

                    if spec.requires_context and not self._has_nearby_context(text, start):
                        continue

                    claimed.append((start, end))
                    findings.append(
                        PIIFinding(pii_type=pii_type, start=start, end=end, matched_text=text[start:end])
                    )

        findings.sort(key=lambda f: f.start)
        return findings

    @staticmethod
    def _trim_trailing_punctuation(text: str, start: int, end: int) -> int:
        while end > start and text[end - 1] in TRAILING_STRIP_CHARS:
            end -= 1
        return end

    @staticmethod
    def _overlaps_claimed(start: int, end: int, claimed: list[tuple[int, int]]) -> bool:
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    @staticmethod
    def _is_plausible_phone(matched_text: str) -> bool:
        digit_count = sum(1 for ch in matched_text if ch.isdigit())
        return MIN_PHONE_DIGITS <= digit_count <= MAX_PHONE_DIGITS

    @staticmethod
    def _has_nearby_context(text: str, match_start: int) -> bool:
        window_start = max(0, match_start - PORTFOLIO_CONTEXT_WINDOW)
        window = text[window_start:match_start].lower()
        return any(keyword in window for keyword in PORTFOLIO_CONTEXT_KEYWORDS)
