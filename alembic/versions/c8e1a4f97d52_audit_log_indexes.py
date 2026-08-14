"""audit_log indexes

Epic 3 Fix 3: audit_log had zero indexes beyond the primary key - every
filter a search/export feature (or any of the 3 existing real readers)
would need was a full table scan. Table is still small (423 rows as of
this migration) so this is cheap to add now, before volume makes it
expensive to add live.

Query-shape evidence gathered before designing these (not a guess):
  - AuditRepository.get_campaign_scoring_history: WHERE campaign_id = ?
    AND action_type IN (...) ORDER BY created_at DESC
  - AuditRepository.get_latest_entry: WHERE campaign_id = ? AND
    action_type = ? ORDER BY created_at DESC LIMIT 1
  - CampaignRepository.get_audit_entries (backs GET /campaigns/{id}/timeline):
    WHERE campaign_id = ? ORDER BY created_at DESC
All 3 real readers filter on campaign_id and sort by created_at DESC;
none filter on entity_type/entity_id or actor_id today (those two indexes
below are added ahead of the search/export feature this precedes, not
because a real query needs them yet).

idx_audit_log_campaign_id_created_at (campaign_id, created_at) - the one
index that actually matches today's real query shapes: campaign_id
narrows first, created_at is already sorted for the ORDER BY ... DESC
every real reader uses, and action_type (only ever filtered alongside
campaign_id, never alone) is cheap to apply as a residual check afterward.
A standalone campaign_id index would be redundant once this composite
exists (leftmost-prefix rule already covers campaign_id-only lookups).

idx_audit_log_created_at (created_at alone) - for a global time-range
query with no campaign_id filter (e.g. a future cross-campaign admin
search/export) - NOT covered by the composite above, since Postgres can't
use a leftmost-prefix restriction without a fixed campaign_id.

idx_audit_log_entity_type_entity_id (entity_type, entity_id) - "everything
that happened to this specific candidate/campaign/etc" - the natural
detail-view audit trail shape, anticipatory (no real reader uses this yet).

idx_audit_log_actor_id (actor_id) - "everything this user did" - also
anticipatory, and useful for auditing the actor_id mis-attribution class
of bug fixed in Epic 3 Fix 2.

Deliberately NOT adding a standalone action_type index - only ~15-20
distinct values today and CANDIDATE_SKILL_MATCHED alone is already over
half of all rows, so a low-cardinality/skewed single-column index here
would have poor selectivity and isn't worth the write overhead. If
action_type filtering in isolation ever matters, it should be a composite
with something more selective, not a standalone index.

No postgresql_concurrently=True: no precedent for concurrent index
creation exists anywhere in this codebase's migration history, and
alembic/env.py wraps every migration in a transaction
(context.begin_transaction()) with no autocommit_block() usage anywhere -
introducing that pattern for a 423-row table isn't worth the change. Plain
op.create_index(), matching every existing index in this codebase.

Purely additive: no column change, no data change, no other table touched.

Revision ID: c8e1a4f97d52
Revises: d3a86f21c9e4
Create Date: 2026-08-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8e1a4f97d52'
down_revision: Union[str, Sequence[str], None] = 'd3a86f21c9e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'idx_audit_log_campaign_id_created_at',
        'audit_log',
        ['campaign_id', 'created_at'],
        unique=False,
    )
    op.create_index(
        'idx_audit_log_created_at',
        'audit_log',
        ['created_at'],
        unique=False,
    )
    op.create_index(
        'idx_audit_log_entity_type_entity_id',
        'audit_log',
        ['entity_type', 'entity_id'],
        unique=False,
    )
    op.create_index(
        'idx_audit_log_actor_id',
        'audit_log',
        ['actor_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_audit_log_actor_id', table_name='audit_log')
    op.drop_index('idx_audit_log_entity_type_entity_id', table_name='audit_log')
    op.drop_index('idx_audit_log_created_at', table_name='audit_log')
    op.drop_index('idx_audit_log_campaign_id_created_at', table_name='audit_log')
