"""contractors, retrofit_geocodes, retrofit lat/long

Revision ID: c8560da99fec
Revises: 9b3dca3662c5
Create Date: 2026-08-13 21:25:56.005235
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'c8560da99fec'
down_revision = '9b3dca3662c5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contractors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("license_no", sa.String(), nullable=False),
        sa.Column("business_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("full_business_name", sa.String(), nullable=True),
        sa.Column("business_type", sa.String(), nullable=True),
        sa.Column("business_address", sa.String(), nullable=True),
        sa.Column("city", sa.String(), nullable=True),
        sa.Column("county", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("zip_code", sa.String(), nullable=True),
        sa.Column("business_phone", sa.String(), nullable=True),
        sa.Column("issue_date", sa.DateTime(), nullable=True),
        sa.Column("expiration_date", sa.DateTime(), nullable=True),
        sa.Column("primary_status", sa.String(), nullable=True),
        sa.Column("secondary_status", sa.String(), nullable=True),
        sa.Column("classifications", sa.String(), nullable=True),
        sa.Column("workers_comp_coverage_type", sa.String(), nullable=True),
        sa.Column("workers_comp_insurance_company", sa.String(), nullable=True),
        sa.Column("workers_comp_policy_number", sa.String(), nullable=True),
        sa.Column("workers_comp_effective_date", sa.DateTime(), nullable=True),
        sa.Column("workers_comp_expiration_date", sa.DateTime(), nullable=True),
        sa.Column("bond_company", sa.String(), nullable=True),
        sa.Column("bond_number", sa.String(), nullable=True),
        sa.Column("bond_effective_date", sa.DateTime(), nullable=True),
        sa.Column("bond_cancellation_date", sa.DateTime(), nullable=True),
        sa.Column("bond_amount", sa.Float(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("geocoded_at", sa.DateTime(), nullable=True),
        sa.Column("geocode_source", sa.String(), nullable=True),
        sa.Column("nearby_replacement_candidates", sa.Integer(), nullable=True),
        sa.Column("nearby_radius_miles", sa.Float(), nullable=True),
        sa.Column("nearby_computed_at", sa.DateTime(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False,
                  server_default="https://www.cslb.ca.gov/onlineservices/dataportal/"),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("last_update", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_contractors_license_no", "contractors", ["license_no"], unique=True)
    op.create_index("ix_contractors_city", "contractors", ["city"])
    op.create_index("ix_contractors_county", "contractors", ["county"])
    op.create_index("ix_contractors_zip_code", "contractors", ["zip_code"])
    op.create_index("ix_contractors_issue_date", "contractors", ["issue_date"])
    op.create_index("ix_contractors_expiration_date", "contractors", ["expiration_date"])
    op.create_index("ix_contractors_primary_status", "contractors", ["primary_status"])
    op.create_index("ix_contractors_classifications", "contractors", ["classifications"])
    op.create_index("ix_contractors_retrieved_at", "contractors", ["retrieved_at"])

    op.create_table(
        "retrofit_geocodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("apn", sa.String(), nullable=False),
        sa.Column("source_address", sa.Text(), nullable=False, server_default=""),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("geocode_source", sa.String(), nullable=False, server_default="us_census_bureau"),
        sa.Column("geocoded_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_retrofit_geocodes_apn", "retrofit_geocodes", ["apn"], unique=True)
    op.create_index("ix_retrofit_geocodes_geocoded_at", "retrofit_geocodes", ["geocoded_at"])

    op.add_column("retrofit_buildings", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("retrofit_buildings", sa.Column("longitude", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("retrofit_buildings", "longitude")
    op.drop_column("retrofit_buildings", "latitude")
    op.drop_table("retrofit_geocodes")
    op.drop_table("contractors")
