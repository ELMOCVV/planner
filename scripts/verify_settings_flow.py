"""Manual verification of the /settings flow: menu, presets, custom-time
free text (real API call — reuses the classifier), immediate rescheduling
of existing birthday reminders, the test-notification button, and the
shared Close button.

Run with: python3 scripts/verify_settings_flow.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

TEST_DATA_DIR = Path(__file__).resolve().parent / "_verify_settings_scratch"
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TIMEZONE", "Europe/Kyiv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

if not config.ANTHROPIC_API_KEY:
    print("ANTHROPIC_API_KEY is not set — cannot verify the custom-time free-text step.")
    raise SystemExit(1)

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import InlineKeyboardMarkup  # noqa: E402

from db.repo import init_db, session_scope, create_person, get_person  # noqa: E402
from handlers import settings  # noqa: E402
from handlers.states import SettingsFlow  # noqa: E402
from handlers.ui import CLOSE_CALLBACK  # noqa: E402
from services import birthdays, scheduler  # noqa: E402


def check(label: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(f"Verification failed: {label}")


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    def __init__(self, user_id: int, text: str = ""):
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.text = text
        self.sent: list[tuple[str, object]] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))
        return self

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.sent.append((self.sent[-1][0] if self.sent else "", reply_markup))
        return self

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, user_id: int, data: str, message: FakeMessage):
        self.from_user = FakeUser(user_id)
        self.data = data
        self.message = message
        self.answered_text = None
        self.show_alert = False

    async def answer(self, text=None, show_alert=False):
        self.answered_text = text
        self.show_alert = show_alert


def new_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def kb_is_empty(markup) -> bool:
    return isinstance(markup, InlineKeyboardMarkup) and markup.inline_keyboard == []


def button_callback(sent_entry, matching_text_substr: str) -> str:
    _, kb = sent_entry
    for row in kb.inline_keyboard:
        for btn in row:
            if matching_text_substr in btn.text:
                return btn.callback_data
    raise AssertionError(f"no button containing {matching_text_substr!r}")


async def main() -> None:
    await init_db()
    scheduler.scheduler.start()
    user_id = 8686

    async with session_scope() as session:
        person = await create_person(session, user_id, "Валера", birthday_month=7, birthday_day=12)
        await birthdays.sync_birthday_reminders(session, person)
        await session.commit()
        person_id = person.id

    async with session_scope() as session:
        person = await get_person(session, person_id)
        reminder_id_before = person.birthday_reminder_id

    # --- default settings menu shows the env-var fallback (15:00) ---
    msg = FakeMessage(user_id)
    await settings.show_settings(msg)
    text, kb = msg.sent[-1]
    check("Settings menu shows default 15:00", "15:00" in text)
    check("Settings menu has 'Изменить время' button", any("Изменить время" in b.text for row in kb.inline_keyboard for b in row))
    check("Settings menu has Test button", any("Тест уведомления" in b.text for row in kb.inline_keyboard for b in row))
    check("Settings menu has Close button", any(b.callback_data == CLOSE_CALLBACK for row in kb.inline_keyboard for b in row))

    # --- tapping a preset reschedules the existing reminder immediately ---
    cb_msg = FakeMessage(user_id)
    cb = FakeCallback(user_id, "settings:changetime", cb_msg)
    await settings.handle_change_time(cb)
    preset_cb_data = button_callback(cb_msg.sent[-1], "18:00")

    cb2 = FakeCallback(user_id, preset_cb_data, cb_msg)
    await settings.handle_preset(cb2)
    final_text, final_kb = cb_msg.sent[-1]
    check("Preset confirmation mentions the new time", "18:00" in final_text)
    check("Preset confirmation reports 1 rescheduled reminder", "Перенёс 1 напоминание" in final_text)
    check("Preset confirmation strips the keyboard", kb_is_empty(final_kb))

    async with session_scope() as session:
        from db.repo import get_reminder

        person = await get_person(session, person_id)
        reminder_id_after = person.birthday_reminder_id
        reminder = await get_reminder(session, reminder_id_after)
    check("Reminder's scheduled hour actually changed to 18:00", reminder.event_time.hour == 18)

    # --- custom time via free text (real classifier call) ---
    state = new_state(user_id)
    cb_msg2 = FakeMessage(user_id)
    cb3 = FakeCallback(user_id, "settings:custom", cb_msg2)
    await settings.handle_custom(cb3, state)
    check("Custom-time prompt asks for a time", "Во сколько" in cb_msg2.sent[-1][0])
    check("FSM state is waiting_custom_time", (await state.get_state()) == SettingsFlow.waiting_custom_time.state)

    text_msg = FakeMessage(user_id, "16 45")
    await settings.handle_custom_time_text(text_msg, state)
    check("Custom time confirmation mentions 16:45", "16:45" in text_msg.sent[-1][0])
    check("FSM state cleared after custom time applied", (await state.get_state()) is None)

    async with session_scope() as session:
        from db.repo import get_reminder

        person = await get_person(session, person_id)
        reminder = await get_reminder(session, person.birthday_reminder_id)
    check("Reminder rescheduled to the custom time (16:45)", reminder.event_time.hour == 16 and reminder.event_time.minute == 45)

    # --- test notification button schedules a real one-off job ---
    test_cb_msg = FakeMessage(user_id)
    test_cb = FakeCallback(user_id, "settings:test", test_cb_msg)
    await settings.handle_test_notification(test_cb)
    check("Test button answers with a confirmation alert", test_cb.show_alert is True)
    check("Test confirmation mentions ~2 minutes", "2 минут" in test_cb.answered_text)

    async with session_scope() as session:
        from db.repo import list_active_reminders

        active = await list_active_reminders(session, user_id)
    test_reminders = [r for r in active if "Тестового Валеры" in r.text]
    check("A real test reminder row was created", len(test_reminders) == 1)

    from db.repo import list_alerts

    async with session_scope() as session:
        test_alerts = await list_alerts(session, test_reminders[0].id)
    check("Test reminder has a live scheduler job", scheduler.scheduler.get_job(test_alerts[0].job_id) is not None)

    # --- Close button ---
    close_msg = FakeMessage(user_id)
    close_cb = FakeCallback(user_id, CLOSE_CALLBACK, close_msg)
    from handlers.common import handle_close

    await handle_close(close_cb)
    check("Close button deletes the settings message", close_msg.deleted is True)

    # --- /export includes the user's settings override ---
    from db.repo import export_user_data

    async with session_scope() as session:
        export = await export_user_data(session, user_id)
    check("Export includes a settings section", "settings" in export)
    check(
        "Export reflects the custom birthday_alert_time (16:45)",
        export["settings"]["birthday_alert_time"] == "16:45",
    )

    scheduler.scheduler.shutdown(wait=False)
    print("\nAll checks passed.")
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
