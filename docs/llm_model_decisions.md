# LLM model decisions

Which model runs which AI purpose, why, and what evidence the choice rests on.
Newest first. Each entry stays short; the raw numbers live with the script that
produced them.

---

## Extraction stays on gemini-3.5-flash-lite (decided 2026-10-13)

**Decision:** keep `gemini-3.5-flash-lite` for extraction (resume and JD parsing,
core-skill confirmation). `gemini-3.8-flash` was compared and ruled out. Backup
for extraction: `gemini-3.1-flash-lite` (replacing the deprecated
`gemini-3.5-flash`, which Google routes to 3.6 Flash at $1.50 / $7.50 per 1M);
set in AI provider settings, not in code.

**Comparison** (`scripts/extraction_model_comparison.py`, campaign "AI
transformation lead Sep", each model alone with no backup, live config
unchanged, nothing written). It was planned as 6 resumes x 3 runs per model and
stopped after the first resume when the Google key returned `DAILY_LIMIT` for
3.8 Flash on its third parse.

| Model | Runs | Core confirmed | v2 score | Outcome | Skills found | Parse time |
|---|---|---|---|---|---|---|
| 3.5 Flash-Lite | 3 | 2/4 each | 46.89, 42.50, 46.89 | REJECT x3 | 6, 5, 6 | 10-13 s |
| 3.8 Flash | 2 of 3 | 2/4 each | 46.89, 46.89 | REJECT x2 | 9, 8 | ~200 s (one run 283 s, two dropped connections) |

**Why:** outcomes matched and stayed the same between runs. 3.8 Flash found a few
more skills, but that changed neither the core count nor the outcome. Its parse
time (~200 s against ~11 s) and the daily limit hit after three requests rule it
out for the pipeline, and it costs more: $0.75 / $3.75 per 1M input / output
tokens until 2026-12-31, then $1.50 / $7.50, against $0.30 / $2.50 for
3.5 Flash-Lite. The evidence is one resume only; token counts were not kept for
this run.

**Follow-ups made alongside:** the Gemini adapter records each call's token usage
and treats a dropped connection or timeout as a retryable UNAVAILABLE error. The
comparison script saves its report after every run and stops on a daily, credit
or quota limit.
