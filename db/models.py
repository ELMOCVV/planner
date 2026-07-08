import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(255))
    tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    birthday_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_reminder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    aliases: Mapped[list["PersonAlias"]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )
    notes: Mapped[list["PersonNote"]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )


class PersonAlias(Base):
    __tablename__ = "person_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), index=True)
    alias: Mapped[str] = mapped_column(String(255))

    person: Mapped["Person"] = relationship(back_populates="aliases")


class PersonNote(Base):
    __tablename__ = "person_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    person: Mapped["Person"] = relationship(back_populates="notes")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    event_time: Mapped[dt.datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="active")
    recurrence_rule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    alerts: Mapped[list["ReminderAlert"]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )


class ReminderAlert(Base):
    __tablename__ = "reminder_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), index=True)
    fire_time: Mapped[dt.datetime] = mapped_column(DateTime)
    label: Mapped[str] = mapped_column(String(64), default="")
    fired: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    reminder: Mapped["Reminder"] = relationship(back_populates="alerts")
