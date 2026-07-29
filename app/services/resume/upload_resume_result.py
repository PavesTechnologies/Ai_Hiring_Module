from dataclasses import dataclass

from app.models.candidates import Candidate, Resume


@dataclass
class UploadResumeResult:
    """
    Epic 3 (M05-E03) Phase C2 — ResumeUploadService.upload()'s return
    contract. A dedicated object instead of a bare Resume or a positional
    tuple, so later phases can add fields here without changing the
    method's signature again.

    matched_resume/matched_candidate are populated whenever an exact
    duplicate was found and resolved (either resolution), regardless of
    whether `resume` ends up being that same matched resume
    ("use_existing") or a brand-new version created under its candidate
    ("upload_anyway").
    """

    resume: Resume
    requires_processing: bool
    duplicate_found: bool = False
    matched_resume: Resume | None = None
    matched_candidate: Candidate | None = None
