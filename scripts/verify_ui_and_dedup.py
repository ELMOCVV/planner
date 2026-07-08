"""Manual verification (mocked Telegram objects, no LLM/network calls) for:

  - Duplicate-note detection: saving a near-duplicate note still saves it
    but the reply flags the existing similar note; "🧹 Почистить дубли"
    surfaces the pair with delete buttons.
  - Preview truncation shows a "(+N заметки)" count marker.
  - UI cleanup: Close button present on the four persistent-menu surfaces
    and tapping it deletes the message; final-action buttons (Создать,
    Отменено, etc.) get their keyboard stripped after firing.

Run with: python3 scripts/verify_ui_and_dedup.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

TEST_DATA_DIR = Path(__file__).resolve().parent / "_verify_ui_scratch"
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("TIMEZONE", "Europe/Kyiv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import InlineKeyboardMarkup  # noqa: E402

from db.repo import init_db, session_scope, create_person  # noqa: E402
from handlers import common, people  # noqa: E402
from handlers.people import _facts_preview, _person_card_kb  # noqa: E402
from handlers.ui import CLOSE_CALLBACK  # noqa: E402


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
        self.answered = False

    async def answer(self, text=None, show_alert=False):
        self.answered = True


def new_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def kb_is_empty(markup) -> bool:
    return isinstance(markup, InlineKeyboardMarkup) and markup.inline_keyboard == []


def has_close_button(markup) -> bool:
    return any(btn.callback_data == CLOSE_CALLBACK for row in markup.inline_keyboard for btn in row)


async def main() -> None:
    await init_db()
    user_id = 777

    async with session_scope() as session:
        person = await create_person(session, user_id, "Настя")
        await session.commit()
        person_id = person.id

    # --- Duplicate note detection ---
    state = new_state(user_id)
    msg1 = FakeMessage(user_id, "любимый цвет чёрный")
    await state.update_data(person_id=person_id)
    await state.set_state(people.PersonFlow.waiting_new_note)
    await people.handle_new_note_text(msg1, state)
    check("First note saved without a duplicate warning", "Добавил заметку" in msg1.sent[-1][0])

    state2 = new_state(user_id)
    msg2 = FakeMessage(user_id, "любит чёрный цвет")
    await state2.update_data(person_id=person_id)
    await state2.set_state(people.PersonFlow.waiting_new_note)
    await people.handle_new_note_text(msg2, state2)
    check("Similar second note still saved (one-step, not blocked)", "Добавил" in msg2.sent[-1][0])
    check("Reply flags the near-duplicate", "похоже на существующую" in msg2.sent[-1][0])
    check("Reply quotes the original note text", "любимый цвет чёрный" in msg2.sent[-1][0])

    async with session_scope() as session:
        from db.repo import get_person

        person = await get_person(session, person_id)
        note_texts = [n.text for n in person.notes]
    check("Both notes actually got saved (dedup never blocks)", len(note_texts) == 2)

    # --- "Почистить дубли" surfaces the pair ---
    card_kb = _person_card_kb(person_id)
    check("Person card has a 'Почистить дубли' button", any("дубли" in btn.text for row in card_kb.inline_keyboard for btn in row))
    check("Person card has a Close button", has_close_button(card_kb))

    cb_msg = FakeMessage(user_id)
    cb = FakeCallback(user_id, f"card:dupes:{person_id}", cb_msg)
    await people.handle_card_dupes(cb)
    dupes_text, dupes_kb = cb_msg.sent[-1]
    check("Dupes view lists the similar pair", "любимый цвет чёрный" in dupes_text and "любит чёрный цвет" in dupes_text)
    check("Dupes view offers delete buttons for both notes", sum(1 for row in dupes_kb.inline_keyboard for btn in row if "🗑" in btn.text) == 2)

    # --- Preview truncation count marker ---
    preview = _facts_preview(["a", "b", "c"], None, None)
    check("Truncated preview shows a count marker", preview == "a, b (+1 заметка)")
    preview2 = _facts_preview(["a", "b", "c", "d"], None, None)
    check("Count marker pluralizes correctly for 2", preview2 == "a, b (+2 заметки)")
    preview3 = _facts_preview(["a", "b"], None, None)
    check("No marker when nothing is truncated", preview3 == "a, b")

    # --- UI cleanup: Close button deletes the message ---
    close_msg = FakeMessage(user_id)
    close_cb = FakeCallback(user_id, CLOSE_CALLBACK, close_msg)
    await common.handle_close(close_cb)
    check("Tapping Close deletes the message", close_msg.deleted is True)

    # --- UI cleanup: final actions strip the keyboard ---
    state3 = new_state(user_id)
    msg3 = FakeMessage(user_id)
    parsed3 = {"person_name": "Незнакомец2", "person_facts": [], "birthday": None, "person_tag": None}
    await people.start_add_person(msg3, state3, parsed3)
    check("Zero-match create-confirm shown", "Создать нового человека?" in msg3.sent[-1][0])

    confirm_cb_msg = FakeMessage(user_id)
    confirm_cb_msg.sent = list(msg3.sent)  # same message being edited
    confirm_cb = FakeCallback(user_id, "pplcreate:confirm", confirm_cb_msg)
    await people.handle_create_confirm(confirm_cb, state3)
    final_text, final_kb = confirm_cb_msg.sent[-1]
    check("Final 'created' message has its keyboard stripped", kb_is_empty(final_kb))
    check("Final message text confirms creation", "Создал" in final_text)

    print("\nAll checks passed.")
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
