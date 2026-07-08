"""Eval harness for the intent classifier (services/llm_parser.py).

Runs a fixed set of test phrases — including every failure reported from
real usage — through the REAL Anthropic API and prints a pass/fail table.
Not a unit test (it costs real API calls and isn't deterministic across
model versions), just a fast way to see classifier regressions before
shipping a prompt change.

Run with:  python3 scripts/eval_classifier.py
"""

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from services import llm_parser  # noqa: E402


def expect_intent(intent: str):
    def check(parsed: dict) -> tuple[bool, str]:
        ok = parsed.get("intent") == intent
        return ok, f"intent={parsed.get('intent')!r} (expected {intent!r})"

    return check


def expect_time_hhmm(hhmm: str):
    def check(parsed: dict) -> tuple[bool, str]:
        event_time = parsed.get("event_time") or ""
        ok = event_time.endswith(f" {hhmm}")
        return ok, f"event_time={event_time!r} (expected ...{hhmm})"

    return check


def expect_time_ambiguous(value: bool):
    def check(parsed: dict) -> tuple[bool, str]:
        ok = bool(parsed.get("time_ambiguous")) == value
        return ok, f"time_ambiguous={parsed.get('time_ambiguous')!r} (expected {value})"

    return check


def expect_not_intent(intent: str):
    def check(parsed: dict) -> tuple[bool, str]:
        ok = parsed.get("intent") != intent
        return ok, f"intent={parsed.get('intent')!r} (must NOT be {intent!r})"

    return check


def expect_birthday_today():
    today = dt.datetime.now().strftime("%Y-%m-%d")

    def check(parsed: dict) -> tuple[bool, str]:
        birthday = parsed.get("birthday") or ""
        ok = birthday == today or birthday.endswith(today[5:])  # allow MM-DD or YYYY-MM-DD
        return ok, f"birthday={birthday!r} (expected {today} or {today[5:]})"

    return check


# Each case: (label, text, context, [checks])
CASES = [
    # --- Reported failure #1: congratulation phrasing with explicit time ---
    (
        "congrats+time (bug #1)",
        "Поздравить с днем рождения Валеру в 15 30",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("15:30"), expect_time_ambiguous(False)],
    ),
    # --- Reported failure #2: congratulation phrasing, "сегодня" only ---
    (
        "congrats+сегодня (bug #2)",
        "Поздравить с днем рождения Валеру сегодня",
        None,
        [expect_intent("create_reminder"), expect_time_ambiguous(True)],
    ),
    # --- Reported failure #3: context-dependent short reply to a birthday question ---
    (
        "context follow-up 'сегодня' (bug #3)",
        "сегодня",
        "Пользователь: у Валеры есть день рождения, но я не помню дату\n"
        "Бот: Когда у Валеры день рождения?",
        [expect_intent("add_person_info"), expect_birthday_today()],
    ),
    # --- Reported failure #4: determinism — see run_determinism_check() below ---
    # --- Cancel false positives ---
    (
        "congrats no time, variant A",
        "напомни поздравить Олю с днем рождения",
        None,
        [expect_intent("create_reminder"), expect_not_intent("delete_reminder"), expect_time_ambiguous(True)],
    ),
    (
        "congrats variant B",
        "поздравь Диму с днюхой завтра в 10",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("10:00"), expect_not_intent("delete_reminder")],
    ),
    (
        "congrats variant C",
        "не забудь поздравить маму с юбилеем",
        None,
        [expect_intent("create_reminder"), expect_not_intent("delete_reminder")],
    ),
    # --- Explicit cancel words (should be delete_reminder from the classifier;
    #     the app itself now also short-circuits these deterministically
    #     before calling the LLM at all — this just checks the model agrees) ---
    ("explicit cancel: отмени", "отмени напоминание", None, [expect_intent("delete_reminder")]),
    ("explicit cancel: удали", "удали напоминание про Валеру", None, [expect_intent("delete_reminder")]),
    ("explicit cancel: убери", "убери это напоминание", None, [expect_intent("delete_reminder")]),
    # --- Time parsing formats ---
    (
        "time: space instead of colon",
        "встреча в 15 30",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("15:30")],
    ),
    (
        "time: space, nonzero minutes",
        "встреча в 9 15",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("09:15")],
    ),
    (
        "time: hour only",
        "напомни выпить таблетки в 9",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("09:00")],
    ),
    (
        "time: пол шестого",
        "разбуди в пол шестого",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("17:30")],
    ),
    (
        "time: через час",
        "напомни про звонок через час",
        None,
        [expect_intent("create_reminder"), expect_time_ambiguous(False)],
    ),
    (
        "time: no time at all",
        "напомни позвонить бабушке",
        None,
        [expect_intent("create_reminder"), expect_time_ambiguous(True)],
    ),
    (
        "time: sanity baseline",
        "позвонить бабушке сегодня в 12",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("12:00")],
    ),
    (
        "recurrence: daily",
        "каждый день в 9 пить воду",
        None,
        [expect_intent("create_reminder"), expect_time_hhmm("09:00")],
    ),
    # --- Other intents (regression guard, not part of the reported bugs) ---
    (
        "list_reminders",
        "мои напоминания",
        None,
        [expect_intent("list_reminders")],
    ),
    (
        "list_reminders: alt phrasing",
        "покажи напоминания",
        None,
        [expect_intent("list_reminders")],
    ),
    (
        "add_person",
        "новый знакомый Валера, день рождения 12 июля, любит рыбалку",
        None,
        [expect_intent("add_person")],
    ),
    (
        "add_person_info",
        "у Валеры есть собака Рекс",
        None,
        [expect_intent("add_person_info")],
    ),
    (
        "add_person_info: alt phrasing",
        "Валера сказал, что любит хоккей",
        None,
        [expect_intent("add_person_info")],
    ),
    (
        "query_person",
        "что я знаю про Валеру?",
        None,
        [expect_intent("query_person")],
    ),
    (
        "query_person: alt phrasing",
        "расскажи про Игоря",
        None,
        [expect_intent("query_person")],
    ),
    (
        "list_people",
        "мои люди",
        None,
        [expect_intent("list_people")],
    ),
    (
        "search_notes",
        "кто любит рыбалку?",
        None,
        [expect_intent("search_notes")],
    ),
    (
        "add_alias",
        "Валера это тот же Валерий",
        None,
        [expect_intent("add_alias")],
    ),
    (
        "chitchat: greeting",
        "привет",
        None,
        [expect_intent("chitchat")],
    ),
    (
        "chitchat: thanks",
        "спасибо!",
        None,
        [expect_intent("chitchat")],
    ),
    # --- Context-dependent follow-up for the reminder-time flow ---
    (
        "context follow-up: time for a reminder",
        "сегодня в 12",
        "Пользователь: Поздравить с днём рождения Валеру\nБот: ⏰ Когда напомнить?",
        [expect_intent("create_reminder"), expect_time_hhmm("12:00")],
    ),
]


async def run_case(label: str, text: str, context: str | None, checks: list) -> tuple[bool, list[str]]:
    parsed = await llm_parser.parse_message(text, context=context)
    results = [check(parsed) for check in checks]
    passed = all(ok for ok, _ in results)
    reasons = [reason for ok, reason in results if not ok]
    return passed, reasons


async def run_determinism_check() -> tuple[bool, str]:
    text = "позвонить бабушке сегодня в 12"
    first = await llm_parser.parse_message(text)
    second = await llm_parser.parse_message(text)
    same = first.get("intent") == second.get("intent") and first.get("event_time") == second.get(
        "event_time"
    )
    detail = (
        f"run1=({first.get('intent')}, {first.get('event_time')}) "
        f"run2=({second.get('intent')}, {second.get('event_time')})"
    )
    return same, detail


async def main() -> None:
    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set — cannot run a live eval.")
        raise SystemExit(1)

    print(f"Running {len(CASES)} classifier cases against the real Anthropic API "
          f"({config.ANTHROPIC_MODEL}, temperature=0)...\n")

    rows = []
    passed_count = 0
    for label, text, context, checks in CASES:
        passed, reasons = await run_case(label, text, context, checks)
        rows.append((label, text, passed, reasons))
        passed_count += int(passed)

    det_passed, det_detail = await run_determinism_check()
    rows.append(("determinism: same msg twice (bug #4)", "позвонить бабушке сегодня в 12", det_passed, [] if det_passed else [det_detail]))
    passed_count += int(det_passed)

    total = len(rows)
    label_w = max(len(r[0]) for r in rows) + 2
    text_w = max(len(r[1]) for r in rows) + 2

    print(f"{'CASE':<{label_w}}{'INPUT':<{text_w}}{'RESULT'}")
    print("-" * (label_w + text_w + 10))
    for label, text, passed, reasons in rows:
        status = "PASS" if passed else "FAIL"
        print(f"{label:<{label_w}}{text:<{text_w}}{status}")
        for reason in reasons:
            print(f"{'':<{label_w}}{'':<{text_w}}  ↳ {reason}")

    print("-" * (label_w + text_w + 10))
    print(f"\n{passed_count}/{total} passed")

    if passed_count != total:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
