import re

from rapidfuzz import fuzz

# Weight of a preferred capability relative to a required one in the
# aggregate functional score.
_PREFERRED_CAPABILITY_WEIGHT = 0.5

_STOPWORDS = {
    "a", "an", "and", "or", "of", "the", "for", "to", "in", "on", "with", "by", "at", "as", "via",
}
_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "es", "ed", "s")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#]*")
_CHUNK_SPLIT_RE = re.compile(r"[.;\n\r•▪●|]+")
_PARENTHETICAL_RE = re.compile(r"\(([^)]*)\)")
_MAX_EVIDENCE_CHARS = 200


def _stem(token: str) -> str:
    token = token.lower()
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            break
    if len(token) > 4 and token[-1] == token[-2]:
        token = token[:-1]
    return token


def _content_tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(text) if token.lower() not in _STOPWORDS and len(token) > 1]


def _tokens_match(capability_stem: str, evidence_stem: str) -> bool:
    if capability_stem == evidence_stem:
        return True
    shortest = min(len(capability_stem), len(evidence_stem))
    if shortest >= 4:
        prefix = 0
        for left, right in zip(capability_stem, evidence_stem):
            if left != right:
                break
            prefix += 1
        if prefix >= 4 and prefix >= 0.8 * shortest:
            return True
    return shortest >= 5 and fuzz.ratio(capability_stem, evidence_stem) >= 88


class DomainCapabilityMatchingService:
    """
    Scores the JD's domain capabilities (business processes / functional
    areas, e.g. "Demand Management") against the free text of a resume -
    summary, work-experience titles/descriptions, project descriptions.
    Resumes describe this work in their own words ("demand intake and
    approval flows"), so matching is fuzzy and per-sentence rather than a
    literal string comparison.

    Never gates: the result's `passed` is always True. The score only feeds
    the functional component of the deterministic score blend.

    Per capability, the score (0-1) is the best fraction of the
    capability's content words found in any single resume sentence (after
    light stemming + fuzzy token matching), or 1.0 if its acronym appears
    as a whole word.
    """

    def evaluate(self, jd_extracted_json: dict | None, resume_parsed_json: dict | None) -> dict:
        jd_json = jd_extracted_json if isinstance(jd_extracted_json, dict) else {}
        capabilities = jd_json.get("domain_capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        required = self._capability_list(capabilities.get("required"))
        preferred = self._capability_list(capabilities.get("preferred"))

        if not required and not preferred:
            return self._result(applicable=False, skipped=True, data_missing=False, score=None)

        chunks = self._evidence_chunks(resume_parsed_json if isinstance(resume_parsed_json, dict) else {})
        if not chunks:
            return self._result(applicable=False, skipped=False, data_missing=True, score=None)

        stemmed_chunks = [(chunk, {_stem(token) for token in _content_tokens(chunk)}, chunk.lower()) for chunk in chunks]
        required_results = [self._score_capability(value, stemmed_chunks) for value in required]
        preferred_results = [self._score_capability(value, stemmed_chunks) for value in preferred]

        weighted_sum = sum(item["score"] for item in required_results) + _PREFERRED_CAPABILITY_WEIGHT * sum(
            item["score"] for item in preferred_results
        )
        weight_total = len(required_results) + _PREFERRED_CAPABILITY_WEIGHT * len(preferred_results)
        score = round(weighted_sum / weight_total * 100, 2)

        return self._result(
            applicable=True, skipped=False, data_missing=False, score=score,
            required=required_results, preferred=preferred_results,
        )

    @staticmethod
    def _capability_list(values) -> list[str]:
        if not isinstance(values, list):
            return []
        return [value.strip() for value in values if isinstance(value, str) and value.strip()]

    @staticmethod
    def _result(applicable, skipped, data_missing, score, required=None, preferred=None) -> dict:
        return {
            "applicable": applicable,
            "skipped": skipped,
            "data_missing": data_missing,
            "passed": True,
            "score": score,
            "required": required or [],
            "preferred": preferred or [],
        }

    @staticmethod
    def _evidence_chunks(resume_json: dict) -> list[str]:
        texts: list[str] = []
        if isinstance(resume_json.get("summary"), str):
            texts.append(resume_json["summary"])
        for entry in resume_json.get("work_experience") or []:
            if isinstance(entry, dict):
                texts.extend(value for value in (entry.get("title"), entry.get("description")) if isinstance(value, str))
        for entry in resume_json.get("projects") or []:
            if isinstance(entry, dict):
                texts.extend(value for value in (entry.get("name"), entry.get("description")) if isinstance(value, str))

        chunks = []
        for text in texts:
            chunks.extend(chunk.strip() for chunk in _CHUNK_SPLIT_RE.split(text) if chunk.strip())
        return chunks

    @staticmethod
    def _score_capability(capability: str, stemmed_chunks: list[tuple[str, set[str], str]]) -> dict:
        acronyms = [
            value.strip() for value in _PARENTHETICAL_RE.findall(capability)
            if value.strip() and " " not in value.strip()
        ]
        base = _PARENTHETICAL_RE.sub(" ", capability)
        capability_stems = list(dict.fromkeys(_stem(token) for token in _content_tokens(base)))

        best_score, best_chunk = 0.0, None
        for chunk, chunk_stems, chunk_lower in stemmed_chunks:
            if any(re.search(rf"\b{re.escape(acronym.lower())}\b", chunk_lower) for acronym in acronyms):
                best_score, best_chunk = 1.0, chunk
                break
            if not capability_stems:
                continue
            matched = sum(
                1 for cap_stem in capability_stems
                if any(_tokens_match(cap_stem, chunk_stem) for chunk_stem in chunk_stems)
            )
            chunk_score = matched / len(capability_stems)
            if chunk_score > best_score:
                best_score, best_chunk = chunk_score, chunk
                if best_score == 1.0:
                    break

        return {
            "capability": capability,
            "score": round(best_score, 2),
            "evidence": best_chunk[:_MAX_EVIDENCE_CHARS] if best_chunk and best_score > 0 else None,
        }
