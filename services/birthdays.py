import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from config import TIMEZONE
from db.models import Person
from db.repo import add_alert, create_reminder, delete_reminder, get_reminder, list_alerts
from services import scheduler

TZ = ZoneInfo(TIMEZONE)


def _next_birthday_dt(month: int, day: int) -> dt.datetime:
    now = dt.datetime.now(TZ)
    try:
        candidate = dt.datetime(now.year, month, day, 9, 0, tzinfo=TZ)
    except ValueError:
        candidate = dt.datetime(now.year, 2, 28, 9, 0, tzinfo=TZ)
    if candidate < now:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


async def _remove_reminder(session: AsyncSession, reminder_id: int | None) -> None:
    if reminder_id is None:
        return
    reminder = await get_reminder(session, reminder_id)
    if reminder is None:
        return
    alerts = await list_alerts(session, reminder_id)
    for a in alerts:
        scheduler.cancel_job(a.job_id)
    await delete_reminder(session, reminder_id)


async def sync_birthday_reminders(session: AsyncSession, person: Person) -> None:
    await _remove_reminder(session, person.birthday_reminder_id)
    await _remove_reminder(session, person.birthday_reminder_before_id)
    person.birthday_reminder_id = None
    person.birthday_reminder_before_id = None

    if person.birthday_month is None or person.birthday_day is None:
        await session.flush()
        return

    event_time = _next_birthday_dt(person.birthday_month, person.birthday_day)
    reminder = await create_reminder(
        session,
        person.user_id,
        f"🎂 День рождения у {person.name}!",
        event_time,
        recurrence_rule="yearly",
    )
    alert = await add_alert(session, reminder.id, event_time, "on_time")
    alert.job_id = scheduler.schedule_alert(alert.id, event_time)
    person.birthday_reminder_id = reminder.id

    before_time = event_time - dt.timedelta(days=1)
    reminder_before = await create_reminder(
        session,
        person.user_id,
        f"Завтра др у {person.name}",
        before_time,
        recurrence_rule="yearly",
    )
    alert_before = await add_alert(session, reminder_before.id, before_time, "on_time")
    alert_before.job_id = scheduler.schedule_alert(alert_before.id, before_time)
    person.birthday_reminder_before_id = reminder_before.id

    await session.flush()


async def remove_birthday_reminders(session: AsyncSession, person: Person) -> None:
    await _remove_reminder(session, person.birthday_reminder_id)
    await _remove_reminder(session, person.birthday_reminder_before_id)
