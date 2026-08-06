"""Estimated equipment value on projects

Ranking on tonnage alone over-values large low-intensity buildings. A 5,000 ton
fulfillment centre on packaged rooftops is a smaller opportunity than a 600 ton
cleanroom on custom air handlers, and the board could not express that.

Revision ID: a4b8e2c15d93
Revises: 7f3a2d81c4e6
"""
import sqlalchemy as sa
import sqlmodel

from alembic import op

revision = "a4b8e2c15d93"
down_revision = "7f3a2d81c4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("equipment_value_low", sa.Float(), nullable=True))
    op.add_column("projects", sa.Column("equipment_value_high", sa.Float(), nullable=True))
    op.add_column("projects", sa.Column("equipment_value_basis",
                                        sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "equipment_value_basis")
    op.drop_column("projects", "equipment_value_high")
    op.drop_column("projects", "equipment_value_low")
