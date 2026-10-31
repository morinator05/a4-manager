"""Database tables (SQLAlchemy). Schema changes need a migration in `migrations/`."""

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class of all tables."""


class Duty(str, enum.Enum):
    """Kitchen slots are one day, trash slots one Monday-Sunday week."""

    kitchen = "kitchen"
    trash = "trash"

    @property
    def label(self) -> str:
        """The German name shown in the UI."""
        return {"kitchen": "Küchendienst", "trash": "Mülldienst"}[self.value]


class Status(str, enum.Enum):
    """State of one person's assignment to a slot."""

    # Not done yet, the deadline hasn't passed.
    pending = "pending"
    # This person did it (marked it done). Gives fairness credit.
    done = "done"
    # Deadline passed without anyone doing it, or the Flursprecher disputed a "done".
    missed = "missed"
    # The Flursprecher let it go: no strike, but no credit either.
    excused = "excused"
    # Someone else on the same slot already did it.
    covered = "covered"

    @property
    def label(self) -> str:
        """The German name shown in the UI."""
        return {
            "pending": "offen",
            "done": "erledigt",
            "missed": "verpasst",
            "excused": "entschuldigt",
            "covered": "nicht nötig",
        }[self.value]


class User(Base):
    """A resident. Deleting a user means they moved out; see `services/users.delete_user`."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    room_number: Mapped[str] = mapped_column(String(20))
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # The Flursprecher, who can manage residents, strikes and everybody's duties.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Inactive users are paused: never picked and they accrue no expected work.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    away_periods: Mapped[list["AwayPeriod"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    strikes: Mapped[list["Strike"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="Strike.user_id",
    )

    @property
    def first_name(self) -> str:
        """The first word of the name; residents are shown by first name and room."""
        return self.name.split()[0] if self.name.strip() else self.name

    @property
    def display_name(self) -> str:
        """For flash messages and confirm dialogs; pages use `render_person()` in `web.py`."""
        return f"{self.first_name} ({self.room_number})"


class AwayPeriod(Base):
    """A time a resident is away (both dates included). They aren't picked then."""

    __tablename__ = "away_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)

    user: Mapped[User] = relationship(back_populates="away_periods")


class Slot(Base):
    """One period of a duty; how long it is depends on the `Duty`."""

    __tablename__ = "slots"
    __table_args__ = (UniqueConstraint("duty", "start_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    duty: Mapped[Duty] = mapped_column(Enum(Duty, name="duty"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # Penalty slots are extra work for strikes; they don't count towards fairness.
    is_penalty: Mapped[bool] = mapped_column(Boolean, default=False)

    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="slot", cascade="all, delete-orphan", order_by="Assignment.id"
    )
    eligible: Mapped[list["SlotEligible"]] = relationship(
        back_populates="slot", cascade="all, delete-orphan"
    )

    @property
    def claimed_assignment(self) -> "Assignment | None":
        """Whoever marked the slot done. Only one person does a slot, so it is closed then."""
        return next((a for a in self.assignments if a.status is Status.done), None)


class SlotEligible(Base):
    """Users that were available for a slot; each accrues an equal share of its expected work."""

    __tablename__ = "slot_eligible"
    __table_args__ = (UniqueConstraint("slot_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id", ondelete="CASCADE"), index=True)
    # NULL once the user was deleted (moved out). The row stays so the others' shares of
    # past slots don't grow: nobody inherits the work of someone who moved out.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    slot: Mapped[Slot] = relationship(back_populates="eligible")


class Assignment(Base):
    """One person put on one slot. How many per slot is set in `config.py`."""

    __tablename__ = "assignments"
    # Only one person does a slot; `mark_duty_done` also takes the schedule lock to keep it so.
    __table_args__ = (
        UniqueConstraint("slot_id", "user_id"),
        Index(
            "uq_assignments_one_done_per_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("status = 'done'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id", ondelete="CASCADE"), index=True)
    # NULL once the user was deleted (moved out); the history stays.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[Status] = mapped_column(Enum(Status, name="status"), default=Status.pending)
    # When and by whom it was marked done or excused (the Flursprecher can do it for others).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Set when the Flursprecher found it was marked done but not actually done.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    slot: Mapped[Slot] = relationship(back_populates="assignments")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    completed_by: Mapped[User | None] = relationship(foreign_keys=[completed_by_id])


class Strike(Base):
    """A strike against a resident. Enough active ones give a penalty Mülldienst week."""

    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    # Given by the daily job for a missed duty (not by the Flursprecher).
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    # The duty it was given for, if any. Marking that duty done revokes the strike.
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL")
    )
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set when the Flursprecher took it back, or the duty was done or excused after all.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the strike was "paid off" by a penalty Mülldienst.
    redeemed_by_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="strikes", foreign_keys=[user_id])
    issued_by: Mapped[User | None] = relationship(foreign_keys=[issued_by_id])

    @hybrid_property
    def is_active(self) -> bool:
        """Counts towards the penalty threshold: neither revoked nor paid off."""
        return self.revoked_at is None and self.redeemed_by_assignment_id is None

    @is_active.inplace.expression
    @classmethod
    def _is_active_expression(cls) -> ColumnElement[bool]:
        """`is_active` as SQL, for queries."""
        return and_(cls.revoked_at.is_(None), cls.redeemed_by_assignment_id.is_(None))
