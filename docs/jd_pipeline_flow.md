# JD Document Processing Pipeline — Flow, Methods & Weight/Confidence Logic

This document explains, stage by stage, what runs, which method/service owns it, and — the part most likely to trip people up — exactly what happens to a skill match's **confidence score** depending on whether it lands above or below the fuzzy-matching threshold.

---

## 1. End-to-End Flow

```
Client Request
   │
   ▼
VALIDATION            ─┐  synchronous, in the HTTP request
   │                    │  app/api/routes/jd_routes.py
STORAGE (file only)    ─┘  JDService.validate_upload_type / validate_and_store_file
   │
   ▼
Return 202 Accepted + task_id
   │
   ▼  (Celery: process_jd_document task_id=... — everything below runs in the background)
   │
TEXT_EXTRACTION        →  TextExtractionService.extract()
   │
   ▼
  [duplicate check — content_hash — only for file uploads, see §4]
   │
   ▼
TEXT_CLEANING          →  PreprocessingService.normalize()
   │
   ▼
AI_EXTRACTION          →  GeminiExtractionService.extract_raw()
   │
   ▼
JSON_VALIDATION        →  JDExtractionResponse.model_validate()
   │
   ▼
SKILL_NORMALIZATION    →  SkillNormalizationService.normalize_skills()   ← §3/§4 below
   │
   ▼
EMBEDDING_GENERATION   →  EmbeddingService.build_canonical_embedding_text() + generate_embedding()
   │
   ▼
PERSISTENCE            →  JDService.persist_processed_jd()   ← one DB transaction, §5
   │
   ▼
JobDescription row exists. Client polls GET /job-descriptions/processing-status/{task_id}.
```

Every stage above is wrapped by `StageExecutionService.run_stage(task_id, DocumentType.JD, STAGE, fn)`, which records `RUNNING → SUCCESS/FAILED` (plus timing) in `document_processing_stage_executions`. That's tracking only — it never touches business data.

---

## 2. Stage → Method Reference Table

| Stage | Method(s) | File |
|---|---|---|
| Validation | `JDService.validate_upload_type` | `app/services/jd/jd_service.py` |
| Storage | `JDService.validate_and_store_file` → `StorageService.upload_file` | `app/services/jd/jd_service.py`, `app/core/storage_service.py` |
| Text Extraction | `TextExtractionService.extract` (dispatches to `extract_pdf_text` / `extract_docx_text`) | `app/services/document_processing/text_extraction_service.py` |
| Text Cleaning | `PreprocessingService.normalize` | `app/services/ai/preprocessing_service.py` |
| AI Extraction | `GeminiExtractionService.extract_raw` | `app/services/extractions/gemini_extraction_service.py` |
| JSON Validation | `JDExtractionResponse.model_validate` (+ its own `field_validator`/`model_validator` rules) | `app/schemas/ai/jd_extraction_response.py` |
| Skill Normalization | `SkillNormalizationService.normalize_skills` → `_match_skill` (7-tier chain, §3) | `app/services/skills/skill_normalization_service.py` |
| Embedding Generation | `EmbeddingService.build_canonical_embedding_text` + `generate_embedding` | `app/services/ai/embedding_service.py` |
| Persistence | `JDService.persist_processed_jd` | `app/services/jd/jd_service.py` |

Orchestration itself lives in `JDProcessingPipeline.run()` (`app/services/jd/jd_processing_pipeline.py`), which calls each of the above through a `JDProcessingContext` (a plain data object each stage reads from / writes back onto).

---

## 3. Skill Normalization — The 7-Tier Matching Order

For **every** raw skill string extracted by the AI (from both `required_skills` and `preferred_skills`), `SkillNormalizationService._match_skill()` tries these tiers **in order**, stopping at the first one that produces a match:

| # | Tier | Method | What it checks |
|---|---|---|---|
| 1 | `EXACT` | `_match_exact` | Raw text equals a `SkillOntology.canonical_name` exactly (case-sensitive, byte-for-byte) |
| 2 | `ALIAS` | `_match_alias` | Raw text appears in a skill's `aliases` array, exactly |
| 3 | `CASE_INSENSITIVE` | `_match_case_insensitive` | Same as 1/2 but lower-cased on both sides |
| 4 | `RULE_BASED` | `_match_rule_based` → `_rule_normalize` | Both sides run through punctuation/whitespace normalization (`.`, `-`, `_`, `/` → space, collapse spaces) before comparing — catches things like `"Node.js"` vs `"node js"` |
| 5 | `FUZZY` | `_match_fuzzy` (RapidFuzz `fuzz.ratio`) | Similarity score 0–100 against every canonical name + alias in the catalog — **this is the tier with a threshold, see §4** |
| 6 | `SEMANTIC` | *(not implemented — explicitly deferred)* | Would use embedding cosine similarity against `SkillOntology.embedding` |
| 7 | `UNKNOWN` | — | Nothing matched — the skill is preserved, never discarded |

Tiers 1–4 either match or they don't — there's no "weight" involved, they're exact string comparisons under increasingly loose normalization. **Tier 5 (fuzzy) is the only tier with a numeric score and a threshold**, which is almost certainly what you're asking about — covered next.

---

## 4. The Threshold: What Happens Above vs. Below

```python
FUZZY_SCORE_THRESHOLD = 85.0   # app/services/skills/skill_normalization_service.py
```

For every skill that falls through tiers 1–4 unmatched, tier 5 computes:

```python
fuzzy_skill, fuzzy_score = self._match_fuzzy(raw_text, catalog)   # score is 0–100
if fuzzy_skill and fuzzy_score >= self.FUZZY_SCORE_THRESHOLD:
    return SkillMatchResult(raw_text, mandatory, fuzzy_skill.id, SkillMatchTier.FUZZY, fuzzy_score / 100)
return SkillMatchResult(raw_text, mandatory, None, SkillMatchTier.UNKNOWN, None)
```

### If the score is **≥ 85** (at or above threshold)

- Treated as a real match against the closest canonical skill.
- `SkillMatchResult.canonical_skill_id` = that skill's id.
- `SkillMatchResult.match_tier` = `FUZZY`.
- `SkillMatchResult.confidence` = `fuzzy_score / 100` (e.g. a score of 92.3 → confidence `0.923`).
- At Persistence, this becomes a **`JDSkill` row**: `canonical_skill_id` set, `match_tier = "FUZZY"`, `confidence = 0.923`, `mandatory` = whether it came from `required_skills` or `preferred_skills`.
- `SkillRepository.bump_occurrence_count()` increments that canonical skill's `occurrence_count` in `skill_ontology`.

### If the score is **< 85** (below threshold)

- Treated as **no match at all** — the closest candidate found by RapidFuzz is discarded entirely, not stored anywhere, not even at reduced confidence.
- Falls straight through to tier 7, `UNKNOWN`: `canonical_skill_id = None`, `confidence = None`.
- At Persistence, this does **not** create a `JDSkill` row. Instead:
  - `SkillRepository.upsert_unknown_skill(raw_text)` — get-or-create in `unknown_skills` by exact raw text, bumping `frequency` if it's been seen before (org-wide, across every JD, not just this one).
  - `SkillRepository.link_unknown_skill_to_jd(jd_id, unknown_skill.id)` — inserts into `jd_unknown_skills`, so *this specific JD* is traceable back to the unknown skill (without breaking the global dedup above).
- Nothing is ever silently dropped — every skill the AI extracted ends up in exactly one of `JDSkill` or `UnknownSkill`.

### Why 85, specifically

This was tuned empirically against real near-miss pairs, not picked arbitrarily:

| Pair | Score | Outcome at threshold 85 |
|---|---|---|
| `"Java Script"` vs `"JavaScript"` | 95.2 | ✅ matches (typo/spacing variant) |
| `"Reactjs"` vs `"React.js"` | 93.3 | ✅ matches |
| `"Kubernets"` vs `"Kubernetes"` | 94.7 | ✅ matches |
| `"Pyhton"` vs `"Python"` | 83.3 | ❌ falls to UNKNOWN (conservative — see note) |
| `"Java"` vs `"JavaScript"` | 57.1 | ❌ correctly rejected — these are different skills |
| `"SQL"` vs `"MySQL"` | 75.0 | ❌ correctly rejected |

Note the scorer is plain `fuzz.ratio`, deliberately **not** `fuzz.WRatio` — WRatio's partial-ratio component scores substring pairs like `"Java"`/`"JavaScript"` at ~90 (a false positive), which is why it isn't used here.

The one visible cost of 85 as the cutoff: very short words with a single-character typo (`"Pyhton"` → 83.3, `"Djnago"` → 83.3) land just under the line and get filed as `UNKNOWN` rather than auto-corrected. That's the deliberate trade-off — erring toward "flag as unknown for a human to review" rather than risk a wrong auto-match.

---

## 5. `weight` vs. `confidence` on `JDSkill` — don't confuse these

Two different numbers live on the same row, for two different purposes:

| Column | Set by | Meaning | Populated today? |
|---|---|---|---|
| `confidence` (`Float`) | The pipeline, from `SkillMatchResult.confidence` | How sure the *normalization match* is: always `1.0` for tiers 1–4, `score/100` for tier 5 (FUZZY) | ✅ always, for every `JDSkill` row |
| `weight` (`Numeric(5,2)`) | *(nothing yet)* | Business-set importance for the deterministic/semantic scoring engine — e.g. a recruiter marking a skill as 2× more important | ❌ reserved, left `NULL` by the automated pipeline |

If you ever see a `JDSkill.weight` value, it did not come from this pipeline — it would have to come from a future feature that lets a human explicitly set it. Don't read `weight` as a proxy for match quality; read `confidence` for that.

---

## 6. Persistence — One Transaction, All or Nothing

`JDService.persist_processed_jd()` does the following in a single DB transaction (one `commit()` at the end, `rollback()` on any exception):

1. Final duplicate re-check (`get_by_content_hash`) — safety net against the async race window.
2. Insert `JobDescription` (`parsed_skills` = full AI extraction dump; `required_skills` = `{"required": [...], "preferred": [...]}` raw lists).
3. For each `SkillMatchResult`:
   - Matched (tiers 1–5): insert `JDSkill` (`canonical_skill_id`, `mandatory`, `match_tier`, `confidence`) + bump `SkillOntology.occurrence_count`.
   - Unmatched (tier 7, `UNKNOWN`): upsert `UnknownSkill` + insert `JDUnknownSkill` link row.
4. Insert `JDEmbedding` (vector from Embedding Generation stage).
5. Write an `AuditLog` row (`JD_CREATED`).
6. Commit.

If **any** step fails, everything rolls back — no `JobDescription` row, no partial `JDSkill`/`UnknownSkill` rows, nothing.
