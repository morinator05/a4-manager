"""one claimer per slot, disputes

Revision ID: 584dbb1fab44
Revises: e2f666e08122
Create Date: 2026-10-27 11:13:08.933559

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '584dbb1fab44'
down_revision: Union[str, Sequence[str], None] = 'e2f666e08122'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE status ADD VALUE IF NOT EXISTS 'covered'")
    op.add_column("assignments", sa.Column("disputed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assignments", "disputed_at")
    # Postgres can't drop a single enum value: map it back and recreate the type.
    op.execute("UPDATE assignments SET status = 'excused' WHERE status = 'covered'")
    op.execute("ALTER TYPE status RENAME TO status_old")
    op.execute("CREATE TYPE status AS ENUM ('pending', 'done', 'missed', 'excused')")
    op.execute(
        "ALTER TABLE assignments ALTER COLUMN status TYPE status USING status::text::status"
    )
    op.execute("DROP TYPE status_old")
