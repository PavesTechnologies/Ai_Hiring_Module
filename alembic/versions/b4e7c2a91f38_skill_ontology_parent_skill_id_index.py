"""Index skill_ontology.parent_skill_id

The CHILD/GRANDCHILD/SIBLING hierarchy tiers in deterministic candidate
scoring (CandidateScoringService.build_mandatory_skill_breakdown) filter
skill_ontology by parent_skill_id on every call - this self-referential FK
column has never had a dedicated index (only aliases GIN and embedding
ivfflat indexes exist on this table), so every one of those lookups is a
sequential scan. Batching those lookups into fewer round trips (see the
candidate_scoring_service.py N+1 fix landing alongside this migration)
still benefits from this index on each of the batched
`parent_skill_id IN (...)` queries.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e7c2a91f38"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_skill_ontology_parent_skill_id",
        "skill_ontology",
        ["parent_skill_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_skill_ontology_parent_skill_id", table_name="skill_ontology")
