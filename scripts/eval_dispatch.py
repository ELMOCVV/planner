"""Dispatch-level eval — calls the REAL production entry point
(handlers.common.dispatch_text, the actual aiogram message handler),
not services.llm_parser.parse_message in isolation.

This is the eval that would have caught the reported bug: "У Валерчика
др 12 июля" was answered in 26-109ms with zero LLM_CLASSIFY log lines,
because handlers/common.py's dispatch_text ran a pre-LLM keyword route
(people.detect_birthday_month_query — matched on "др" + a month-name
substring) that intercepted the message before the classifier ever saw
it. scripts/eval_classifier.py alone could never catch that class of bug
since it calls llm_parser.parse_message directly, bypassing dispatch_text
entirely — exactly the blind spot that let this ship.

For each case this script:
  - monkeypatches services.llm_parser.parse_message with a counting
    wrapper (still calls the real implementation — real API, real cost)
    so we can assert whether the classifier actually ran;
  - calls handlers.common.dispatch_text(fake_message, fake_state) exactly
    as aiogram would for an incoming text message;
  - checks the actual reply text sent back, AND whether the LLM was
    called, AND flags suspiciously fast responses (a real Anthropic call
    takes ~300ms+; a bypassed one is typically <50ms) as an extra signal
    that something intercepted the message pre-LLM.

Run with: python3 scripts/eval_dispatch.py
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

TEST_DATA_DIR = Path(__file__).resolve().parent / "_eval_dispatch_scratch"
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TIMEZONE", "Europe/Kyiv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

if not config.ANTHROPIC_API_KEY:
    print("ANTHROPIC_API_KEY is not set — cannot run a live dispatch eval.")
    raise SystemExit(1)

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from db.repo import init_db, session_scope, create_person  # noqa: E402
from handlers import common  # noqa: E402
from services import llm_parser  # noqa: E402

# --- instrument the real classifier with a call counter, without
# changing production code: patch the module attribute, which every
# caller looks up dynamically (llm_parser.parse_message(...)) so this
# takes effect everywhere regardless of import style. ---
_real_parse_message = llm_parser.parse_message
_call_count = {"n": 0}


async def _counting_parse_message(text, context=None):
    _call_count["n"] += 1
    return await _real_parse_message(text, context=context)


llm_parser.parse_message = _counting_parse_message

# A real network call to Anthropic is typically 300ms+; a pre-LLM
# shortcut that never touches the network is usually well under 100ms.
# This is exactly the symptom the production logs showed (26-109ms).
SUSPICIOUSLY_FAST_MS = 150


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    def __init__(self, user_id: int, text: str):
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.text = text
        self.sent: list[str] = []  # flattened text + any button labels, for easy substring checks

    def _record(self, text, reply_markup):
        self.sent.append(text)
        if reply_markup is not None:
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    self.sent.append(btn.text)

    async def answer(self, text, reply_markup=None, **kwargs):
        self._record(text, reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self._record(text, reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        return self

    async def delete(self):
        pass


def new_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def expect_llm_called(expected: bool):
    def check(replies, calls_made, elapsed_ms):
        ok = (calls_made > 0) == expected
        return ok, f"llm_calls={calls_made} (expected {'>0' if expected else '0'})"

    return check


def expect_reply_contains(substr: str):
    def check(replies, calls_made, elapsed_ms):
        joined = "\n".join(replies)
        ok = substr in joined
        return ok, f"replies={replies!r} (expected to contain {substr!r})"

    return check


def expect_reply_not_contains(substr: str):
    def check(replies, calls_made, elapsed_ms):
        joined = "\n".join(replies)
        ok = substr not in joined
        return ok, f"replies={replies!r} (must NOT contain {substr!r})"

    return check


CASES = [
    (
        "birthday STATEMENT with date (the reported bug)",
        "У Валерчика др 12 июля",
        [
            expect_llm_called(True),
            expect_reply_contains("Добавляю к"),
            expect_reply_not_contains("этом месяце"),
        ],
    ),
    (
        "birthday QUESTION about one person",
        "Когда др у Валерчика?",
        [expect_llm_called(True), expect_reply_not_contains("этом месяце")],
    ),
    (
        "birthday QUESTION about a month (mass query)",
        "У кого др в июле?",
        [expect_llm_called(True), expect_reply_contains("Валерчик")],
    ),
    (
        "explicit cancel (the one justified pre-LLM shortcut)",
        "отмени",
        [expect_llm_called(False), expect_reply_contains("Нечего отменять")],
    ),
    (
        "congrats + explicit time (previously-fixed bug, full path)",
        "Поздравить с днем рождения Валеру в 15 30",
        [expect_llm_called(True), expect_reply_contains("Напоминание")],
    ),
    (
        "congrats, no time (previously-fixed bug, full path)",
        "Поздравить с днем рождения Валеру сегодня",
        [expect_llm_called(True), expect_reply_contains("Когда напомнить")],
    ),
]


async def main() -> None:
    await init_db()
    user_id = 4242

    async with session_scope() as session:
        # Birthday is seeded directly (not via the message flow) because
        # add_person_info requires a button-tap confirmation this script
        # doesn't simulate — dispatch_text only gets as far as showing
        # "Добавляю к Валерчик...?" and stops there, same as a real user
        # who hasn't tapped "Да" yet.
        await create_person(session, user_id, "Валерчик", birthday_month=7, birthday_day=12)
        await session.commit()

    rows = []
    passed_count = 0
    for label, text, checks in CASES:
        _call_count["n"] = 0
        msg = FakeMessage(user_id, text)
        state = new_state(user_id)

        start = time.monotonic()
        await common.dispatch_text(msg, state)
        elapsed_ms = (time.monotonic() - start) * 1000

        calls_made = _call_count["n"]
        results = [check(msg.sent, calls_made, elapsed_ms) for check in checks]

        # Extra automatic check: if the LLM was supposedly called but the
        # whole dispatch took less time than a real network round-trip
        # plausibly could, something intercepted it before the LLM ran —
        # this is the literal symptom from the production logs.
        if calls_made > 0 and elapsed_ms < SUSPICIOUSLY_FAST_MS:
            results.append(
                (
                    False,
                    f"elapsed={elapsed_ms:.0f}ms with {calls_made} LLM call(s) — "
                    f"suspiciously fast for a real API round-trip (<{SUSPICIOUSLY_FAST_MS}ms)",
                )
            )

        passed = all(ok for ok, _ in results)
        reasons = [reason for ok, reason in results if not ok]
        passed_count += int(passed)
        rows.append((label, text, elapsed_ms, calls_made, passed, reasons))

    label_w = max(len(r[0]) for r in rows) + 2
    text_w = max(len(r[1]) for r in rows) + 2
    print(f"{'CASE':<{label_w}}{'INPUT':<{text_w}}{'MS':<8}{'LLM':<5}{'RESULT'}")
    print("-" * (label_w + text_w + 25))
    for label, text, elapsed_ms, calls_made, passed, reasons in rows:
        status = "PASS" if passed else "FAIL"
        print(f"{label:<{label_w}}{text:<{text_w}}{elapsed_ms:<8.0f}{calls_made:<5}{status}")
        for reason in reasons:
            print(f"{'':<{label_w}}{'':<{text_w}}  ↳ {reason}")

    print("-" * (label_w + text_w + 25))
    print(f"\n{passed_count}/{len(rows)} passed")

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    if passed_count != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
