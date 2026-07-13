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
    get_user_settings,
    list_alerts,
    list_people_with_birthday,
    session_scope,
)
from services import scheduler

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)


def _parse_hhmm(value: str | None, fallback: tuple[int, int] = (15, 0)) -> tuple[int, int]:
    if not value:
        return fallback
    try:
        hour_s, minute_s = value.split(":")
        return int(hour_s), int(minute_s)
    except (ValueError, AttributeError):
        logger.warning("Invalid time string %r, falling back to %02d:%02d", value, *fallback)
        return fallback


def _default_alert_hour_minute() -> tuple[int, int]:
    return _parse_hhmm(BIRTHDAY_ALERT_TIME)


async def get_effective_alert_time(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """A user's own /settings override, falling back to the
    BIRTHDAY_ALERT_TIME env var default."""
    settings = await get_user_settings(session, user_id)
    if settings and settings.birthday_alert_time:
        return _parse_hhmm(settings.birthday_alert_time, fallback=_default_alert_hour_minute())
    return _default_alert_hour_minute()


async def get_effective_alert_time_str(user_id: int) -> str:
    async with session_scope() as session:
        hour, minute = await get_effective_alert_time(session, user_id)
    return f"{hour:02d}:{minute:02d}"


def _next_birthday_dt(month: int, day: int, hour: int, minute: int) -> dt.datetime:
    now = dt.datetime.now(TZ)
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

    hour, minute = await get_effective_alert_time(session, person.user_id)
    event_time = _next_birthday_dt(person.birthday_month, person.birthday_day, hour, minute)
    text = f"🎂 Сегодня день рождения у {person.name}!"
    if person.birthday_year:
        # Age as of the scheduled occurrence. The yearly rollover in
        # services/scheduler.py increments it when re-arming for next year.
        text += f" Исполняется {event_time.year - person.birthday_year}."
    reminder = await create_reminder(
        session,
        person.user_id,
        text,
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


async def reschedule_all_for_user(user_id: int) -> int:
    """Re-create every one of this user's yearly birthday reminders using
    their current effective alert time. Call this right after saving a new
    /settings time (the setting must already be persisted — this function
    re-reads it via get_effective_alert_time) so the change takes effect
    immediately, not on the next restart."""
    count = 0
    async with session_scope() as session:
        people = await list_people_with_birthday(session, user_id=user_id)
        for person in people:
            await sync_birthday_reminders(session, person)
            count += 1
        await session.commit()
    return count


async def schedule_test_alert(user_id: int, minutes_from_now: int = 2) -> dt.datetime:
    """Schedule a one-off birthday-style alert through the real scheduler
    and jobstore (not a direct send) — this exercises the exact same
    create_reminder/add_alert/schedule_alert/fire_alert path a real
    birthday reminder uses, so if this test message arrives, real ones
    will too."""
    fire_time = dt.datetime.now(TZ) + dt.timedelta(minutes=minutes_from_now)
    async with session_scope() as session:
        reminder = await create_reminder(
            session,
            user_id,
            "🎂 Тест: сегодня день рождения у Тестового Валеры!",
            fire_time,
            recurrence_rule=None,
        )
        alert = await add_alert(session, reminder.id, fire_time, "on_time")
        alert.job_id = scheduler.schedule_alert(alert.id, fire_time)
        await session.commit()
    return fire_time


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
