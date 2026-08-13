from .identity import User, Organization

from .embeddings import EmbeddingModelVersion, ResumeEmbedding

from .jd.job_descriptions import JobDescription, JDEmbedding

from .campaigns import HiringCampaign

from .compliance import AuditLog

from .saved_views import UserSavedView

from .candidate_notes import CandidateNote

from .ai_pipeline import PromptVersion

from .prompt_template import PromptTemplate, PromptTemplateStatus

from app.models.config import *

from .candidates import (
    Candidate,
    Resume,
    ResumeParseAttempt,
)

from .pipeline import (
    CampaignCandidate,
    CampaignCandidateAIEvaluation,
    AllowedTransition,
    CampaignCandidateStageHistory,
)

from .async_tasks import (
    CeleryTaskLog,
    DeadLetterQueue,
    BulkUploadJob,
    DocumentProcessingStageExecution,
    DocumentType,
    ProcessingStage,
    StageExecutionStatus,
)

from .skills import (
    SkillOntology,
    UnknownSkill,
    SkillSuggestion,
    JDSkill,
    JDUnknownSkill,
    CandidateSkill,
)

from .email import (
    EmailTemplate,
    EmailNotification,
    EmailTriggerEvent,
    EmailNotificationStatus,
)