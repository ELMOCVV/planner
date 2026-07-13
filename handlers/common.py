import datetime as dt
import json
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, TelegramObject

from config import ALLOWED_USER_IDS, EXPORT_DIR
from db.repo import count_active_reminders, count_notes, count_people, export_user_data, session_scope
from handlers import people, reminders
from handlers.ui import CLOSE_CALLBACK
from services import conversation, llm_parser

logger = logging.getLogger(__name__)
router = Router(name="common")

START_TEXT = (
    "👋 Привет! Я Kontinental — твой личный помощник: напоминания и память о людях.\n\n"
    "Просто пиши обычным текстом:\n"
    "⏰ «напомни позвонить бабушке завтра в 12»\n"
    "🔁 «каждый день в 9 пить воду»\n"
    "👤 «новый знакомый Валера, др 12 июля, любит рыбалку»\n"
    "📝 «у Валеры есть собака Рекс»\n"
    "🔎 «кто любит рыбалку?» / «что я знаю про Валеру?»\n"
    "❓ «напомнишь ли ты мне что-то 12 июля?»\n\n"
    "📋 «мои напоминания» / «мои люди» / «настройки»\n"
    "Подробнее — /help"
)

HELP_TEXT = (
    "ℹ️ Что я умею\n\n"
    "⏰ <b>Напоминания</b>\n"
    "Напиши, что и когда напомнить — понимаю «завтра в 10», «через час», "
    "«в 15 30», «в пол шестого». Спрошу, за сколько предупредить заранее "
    "(можно несколько вариантов). Повторяющиеся: «каждый день в 9», «каждый понедельник». "
    "Когда напоминание сработает — кнопки «Сделано» и «Отложить».\n"
    "Спросить, что запланировано: «напомнишь ли мне это 12 июля?», /reminders, /today.\n"
    "Удалить: «удали напоминание» → выберешь из списка.\n\n"
    "👤 <b>Люди</b>\n"
    "«Новый знакомый Валера, любит рыбалку» — заведу карточку. Дальше просто "
    "пиши факты: «у Валеры собака Рекс» — сам пойму, к кому это (Валера, Валерчик "
    "и Валерий для меня один человек; если не уверен — переспрошу). "
    "«Валера это тот же Валерий» — склею имена.\n"
    "«Что я знаю про Валеру?» — карточка. «Кто любит рыбалку?» — поиск по заметкам. "
    "«У кого др в июле?» — дни рождения. /people — все люди.\n\n"
    "🎂 <b>Дни рождения</b>\n"
    "Скажи «у Валеры др 12 июля» — заведу ежегодное напоминание. "
    "Время уведомления настраивается в /settings (там же — тестовое уведомление).\n\n"
    "🛠 <b>Команды</b>\n"
    "/today — что сегодня (напоминания + дни рождения)\n"
    "/reminders — активные напоминания\n"
    "/people — все люди\n"
    "/settings — время др-уведомлений, тест\n"
    "/stats — сколько всего записано\n"
    "/export — бэкап в JSON-файл\n"
    "/cancel — отменить текущее действие"
)


class ErrorGuardMiddleware(BaseMiddleware):
    """Last line of defence: any unhandled exception in a handler is
    logged with its traceback and turned into a short apologetic message;
    the bot keeps running and the update is considered consumed."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Unhandled exception in handler for %s", type(event).__name__)
            try:
                if isinstance(event, Message):
                    await event.answer("😵 Что-то пошло не так. Попробуй ещё раз.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("😵 Что-то пошло не так. Попробуй ещё раз.", show_alert=True)
            except Exception:
                logger.warning("Failed to deliver the error apology", exc_info=True)
            return None


class AccessControlMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
            if isinstance(event, Message):
                await event.answer("Это приватный бот 🔒")
            elif isinstance(event, CallbackQuery):
                await event.answer("Это приватный бот 🔒", show_alert=True)
            return None
        return await handler(event, data)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    conversation.clear(message.from_user.id)
    await message.answer("Отменил текущее действие.")


@router.callback_query(F.data == CLOSE_CALLBACK)
async def handle_close(callback: CallbackQuery) -> None:
    """Shared "✖️ Закрыть" handler for persistent menus (person card,
    people list, candidate list, reminders list) — deletes the message
    outright rather than just stripping its buttons, so it stops
    cluttering the chat entirely."""
    try:
        await callback.message.delete()
    except Exception:
        logger.warning("Failed to delete message on close", exc_info=True)
    await callback.answer()


@router.message(Command("today"))
@router.message(F.text.casefold() == "что сегодня")
async def cmd_today(message: Message) -> None:
    import datetime as _dt

    from zoneinfo import ZoneInfo

    from config import TIMEZONE
    from db.repo import list_active_reminders, people_with_birthday_on

    tz = ZoneInfo(TIMEZONE)
    now = _dt.datetime.now(tz)
    user_id = message.from_user.id

    async with session_scope() as session:
        active = await list_active_reminders(session, user_id)
        bday_people = await people_with_birthday_on(session, user_id, now.month, now.day)

    # Birthdays are listed from the people table; skip the auto-created
    # 🎂 reminders so they don't show up twice.
    todays = [
        r for r in active
        if r.event_time.date() == now.date() and not r.text.startswith("🎂")
    ]
    lines = [f"📅 Сегодня, {now.day} {_month_ru(now.month)}:"]
    if bday_people:
        for p in bday_people:
            lines.append(f"🎂 День рождения у {p.name}!")
    if todays:
        for r in todays:
            lines.append(f"⏰ {r.event_time.strftime('%H:%M')} — {r.text}")
    if not bday_people and not todays:
        lines.append("Ничего не запланировано — свободный день 🙌")
    await message.answer("\n".join(lines))


def _month_ru(month: int) -> str:
    from handlers.ui import MONTHS_RU_GEN

    return MONTHS_RU_GEN[month - 1]


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    user_id = message.from_user.id
    async with session_scope() as session:
        people_count = await count_people(session, user_id)
        notes_count = await count_notes(session, user_id)
        reminders_count = await count_active_reminders(session, user_id)
    await message.answer(
        "📊 Статистика:\n"
        f"👤 Людей: {people_count}\n"
        f"📝 Заметок: {notes_count}\n"
        f"⏰ Активных напоминаний: {reminders_count}"
    )


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    user_id = message.from_user.id
    async with session_scope() as session:
        data = await export_user_data(session, user_id)

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    export_path = EXPORT_DIR / f"export_{user_id}_{timestamp}.json"
    export_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        await message.answer_document(
            FSInputFile(export_path, filename=export_path.name),
            caption=(
                f"📦 Бэкап: {len(data['people'])} человек, "
                f"{len(data['reminders'])} напоминаний."
            ),
        )
    finally:
        export_path.unlink(missing_ok=True)


async def _reply(message: Message, text: str, **kwargs) -> None:
    """message.answer wrapper that also records the bot's side of the
    conversation, so the next classifier call can tell a short follow-up
    reply ("сегодня", "в 12") apart from a fresh, standalone message."""
    conversation.record_bot(message.from_user.id, text)
    await message.answer(text, **kwargs)


@router.message(F.text)
async def dispatch_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    user_id = message.from_user.id

    # Deterministic cancel/delete guard: only explicit keywords ever cancel
    # anything. This runs before the LLM is even called, so ambiguous
    # phrasing (e.g. "поздравить с днём рождения") can never be misread as
    # a cancellation by a probabilistic classifier.
    if conversation.is_explicit_cancel(text):
        had_state = await state.get_state() is not None
        await state.clear()
        conversation.record_user(user_id, text)
        if had_state:
            await _reply(message, "Отменил текущее действие.")
        else:
            # Nothing in progress — treat "удали/отмени ..." as a deletion
            # request and show the reminders list with delete buttons
            # (still deterministic: nothing is deleted without a tap).
            await _reply(
                message,
                "Сейчас ничего не выполняется. Если хочешь удалить напоминание — "
                "выбери его ниже. Людей можно удалить из карточки (/people).",
            )
            await reminders.show_reminders_list(message)
        return

    # Any FSM state that lets a text message fall through to this handler
    # is by construction a button-wait state (text-wait states have their
    # own message handlers on earlier routers). The user typed something
    # new instead of tapping — treat that as abandoning the old menu, or
    # its stale draft data would leak into the next flow.
    if await state.get_state() is not None:
        await state.clear()

    # Classification takes a real API round-trip (~1-2s) — show typing so
    # the user gets feedback immediately.
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    context = conversation.get_context_text(user_id)
    conversation.record_user(user_id, text)
    parsed = await llm_parser.parse_message(text, context=context)
    intent = parsed.get("intent")

    if parsed.get("api_error"):
        await _reply(
            message,
            "😵 Не получилось обработать сообщение — сервис распознавания "
            "сейчас недоступен. Попробуй ещё раз через минуту.",
        )
        return

    handlers_map: dict[str, Callable[[], Awaitable[Any]]] = {
        "create_reminder": lambda: reminders.start_reminder_flow(message, state, parsed),
        "query_reminder": lambda: reminders.start_query_reminder(message, parsed),
        "list_reminders": lambda: reminders.show_reminders_list(message),
        "delete_reminder": lambda: reminders.show_reminders_list(message),
        "add_person": lambda: people.start_add_person(message, state, parsed),
        "add_person_info": lambda: people.start_add_person_info(message, state, parsed),
        "add_alias": lambda: people.start_add_alias(message, parsed),
        "query_person": lambda: people.start_query_person(message, parsed),
        "list_people": lambda: people.show_people_list(message),
        "search_notes": lambda: people.start_search_notes(message, parsed),
    }

    action = handlers_map.get(intent)
    if action is not None:
        await action()
        return

    if intent == "chitchat" and parsed.get("reply_text"):
        await _reply(message, parsed["reply_text"])
        return

    await _reply(
        message,
        "🤔 Не понял сообщение. Например, можно написать:\n"
        "«позвонить бабушке сегодня в 12» или «новый знакомый Валера, любит рыбалку».",
    )
