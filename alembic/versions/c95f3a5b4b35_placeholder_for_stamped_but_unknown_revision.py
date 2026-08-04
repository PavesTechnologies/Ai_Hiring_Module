"""placeholder for stamped-but-unknown revision

The live `airs` database's alembic_version table is stamped at
'c95f3a5b4b35' - a revision ID that does not exist anywhere in this repo's
alembic/versions/, and does not appear in `git log --all -S` across the
entire history/reflog either. Same class of issue as a1c6d9f3e7b2 and
265912f5590a's own placeholder fix: someone ran `alembic stamp c95f3a5b4b35`
(or an out-of-band DB restore/clone carried this stamp) without ever
committing a matching migration file, and - unlike a1c6d9f3e7b2 - this repo
was ALSO multi-headed at the time (`alembic upgrade head` cannot run
against multiple heads at all), so migrations have been applied by hand
against this database, not via `alembic upgrade` (see
docs/resume_intake_implementation_log.md's Epic 2 note).

Because 'c95f3a5b4b35' was completely unreachable from any node in the
graph, `alembic current`/`alembic upgrade head` failed outright with
"Can't locate revision identified by 'c95f3a5b4b35'" BEFORE attempting any
DDL - this is why the previously-added a558bcbcdb92/ee6515ea0cf6 repair
(which unified the 8 then-existing heads and guarded-added the missing
composite-scoring objects) never actually ran here: there was no valid
starting point for Alembic to compute an upgrade path from.

Verification performed against the live database (direct psycopg2
inspection, since `alembic current` could not run) before writing this
file - every column/constraint/index/enum-value introduced by each of the
7 non-composite-scoring branch tips that existed at the time of the
a558bcbcdb92 merge was confirmed present and exactly matching:

- 3ae81a38abdc: resumes.original_filename, resumes.file_size_bytes -> EXISTS
- 3e7800c51995: pure bookkeeping merge, no DDL of its own
- 90b05f9f2aa1: audit_action_type_enum.CANDIDATE_DATA_ERASED -> EXISTS
- b4d8e1f6a3c7: bulk_upload_job_files.task_id -> EXISTS
- d4b7f1c8a3e6: uq_resume_embeddings_resume_model_version,
  uq_celery_task_log_idempotency_key -> BOTH EXIST
- d6b8e3a1f4c9: email_trigger_event_enum.UPLOAD_PERMANENTLY_FAILED -> EXISTS
- f5b8d2c4a917: processing_stage_enum.PII_DETECTION/PII_REDACTION -> BOTH EXIST
- e2c8a4f6b9d1: campaign_candidates.semantic_score_breakdown,
  audit_action_type_enum.SEMANTIC_SCORE_COMPUTED -> BOTH EXIST (added after
  a first `alembic upgrade head` attempt failed with DuplicateColumn on
  semantic_score_breakdown - this branch-tip's own direct ancestor,
  e2c8a4f6b9d1, had been hand-applied too but was missed in the first pass
  of this verification since it is not itself a head; the real, empirically
  confirmed boundary is between e2c8a4f6b9d1 and b1f4c9a2e7d3, not between
  d3f7b9c2a1e5 and its child branches)

By contrast, every object introduced by the composite-scoring branch
(b1f4c9a2e7d3/c2e6a1f8d4b7 - campaign_candidates.composite_score_computed_at,
candidate_composite_score_history, campaign_weight_configuration_history,
composite_score_trigger_source_enum, and both new audit_action_type_enum
values) was confirmed ABSENT. That branch is therefore NOT an ancestor of
this placeholder and is deliberately excluded from down_revision below -
it is instead merged in separately by a558bcbcdb92, which now descends from
(this revision, c2e6a1f8d4b7) so the still-outstanding composite-scoring
DDL gets picked up by ee6515ea0cf6's guarded repair on the next
`alembic upgrade head`, exactly as originally intended.

No unexplained schema was found beyond what's listed above, so this is a
no-op placeholder, not a guess at unknown DDL.

Revision ID: c95f3a5b4b35
Revises: 3ae81a38abdc, 3e7800c51995, 90b05f9f2aa1, b4d8e1f6a3c7, d4b7f1c8a3e6, d6b8e3a1f4c9, f5b8d2c4a917, e2c8a4f6b9d1
Create Date: 2026-08-04 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c95f3a5b4b35'
down_revision: Union[str, Sequence[str], None] = ('3ae81a38abdc', '3e7800c51995', '90b05f9f2aa1', 'b4d8e1f6a3c7', 'd4b7f1c8a3e6', 'd6b8e3a1f4c9', 'f5b8d2c4a917', 'e2c8a4f6b9d1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
