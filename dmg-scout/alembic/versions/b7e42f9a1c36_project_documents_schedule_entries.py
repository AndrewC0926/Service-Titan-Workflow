"""project documents and equipment schedule entries

Adds project_documents (a rep-attached PDF -- drawing set, Division 23 spec
section, or mechanical sheets) and schedule_entries (per-tag equipment
schedule rows extracted from one). See app/models.py's ProjectDocument and
ScheduleEntry docstrings, and app/pipeline/schedule.py.

Revision ID: b7e42f9a1c36
Revises: a3f7e91c5d02
Create Date: 2026-08-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7e42f9a1c36'
down_revision = 'a3f7e91c5d02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'project_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('content_type', sa.String(), nullable=False, server_default='application/pdf'),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('doc_type', sa.String(), nullable=False, server_default='other'),
        sa.Column('page_count', sa.Integer(), nullable=True),
        sa.Column('raw_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.Column('uploaded_by', sa.String(), nullable=True),
        sa.Column('extracted_at', sa.DateTime(), nullable=True),
        sa.Column('extraction_error', sa.String(), nullable=True),
    )
    op.create_index('ix_project_documents_project_id', 'project_documents', ['project_id'])
    op.create_index('ix_project_documents_doc_type', 'project_documents', ['doc_type'])
    op.create_index('ix_project_documents_uploaded_at', 'project_documents', ['uploaded_at'])

    op.create_table(
        'schedule_entries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_document_id', sa.Integer(), sa.ForeignKey('project_documents.id'), nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('tag', sa.String(), nullable=False),
        sa.Column('equipment_type', sa.String(), nullable=True),
        sa.Column('capacity_value', sa.Float(), nullable=True),
        sa.Column('capacity_unit', sa.String(), nullable=True),
        sa.Column('airflow_cfm', sa.Float(), nullable=True),
        sa.Column('basis_of_design_manufacturer', sa.String(), nullable=True),
        sa.Column('approved_equals', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('source_quote', sa.Text(), nullable=False, server_default=''),
        sa.Column('source_page', sa.Integer(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('needs_review', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('review_reason', sa.String(), nullable=True),
        sa.Column('extraction_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_schedule_entries_project_document_id', 'schedule_entries', ['project_document_id'])
    op.create_index('ix_schedule_entries_project_id', 'schedule_entries', ['project_id'])
    op.create_index('ix_schedule_entries_tag', 'schedule_entries', ['tag'])
    op.create_index('ix_schedule_entries_needs_review', 'schedule_entries', ['needs_review'])


def downgrade() -> None:
    op.drop_table('schedule_entries')
    op.drop_table('project_documents')
