import datetime as dt
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from zoneinfo import ZoneInfo

from config import TIMEZONE
from db.repo import (
    add_alert,
    create_reminder,
    delete_reminder,
    get_reminder,
    list_active_reminders,
    list_alerts,
    session_scope,
)
from handlers.states import ReminderFlow
from handlers.ui import CLOSE_BUTTON, EMPTY_KB
from services import conversation, llm_parser, scheduler

logger = logging.getLogger(__name__)
router = Router(name="reminders")

TZ = ZoneInfo(TIMEZONE)

OFFSET_OPTIONS = [
    (0, "В момент события"),
    (5, "За 5 мин"),
    (15, "За 15 мин"),
    (60, "За 1 час"),
    (120, "За 2 часа"),
]


def _offsets_keyboard(selected: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for minutes, label in OFFSET_OPTIONS:
        mark = "✅ " if minutes in selected else ""
        rows.append(
            [InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"remoff:toggle:{minutes}")]
        )
    for minutes in selected:
        if minutes not in [m for m, _ in OFFSET_OPTIONS]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Своё: за {minutes} мин", callback_data=f"remoff:toggle:{minutes}"
                    )
                ]
            )
    rows.append(
        [
            InlineKeyboardButton(text="Своё время", callback_data="remoff:custom"),
            InlineKeyboardButton(text="Готово", callback_data="remoff:done"),
        ]
    )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="remoff:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_time(event_time: dt.datetime) -> str:
    now = dt.datetime.now(TZ)
    if event_time.date() == now.date():
        return f"сегодня в {event_time.strftime('%H:%M')}"
    if event_time.date() == (now + dt.timedelta(days=1)).date():
        return f"завтра в {event_time.strftime('%H:%M')}"
    return event_time.strftime("%d.%m в %H:%M")


async def start_reminder_flow(message: Message, state: FSMContext, parsed: dict) -> None:
    text = parsed.get("reminder_text") or message.text
    event_time = llm_parser.parse_event_time(parsed.get("event_time"))
    recurrence = parsed.get("recurrence_rule")

    if event_time is None or parsed.get("time_ambiguous"):
        await state.update_data(draft_text=text, recurrence_rule=recurrence)
        await state.set_state(ReminderFlow.waiting_time)
        reply = "⏰ Когда напомнить?"
        conversation.record_bot(message.from_user.id, reply)
        await message.answer(reply)
        return

    now = dt.datetime.now(TZ)
    if event_time < now and recurrence is None:
        await state.update_data(
            draft_text=text,
            draft_time=event_time.isoformat(),
            recurrence_rule=recurrence,
        )
        await state.set_state(ReminderFlow.waiting_reschedule)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да", callback_data="remresched:yes"),
                    InlineKeyboardButton(text="Нет", callback_data="remresched:no"),
                ]
            ]
        )
        await message.answer(
            f"{event_time.strftime('%H:%M')} уже прошло. Перенести на завтра?", reply_markup=kb
        )
        return

    await _show_offsets(message, state, text, event_time, recurrence)


async def _show_offsets(
    message: Message,
    state: FSMContext,
    text: str,
    event_time: dt.datetime,
    recurrence: str | None,
) -> None:
    await state.update_data(
        draft_text=text,
        draft_time=event_time.isoformat(),
        recurrence_rule=recurrence,
        selected_offsets=[0],
    )
    await state.set_state(ReminderFlow.choosing_offsets)
    await message.answer(
        f"⏰ Напоминание: {text} — {_fmt_time(event_time)}. За сколько напомнить заранее?",
        reply_markup=_offsets_keyboard([0]),
    )


@router.message(ReminderFlow.waiting_time)
async def handle_time_reply(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text = message.text or ""

    if conversation.is_explicit_cancel(text):
        await state.clear()
        conversation.record_user(user_id, text)
        await message.answer("Отменил текущее действие.")
        return

    data = await state.get_data()
    context = conversation.get_context_text(user_id)
    conversation.record_user(user_id, text)
    parsed = await llm_parser.parse_message(text, context=context)
    event_time = llm_parser.parse_event_time(parsed.get("event_time"))
    if event_time is None:
        reply = "Не разобрал время. Попробуй ещё раз, например «завтра в 10:00»."
        conversation.record_bot(user_id, reply)
        await message.answer(reply)
        return
    recurrence = data.get("recurrence_rule") or parsed.get("recurrence_rule")
    await _show_offsets(message, state, data.get("draft_text", message.text), event_time, recurrence)


@router.callback_query(F.data.startswith("remresched:"))
async def handle_reschedule(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if choice == "no":
        await state.clear()
        await callback.message.edit_text("Хорошо, отменил.", reply_markup=EMPTY_KB)
        await callback.answer()
        return
    await callback.message.edit_reply_markup(reply_markup=EMPTY_KB)
    event_time = dt.datetime.fromisoformat(data["draft_time"]) + dt.timedelta(days=1)
    await _show_offsets(
        callback.message, state, data.get("draft_text", ""), event_time, data.get("recurrence_rule")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("remoff:"))
async def handle_offset_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_offsets", [0]))

    if action == "toggle":
        minutes = int(callback.data.split(":")[2])
        if minutes in selected:
            selected.remove(minutes)
        else:
            selected.append(minutes)
        await state.update_data(selected_offsets=selected)
        await callback.message.edit_reply_markup(reply_markup=_offsets_keyboard(selected))
        await callback.answer()
        return

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.", reply_markup=EMPTY_KB)
        await callback.answer()
        return

    if action == "custom":
        await state.set_state(ReminderFlow.waiting_custom_offset)
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=EMPTY_KB)
        reply = "За сколько напомнить? Например «за 40 минут» или «за день»."
        conversation.record_bot(callback.from_user.id, reply)
        await callback.message.answer(reply)
        return

    if action == "done":
        if not selected:
            await callback.answer("Выбери хотя бы один вариант", show_alert=True)
            return
        if "draft_text" not in data:
            # Double-tap after the first tap already created the reminder.
            await callback.answer("Уже обработано", show_alert=False)
            return
        text = data["draft_text"]
        event_time = dt.datetime.fromisoformat(data["draft_time"])
        recurrence = data.get("recurrence_rule")

        async with session_scope() as session:
            reminder = await create_reminder(session, callback.from_user.id, text, event_time, recurrence)
            await session.commit()
            for minutes in selected:
                fire_time = scheduler.offset_to_fire_time(event_time, minutes)
                label = "on_time" if minutes == 0 else f"before_{minutes}"
                alert = await add_alert(session, reminder.id, fire_time, label)
                job_id = scheduler.schedule_alert(alert.id, fire_time)
                alert.job_id = job_id
            await session.commit()

        await state.clear()
        await callback.message.edit_text(
            f"✅ Напоминание создано: {text} — {_fmt_time(event_time)}", reply_markup=EMPTY_KB
        )
        await callback.answer()


@router.message(ReminderFlow.waiting_custom_offset)
async def handle_custom_offset(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text = message.text or ""

    if conversation.is_explicit_cancel(text):
        await state.clear()
        conversation.record_user(user_id, text)
        await message.answer("Отменил текущее действие.")
        return

    conversation.record_user(user_id, text)
    minutes = await llm_parser.parse_offset_minutes(text)
    if minutes is None:
        reply = "Не понял. Попробуй, например, «за 40 минут» или «за 2 часа»."
        conversation.record_bot(user_id, reply)
        await message.answer(reply)
        return
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_offsets", [0]))
    if minutes not in selected:
        selected.append(minutes)
    await state.update_data(selected_offsets=selected)
    await state.set_state(ReminderFlow.choosing_offsets)
    await message.answer("Добавил. Что ещё?", reply_markup=_offsets_keyboard(selected))


@router.message(Command("reminders"))
@router.message(F.text.casefold().in_({"мои напоминания", "напоминания"}))
async def list_reminders_cmd(message: Message) -> None:
    await show_reminders_list(message)


async def show_reminders_list(message: Message) -> None:
    async with session_scope() as session:
        reminders = await list_active_reminders(session, message.from_user.id)
    if not reminders:
        await message.answer("Активных напоминаний нет.")
        return
    rows = []
    for r in reminders:
        event_local = r.event_time if r.event_time.tzinfo else r.event_time.replace(tzinfo=TZ)
        label = f"{r.text} — {_fmt_time(event_local)}"
        if r.recurrence_rule:
            label += " 🔁"
        rows.append([InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"remdel:{r.id}")])
    rows.append([CLOSE_BUTTON])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("Твои напоминания:", reply_markup=kb)


@router.callback_query(F.data.startswith("remdel:"))
async def handle_delete_reminder(callback: CallbackQuery) -> None:
    reminder_id = int(callback.data.split(":")[1])
    async with session_scope() as session:
        reminder = await get_reminder(session, reminder_id)
        if reminder is None:
            await callback.answer("Уже удалено", show_alert=True)
            return
        alerts = await list_alerts(session, reminder_id)
        for a in alerts:
            scheduler.cancel_job(a.job_id)
        await delete_reminder(session, reminder_id)
        await session.commit()
    await callback.answer("Удалено")
    await callback.message.edit_text("🗑 Напоминание удалено.", reply_markup=EMPTY_KB)


@router.callback_query(F.data.startswith("rem_done:"))
async def handle_rem_done(callback: CallbackQuery) -> None:
    reminder_id = int(callback.data.split(":")[1])
    async with session_scope() as session:
        reminder = await get_reminder(session, reminder_id)
        if reminder and reminder.recurrence_rule is None:
            alerts = await list_alerts(session, reminder_id)
            for a in alerts:
                scheduler.cancel_job(a.job_id)
            await delete_reminder(session, reminder_id)
            await session.commit()
    await callback.answer("Отлично!")
    await callback.message.edit_text(f"✅ {callback.message.text}", reply_markup=EMPTY_KB)


@router.callback_query(F.data.startswith("rem_snooze:"))
async def handle_rem_snooze(callback: CallbackQuery) -> None:
    _, reminder_id_s, minutes_s = callback.data.split(":")
    fire_time = await scheduler.snooze_reminder(int(reminder_id_s), int(minutes_s))
    await callback.answer("Отложено")
    await callback.message.edit_text(
        f"{callback.message.text}\n\n⏰ Отложено до {fire_time.strftime('%H:%M')}", reply_markup=EMPTY_KB
    )
