import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from config import BIRTHDAY_ALERT_TIME, TIMEZONE
from db.models import Person
from db.repo import (
    add_alert,
    create_reminder,
    delete_reminder,
    get_reminder,
    list_alerts,
    list_people_with_birthday,
    session_scope,
)
from services import scheduler

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)


def _alert_hour_minute() -> tuple[int, int]:
    try:
        hour_s, minute_s = BIRTHDAY_ALERT_TIME.split(":")
        return int(hour_s), int(minute_s)
    except (ValueError, AttributeError):
        logger.warning("Invalid BIRTHDAY_ALERT_TIME=%r, falling back to 15:00", BIRTHDAY_ALERT_TIME)
        return 15, 0


def _next_birthday_dt(month: int, day: int) -> dt.datetime:
    now = dt.datetime.now(TZ)
    hour, minute = _alert_hour_minute()
    try:
        candidate = dt.datetime(now.year, month, day, hour, minute, tzinfo=TZ)
    except ValueError:
        # Feb 29 on a non-leap year — observe on Feb 28 instead.
        candidate = dt.datetime(now.year, 2, 28, hour, minute, tzinfo=TZ)
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
    """(Re)create the yearly birthday reminder for a person from scratch.

    Call this whenever a birthday is set or changed, and whenever the
    person's name changes (the reminder text embeds the name).
    """
    await _remove_reminder(session, person.birthday_reminder_id)
    person.birthday_reminder_id = None

    if person.birthday_month is None or person.birthday_day is None:
        await session.flush()
        return

    event_time = _next_birthday_dt(person.birthday_month, person.birthday_day)
    reminder = await create_reminder(
        session,
        person.user_id,
        f"🎂 Сегодня день рождения у {person.name}!",
        event_time,
        recurrence_rule="yearly",
    )
    alert = await add_alert(session, reminder.id, event_time, "on_time")
    alert.job_id = scheduler.schedule_alert(alert.id, event_time)
    person.birthday_reminder_id = reminder.id

    await session.flush()


async def remove_birthday_reminders(session: AsyncSession, person: Person) -> None:
    await _remove_reminder(session, person.birthday_reminder_id)
    person.birthday_reminder_id = None


async def _reminder_is_healthy(session: AsyncSession, person: Person) -> bool:
    """Check whether the person's existing birthday reminder still has a
    live DB row and a live APScheduler job, so we don't needlessly churn
    reminder IDs on every startup."""
    if person.birthday_reminder_id is None:
        return False
    reminder = await get_reminder(session, person.birthday_reminder_id)
    if reminder is None or reminder.status != "active":
        return False
    alerts = await list_alerts(session, reminder.id)
    if not alerts:
        return False
    return any(a.job_id and scheduler.scheduler.get_job(a.job_id) for a in alerts)


async def sync_all_birthday_reminders() -> None:
    """Idempotent startup pass: make sure every person with a birthday has
    a live yearly reminder, without recreating ones that already work."""
    created = 0
    async with session_scope() as session:
        people = await list_people_with_birthday(session)
        for person in people:
            if await _reminder_is_healthy(session, person):
                continue
            await sync_birthday_reminders(session, person)
            created += 1
        await session.commit()
    if created:
        logger.info("Birthday reminder sync: (re)created %d reminder(s)", created)
    else:
        logger.info("Birthday reminder sync: all reminders already healthy")
