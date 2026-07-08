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
    "👋 Привет! Я твой личный помощник.\n\n"
    "Примеры того, что мне можно написать:\n"
    "⏰ «позвонить бабушке сегодня в 12»\n"
    "👤 «новый знакомый Валера, день рождения 12 июля, любит рыбалку»\n"
    "📝 «у Валеры есть собака Рекс»\n"
    "🔎 «кто любит рыбалку?»\n"
    "📋 «мои напоминания» / «мои люди»\n\n"
    "Используй /help для подробностей."
)

HELP_TEXT = (
    "ℹ️ Что я умею:\n\n"
    "⏰ Напоминания — просто напиши, что и когда напомнить. Могу спросить, "
    "за сколько предупредить заранее, и умею повторяющиеся напоминания "
    "(«каждый день в 9», «каждый понедельник»).\n"
    "/reminders — список активных напоминаний.\n\n"
    "👤 Люди — расскажи о новом знакомом, я сохраню карточку. Дальше просто "
    "пиши новые факты о нём («Валера теперь работает в такси»), я сам пойму, "
    "к кому это относится (и переспрошу, если не уверен).\n"
    "/people — список всех людей.\n\n"
    "🎂 Если укажешь день рождения — я сам заведу ежегодное напоминание.\n"
    "🔎 Спроси «кто любит рыбалку?» или «что я знаю про Валеру?» — найду по заметкам.\n\n"
    "/export — выгрузить всё в JSON-файл (бэкап).\n"
    "/stats — сколько людей, заметок и активных напоминаний сейчас в базе.\n\n"
    "Если не пойму сообщение — просто честно скажу и попрошу переформулировать."
)


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
        reply = "Отменил текущее действие." if had_state else "Нечего отменять — сейчас ничего не выполняется."
        await _reply(message, reply)
        return

    context = conversation.get_context_text(user_id)
    conversation.record_user(user_id, text)
    parsed = await llm_parser.parse_message(text, context=context)
    intent = parsed.get("intent")

    handlers_map: dict[str, Callable[[], Awaitable[Any]]] = {
        "create_reminder": lambda: reminders.start_reminder_flow(message, state, parsed),
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
