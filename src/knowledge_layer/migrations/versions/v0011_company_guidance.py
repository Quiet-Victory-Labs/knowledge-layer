"""Immutable company guidance revisions with independent steward publication.

Revision ID: 0011_company_guidance
Revises: 0010_refresh_schedules
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_company_guidance"
down_revision = "0010_refresh_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_guidance_bundles",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(80), primary_key=True),
        sa.Column("latest_revision", sa.Integer, nullable=False),
        sa.Column("published_revision", sa.Integer),
        sa.Column("published_digest", sa.String(64)),
        sa.Column("published_at", sa.String(40)),
        sa.Column("published_by", sa.String(255)),
        sa.Column("publication_rationale", sa.String(1000)),
        sa.Column("revoked_at", sa.String(40)),
        sa.Column("revoked_by", sa.String(255)),
        sa.Column("revoke_reason", sa.String(1000)),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "knowledge_guidance_revisions",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(80), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "slug"],
                                ["knowledge_guidance_bundles.tenant_id", "knowledge_guidance_bundles.slug"],
                                name="fk_knowledge_guidance_revisions_bundle", ondelete="CASCADE"),
    )


def downgrade() -> None:
    raise RuntimeError("Company guidance history is forward-only; restore a verified database backup")
