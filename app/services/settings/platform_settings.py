"""
The platform settings HR_ADMIN edits on the Settings page (settings phase 1, 2026-11-11).

Only keys in REGISTRY can be read or written through the settings API, each with its type, range and unit:
a value outside them is refused before anything is saved. Not here on purpose:
  - scoring-related keys (experience tolerance, equivalent experience, score-rejects, the match blocklist):
    they move into scoring versions (phase 3); the campaign default weights keep their own screen;
  - LLM and embedding keys (caps, retries, capability map, prices, embedding batch/retries): phase 4;
  - consent keys (CONSENT_VERSION, JURISDICTION_CONSENT_CONFIG): hidden until the consent work;
  - DEADLINE_CHECK_INTERVAL_HOURS: shown read-only with the schedules (beat reads it at start-up).

Every reader still reads platform_config per call, so a saved value applies from the next use.
"""
from dataclasses import dataclass

from app.enums.constants import DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    label: str
    help: str
    kind: str          # "int" | "float"
    minimum: float
    maximum: float
    default: float
    unit: str = ""
    warn_below: float | None = None
    warn_text: str = ""


UPLOADS, ALERTS, ANALYTICS, SKILLS, OPERATIONS = (
    "Uploads", "Alerts and SLAs", "Analytics and fairness", "Skill catalogue", "Exports, links and retries")
GROUP_ORDER = (UPLOADS, ALERTS, ANALYTICS, SKILLS, OPERATIONS)

REGISTRY: tuple[Setting, ...] = (
    Setting("RESUME_MAX_SIZE_MB", UPLOADS, "Largest resume file", "Uploads above this are refused.",
            "float", 1, 50, 10, "MB"),
    Setting("ZIP_MAX_SIZE_MB", UPLOADS, "Largest bulk-upload zip", "Zips above this are refused.",
            "float", 10, 2000, 500, "MB"),
    Setting("MAX_FILES_PER_ZIP", UPLOADS, "Most files in one zip", "Zips with more files are refused.",
            "int", 1, 1000, 200, "files"),

    Setting("CAP_WARNING_PERCENTAGE", ALERTS, "Campaign capacity warning",
            "Warn when a campaign has used this share of its candidate cap.", "float", 1, 100, 80, "%"),
    Setting("DEADLINE_WARNING_DAYS", ALERTS, "Deadline warning", "Warn this many days before a campaign closes.",
            "int", 1, 60, 3, "days"),
    Setting("SCREENING_SLA_HOURS", ALERTS, "Screening SLA", "A candidate in Screening longer than this is stalled.",
            "float", 1, 720, 48, "hours"),
    Setting("HM_REVIEW_SLA_DAYS", ALERTS, "Hiring manager review SLA",
            "A candidate in HM Review longer than this is stalled.", "int", 1, 90, 5, "days"),
    Setting("INTERVIEW_SLA_DAYS", ALERTS, "Interview SLA", "A candidate in Interview longer than this is stalled.",
            "float", 1, 90, 7, "days"),
    Setting("STALE_CAMPAIGN_DAYS", ALERTS, "Stale campaign", "A campaign with no activity for this long is flagged.",
            "int", 1, 365, 7, "days"),
    Setting("OVERRIDE_RATE_ALERT_THRESHOLD", ALERTS, "Override rate alert",
            "Alert when people override this share of a campaign's screening outcomes.", "float", 1, 100, 20, "%"),
    Setting("DETERMINISTIC_REJECTION_ALERT_THRESHOLD", ALERTS, "Screening rejection alert",
            "Alert when screening rejects this share of a campaign's candidates.", "float", 1, 100, 80, "%"),
    Setting("DEAD_TASK_ALERT_THRESHOLD", ALERTS, "Failed tasks alert",
            "Alert when a campaign has this many dead-lettered tasks.", "int", 1, 1000, 5, "tasks"),
    Setting("FRAUD_ALERT_THRESHOLD", ALERTS, "Fraud review alert",
            "Alert when a campaign has this many candidates in fraud review.", "int", 1, 1000, 3, "candidates"),
    Setting("CROSS_CAMPAIGN_SUBMISSION_ALERT_THRESHOLD", ALERTS, "Repeat submissions alert",
            "Alert when one candidate is submitted to this many campaigns within the window below.",
            "int", 2, 100, 3, "campaigns"),
    Setting("CROSS_CAMPAIGN_SUBMISSION_WINDOW_DAYS", ALERTS, "Repeat submissions window",
            "The window for the repeat submissions alert.", "int", 1, 365, 30, "days"),
    Setting("EMBEDDING_FAILURE_ALERT_THRESHOLD", ALERTS, "Embedding failure alert",
            "Email HR admins when this share of a campaign's screening candidates can't be embedded.",
            "float", 1, 100, 20, "%"),
    Setting("EMBEDDING_FAILURE_ALERT_REPEAT_HOURS", ALERTS, "Embedding alert repeat",
            "Don't repeat the embedding alert for the same campaign within this time.", "float", 1, 168, 24, "hours"),
    Setting("EMAIL_SEND_STUCK_ALERT_REPEAT_HOURS", ALERTS, "Stuck email alert repeat",
            "Don't repeat the stuck-email alert within this time.", "float", 1, 168, 24, "hours"),

    Setting("MIN_CANDIDATES_FOR_ANALYTICS", ANALYTICS, "Candidates before analytics",
            "Rejection analytics and JD calibration advice appear once a campaign has this many candidates.",
            "int", 1, 1000, DEFAULT_MIN_CANDIDATES_FOR_ANALYTICS, "candidates"),
    Setting("SKILL_MISMATCH_RATE_THRESHOLD", ANALYTICS, "Skill mismatch advice",
            "Suggest recalibrating the JD when this share of rejections is for missing skills.",
            "float", 1, 100, 60, "%"),
    Setting("EXPERIENCE_ONLY_RATE_THRESHOLD", ANALYTICS, "Experience-only advice",
            "Suggest reviewing the experience range when this share of rejections is for experience alone.",
            "float", 1, 100, 40, "%"),
    Setting("DETERMINISTIC_HIGH_REJECTION_THRESHOLD", ANALYTICS, "High screening rejections",
            "Flag the screening layer in analytics above this rejection rate.", "float", 1, 100, 60, "%"),
    Setting("SEMANTIC_HIGH_REJECTION_THRESHOLD", ANALYTICS, "High semantic rejections",
            "Flag the semantic layer in analytics above this rejection rate.", "float", 1, 100, 40, "%"),
    Setting("AI_HIGH_REJECTION_THRESHOLD", ANALYTICS, "High AI rejections",
            "Flag the AI evaluation in analytics above this rejection rate.", "float", 1, 100, 40, "%"),
    Setting("FAIRNESS_MIN_GROUP_SIZE", ANALYTICS, "Fairness report: smallest group",
            "Groups with fewer candidates are not shown, so no group points to a few people.",
            "int", 2, 1000, 10, "candidates", warn_below=5,
            warn_text="Groups this small can point to individual candidates."),

    Setting("SKILL_SIMILARITY_THRESHOLD", SKILLS, "Duplicate skill hint",
            "Warn that a new catalogue skill may duplicate an existing one above this name similarity.",
            "float", 50, 100, 90, "%"),
    Setting("UNKNOWN_SKILL_SUGGESTION_TOP_K", SKILLS, "Suggestions per unknown skill",
            "How many catalogue skills to suggest for an unknown skill.", "int", 1, 50, 10, "skills"),
    Setting("UNKNOWN_SKILL_SUGGESTION_RAPIDFUZZ_THRESHOLD", SKILLS, "Suggestion name similarity",
            "Suggest a catalogue skill whose name is at least this similar.", "float", 50, 100, 85, "%"),
    Setting("UNKNOWN_SKILL_SUGGESTION_SEMANTIC_THRESHOLD", SKILLS, "Suggestion meaning similarity",
            "Suggest a catalogue skill whose meaning is at least this similar (0 to 1).", "float", 0.5, 1, 0.80),

    Setting("EXPORT_ASYNC_THRESHOLD", OPERATIONS, "Background export above",
            "Exports with more rows than this run in the background and are linked when ready.",
            "int", 10, 100000, 500, "rows"),
    Setting("EXPORT_LINK_EXPIRY_HOURS", OPERATIONS, "Export link lifetime", "How long an export download link works.",
            "int", 1, 168, 24, "hours"),
    Setting("RESUME_DOWNLOAD_URL_EXPIRY_SECONDS", OPERATIONS, "Resume download link lifetime",
            "How long a resume download link works.", "int", 30, 3600, 300, "seconds"),
    Setting("RESUME_FRESHNESS_MAX_AGE_DAYS", OPERATIONS, "Talent Pool resume age",
            "Resumes older than this aren't offered for new campaigns from the Talent Pool.",
            "int", 30, 1095, 180, "days"),
    Setting("MAX_DLQ_REPLAYS_PER_TASK", OPERATIONS, "Dead-letter replays",
            "How many times one failed task may be replayed from the dead-letter queue.", "int", 1, 10, 3, "times"),
    Setting("MAX_EMAIL_RETRY_COUNT", OPERATIONS, "Email send attempts",
            "Attempts for an email that the mail service certainly didn't take, before it is dead-lettered.",
            "int", 1, 10, 4, "attempts"),
    Setting("SCORING_DECISION_RECOVERY_HOURS", OPERATIONS, "Undecided candidate recovery",
            "A candidate still undecided after scoring for this long is picked up again.", "float", 1, 72, 6, "hours"),
    Setting("OAUTH_TOKEN_REFRESH_BUFFER_SECONDS", OPERATIONS, "Calendar token refresh",
            "Refresh a calendar connection's token this long before it expires.", "int", 30, 3600, 300, "seconds"),
)
BY_KEY = {s.key: s for s in REGISTRY}


class InvalidSetting(ValueError):
    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


def _number(value):
    return int(value) if float(value).is_integer() else float(value)


def parse_stored(setting: Setting, raw):
    """The stored text as a number, or None when it is missing or unreadable (the reader's default applies)."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if setting.kind == "int" and value.is_integer() else value


def validate(values: dict) -> dict[str, int | float]:
    """{key: value} checked against the registry; raises InvalidSetting naming every problem."""
    if not isinstance(values, dict) or not values:
        raise InvalidSetting({"values": "Give at least one setting to change."})
    problems, clean = {}, {}
    for key, value in values.items():
        setting = BY_KEY.get(key)
        if setting is None:
            problems[key] = "This setting can't be changed here."
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems[key] = "Enter a number."
            continue
        if setting.kind == "int" and not float(value).is_integer():
            problems[key] = "Enter a whole number."
            continue
        if not setting.minimum <= value <= setting.maximum:
            problems[key] = f"Enter a value from {_number(setting.minimum)} to {_number(setting.maximum)}."
            continue
        clean[key] = int(value) if setting.kind == "int" else float(value)
    if problems:
        raise InvalidSetting(problems)
    return clean


def warnings_for(clean: dict) -> list[dict]:
    return [{"key": key, "message": BY_KEY[key].warn_text} for key, value in clean.items()
            if BY_KEY[key].warn_below is not None and value < BY_KEY[key].warn_below]


def stored_text(setting: Setting, value) -> str:
    return str(int(value)) if setting.kind == "int" else f"{float(value):g}"
