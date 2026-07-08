import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.repo import session_scope, set_birthday_alert_time
from handlers.states import SettingsFlow
from handlers.ui import CLOSE_BUTTON, EMPTY_KB
from services import birthdays, conversation, llm_parser

logger = logging.getLogger(__name__)
router = Router(name="settings")

PRESETS = ["09:00", "12:00", "15:00", "18:00", "20:00"]


def _pluralize_reminders(n: int) -> str:
    n_mod10, n_mod100 = n % 10, n % 100
    if n_mod10 == 1 and n_mod100 != 11:
        return "напоминание"
    if 2 <= n_mod10 <= 4 and not (12 <= n_mod100 <= 14):
        return "напоминания"
    return "напоминаний"


def _settings_text(hhmm: str) -> str:
    return f"⚙️ Настройки\n🎂 Время уведомлений о др: {hhmm}"


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎂 Изменить время др-уведомлений", callback_data="settings:changetime")],
            [InlineKeyboardButton(text="🔔 Тест уведомления", callback_data="settings:test")],
            [CLOSE_BUTTON],
        ]
    )


def _presets_kb() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for preset in PRESETS:
        row.append(InlineKeyboardButton(text=preset, callback_data=f"settings:preset:{preset.replace(':', '')}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Своё время", callback_data="settings:custom")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:back"), CLOSE_BUTTON])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_settings(message: Message) -> None:
    hhmm = await birthdays.get_effective_alert_time_str(message.from_user.id)
    await message.answer(_settings_text(hhmm), reply_markup=_settings_kb())


@router.message(Command("settings"))
@router.message(F.text.casefold() == "настройки")
async def cmd_settings(message: Message) -> None:
    await show_settings(message)


@router.callback_query(F.data == "settings:changetime")
async def handle_change_time(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Выбери время уведомлений о днях рождения:", reply_markup=_presets_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "settings:back")
async def handle_back(callback: CallbackQuery) -> None:
    hhmm = await birthdays.get_effective_alert_time_str(callback.from_user.id)
    await callback.message.edit_text(_settings_text(hhmm), reply_markup=_settings_kb())
    await callback.answer()


async def _save_and_reschedule(user_id: int, hour: int, minute: int) -> str:
    hhmm = f"{hour:02d}:{minute:02d}"
    async with session_scope() as session:
        await set_birthday_alert_time(session, user_id, hhmm)
        await session.commit()
    count = await birthdays.reschedule_all_for_user(user_id)
    return f"✅ Теперь буду напоминать о др в {hhmm}. Перенёс {count} {_pluralize_reminders(count)}."


@router.callback_query(F.data.startswith("settings:preset:"))
async def handle_preset(callback: CallbackQuery) -> None:
    digits = callback.data.split(":")[2]  # e.g. "0900"
    hour, minute = int(digits[:2]), int(digits[2:])
    text = await _save_and_reschedule(callback.from_user.id, hour, minute)
    await callback.message.edit_text(text, reply_markup=EMPTY_KB)
    await callback.answer()


@router.callback_query(F.data == "settings:custom")
async def handle_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsFlow.waiting_custom_time)
    await callback.message.edit_reply_markup(reply_markup=EMPTY_KB)
    reply = "Во сколько напоминать о днях рождения? Например «16:45», «16 45» или «в 5 вечера»."
    conversation.record_bot(callback.from_user.id, reply)
    await callback.message.answer(reply)
    await callback.answer()


@router.message(SettingsFlow.waiting_custom_time)
async def handle_custom_time_text(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text = message.text or ""

    if conversation.is_explicit_cancel(text):
        await state.clear()
        conversation.record_user(user_id, text)
        await message.answer("Отменил текущее действие.")
        return

    conversation.record_user(user_id, text)
    parsed_time = await llm_parser.parse_time_of_day(text)
    if parsed_time is None:
        reply = "Не понял время. Попробуй, например, «16:45» или «в 5 вечера»."
        conversation.record_bot(user_id, reply)
        await message.answer(reply)
        return

    await state.clear()
    hour, minute = parsed_time
    reply = await _save_and_reschedule(user_id, hour, minute)
    await message.answer(reply)


@router.callback_query(F.data == "settings:test")
async def handle_test_notification(callback: CallbackQuery) -> None:
    fire_time = await birthdays.schedule_test_alert(callback.from_user.id)
    await callback.answer(
        f"🔔 Тестовое уведомление придёт в {fire_time.strftime('%H:%M')} (через ~2 минуты).",
        show_alert=True,
    )
