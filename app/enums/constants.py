from enum import Enum
import enum


class UserRole(str, Enum):
    HR_ADMIN       = "HR_ADMIN"
    RECRUITER      = "RECRUITER"
    HIRING_MANAGER = "HIRING_MANAGER"


class PipelineStage(str, Enum):
    APPLIED       = "APPLIED"
    SCREENING     = "SCREENING"
    SHORTLISTED   = "SHORTLISTED"
    INTERVIEW     = "INTERVIEW"
    OFFER         = "OFFER"
    HIRED         = "HIRED"
    REJECTED      = "REJECTED"


class Jurisdiction(str, Enum):
    GLOBAL = "GLOBAL"
    EU     = "EU"
    US     = "US"
    IN     = "IN"
    
    
class ActionType(enum.Enum):
    JD_CREATED= "JD_CREATED"
    JD_UPDATED= "JD_UPDATED"
    JD_VERSION_CREATED= "JD_VERSION_CREATED"
    JD_CLOSED= "JD_CLOSED"

class EntityType(enum.Enum):
    JOB_DESCRIPTION= "JOB_DESCRIPTION"


# Resume storage prefix inside the S3 bucket
S3_RESUME_PREFIX = "resumes/"
S3_JD_PREFIX     = "job-descriptions/"

# Embedding dimensions for all-MiniLM-L6-v2
EMBEDDING_DIM = 384

# Default pagination
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE     = 100

# API prefix — all routes must be registered under this
API_PREFIX = "/airs"
