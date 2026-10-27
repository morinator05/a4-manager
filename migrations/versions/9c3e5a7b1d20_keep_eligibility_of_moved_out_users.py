"""keep eligibility of moved-out users

Deleting a user used to delete their slot_eligible rows, which made everybody else's
share of past slots bigger. Now the rows stay with user_id NULL, like assignments.

Revision ID: 9c3e5a7b1d20
Revises: 584dbb1fab44
Create Date: 2026-10-27 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9c3e5a7b1d20'
down_revision: Union[str, Sequence[str], None] = '584dbb1fab44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # user_id can't stay in the primary key once it may be NULL.
    op.drop_constraint("slot_eligible_pkey", "slot_eligible", type_="primary")
    op.execute("ALTER TABLE slot_eligible ADD COLUMN id SERIAL PRIMARY KEY")
    op.alter_column("slot_eligible", "user_id", nullable=True)
    op.create_unique_constraint(
        "slot_eligible_slot_id_user_id_key", "slot_eligible", ["slot_id", "user_id"]
    )
    op.create_index("ix_slot_eligible_slot_id", "slot_eligible", ["slot_id"])
    op.drop_constraint("slot_eligible_user_id_fkey", "slot_eligible", type_="foreignkey")
    op.create_foreign_key(
        "slot_eligible_user_id_fkey", "slot_eligible", "users", ["user_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM slot_eligible WHERE user_id IS NULL")
    op.drop_constraint("slot_eligible_user_id_fkey", "slot_eligible", type_="foreignkey")
    op.create_foreign_key(
        "slot_eligible_user_id_fkey", "slot_eligible", "users", ["user_id"], ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_slot_eligible_slot_id", table_name="slot_eligible")
    op.drop_constraint("slot_eligible_slot_id_user_id_key", "slot_eligible", type_="unique")
    op.alter_column("slot_eligible", "user_id", nullable=False)
    op.drop_column("slot_eligible", "id")
    op.create_primary_key("slot_eligible_pkey", "slot_eligible", ["slot_id", "user_id"])
