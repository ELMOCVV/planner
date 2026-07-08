"""Manual verification script (not a pytest suite) for:

  1. DATA_DIR override — both DB files must be created inside it.
  2. Birthday reminders — creating a person with a birthday schedules a
     yearly APScheduler job at the right time; updating the birthday
     reschedules it; deleting the person removes it.

Run with:  python3 scripts/verify_birthdays.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

TEST_DATA_DIR = Path(__file__).resolve().parent / "_verify_scratch"
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

# Must be set before `config` (or anything importing it) is loaded, since
# config.py reads these env vars at import time.
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["BIRTHDAY_ALERT_TIME"] = "15:00"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("TIMEZONE", "Europe/Kyiv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from db.repo import (  # noqa: E402
    create_person,
    delete_person,
    get_person,
    get_reminder,
    list_alerts,
    session_scope,
)
from services import birthdays, scheduler  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Verification failed: {label}")


async def main() -> None:
    from db.repo import init_db

    await init_db()
    scheduler.scheduler.start()

    print(f"DATA_DIR = {config.DATA_DIR}")
    print(f"DB_PATH = {config.DB_PATH}")
    print(f"SCHEDULER_DB_PATH = {config.SCHEDULER_DB_PATH}")

    check("DATA_DIR exists", config.DATA_DIR.is_dir())
    check("DATA_DIR is the overridden test dir", config.DATA_DIR == TEST_DATA_DIR.resolve())
    check("Main DB file created inside DATA_DIR", config.DB_PATH.exists())
    check(
        "Main DB path is inside DATA_DIR",
        config.DB_PATH.resolve().parent == config.DATA_DIR,
    )
    # The scheduler DB file is created lazily by APScheduler on first job
    # write, which happens below — checked again after that point.

    # --- create person with birthday ---
    async with session_scope() as session:
        person = await create_person(session, user_id=999, name="Валера", birthday_month=7, birthday_day=12)
        await birthdays.sync_birthday_reminders(session, person)
        await session.commit()
        person_id = person.id

    check(
        "Scheduler DB file created inside DATA_DIR",
        config.SCHEDULER_DB_PATH.exists()
        and config.SCHEDULER_DB_PATH.resolve().parent == config.DATA_DIR,
    )

    async with session_scope() as session:
        person = await get_person(session, person_id)
        reminder_id = person.birthday_reminder_id
        reminder = await get_reminder(session, reminder_id)
        alerts = await list_alerts(session, reminder_id)

    check("Birthday reminder created", reminder is not None)
    check("Birthday reminder is yearly recurrence", reminder.recurrence_rule == "yearly")
    check(
        "Birthday reminder message matches required format",
        reminder.text == "🎂 Сегодня день рождения у Валера!",
    )
    check("Exactly one alert for the birthday reminder", len(alerts) == 1)

    alert = alerts[0]
    job = scheduler.scheduler.get_job(alert.job_id)
    check("APScheduler job exists for the birthday alert", job is not None)
    fire_time = job.next_run_time
    print(f"Scheduled fire time: {fire_time}")
    check("Fire time is July 12", fire_time.month == 7 and fire_time.day == 12)
    check("Fire time uses configured BIRTHDAY_ALERT_TIME (15:00)", fire_time.hour == 15 and fire_time.minute == 0)

    old_reminder_id = reminder_id

    # --- update the birthday ---
    async with session_scope() as session:
        person = await get_person(session, person_id)
        person.birthday_month, person.birthday_day = 3, 5
        await birthdays.sync_birthday_reminders(session, person)
        await session.commit()

    from db.repo import list_active_reminders

    async with session_scope() as session:
        person = await get_person(session, person_id)
        new_reminder_id = person.birthday_reminder_id
        new_reminder = await get_reminder(session, new_reminder_id)
        new_alerts = await list_alerts(session, new_reminder_id)
        active = await list_active_reminders(session, 999)

    # Note: SQLite reuses the lowest free rowid for a plain INTEGER PRIMARY
    # KEY once a table is empty, so new_reminder_id/new job_id can
    # legitimately equal the old ones here — that's not a bug (verified
    # separately that the old rows/alerts are actually gone, not just
    # shadowed — see delete_reminder's alert cascade). What matters is that
    # there's exactly one live reminder for this birthday, with no leftover
    # scheduler jobs pointing at the old date.
    check(
        "Exactly one active yearly reminder remains (no duplicate left behind)",
        len([r for r in active if r.recurrence_rule == "yearly"]) == 1,
    )
    check("Exactly one alert row for the updated reminder", len(new_alerts) == 1)
    all_jobs = scheduler.scheduler.get_jobs()
    check("Exactly one job scheduled overall (no leftover old-date job)", len(all_jobs) == 1)
    new_job = scheduler.scheduler.get_job(new_alerts[0].job_id)
    check("New job scheduled after update", new_job is not None)
    new_fire = new_job.next_run_time
    print(f"Rescheduled fire time: {new_fire}")
    check("New fire time is March 5", new_fire.month == 3 and new_fire.day == 5)
    check(
        "New reminder message updated",
        new_reminder.text == "🎂 Сегодня день рождения у Валера!",
    )

    # --- delete the person ---
    async with session_scope() as session:
        person = await get_person(session, person_id)
        await birthdays.remove_birthday_reminders(session, person)
        await delete_person(session, person_id)
        await session.commit()

    check("Job removed from scheduler after person deletion", scheduler.scheduler.get_job(new_alerts[0].job_id) is None)
    async with session_scope() as session:
        gone_reminder = await get_reminder(session, new_reminder_id)
        gone_person = await get_person(session, person_id)
    check("Reminder row removed after person deletion", gone_reminder is None)
    check("Person row removed after deletion", gone_person is None)

    # --- idempotent startup sync ---
    async with session_scope() as session:
        p2 = await create_person(session, user_id=999, name="Игорь", birthday_month=9, birthday_day=1)
        await session.commit()
        p2_id = p2.id
    # No reminder created yet (simulating pre-existing data / a missed sync)
    async with session_scope() as session:
        p2 = await get_person(session, p2_id)
        check("Person created without a reminder yet", p2.birthday_reminder_id is None)

    await birthdays.sync_all_birthday_reminders()

    async with session_scope() as session:
        p2 = await get_person(session, p2_id)
        check("Startup sync created a missing reminder", p2.birthday_reminder_id is not None)
        r2 = await get_reminder(session, p2.birthday_reminder_id)
        check("Synced reminder is active/yearly", r2 is not None and r2.recurrence_rule == "yearly")

    # Running sync again should NOT create a new reminder (idempotent)
    reminder_id_before = p2.birthday_reminder_id
    await birthdays.sync_all_birthday_reminders()
    async with session_scope() as session:
        p2 = await get_person(session, p2_id)
    check("Second sync run is idempotent (same reminder id)", p2.birthday_reminder_id == reminder_id_before)

    scheduler.scheduler.shutdown(wait=False)
    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
