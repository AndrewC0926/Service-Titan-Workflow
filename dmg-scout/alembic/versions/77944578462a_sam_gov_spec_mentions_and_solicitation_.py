"""sam_gov spec_mentions and solicitation checks

Revision ID: 77944578462a
Revises: 66142e80aa58
Create Date: 2026-08-13 15:14:34.985042
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '77944578462a'
down_revision = '66142e80aa58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sam_solicitation_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notice_id", sa.String(), nullable=False),
        sa.Column("solicitation_number", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("agency", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("naics_code", sa.String(), nullable=True),
        sa.Column("posted_date", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sam_solicitation_checks_notice_id", "sam_solicitation_checks", ["notice_id"], unique=True)
    op.create_index("ix_sam_solicitation_checks_state", "sam_solicitation_checks", ["state"])
    op.create_index("ix_sam_solicitation_checks_outcome", "sam_solicitation_checks", ["outcome"])
    op.create_index("ix_sam_solicitation_checks_checked_at", "sam_solicitation_checks", ["checked_at"])

    op.create_table(
        "spec_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notice_id", sa.String(), nullable=False),
        sa.Column("solicitation_number", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("agency", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("specifying_firm", sa.String(), nullable=True),
        sa.Column("spec_section", sa.String(), nullable=True),
        sa.Column("spec_section_title", sa.String(), nullable=True),
        sa.Column("manufacturer_name", sa.String(), nullable=False),
        sa.Column("mention_type", sa.String(), nullable=False),
        sa.Column("on_dmg_line_card", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dmg_line_card_name", sa.String(), nullable=True),
        sa.Column("extraction_basis", sa.Text(), nullable=True),
        sa.Column("posted_date", sa.DateTime(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_spec_mentions_notice_id", "spec_mentions", ["notice_id"])
    op.create_index("ix_spec_mentions_state", "spec_mentions", ["state"])
    op.create_index("ix_spec_mentions_specifying_firm", "spec_mentions", ["specifying_firm"])
    op.create_index("ix_spec_mentions_manufacturer_name", "spec_mentions", ["manufacturer_name"])
    op.create_index("ix_spec_mentions_mention_type", "spec_mentions", ["mention_type"])
    op.create_index("ix_spec_mentions_on_dmg_line_card", "spec_mentions", ["on_dmg_line_card"])
    op.create_index("ix_spec_mentions_retrieved_at", "spec_mentions", ["retrieved_at"])


def downgrade() -> None:
    op.drop_table("spec_mentions")
    op.drop_table("sam_solicitation_checks")
