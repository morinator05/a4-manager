import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Duty(str, enum.Enum):
    kitchen = "kitchen"
    trash = "trash"

    @property
    def label(self) -> str:
        return {"kitchen": "Küchendienst", "trash": "Mülldienst"}[self.value]


class Status(str, enum.Enum):
    pending = "pending"
    done = "done"
    missed = "missed"
    excused = "excused"
    # Someone else on the same slot already did it.
    covered = "covered"

    @property
    def label(self) -> str:
        return {
            "pending": "offen",
            "done": "erledigt",
            "missed": "verpasst",
            "excused": "entschuldigt",
            "covered": "nicht nötig",
        }[self.value]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    room_number: Mapped[str] = mapped_column(String(20))
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
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
        return self.name.split()[0] if self.name.strip() else self.name

    @property
    def display_name(self) -> str:
        """How people are shown in the UI, e.g. "Anna (A401)"."""
        return f"{self.first_name} ({self.room_number})"


class AwayPeriod(Base):
    __tablename__ = "away_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)

    user: Mapped[User] = relationship(back_populates="away_periods")


class Slot(Base):
    """One duty period: a single day for kitchen, a Monday-Sunday week for trash."""

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


class SlotEligible(Base):
    """Users that were available for a slot; each accrues an equal share of its expected work."""

    __tablename__ = "slot_eligible"

    slot_id: Mapped[int] = mapped_column(
        ForeignKey("slots.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    slot: Mapped[Slot] = relationship(back_populates="eligible")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id", ondelete="CASCADE"), index=True)
    # NULL once the user was deleted (moved out); the history stays.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[Status] = mapped_column(Enum(Status, name="status"), default=Status.pending)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Set when the Flursprecher found it was marked done but not actually done.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    slot: Mapped[Slot] = relationship(back_populates="assignments")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    completed_by: Mapped[User | None] = relationship(foreign_keys=[completed_by_id])


class Strike(Base):
    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL")
    )
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the strike was "paid off" by a penalty Mülldienst.
    redeemed_by_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="strikes", foreign_keys=[user_id])
    assignment: Mapped[Assignment | None] = relationship(foreign_keys=[assignment_id])
    issued_by: Mapped[User | None] = relationship(foreign_keys=[issued_by_id])

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.redeemed_by_assignment_id is None
