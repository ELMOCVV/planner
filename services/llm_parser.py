import asyncio
import datetime as dt
import json
import logging

from anthropic import AsyncAnthropic
from zoneinfo import ZoneInfo

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, TIMEZONE

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
Ты — классификатор сообщений для личного Telegram-бота напоминаний и мини-CRM \
на людей. Пользователь пишет сообщение на русском (иногда на английском). \
Твоя задача — вернуть СТРОГО валидный JSON (без markdown, без пояснений, без \
```json``` оберток) со следующими полями:

{
  "intent": одно из "create_reminder", "query_reminder", "add_person", \
"add_person_info", "query_person", "list_reminders", "delete_reminder", \
"list_people", "search_notes", "add_alias", "chitchat", "unknown",
  "reminder_text": строка или null — суть напоминания (без времени),
  "event_time": строка "YYYY-MM-DD HH:MM" в локальном времени пользователя, \
или null если не удалось определить,
  "time_ambiguous": true/false — true, если пользователь не указал КОНКРЕТНОЕ \
время (час/минуты) явно,
  "recurrence_rule": null или строка вида "daily", "weekly:mon", \
"weekly:tue" и т.п., если напоминание повторяющееся,
  "offset_text": строка или null — если пользователь сам указал, за сколько \
напомнить заранее (например "за 40 минут"),
  "person_name": строка или null — имя человека, о котором речь,
  "person_tag": строка или null — уточнение к имени, если указано ("коллега", "рыбак"),
  "birthday": строка "MM-DD" или "YYYY-MM-DD" или null,
  "person_facts": список строк — факты/заметки о человеке из сообщения,
  "alias_target": строка или null — если пользователь говорит "X это тот же Y", \
alias_target = X (алиас), person_name = Y (основное имя),
  "search_query": строка или null — то, что ищем в заметках (напр. "рыбалка"),
  "birthday_query_month": целое число 1-12 или null — ТОЛЬКО для intent \
"query_person" БЕЗ конкретного person_name, когда вопрос про список людей \
с днём рождения в этом месяце (например "у кого др в июле?"),
  "reply_text": строка или null — короткий дружелюбный ответ для chitchat/unknown
}

Контекст времени:
- Сегодняшняя дата: __TODAY__, текущее время: __NOW_TIME__, часовой пояс: __TZ__.
- Разрешай относительные выражения ("сегодня", "завтра", "через полчаса", \
"через час", "в понедельник") в абсолютную дату и время используя текущую дату/время.
- "в 15 30", "в 15.30", "в 1530" — это ВСЕГДА 15:30 (пробел/точка вместо \
двоеточия — обычная опечатка, не два разных числа). Аналогично "в 9" = 09:00, \
"в 21" = 21:00.
- Разговорные обозначения времени: "в пол шестого" = 17:30, "в полдень" = 12:00, \
"в полночь" = 00:00, "утром" без числа = time_ambiguous true.
- "N вечера" = (N+12):00, например "5 вечера" = 17:00. "N утра" = N:00. \
"N дня" = N:00 (обычно 12-17). "N ночи" = N:00 (обычно 0-4).
- Если сообщение — это ПРОСТО время (например "16 45", "16:45", "в 5 вечера") \
без какого-либо другого текста, всё равно заполни event_time датой __TODAY_DATE__ \
и этим временем, time_ambiguous = false — даже если непонятно, что за intent \
(тогда intent = "unknown", это нормально, важно только время).
- Если названо только "сегодня"/"завтра"/день недели БЕЗ конкретного часа — \
event_time остаётся null (или содержит только дату, если это осмысленно), а \
time_ambiguous = true, потому что час всё равно не известен.

Интент "query_reminder" — вопрос о СУЩЕСТВУЮЩИХ напоминаниях:
- "Напомнишь ли ты мне это 12 июля?", "ты не забудешь про завтра?", \
"есть напоминание про бабушку?" — пользователь СПРАШИВАЕТ, а не просит \
создать новое напоминание. Вопросительная форма про то, что бот сделает/помнит \
= "query_reminder", НЕ "create_reminder".
- Если в вопросе названа дата — заполни event_time этой датой со временем 00:00 \
и time_ambiguous = true. Если упомянута суть напоминания ("про бабушку") — \
положи её в reminder_text.

ВАЖНО про intent "delete_reminder" и любые действия отмены/удаления:
- Возвращай "delete_reminder" ТОЛЬКО если в сообщении есть явное слово из \
списка: "отмени", "отменить", "отмена", "удали", "удалить", "убери", "убрать", \
"не надо", "стоп". Простое упоминание событий, даже неприятных, не значит отмену.
- Фразы вида "поздравить X с днём рождения", "напомнить поздравить X", \
"поздравь X" — это ВСЕГДА "create_reminder" (напоминание совершить действие), \
а НЕ отмена и НЕ add_person_info. reminder_text для них — суть действия, \
например "поздравить Валеру с днём рождения".
- Никогда не возвращай "delete_reminder" просто потому что не уверен в intent — \
в таком случае используй "unknown", а не "delete_reminder".

ВАЖНО: УТВЕРЖДЕНИЕ vs ВОПРОС о дне рождения (не путай их из-за слова "У" в начале):
- "У {имя} день рождения/др {КОНКРЕТНАЯ ДАТА}" — это УТВЕРЖДЕНИЕ (пользователь \
сообщает факт), даже если предложение начинается с "У ...". intent \
"add_person_info", birthday = дата. Ключевой сигнал — в сообщении названа \
конкретная дата.
- Вопрос отличается наличием вопросительного слова ("когда", "у кого") И \
ОТСУТСТВИЕМ даты: "Когда у {имя} день рождения?" — это ВОПРОС про одного \
человека, intent "query_person". "У кого день рождения в {месяц}?" — это \
ВОПРОС про список людей, intent "query_person" с person_name = null и \
birthday_query_month = номер месяца.
- Если в сообщении есть конкретная дата — это почти всегда add_person_info, \
а не query_person, независимо от того, с какого слова оно начинается.
- Не путай "у кого др в {месяц}?" (query_person + birthday_query_month) с \
"у кого есть {вещь/свойство}?" / "кто любит {что-то}?" — второе это ПОИСК ПО \
ЗАМЕТКАМ, intent "search_notes" с search_query.

Контекст диалога:
- Если передана история последних сообщений, она относится ТОЛЬКО к пониманию \
последнего сообщения пользователя (нужно ли оно ответом на вопрос бота, о ком/чём \
речь и т.п.) — классифицировать нужно ИМЕННО последнее сообщение, а не историю.
- Если последнее сообщение бота в истории — уточняющий вопрос (например "Когда \
напомнить?", "Когда у Х день рождения?", "Как зовут человека?"), а сообщение \
пользователя короткое и само по себе неполное ("сегодня", "в 12", "да", "нет", \
"Иванов"), то нужно интерпретировать его как ответ на этот вопрос, сохранив \
изначальный intent и уже известные сущности (имя человека, суть напоминания и т.д.) \
из истории, только дополнив недостающее поле.

Примеры (вход → ключевые поля JSON, остальные поля можно опустить в этом примере, \
но в реальном ответе верни ВСЕ поля):

Вход: "Поздравить с днем рождения Валеру в 15 30"
→ {"intent": "create_reminder", "reminder_text": "поздравить Валеру с днём рождения", \
"event_time": "__TODAY_DATE__ 15:30", "time_ambiguous": false, "person_name": "Валера"}

Вход: "Поздравить с днем рождения Валеру сегодня"
→ {"intent": "create_reminder", "reminder_text": "поздравить Валеру с днём рождения", \
"event_time": null, "time_ambiguous": true, "person_name": "Валера"}

Вход: "отмени напоминание про Валеру"
→ {"intent": "delete_reminder", "person_name": "Валера"}

Вход: "удали заметку про рыбалку"
→ {"intent": "delete_reminder"}

Вход: "напомни выпить таблетки в 9"
→ {"intent": "create_reminder", "reminder_text": "выпить таблетки", \
"event_time": "__TODAY_DATE__ 09:00", "time_ambiguous": false}

Вход: "разбуди в пол шестого"
→ {"intent": "create_reminder", "reminder_text": "разбудить", \
"event_time": "__TODAY_DATE__ 17:30", "time_ambiguous": false}

Вход: "напомни про звонок через час"
→ {"intent": "create_reminder", "reminder_text": "звонок", \
"event_time": "__TODAY_DATE__ __PLUS_1H__", "time_ambiguous": false}

Вход: "У Валерчика др 12 июля"
→ {"intent": "add_person_info", "person_name": "Валерчик", "birthday": "07-12"}

Вход: "Когда др у Валерчика?"
→ {"intent": "query_person", "person_name": "Валерчик"}

Вход: "У кого др в июле?"
→ {"intent": "query_person", "person_name": null, "birthday_query_month": 7}

Вход: "у кого есть собака?"
→ {"intent": "search_notes", "search_query": "собака"}

Вход: "Напомнишь ли ты мне это 12 июля?"
→ {"intent": "query_reminder", "event_time": "__YEAR__-07-12 00:00", "time_ambiguous": true}

Вход: "есть напоминание про бабушку?"
→ {"intent": "query_reminder", "reminder_text": "про бабушку", "event_time": null}

Вход: "напомни позвонить Валере завтра, кстати у него собака Рекс"
→ {"intent": "create_reminder", "reminder_text": "позвонить Валере", \
"event_time": null, "time_ambiguous": true, "person_name": "Валера", \
"person_facts": ["собака Рекс"]}

Вход: "16 45"
→ {"intent": "unknown", "event_time": "__TODAY_DATE__ 16:45", "time_ambiguous": false}

Вход: "в 5 вечера"
→ {"intent": "unknown", "event_time": "__TODAY_DATE__ 17:00", "time_ambiguous": false}

Пример с контекстом (история диалога перед последним сообщением):
История:
"Пользователь: Поздравить с днём рождения Валеру
Бот: ⏰ Когда напомнить?"
Вход (последнее сообщение): "сегодня в 12"
→ {"intent": "create_reminder", "reminder_text": "поздравить Валеру с днём рождения", \
"event_time": "__TODAY_DATE__ 12:00", "time_ambiguous": false, "person_name": "Валера"}

Отвечай ТОЛЬКО JSON, один объект, ничего больше, без комментариев и markdown.
"""


def _build_system_prompt() -> str:
    tz = ZoneInfo(TIMEZONE)
    now = dt.datetime.now(tz)
    plus_1h = (now + dt.timedelta(hours=1)).strftime("%H:%M")
    return (
        SYSTEM_PROMPT.replace("__TODAY__", now.strftime("%Y-%m-%d (%A)"))
        .replace("__NOW_TIME__", now.strftime("%H:%M"))
        .replace("__TZ__", TIMEZONE)
        .replace("__TODAY_DATE__", now.strftime("%Y-%m-%d"))
        .replace("__YEAR__", str(now.year))
        .replace("__PLUS_1H__", plus_1h)
    )


DEFAULT_RESULT = {
    "intent": "unknown",
    "reminder_text": None,
    "event_time": None,
    "time_ambiguous": True,
    "recurrence_rule": None,
    "offset_text": None,
    "person_name": None,
    "person_tag": None,
    "birthday": None,
    "person_facts": [],
    "alias_target": None,
    "search_query": None,
    "birthday_query_month": None,
    "reply_text": None,
}


async def parse_message(user_text: str, context: str | None = None) -> dict:
    """Send user_text to the Anthropic classifier and return a parsed dict.

    `context` is a plain-text rendering of the last few conversation turns
    (see services/conversation.py), used only to resolve short follow-up
    replies to a prior bot question — it is embedded in the system prompt,
    not as fake message turns, so the model isn't tempted to "answer" it.

    On invalid JSON returns a safe default with intent "unknown" so the
    bot can ask the user to rephrase. On an API failure (after one retry
    with backoff) the default additionally carries "api_error": True so
    the dispatcher can tell "couldn't reach the model" apart from
    "message genuinely wasn't understood".
    """
    system_prompt = _build_system_prompt()
    if context:
        system_prompt += f"\n\nИстория последних сообщений (для контекста):\n{context}\n"

    raw = None
    for attempt in (1, 2):
        try:
            response = await _client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_text}],
            )
            raw = response.content[0].text.strip()
            break
        except Exception:
            if attempt == 1:
                logger.warning("Anthropic API call failed (attempt 1), retrying in 1s", exc_info=True)
                await asyncio.sleep(1)
            else:
                logger.exception("Anthropic API call failed after retry for message=%r", user_text)
                result = dict(DEFAULT_RESULT)
                result["api_error"] = True
                return result

    raw = _strip_code_fences(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON output for message=%r: %r", user_text, raw)
        logger.info("LLM_CLASSIFY | in=%r | context=%r | raw_out=%r | parsed=FAILED", user_text, context, raw)
        return dict(DEFAULT_RESULT)

    result = dict(DEFAULT_RESULT)
    result.update({k: v for k, v in data.items() if k in DEFAULT_RESULT})

    # Compact single-line JSON so log lines stay grep-able; the exact raw
    # wire text is still available at DEBUG for troubleshooting weird
    # formatting issues (extra prose, code fences that didn't strip, etc).
    logger.info(
        "LLM_CLASSIFY | in=%r | context=%r | out=%s",
        user_text,
        context,
        json.dumps(result, ensure_ascii=False),
    )
    logger.debug("LLM_CLASSIFY raw_out=%r", raw)

    return result


def _strip_code_fences(raw: str) -> str:
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = [line_ for line_ in lines if not line_.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    return raw


OFFSET_SYSTEM_PROMPT = """\
Пользователь описывает, за сколько времени до события напомнить \
("за 40 минут", "за день", "за пару часов"). Верни СТРОГО JSON вида \
{"minutes": <целое число минут>} без пояснений. Если не удалось понять — \
{"minutes": null}.
"""


async def parse_offset_minutes(offset_text: str) -> int | None:
    try:
        response = await _client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=64,
            temperature=0,
            system=OFFSET_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": offset_text}],
        )
        raw = _strip_code_fences(response.content[0].text.strip())
        data = json.loads(raw)
        minutes = data.get("minutes")
        return int(minutes) if minutes is not None else None
    except Exception:
        logger.exception("Failed to parse offset text %r", offset_text)
        return None


def parse_event_time(event_time: str | None) -> dt.datetime | None:
    if not event_time:
        return None
    tz = ZoneInfo(TIMEZONE)
    try:
        naive = dt.datetime.strptime(event_time, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=tz)


async def parse_time_of_day(text: str) -> tuple[int, int] | None:
    """Parse a bare time-of-day expression ("16:45", "16 45", "в 5 вечера")
    via the same classifier used everywhere else — reused here (rather
    than a separate parser) for /settings' custom-time input. Only the
    hour/minute are used; whatever date the classifier infers is ignored."""
    parsed = await parse_message(text)
    event_dt = parse_event_time(parsed.get("event_time"))
    if event_dt is None or parsed.get("time_ambiguous"):
        return None
    return event_dt.hour, event_dt.minute
