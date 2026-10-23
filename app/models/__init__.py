from .identity import User, Organization

from .embeddings import EmbeddingModelVersion, ResumeEmbedding

from .jd.job_descriptions import JobDescription, JDEmbedding, JDCalibrationVersion
from .jd.profile_templates import JDProfileTemplate

from .campaigns import HiringCampaign

from .compliance import AuditLog

from .education import EducationFieldRelation


from .candidate_notes import CandidateNote

from .ai_pipeline import PromptVersion
from .llm_call_log import LLMCallLog

from .prompt_template import PromptTemplate, PromptTemplateStatus

from .ai_provider import AIProviderConfig, AIProviderModel

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
    StageTransitionLog,
)

from .async_tasks import (
    CeleryTaskLog,
    DeadLetterQueue,
    BulkUploadJob,
    BulkUploadJobFile,
    DocumentProcessingStageExecution,
    DocumentType,
    ProcessingStage,
    StageExecutionStatus,
)

from .upload_decisions import UploadDecision

from .skills import (
    SkillOntology,
    UnknownSkill,
    SkillSuggestion,
    JDSkill,
    JDUnknownSkill,
    CandidateSkill,
)

from .scoring_v2 import (
    SkillSense,
    SkillScanTerm,
    ResumeSkillEvidence,
    ResumeParseCache,
    CoreSkillConfirmation,
    CandidateScoringResult,
    ResumeParseVersion,
    ResumeProvisionalSkillScan,
    SkillPatternRule,
    ScoringShadowRanking,
)

from .email import (
    EmailTemplate,
    EmailNotification,
    EmailTriggerEvent,
    EmailNotificationStatus,
)