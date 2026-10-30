"""one done assignment per slot

Revision ID: b7d41c9e2a63
Revises: 9c3e5a7b1d20
Create Date: 2026-10-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d41c9e2a63'
down_revision: Union[str, Sequence[str], None] = '9c3e5a7b1d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "uq_assignments_one_done_per_slot",
        "assignments",
        ["slot_id"],
        unique=True,
        postgresql_where=sa.text("status = 'done'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_assignments_one_done_per_slot", table_name="assignments")
