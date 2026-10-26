"""resumes.parse_text_hashes: every hash of the resume's redacted text a parse has seen

Revision ID: c9e1a3b5d7f2
Revises: b7d9f1a3c5e8
Create Date: 2026-10-26

The parse cache (resume_parse_cache) is keyed by the hash of a resume's redacted
text but holds no resume id, so erasure could only find a resume's cache entries
through hashes its derived rows happened to record. The pipeline now records the
hash on the resume right after redaction. Existing resumes are backfilled from
the hashes their skill evidence, provisional scans and core-skill confirmations
recorded. Additive: a new nullable column, filled - no existing value changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9e1a3b5d7f2"
down_revision: Union[str, Sequence[str], None] = "b7d9f1a3c5e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("parse_text_hashes", postgresql.ARRAY(sa.String(64)), nullable=True))
    op.execute("""
        UPDATE resumes r SET parse_text_hashes = h.hashes
        FROM (
            SELECT resume_id, array_agg(DISTINCT text_hash ORDER BY text_hash) AS hashes
            FROM (
                SELECT resume_id, text_hash FROM resume_skill_evidence
                UNION SELECT resume_id, text_hash FROM resume_provisional_skill_scans
                UNION SELECT resume_id, text_hash FROM core_skill_confirmations
            ) recorded
            WHERE text_hash IS NOT NULL AND text_hash <> ''
            GROUP BY resume_id
        ) h
        WHERE h.resume_id = r.id
    """)


def downgrade() -> None:
    op.drop_column("resumes", "parse_text_hashes")
