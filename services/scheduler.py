import datetime as dt
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from config import SCHEDULER_DB_URL, TIMEZONE
from db import repo
from db.repo import session_scope

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)

scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=SCHEDULER_DB_URL)},
    timezone=TIMEZONE,
)

_bot: Bot | None = None

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def init_scheduler(bot: Bot) -> None:
    global _bot
    _bot = bot
    scheduler.start()


def schedule_alert(alert_id: int, fire_time: dt.datetime) -> str:
    job_id = f"alert_{alert_id}"
    scheduler.add_job(
        fire_alert,
        trigger="date",
        run_date=fire_time,
        args=[alert_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    return job_id


def cancel_job(job_id: str | None) -> None:
    if not job_id:
        return
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


async def fire_alert(alert_id: int) -> None:
    if _bot is None:
        logger.error("Scheduler fired before bot was initialized")
        return

    async with session_scope() as session:
        alert = await repo.get_alert(session, alert_id)
        if alert is None or alert.fired:
            return
        reminder = await repo.get_reminder(session, alert.reminder_id)
        if reminder is None or reminder.status != "active":
            return

        alert.fired = True
        await session.commit()

        event_local = reminder.event_time
        if event_local.tzinfo is None:
            event_local = event_local.replace(tzinfo=TZ)
        else:
            event_local = event_local.astimezone(TZ)

        text = (
            f"🔔 Напоминание: {reminder.text} "
            f"(событие в {event_local.strftime('%H:%M')})"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Сделано", callback_data=f"rem_done:{reminder.id}"),
                ],
                [
                    InlineKeyboardButton(
                        text="Отложить на 10 мин", callback_data=f"rem_snooze:{reminder.id}:10"
                    ),
                    InlineKeyboardButton(
                        text="Отложить на 1 час", callback_data=f"rem_snooze:{reminder.id}:60"
                    ),
                ],
            ]
        )
        try:
            await _bot.send_message(reminder.user_id, text, reply_markup=kb)
        except Exception:
            logger.exception("Failed to send reminder alert %s", alert_id)

        all_alerts = await repo.list_alerts(session, reminder.id)
        remaining = [a for a in all_alerts if not a.fired]
        if not remaining and reminder.recurrence_rule:
            await _reschedule_recurrence(session, reminder)
        elif not remaining:
            reminder.status = "done"
            await session.commit()


async def _reschedule_recurrence(session, reminder) -> None:
    next_time = compute_next_occurrence(reminder.event_time, reminder.recurrence_rule)
    if next_time is None:
        reminder.status = "done"
        await session.commit()
        return
    reminder.event_time = next_time
    await session.flush()
    alert = await repo.add_alert(session, reminder.id, next_time, label="on_time")
    job_id = schedule_alert(alert.id, next_time)
    alert.job_id = job_id
    await session.commit()


def compute_next_occurrence(current: dt.datetime, rule: str) -> dt.datetime | None:
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    if rule == "daily":
        return current + dt.timedelta(days=1)
    if rule == "yearly":
        try:
            return current.replace(year=current.year + 1)
        except ValueError:
            # Feb 29 on a non-leap year
            return current.replace(year=current.year + 1, day=28)
    if rule.startswith("weekly:"):
        target = rule.split(":", 1)[1]
        if target not in WEEKDAYS:
            return current + dt.timedelta(weeks=1)
        target_idx = WEEKDAYS.index(target)
        days_ahead = (target_idx - current.weekday()) % 7
        days_ahead = days_ahead or 7
        return current + dt.timedelta(days=days_ahead)
    return None


async def snooze_reminder(reminder_id: int, minutes: int) -> dt.datetime:
    fire_time = dt.datetime.now(TZ) + dt.timedelta(minutes=minutes)
    async with session_scope() as session:
        alert = await repo.add_alert(session, reminder_id, fire_time, label="snooze")
        job_id = schedule_alert(alert.id, fire_time)
        alert.job_id = job_id
        await session.commit()
    return fire_time


def offset_to_fire_time(event_time: dt.datetime, minutes_before: int) -> dt.datetime:
    return event_time - dt.timedelta(minutes=minutes_before)
