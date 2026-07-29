from app.services.pii.pii_types import PIIFinding


class PIIRedactionService:
    """
    Deterministic, stateless redactor. Given the non-overlapping findings
    PIIDetectionService.detect() already produced, replaces each span with a
    placeholder ("[EMAIL]", "[PHONE]", ...) in a single linear pass -- O(n)
    in text length, no repeated string concatenation. Text outside redacted
    spans (including line breaks/structure) is untouched.
    """

    def redact(self, text: str, findings: list[PIIFinding]) -> str:
        if not findings:
            return text

        parts: list[str] = []
        cursor = 0
        for finding in sorted(findings, key=lambda f: f.start):
            if finding.start < cursor:
                # Defensive: detection already guarantees non-overlapping
                # spans, but never let an unexpected overlap corrupt output.
                continue
            parts.append(text[cursor:finding.start])
            parts.append(f"[{finding.pii_type.value}]")
            cursor = finding.end

        parts.append(text[cursor:])
        return "".join(parts)
