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
  "intent": одно из "create_reminder", "add_person", "add_person_info", \
"query_person", "list_reminders", "delete_reminder", "list_people", \
"search_notes", "add_alias", "chitchat", "unknown",
  "reminder_text": строка или null — суть напоминания (без времени),
  "event_time": строка "YYYY-MM-DD HH:MM" в локальном времени пользователя, \
или null если не удалось определить,
  "time_ambiguous": true/false — true, если пользователь не указал время \
явно или оно совсем неясно,
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
  "reply_text": строка или null — короткий дружелюбный ответ для chitchat/unknown
}

Правила:
- Сегодняшняя дата: __TODAY__, текущее время: __NOW_TIME__, часовой пояс: __TZ__.
- Разрешай относительные выражения ("сегодня", "завтра", "через полчаса", \
"через час", "в понедельник") в абсолютную дату и время используя текущую дату/время.
- Если время не указано вообще (например "напомни позвонить бабушке") — \
event_time = null, time_ambiguous = true.
- Если из контекста понятно, что сообщение продолжает разговор о ранее \
упомянутом человеке (например "у него ещё есть кот"), верни intent \
"add_person_info" и person_name как в предыдущем сообщении, если он был \
передан в истории.
- Отвечай ТОЛЬКО JSON, один объект, ничего больше.
"""


def _build_system_prompt() -> str:
    tz = ZoneInfo(TIMEZONE)
    now = dt.datetime.now(tz)
    return (
        SYSTEM_PROMPT.replace("__TODAY__", now.strftime("%Y-%m-%d (%A)"))
        .replace("__NOW_TIME__", now.strftime("%H:%M"))
        .replace("__TZ__", TIMEZONE)
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
    "reply_text": None,
}


async def parse_message(user_text: str, context: str | None = None) -> dict:
    """Send user_text to the Anthropic classifier and return a parsed dict.

    On any failure (network, invalid JSON) returns a safe default with
    intent "unknown" so the bot can ask the user to rephrase.
    """
    messages = []
    if context:
        messages.append({"role": "user", "content": f"[Контекст диалога: {context}]"})
        messages.append({"role": "assistant", "content": "Понял, учту контекст."})
    messages.append({"role": "user", "content": user_text})

    try:
        response = await _client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=_build_system_prompt(),
            messages=messages,
        )
        raw = response.content[0].text.strip()
    except Exception:
        logger.exception("Anthropic API call failed")
        return dict(DEFAULT_RESULT)

    raw = _strip_code_fences(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON output: %r", raw)
        return dict(DEFAULT_RESULT)

    result = dict(DEFAULT_RESULT)
    result.update({k: v for k, v in data.items() if k in DEFAULT_RESULT})
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
