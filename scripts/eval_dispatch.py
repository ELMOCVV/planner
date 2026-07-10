"""Dispatch-level eval — 65 cases through the REAL production entry point
(handlers.common.dispatch_text, the actual aiogram message handler), not
the classifier in isolation.

Why dispatch-level: a pre-LLM keyword route once intercepted messages
before the classifier and shipped a bug invisible to a classifier-only
eval (production symptom: replies in 26-109ms with zero LLM_CLASSIFY log
lines). Each case here asserts the final reply, whether the classifier
actually ran (call-counting monkeypatch), and flags LLM-attributed
responses under 150ms as suspiciously fast for a real API round-trip.

Output ends with a confusion-style summary (pass rate per intent group)
and a determinism fingerprint — run the script twice and compare the
fingerprint lines; they must be identical.

Run with: python3 scripts/eval_dispatch.py
"""

import asyncio
import datetime as dt
import hashlib
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
from zoneinfo import ZoneInfo  # noqa: E402

from db.repo import add_note, create_person, create_reminder, init_db, session_scope  # noqa: E402
from handlers import common  # noqa: E402
from services import conversation, llm_parser  # noqa: E402

TZ = ZoneInfo(config.TIMEZONE)

_real_parse_message = llm_parser.parse_message
_calls = {"n": 0}


async def _counting_parse_message(text, context=None):
    _calls["n"] += 1
    return await _real_parse_message(text, context=context)


llm_parser.parse_message = _counting_parse_message

SUSPICIOUSLY_FAST_MS = 150


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass


class FakeMessage:
    def __init__(self, uid, text):
        self.from_user = FakeUser(uid)
        self.chat = FakeChat(uid)
        self.bot = FakeBot()
        self.text = text
        self.sent: list[str] = []  # flattened text + button labels

    def _rec(self, text, kb):
        self.sent.append(text)
        if kb is not None:
            for row in kb.inline_keyboard:
                for b in row:
                    self.sent.append(b.text)

    async def answer(self, text, reply_markup=None, **kw):
        self._rec(text, reply_markup)
        return self

    async def answer_document(self, *a, **kw):
        self.sent.append("<document>")
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self._rec(text, reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None, **kw):
        return self

    async def delete(self):
        pass


def new_state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


# ---- check helpers: each returns (ok, reason) -------------------------------


def llm(expected: bool):
    def c(replies, calls, ms):
        ok = (calls > 0) == expected
        return ok, f"llm_calls={calls} (expected {'>0' if expected else '0'})"

    return c


def has(substr):
    def c(replies, calls, ms):
        joined = "\n".join(replies)
        return substr in joined, f"replies missing {substr!r}: {replies!r}"

    return c


def has_any(*subs):
    def c(replies, calls, ms):
        joined = "\n".join(replies)
        ok = any(s in joined for s in subs)
        return ok, f"replies missing all of {subs!r}: {replies!r}"

    return c


def lacks(substr):
    def c(replies, calls, ms):
        joined = "\n".join(replies)
        return substr not in joined, f"replies must NOT contain {substr!r}: {replies!r}"

    return c


# Offsets menu vs "time already passed" reschedule prompt: both are correct
# branches of the create flow depending on wall-clock time at eval runtime.
CREATE_OK = has_any("За сколько напомнить", "уже прошло")

# ---- cases: (group, label, text, context_seed, checks) ----------------------
# context_seed: list of ("user"|"bot", text) pre-recorded into conversation
# history before dispatch (simulating a prior exchange).

CASES = [
    # --- create_reminder ---
    ("create_reminder", "explicit tomorrow time", "позвонить бабушке завтра в 12", None,
     [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "hour only (may be past)", "напомни выпить таблетки в 9", None,
     [llm(True), CREATE_OK]),
    ("create_reminder", "congrats + '15 30' (prod bug #1)", "Поздравить с днем рождения Валеру в 15 30", None,
     [llm(True), CREATE_OK, lacks("Отменено")]),
    ("create_reminder", "congrats + сегодня (prod bug #2)", "Поздравить с днем рождения Валеру сегодня", None,
     [llm(True), has("Когда напомнить")]),
    ("create_reminder", "no time at all", "напомни позвонить бабушке", None,
     [llm(True), has("Когда напомнить")]),
    ("create_reminder", "через час", "напомни про звонок через час", None,
     [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "recurrence daily", "каждый день в 9 пить воду", None,
     [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "пол шестого", "разбуди в пол шестого", None,
     [llm(True), CREATE_OK]),
    ("create_reminder", "typo in verb", "напомни позваниь бабушке завтра в 10", None,
     [llm(True), CREATE_OK]),
    ("create_reminder", "emoji in text", "напомни купить торт 🎂 завтра в 18:00", None,
     [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "multi-line", "напомни:\nкупить хлеб\nзавтра в 9", None,
     [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "mixed intent: reminder + person fact", "напомни позвонить Валере завтра, кстати у него собака Рекс", None,
     [llm(True), has_any("Когда напомнить", "За сколько напомнить"), has("Кстати")]),
    ("create_reminder", "cancel-word inside sentence (guard fix)", "напомни, что не надо покупать хлеб", None,
     [llm(True), has_any("Когда напомнить", "За сколько напомнить")]),
    # --- time formats within create ---
    ("create_reminder", "space time 9 15", "встреча в 9 15", None, [llm(True), CREATE_OK]),
    ("create_reminder", "glued time 1530", "встреча завтра в 1530", None, [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "полдень", "напомни в полдень пообедать", None, [llm(True), CREATE_OK]),
    ("create_reminder", "через полчаса", "напомни через полчаса сделать паузу", None,
     [llm(True), has("За сколько напомнить")]),
    ("create_reminder", "weekday + time", "напомни в воскресенье в 10 позвонить маме", None,
     [llm(True), has("За сколько напомнить")]),
    # --- query_reminder (the known open gap) ---
    ("query_reminder", "напомнишь ли 12 июля (open gap)", "Напомнишь ли ты мне это 12 июля?", None,
     [llm(True), has("Да, напомню"), has("12 июля"), lacks("Когда напомнить")]),
    ("query_reminder", "есть напоминание про бабушку?", "есть напоминание про бабушку?", None,
     [llm(True), has("Да, напомню"), lacks("Когда напомнить")]),
    ("query_reminder", "date with nothing scheduled", "Напомнишь ли ты мне что-нибудь 25 декабря?", None,
     [llm(True), has("пока нет напоминаний")]),
    ("query_reminder", "не забудешь про цветы?", "ты не забудешь про цветы?", None,
     [llm(True), has("Да, напомню")]),
    # --- delete/cancel (the one justified pre-LLM shortcut) ---
    ("cancel_guard", "bare отмени", "отмени", None,
     [llm(False), has("Сейчас ничего не выполняется")]),
    ("cancel_guard", "удали напоминание про бабушку", "удали напоминание про бабушку", None,
     [llm(False), has("Твои напоминания")]),
    ("cancel_guard", "bare убери", "убери", None, [llm(False)]),
    ("cancel_guard", "bare стоп", "стоп", None, [llm(False)]),
    # --- add_person ---
    ("add_person", "new person, dup candidate exists", "новый знакомый Валера, день рождения 12 июля, любит рыбалку", None,
     [llm(True), has("Нашёл похожих")]),
    ("add_person", "new person, no match", "новый знакомый Игорь", None,
     [llm(True), has("Создать нового человека?")]),
    ("add_person", "same first name exists", "новая знакомая Оля из спортзала", None,
     [llm(True), has("Нашёл похожих")]),
    ("add_person", "colloquial phrasing", "запиши нового человека: Петя, коллега", None,
     [llm(True), has("Создать нового человека?")]),
    ("add_person", "diminutive of existing (Валеныч)", "новый знакомый Валеныч", None,
     [llm(True), has("Нашёл похожих")]),
    ("add_person", "typo in intro", "новый знакомы Тимур", None,
     [llm(True), has("Создать нового человека?")]),
    # --- add_person_info ---
    ("add_person_info", "plain fact", "у Валерчика есть кот Барсик", None,
     [llm(True), has("Добавляю к")]),
    ("add_person_info", "У + др + date (prod bug)", "У Валерчика др 12 июля", None,
     [llm(True), has("Добавляю к"), lacks("этом месяце")]),
    ("add_person_info", "said-that phrasing", "Валерчик сказал, что любит хоккей", None,
     [llm(True), has("Добавляю к")]),
    ("add_person_info", "др колл. word order", "др валерчика 12 июля", None,
     [llm(True), has("Добавляю к")]),
    ("add_person_info", "днюха colloquial", "у Валерчика днюха 12 июля", None,
     [llm(True), has("Добавляю к")]),
    ("add_person_info", "unknown person fact", "у Пети деньрож 5 марта", None,
     [llm(True), has_any("Не нашёл", "Создать нового")]),
    # --- query_person ---
    ("query_person", "что я знаю про", "что я знаю про Валерчика?", None,
     [llm(True), has("Валерчик")]),
    ("query_person", "расскажи про", "расскажи про Валерчика", None,
     [llm(True), has("Валерчик")]),
    ("query_person", "когда др у X (question)", "Когда др у Валерчика?", None,
     [llm(True), has("Валерчик"), lacks("Добавляю к")]),
    ("query_person", "у кого др в июле (mass)", "У кого др в июле?", None,
     [llm(True), has("Валерчик")]),
    ("query_person", "кто такая", "кто такая Оля?", None,
     [llm(True), has("Оля")]),
    ("query_person", "unknown person", "что я знаю про Свету?", None,
     [llm(True), has("Не нашёл")]),
    # --- search_notes ---
    ("search_notes", "кто любит рыбалку", "кто любит рыбалку?", None,
     [llm(True), has("Валерчик")]),
    ("search_notes", "у кого собака", "у кого есть собака?", None,
     [llm(True), has("Валерчик")]),
    ("search_notes", "no results", "кто ходит в спортзал?", None,
     [llm(True), has_any("Никого не нашёл", "не нашёл")]),
    # --- lists ---
    # Exact phrases ("мои напоминания", "мои люди", "настройки") are handled
    # by dedicated routers registered BEFORE common; they never reach
    # dispatch_text in production, so those cases live in EXACT_ROUTE_CASES
    # below and call the same handler the router dispatches to.
    ("list_reminders", "free phrasing (via LLM)", "покажи напоминания", None,
     [llm(True), has("Твои напоминания")]),
    ("list_people", "free phrasing (via LLM)", "покажи всех людей", None,
     [llm(True), has("Твои люди")]),
    # --- add_alias ---
    ("add_alias", "X это тот же Y", "Валера это тот же Валерчик", None,
     [llm(True), has("Готово")]),
    ("add_alias", "second alias", "Шурик это тот же Валерчик", None,
     [llm(True), has("Готово")]),
    # --- chitchat / unknown ---
    ("chitchat", "привет", "привет", None, [llm(True), lacks("Не понял сообщение")]),
    ("chitchat", "спасибо", "спасибо!", None, [llm(True), lacks("Не понял сообщение")]),
    ("unknown", "keyboard mash", "ыфвафыва", None, [llm(True), has("Не понял")]),
    ("unknown", "emoji only", "🎂🎂🎂", None, [llm(True)]),
    # --- context follow-ups (short replies to a prior bot question) ---
    ("context", "time reply after 'Когда напомнить?' (prod bug #3)",
     "сегодня в 23:55",
     [("user", "Поздравить с днём рождения Валеру"), ("bot", "⏰ Когда напомнить?")],
     [llm(True), CREATE_OK]),
    ("context", "date reply after birthday question",
     "12 июля",
     [("user", "у Валерчика скоро день рождения"), ("bot", "Когда у Валерчика день рождения?")],
     [llm(True), has_any("Добавляю к", "Валерчик")]),
    ("context", "bare 'сегодня' after birthday question",
     "сегодня",
     [("user", "у Валеры есть день рождения, но я не помню дату"), ("bot", "Когда у Валеры день рождения?")],
     [llm(True), has_any("Добавляю к", "Валерчик")]),
    ("context", "bare time after reminder question",
     "в 19 30",
     [("user", "напомни полить цветы"), ("bot", "⏰ Когда напомнить?")],
     [llm(True), CREATE_OK]),
    # --- birthday statements for the second person ---
    ("add_person_info", "др for Оля", "у Оли др 3 января", None,
     [llm(True), has("Добавляю к")]),
    ("add_person_info", "днюха у Оли", "днюха у Оли 3 января", None,
     [llm(True), has("Добавляю к")]),
    ("add_person_info", "spelled-out date", "запиши: у Оли день рождения третьего января", None,
     [llm(True), has("Добавляю к")]),
]


# Exact-phrase routes: matched by dedicated routers registered before
# common (verified in the Phase 1 route audit), so production never sends
# them through dispatch_text. Each entry names the handler the router
# dispatches to; the eval asserts the reply AND that the LLM stays out.
def _exact_route_cases():
    from handlers import people, reminders, settings

    return [
        ("list_reminders", "exact phrase 'мои напоминания' (pre-LLM route)", "мои напоминания",
         reminders.list_reminders_cmd, [llm(False), has("Твои напоминания")]),
        ("list_people", "exact phrase 'мои люди' (pre-LLM route)", "мои люди",
         people.people_cmd, [llm(False), has("Твои люди")]),
        ("settings", "exact phrase 'настройки' (pre-LLM route)", "настройки",
         settings.cmd_settings, [llm(False), has("⚙️ Настройки")]),
    ]


async def run() -> None:
    await init_db()
    uid = 4242

    now = dt.datetime.now(TZ)
    year = now.year if now <= dt.datetime(now.year, 7, 12, 15, 0, tzinfo=TZ) else now.year + 1
    async with session_scope() as session:
        p = await create_person(session, uid, "Валерчик", birthday_month=7, birthday_day=12)
        await add_note(session, p.id, "любит рыбалку")
        await add_note(session, p.id, "есть собака Рекс")
        await create_person(session, uid, "Оля")
        await create_reminder(session, uid, "поздравить бабушку", dt.datetime(year, 7, 12, 15, 0))
        await create_reminder(session, uid, "полить цветы", (now + dt.timedelta(days=1)).replace(hour=10, minute=0, tzinfo=None))
        await session.commit()

    rows = []
    for group, label, text, ctx_seed, checks in CASES:
        conversation.clear(uid)
        if ctx_seed:
            for role, t in ctx_seed:
                (conversation.record_user if role == "user" else conversation.record_bot)(uid, t)
        _calls["n"] = 0
        msg = FakeMessage(uid, text)
        state = new_state(uid)
        start = time.monotonic()
        try:
            await common.dispatch_text(msg, state)
            error = None
        except Exception as exc:  # a crash is itself a failure, not an abort
            error = f"EXCEPTION: {exc!r}"
        ms = (time.monotonic() - start) * 1000
        calls = _calls["n"]

        results = [] if error is None else [(False, error)]
        if error is None:
            results = [c(msg.sent, calls, ms) for c in checks]
            if calls > 0 and ms < SUSPICIOUSLY_FAST_MS:
                results.append((False, f"suspiciously fast ({ms:.0f}ms) for a real LLM call"))
        passed = all(ok for ok, _ in results)
        reasons = [r for ok, r in results if not ok]
        rows.append((group, label, text, ms, calls, passed, reasons))

    for group, label, text, handler, checks in _exact_route_cases():
        conversation.clear(uid)
        _calls["n"] = 0
        msg = FakeMessage(uid, text)
        start = time.monotonic()
        try:
            await handler(msg)
            error = None
        except Exception as exc:
            error = f"EXCEPTION: {exc!r}"
        ms = (time.monotonic() - start) * 1000
        results = [(False, error)] if error else [c(msg.sent, _calls["n"], ms) for c in checks]
        passed = all(ok for ok, _ in results)
        rows.append((group, label, text, ms, _calls["n"], passed, [r for ok, r in results if not ok]))

    # ---- table ----
    label_w = max(len(r[1]) for r in rows) + 2
    print(f"{'CASE':<{label_w}}{'GROUP':<18}{'MS':<7}{'LLM':<4}{'RESULT'}")
    print("-" * (label_w + 40))
    for group, label, text, ms, calls, passed, reasons in rows:
        print(f"{label:<{label_w}}{group:<18}{ms:<7.0f}{calls:<4}{'PASS' if passed else 'FAIL'}")
        for r in reasons:
            print(f"{'':<{label_w}}  ↳ {r}")
    print("-" * (label_w + 40))

    # ---- confusion-style summary by intent group ----
    groups: dict[str, list[bool]] = {}
    for group, _, _, _, _, passed, _ in rows:
        groups.setdefault(group, []).append(passed)
    print("\nPer-intent summary:")
    for group in sorted(groups):
        results = groups[group]
        n_pass = sum(results)
        mark = "✓" if n_pass == len(results) else "✗"
        print(f"  {mark} {group:<18} {n_pass}/{len(results)}")

    total_pass = sum(1 for r in rows if r[5])
    print(f"\n{total_pass}/{len(rows)} passed")

    # ---- determinism fingerprint: identical across runs iff every case
    # produced the same pass/fail verdict ----
    fp = hashlib.sha256(
        "|".join(f"{r[1]}={int(r[5])}" for r in rows).encode()
    ).hexdigest()[:16]
    print(f"determinism-fingerprint: {fp}")

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    if total_pass != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
