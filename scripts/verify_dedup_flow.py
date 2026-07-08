"""Manual verification of the dedup candidate-list flow (no LLM calls —
this exercises the routing/state logic directly with mocked Telegram
objects), for:

  - Диminutive-aware matching in the CREATE flow (Валерчик/Валера/Валеныч)
  - Candidate-list UX (buttons, "создать нового", "отмена")
  - Alias-offer on picking a candidate (Да -> alias saved, Нет -> not)
  - Zero-candidate case falls straight through to plain create-confirm
  - add_person_info flow is unaffected (still strict, direct add)

Run with: python3 scripts/verify_dedup_flow.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

TEST_DATA_DIR = Path(__file__).resolve().parent / "_verify_dedup_scratch"
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("TIMEZONE", "Europe/Kyiv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from db.repo import init_db, session_scope, create_person, get_person  # noqa: E402
from handlers import people  # noqa: E402
from handlers.states import PersonFlow  # noqa: E402


def check(label: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(f"Verification failed: {label}")


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id: int, text: str = ""):
        self.from_user = FakeUser(user_id)
        self.text = text
        self.sent: list[tuple[str, object]] = []

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
        pass


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


def button_texts(sent_entry) -> list[str]:
    _, kb = sent_entry
    if kb is None:
        return []
    return [btn.text for row in kb.inline_keyboard for btn in row]


def button_callback(sent_entry, matching_text_substr: str) -> str:
    _, kb = sent_entry
    for row in kb.inline_keyboard:
        for btn in row:
            if matching_text_substr in btn.text:
                return btn.callback_data
    raise AssertionError(f"no button containing {matching_text_substr!r}")


async def main() -> None:
    await init_db()
    user_id = 555

    async with session_scope() as session:
        valerchik = await create_person(session, user_id, "Валерчик", birthday_month=7, birthday_day=12)
        await session.commit()
        valerchik_id = valerchik.id

    # --- Scenario 1: "новый знакомый Валера" should surface Валерчик as a candidate ---
    state = new_state(user_id)
    msg = FakeMessage(user_id)
    parsed = {"person_name": "Валера", "person_facts": ["любит футбол"], "birthday": None, "person_tag": None}
    await people.start_add_person(msg, state, parsed)

    check("Candidate list shown for 'Валера' (not silent create)", len(msg.sent) == 1)
    texts = button_texts(msg.sent[0])
    check("Prompt asks 'кто-то из них'", msg.sent[0][0] == "Нашёл похожих. Это кто-то из них?")
    check("Валерчик appears as a candidate button", any("Валерчик" in t for t in texts))
    check("'создать нового' option present", any("создать нового" in t for t in texts))
    check("FSM state is 'candidates'", (await state.get_state()) == PersonFlow.candidates.state)

    dup_cb_data = button_callback(msg.sent[0], "Валерчик")

    # Tap the Валерчик candidate button
    cb_msg = FakeMessage(user_id)
    cb_msg.sent = msg.sent  # reuse for edit_text tracking, but track fresh list
    cb_msg.sent = []
    cb = FakeCallback(user_id, dup_cb_data, cb_msg)
    await people.handle_dup_candidate(cb, state)

    check("Alias question shown", "алиас" in cb_msg.sent[-1][0])
    check("Alias question mentions both names", "Валера" in cb_msg.sent[-1][0] and "Валерчик" in cb_msg.sent[-1][0])
    check("FSM state is 'confirm_alias_on_create'", (await state.get_state()) == PersonFlow.confirm_alias_on_create.state)

    yes_cb_data = button_callback(cb_msg.sent[-1], "Да")
    cb2 = FakeCallback(user_id, yes_cb_data, cb_msg)
    await people.handle_alias_yes_no(cb2, state)

    async with session_scope() as session:
        person = await get_person(session, valerchik_id)
        aliases = [a.alias for a in person.aliases]
        notes = [n.text for n in person.notes]

    check("'Валера' saved as alias to Валерчик", "Валера" in aliases)
    check("Fact from message saved as a note on Валерчик", "любит футбол" in notes)
    check("No new person was created (still same id)", person.id == valerchik_id)
    check("State cleared after completion", (await state.get_state()) is None)

    # --- Scenario 2: "новый знакомый Валеныч" should ALSO surface Валерчик ---
    state2 = new_state(user_id)
    msg2 = FakeMessage(user_id)
    parsed2 = {"person_name": "Валеныч", "person_facts": [], "birthday": None, "person_tag": None}
    await people.start_add_person(msg2, state2, parsed2)
    texts2 = button_texts(msg2.sent[0])
    check("Валерчик appears as a candidate for 'Валеныч'", any("Валерчик" in t for t in texts2))

    # Tap "Нет" this time — should attach info without saving an alias
    dup_cb_data2 = button_callback(msg2.sent[0], "Валерчик")
    cb_msg2 = FakeMessage(user_id)
    cb2a = FakeCallback(user_id, dup_cb_data2, cb_msg2)
    await people.handle_dup_candidate(cb2a, state2)
    no_cb_data = button_callback(cb_msg2.sent[-1], "Нет")
    cb2b = FakeCallback(user_id, no_cb_data, cb_msg2)
    await people.handle_alias_yes_no(cb2b, state2)

    async with session_scope() as session:
        person = await get_person(session, valerchik_id)
        aliases = [a.alias for a in person.aliases]

    check("'Валеныч' NOT saved as alias when user tapped Нет", "Валеныч" not in aliases)
    check("'Валера' alias from scenario 1 still present", "Валера" in aliases)

    # --- Scenario 3: zero candidates -> straight to plain create confirmation ---
    state3 = new_state(user_id)
    msg3 = FakeMessage(user_id)
    parsed3 = {"person_name": "Незнакомец", "person_facts": [], "birthday": None, "person_tag": None}
    await people.start_add_person(msg3, state3, parsed3)
    check("Zero matches -> straight to create-confirm (no candidate list)", "Создать нового человека?" in msg3.sent[0][0])
    check("FSM state is 'confirm_create' for zero matches", (await state3.get_state()) == PersonFlow.confirm_create.state)

    # --- Scenario 4: "создать нового" from the candidate list actually creates a new person ---
    state4 = new_state(user_id)
    msg4 = FakeMessage(user_id)
    parsed4 = {"person_name": "Валерка", "person_facts": [], "birthday": None, "person_tag": None}
    await people.start_add_person(msg4, state4, parsed4)
    new_cb_data = button_callback(msg4.sent[0], "создать нового")
    cb_msg4 = FakeMessage(user_id)
    cb4 = FakeCallback(user_id, new_cb_data, cb_msg4)
    await people.handle_dup_candidate(cb4, state4)
    check("'создать нового' shows the plain create-confirm prompt", "Создать нового человека?" in cb_msg4.sent[-1][0])
    check("FSM state is 'confirm_create' after choosing 'создать нового'", (await state4.get_state()) == PersonFlow.confirm_create.state)

    # --- Scenario 5: add_person_info flow is unaffected (strict threshold, direct add, no candidate list) ---
    state5 = new_state(user_id)
    msg5 = FakeMessage(user_id)
    parsed5 = {"person_name": "Валера", "person_facts": ["любит рыбалку"], "birthday": None}
    await people.start_add_person_info(msg5, state5, parsed5)
    # "Валера" vs "Валерчик" at the strict threshold (80) still clears via the
    # family-match bonus (92), so it should go straight to confirm_add_existing,
    # not a candidate list — this flow's UX is intentionally unchanged.
    check(
        "add_person_info still uses direct confirm (not candidate list)",
        "Добавляю к" in msg5.sent[0][0],
    )
    check("FSM state is 'confirm_add_existing' for add_person_info", (await state5.get_state()) == PersonFlow.confirm_add_existing.state)

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
