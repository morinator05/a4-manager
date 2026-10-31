"""one assignment per person and slot

Revision ID: c4a8f2e6d913
Revises: b7d41c9e2a63
Create Date: 2026-10-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4a8f2e6d913'
down_revision: Union[str, Sequence[str], None] = 'b7d41c9e2a63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(
        "assignments_slot_id_user_id_key", "assignments", ["slot_id", "user_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("assignments_slot_id_user_id_key", "assignments", type_="unique")
