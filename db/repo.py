import datetime as dt

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from config import DB_URL
from db.models import Base, Person, PersonAlias, PersonNote, Reminder, ReminderAlert

engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS person_notes_fts "
                "USING fts5(text, content='person_notes', content_rowid='id')"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS person_notes_ai AFTER INSERT ON person_notes "
                "BEGIN INSERT INTO person_notes_fts(rowid, text) VALUES (new.id, new.text); END"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS person_notes_ad AFTER DELETE ON person_notes "
                "BEGIN INSERT INTO person_notes_fts(person_notes_fts, rowid, text) "
                "VALUES ('delete', old.id, old.text); END"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS person_notes_au AFTER UPDATE ON person_notes "
                "BEGIN INSERT INTO person_notes_fts(person_notes_fts, rowid, text) "
                "VALUES ('delete', old.id, old.text); "
                "INSERT INTO person_notes_fts(rowid, text) VALUES (new.id, new.text); END"
            )
        )


def session_scope() -> AsyncSession:
    return async_session()


# ---------- People ----------


async def create_person(
    session: AsyncSession,
    user_id: int,
    name: str,
    tag: str | None = None,
    birthday_month: int | None = None,
    birthday_day: int | None = None,
    birthday_year: int | None = None,
) -> Person:
    person = Person(
        user_id=user_id,
        name=name,
        tag=tag,
        birthday_month=birthday_month,
        birthday_day=birthday_day,
        birthday_year=birthday_year,
    )
    session.add(person)
    await session.flush()
    return person


async def get_person(session: AsyncSession, person_id: int) -> Person | None:
    res = await session.execute(
        select(Person)
        .options(selectinload(Person.aliases), selectinload(Person.notes))
        .where(Person.id == person_id)
    )
    return res.scalar_one_or_none()


async def list_people(session: AsyncSession, user_id: int) -> list[Person]:
    res = await session.execute(
        select(Person)
        .options(selectinload(Person.aliases), selectinload(Person.notes))
        .where(Person.user_id == user_id)
        .order_by(Person.name)
    )
    return list(res.scalars().all())


async def delete_person(session: AsyncSession, person_id: int) -> None:
    await session.execute(delete(Person).where(Person.id == person_id))


async def add_alias(session: AsyncSession, person_id: int, alias: str) -> PersonAlias:
    pa = PersonAlias(person_id=person_id, alias=alias)
    session.add(pa)
    await session.flush()
    return pa


async def add_note(session: AsyncSession, person_id: int, text_: str) -> PersonNote:
    note = PersonNote(person_id=person_id, text=text_)
    session.add(note)
    await session.flush()
    return note


async def delete_note(session: AsyncSession, note_id: int) -> None:
    await session.execute(delete(PersonNote).where(PersonNote.id == note_id))


async def search_notes(session: AsyncSession, user_id: int, query: str) -> list[PersonNote]:
    fts_sql = text(
        "SELECT pn.id FROM person_notes pn "
        "JOIN person_notes_fts fts ON fts.rowid = pn.id "
        "JOIN people p ON p.id = pn.person_id "
        "WHERE person_notes_fts MATCH :q AND p.user_id = :uid"
    )
    try:
        res = await session.execute(fts_sql, {"q": f'"{query}"*', "uid": user_id})
        ids = [row[0] for row in res.fetchall()]
    except Exception:
        ids = []

    if not ids:
        # FTS5's unicode61 tokenizer has no Russian stemming, so a query like
        # "рыбалка" won't match a note stored as "рыбалку" (different case
        # ending). Fall back to a substring search on the note text.
        like_sql = text(
            "SELECT pn.id FROM person_notes pn "
            "JOIN people p ON p.id = pn.person_id "
            "WHERE p.user_id = :uid AND pn.text LIKE :pattern"
        )
        stem = query[:-2] if len(query) > 4 else query
        res = await session.execute(
            like_sql, {"uid": user_id, "pattern": f"%{stem}%"}
        )
        ids = [row[0] for row in res.fetchall()]

    if not ids:
        return []
    res2 = await session.execute(select(PersonNote).where(PersonNote.id.in_(ids)))
    return list(res2.scalars().all())


async def people_with_birthday_on(
    session: AsyncSession, user_id: int, month: int, day: int | None = None
) -> list[Person]:
    stmt = select(Person).where(Person.user_id == user_id, Person.birthday_month == month)
    if day is not None:
        stmt = stmt.where(Person.birthday_day == day)
    res = await session.execute(stmt)
    return list(res.scalars().all())


# ---------- Reminders ----------


async def create_reminder(
    session: AsyncSession,
    user_id: int,
    text_: str,
    event_time: dt.datetime,
    recurrence_rule: str | None = None,
) -> Reminder:
    reminder = Reminder(
        user_id=user_id, text=text_, event_time=event_time, recurrence_rule=recurrence_rule
    )
    session.add(reminder)
    await session.flush()
    return reminder


async def get_reminder(session: AsyncSession, reminder_id: int) -> Reminder | None:
    res = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
    return res.scalar_one_or_none()


async def list_active_reminders(session: AsyncSession, user_id: int) -> list[Reminder]:
    res = await session.execute(
        select(Reminder)
        .where(Reminder.user_id == user_id, Reminder.status == "active")
        .order_by(Reminder.event_time)
    )
    return list(res.scalars().all())


async def delete_reminder(session: AsyncSession, reminder_id: int) -> None:
    await session.execute(delete(Reminder).where(Reminder.id == reminder_id))


async def add_alert(
    session: AsyncSession,
    reminder_id: int,
    fire_time: dt.datetime,
    label: str,
    job_id: str | None = None,
) -> ReminderAlert:
    alert = ReminderAlert(reminder_id=reminder_id, fire_time=fire_time, label=label, job_id=job_id)
    session.add(alert)
    await session.flush()
    return alert


async def list_alerts(session: AsyncSession, reminder_id: int) -> list[ReminderAlert]:
    res = await session.execute(
        select(ReminderAlert).where(ReminderAlert.reminder_id == reminder_id)
    )
    return list(res.scalars().all())


async def get_alert(session: AsyncSession, alert_id: int) -> ReminderAlert | None:
    res = await session.execute(select(ReminderAlert).where(ReminderAlert.id == alert_id))
    return res.scalar_one_or_none()


async def mark_alert_fired(session: AsyncSession, alert_id: int) -> None:
    alert = await get_alert(session, alert_id)
    if alert:
        alert.fired = True
