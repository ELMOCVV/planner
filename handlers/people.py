import datetime as dt
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.repo import (
    add_alias,
    add_note,
    delete_note,
    delete_person,
    get_person,
    list_people,
    search_notes,
    session_scope,
)
import db.repo as repo
from handlers.states import PersonFlow
from services import birthdays
from services.person_matcher import find_matches, normalize

logger = logging.getLogger(__name__)
router = Router(name="people")

PAGE_SIZE = 8


def parse_birthday(value: str | None) -> tuple[int | None, int | None, int | None]:
    if not value:
        return None, None, None
    parts = value.split("-")
    try:
        if len(parts) == 3:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            return month, day, year
        if len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
            return month, day, None
    except ValueError:
        pass
    return None, None, None


def _facts_preview(facts: list[str], birthday_month: int | None, birthday_day: int | None) -> str:
    bits = []
    if birthday_month and birthday_day:
        bits.append(f"др {birthday_day:02d}.{birthday_month:02d}")
    bits.extend(facts[:2])
    return ", ".join(bits) if bits else "пока без деталей"


async def start_add_person(message: Message, state: FSMContext, parsed: dict) -> None:
    name = parsed.get("person_name")
    if not name:
        await message.answer("Не понял, о ком речь. Как зовут человека?")
        return

    month, day, year = parse_birthday(parsed.get("birthday"))
    facts = parsed.get("person_facts") or []
    tag = parsed.get("person_tag")

    draft = {
        "name": name,
        "tag": tag,
        "month": month,
        "day": day,
        "year": year,
        "facts": facts,
    }

    async with session_scope() as session:
        matches = await find_matches(session, message.from_user.id, name)

    if not matches:
        await state.update_data(draft=draft)
        await state.set_state(PersonFlow.confirm_create)
        preview = _facts_preview(facts, month, day)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Создать", callback_data="pplcreate:confirm")],
                [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="pplcreate:editname")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pplcreate:cancel")],
            ]
        )
        await message.answer(
            f"Создать нового человека? Имя: {name}. {preview}.", reply_markup=kb
        )
        return

    if len(matches) == 1:
        person = matches[0].person
        await state.update_data(draft=draft, existing_id=person.id)
        await state.set_state(PersonFlow.confirm_add_existing)
        preview = _facts_preview([n.text for n in person.notes], person.birthday_month, person.birthday_day)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да", callback_data=f"pplexist:yes:{person.id}")],
                [InlineKeyboardButton(text="➕ Это другой", callback_data="pplexist:new")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pplexist:cancel")],
            ]
        )
        await message.answer(
            f"Добавляю к {person.name} ({preview})?", reply_markup=kb
        )
        return

    await state.update_data(draft=draft)
    await state.set_state(PersonFlow.disambiguate)
    rows = []
    for m in matches[:6]:
        preview = _facts_preview([n.text for n in m.person.notes], m.person.birthday_month, m.person.birthday_day)
        tag_part = f" ({m.person.tag})" if m.person.tag else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{m.person.name}{tag_part} — {preview}",
                    callback_data=f"ppldis:{m.person.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=f"➕ Новый {name}", callback_data="ppldis:new")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ppldis:cancel")])
    await message.answer("Нашёл несколько похожих, кто это?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _create_person_from_draft(session, user_id: int, draft: dict):
    person = await repo.create_person(
        session,
        user_id,
        draft["name"],
        tag=draft.get("tag"),
        birthday_month=draft.get("month"),
        birthday_day=draft.get("day"),
        birthday_year=draft.get("year"),
    )
    for fact in draft.get("facts", []):
        await add_note(session, person.id, fact)
    if draft.get("month") and draft.get("day"):
        await birthdays.sync_birthday_reminders(session, person)
    await session.commit()
    return person


@router.callback_query(F.data.startswith("pplcreate:"))
async def handle_create_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    data = await state.get_data()
    draft = data.get("draft", {})

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    if action == "editname":
        await state.set_state(PersonFlow.waiting_name_edit)
        await callback.answer()
        await callback.message.answer("Как зовут человека?")
        return

    if action == "confirm":
        async with session_scope() as session:
            person = await _create_person_from_draft(session, callback.from_user.id, draft)
        await state.clear()
        await callback.message.edit_text(f"👤 Создал: {person.name}")
        await callback.answer()


@router.message(PersonFlow.waiting_name_edit)
async def handle_name_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft", {})
    draft["name"] = message.text.strip()
    async with session_scope() as session:
        person = await _create_person_from_draft(session, message.from_user.id, draft)
    await state.clear()
    await message.answer(f"👤 Создал: {person.name}")


@router.callback_query(F.data.startswith("pplexist:"))
async def handle_existing_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    data = await state.get_data()
    draft = data.get("draft", {})

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    if action == "new":
        await state.set_state(PersonFlow.confirm_create)
        preview = _facts_preview(draft.get("facts", []), draft.get("month"), draft.get("day"))
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Создать", callback_data="pplcreate:confirm")],
                [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="pplcreate:editname")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pplcreate:cancel")],
            ]
        )
        await callback.message.edit_text(
            f"Создать нового человека? Имя: {draft.get('name')}. {preview}.\n"
            "Подскажи уточнение-тег, чтобы не путать с тёзкой (например «коллега», «рыбак») — "
            "можешь написать его сообщением после создания.",
        )
        await callback.message.answer("Создать?", reply_markup=kb)
        await callback.answer()
        return

    if action == "yes":
        person_id = int(parts[2])
        async with session_scope() as session:
            person = await get_person(session, person_id)
            for fact in draft.get("facts", []):
                await add_note(session, person.id, fact)
            month, day = draft.get("month"), draft.get("day")
            if month and day:
                person.birthday_month, person.birthday_day, person.birthday_year = (
                    month,
                    day,
                    draft.get("year"),
                )
                await birthdays.sync_birthday_reminders(session, person)
            await session.commit()
        await state.clear()
        await callback.message.edit_text(f"✅ Добавил к {person.name}.")
        await callback.answer()


@router.callback_query(F.data.startswith("ppldis:"))
async def handle_disambiguate(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]
    data = await state.get_data()
    draft = data.get("draft", {})

    if choice == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    if choice == "new":
        await state.set_state(PersonFlow.confirm_create)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Создать", callback_data="pplcreate:confirm")],
                [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="pplcreate:editname")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pplcreate:cancel")],
            ]
        )
        await callback.message.edit_text(f"Создать нового человека {draft.get('name')}?")
        await callback.message.answer("Подтверди:", reply_markup=kb)
        await callback.answer()
        return

    person_id = int(choice)
    async with session_scope() as session:
        person = await get_person(session, person_id)
        for fact in draft.get("facts", []):
            await add_note(session, person.id, fact)
        month, day = draft.get("month"), draft.get("day")
        if month and day:
            person.birthday_month, person.birthday_day, person.birthday_year = month, day, draft.get("year")
            await birthdays.sync_birthday_reminders(session, person)
        await session.commit()
    await state.clear()
    await callback.message.edit_text(f"✅ Добавил к {person.name}.")
    await callback.answer()


async def start_add_person_info(message: Message, state: FSMContext, parsed: dict) -> None:
    name = parsed.get("person_name")
    facts = parsed.get("person_facts") or []
    month, day, year = parse_birthday(parsed.get("birthday"))

    if not name:
        await message.answer("Не понял, о ком эта заметка. Уточни имя.")
        return

    async with session_scope() as session:
        matches = await find_matches(session, message.from_user.id, name)

    draft = {"name": name, "facts": facts, "month": month, "day": day, "year": year, "tag": None}

    if not matches:
        await state.update_data(draft=draft)
        await state.set_state(PersonFlow.confirm_create)
        preview = _facts_preview(facts, month, day)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Создать", callback_data="pplcreate:confirm")],
                [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="pplcreate:editname")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pplcreate:cancel")],
            ]
        )
        await message.answer(
            f"Не нашёл {name} в базе. Создать нового? {preview}.", reply_markup=kb
        )
        return

    if len(matches) == 1:
        person = matches[0].person
        await state.update_data(draft=draft, existing_id=person.id)
        await state.set_state(PersonFlow.confirm_add_existing)
        preview = _facts_preview([n.text for n in person.notes], person.birthday_month, person.birthday_day)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да", callback_data=f"pplexist:yes:{person.id}")],
                [InlineKeyboardButton(text="➕ Это другой", callback_data="pplexist:new")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pplexist:cancel")],
            ]
        )
        await message.answer(f"Добавляю к {person.name} ({preview})?", reply_markup=kb)
        return

    await state.update_data(draft=draft)
    await state.set_state(PersonFlow.disambiguate)
    rows = []
    for m in matches[:6]:
        preview = _facts_preview([n.text for n in m.person.notes], m.person.birthday_month, m.person.birthday_day)
        tag_part = f" ({m.person.tag})" if m.person.tag else ""
        rows.append(
            [InlineKeyboardButton(text=f"{m.person.name}{tag_part} — {preview}", callback_data=f"ppldis:{m.person.id}")]
        )
    rows.append([InlineKeyboardButton(text=f"➕ Новый {name}", callback_data="ppldis:new")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ppldis:cancel")])
    await message.answer("Про кого из них?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def start_add_alias(message: Message, parsed: dict) -> None:
    alias = parsed.get("alias_target")
    main_name = parsed.get("person_name")
    if not alias or not main_name:
        await message.answer("Не понял, кого с кем объединить. Напиши, например: «Валера это тот же Валерий».")
        return
    async with session_scope() as session:
        matches = await find_matches(session, message.from_user.id, main_name)
        if not matches:
            await message.answer(f"Не нашёл {main_name} в базе.")
            return
        person = matches[0].person
        await add_alias(session, person.id, alias)
        await session.commit()
    await message.answer(f"Готово, теперь {alias} = {person.name}.")


def _person_card_text(person) -> str:
    lines = [f"👤 {person.name}"]
    if person.tag:
        lines[0] += f" ({person.tag})"
    if person.aliases:
        lines.append("Алиасы: " + ", ".join(a.alias for a in person.aliases))
    if person.birthday_month and person.birthday_day:
        lines.append(f"🎂 День рождения: {person.birthday_day:02d}.{person.birthday_month:02d}")
    if person.notes:
        lines.append("Заметки:")
        for n in sorted(person.notes, key=lambda x: x.created_at):
            lines.append(f"  • {n.text} ({n.created_at.strftime('%d.%m.%Y')})")
    else:
        lines.append("Заметок пока нет.")
    return "\n".join(lines)


def _person_card_kb(person_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить заметку", callback_data=f"card:addnote:{person_id}")],
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"card:rename:{person_id}")],
            [InlineKeyboardButton(text="🗑 Удалить заметку", callback_data=f"card:delnote:{person_id}")],
            [InlineKeyboardButton(text="❌ Удалить человека", callback_data=f"card:delperson:{person_id}")],
        ]
    )


MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def detect_birthday_month_query(text: str) -> int | None:
    lowered = text.lower()
    if "рожден" not in lowered and "др" not in lowered:
        return None
    for stem, month in MONTHS_RU.items():
        if re.search(stem, lowered):
            return month
    return None


async def start_birthday_month_query(message: Message, month: int) -> None:
    async with session_scope() as session:
        people = await repo.people_with_birthday_on(session, message.from_user.id, month)
    if not people:
        await message.answer("В этом месяце дней рождения не нашёл.")
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.name} — {p.birthday_day:02d}.{p.birthday_month:02d}",
                callback_data=f"cardshow:{p.id}",
            )
        ]
        for p in sorted(people, key=lambda x: x.birthday_day or 0)
    ]
    await message.answer("Дни рождения в этом месяце:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def start_query_person(message: Message, parsed: dict) -> None:
    name = parsed.get("person_name")
    if not name:
        await message.answer("О ком рассказать? Уточни имя.")
        return
    async with session_scope() as session:
        matches = await find_matches(session, message.from_user.id, name)
        if not matches:
            await message.answer(f"Не нашёл {name} в базе людей.")
            return
        if len(matches) > 1:
            rows = [
                [InlineKeyboardButton(text=m.person.name + (f" ({m.person.tag})" if m.person.tag else ""), callback_data=f"cardshow:{m.person.id}")]
                for m in matches[:6]
            ]
            await message.answer("Кого из них?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            return
        person = matches[0].person
        await message.answer(_person_card_text(person), reply_markup=_person_card_kb(person.id))


@router.callback_query(F.data.startswith("cardshow:"))
async def handle_card_show(callback: CallbackQuery) -> None:
    person_id = int(callback.data.split(":")[1])
    async with session_scope() as session:
        person = await get_person(session, person_id)
    if person is None:
        await callback.answer("Не нашёл", show_alert=True)
        return
    await callback.message.edit_text(_person_card_text(person), reply_markup=_person_card_kb(person.id))
    await callback.answer()


async def show_people_list(message: Message, page: int = 0) -> None:
    async with session_scope() as session:
        people = await list_people(session, message.from_user.id)
    if not people:
        await message.answer("В базе пока никого нет.")
        return
    start = page * PAGE_SIZE
    chunk = people[start : start + PAGE_SIZE]
    rows = []
    for p in chunk:
        tag_part = f" ({p.tag})" if p.tag else ""
        preview = _facts_preview([n.text for n in p.notes], p.birthday_month, p.birthday_day)
        rows.append(
            [InlineKeyboardButton(text=f"{p.name}{tag_part} — {preview}", callback_data=f"cardshow:{p.id}")]
        )
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pplpage:{page-1}"))
    if start + PAGE_SIZE < len(people):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pplpage:{page+1}"))
    if nav:
        rows.append(nav)
    await message.answer(f"Твои люди ({len(people)}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("people"))
@router.message(F.text.casefold() == "мои люди")
async def people_cmd(message: Message) -> None:
    await show_people_list(message)


@router.callback_query(F.data.startswith("pplpage:"))
async def handle_people_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])
    async with session_scope() as session:
        people = await list_people(session, callback.from_user.id)
    start = page * PAGE_SIZE
    chunk = people[start : start + PAGE_SIZE]
    rows = []
    for p in chunk:
        tag_part = f" ({p.tag})" if p.tag else ""
        preview = _facts_preview([n.text for n in p.notes], p.birthday_month, p.birthday_day)
        rows.append(
            [InlineKeyboardButton(text=f"{p.name}{tag_part} — {preview}", callback_data=f"cardshow:{p.id}")]
        )
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pplpage:{page-1}"))
    if start + PAGE_SIZE < len(people):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pplpage:{page+1}"))
    if nav:
        rows.append(nav)
    await callback.message.edit_text(f"Твои люди ({len(people)}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def start_search_notes(message: Message, parsed: dict) -> None:
    query = parsed.get("search_query")
    if not query:
        await message.answer("Что ищем в заметках?")
        return
    async with session_scope() as session:
        notes = await search_notes(session, message.from_user.id, query)
        people_ids = {n.person_id for n in notes}
        people = [await get_person(session, pid) for pid in people_ids]
    if not people:
        await message.answer(f"Никого не нашёл по запросу «{query}».")
        return
    rows = [
        [InlineKeyboardButton(text=p.name + (f" ({p.tag})" if p.tag else ""), callback_data=f"cardshow:{p.id}")]
        for p in people
        if p is not None
    ]
    await message.answer("Нашёл:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("card:addnote:"))
async def handle_card_addnote(callback: CallbackQuery, state: FSMContext) -> None:
    person_id = int(callback.data.split(":")[2])
    await state.update_data(person_id=person_id)
    await state.set_state(PersonFlow.waiting_new_note)
    await callback.answer()
    await callback.message.answer("Что добавить в заметки?")


@router.message(PersonFlow.waiting_new_note)
async def handle_new_note_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    person_id = data["person_id"]
    async with session_scope() as session:
        await add_note(session, person_id, message.text)
        await session.commit()
        person = await get_person(session, person_id)
    await state.clear()
    await message.answer(f"✅ Добавил заметку.\n\n{_person_card_text(person)}", reply_markup=_person_card_kb(person.id))


@router.callback_query(F.data.startswith("card:rename:"))
async def handle_card_rename(callback: CallbackQuery, state: FSMContext) -> None:
    person_id = int(callback.data.split(":")[2])
    await state.update_data(person_id=person_id)
    await state.set_state(PersonFlow.waiting_rename)
    await callback.answer()
    await callback.message.answer("Новое имя?")


@router.message(PersonFlow.waiting_rename)
async def handle_rename_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    person_id = data["person_id"]
    async with session_scope() as session:
        person = await get_person(session, person_id)
        person.name = message.text.strip()
        await session.commit()
        person = await get_person(session, person_id)
    await state.clear()
    await message.answer(f"✅ Переименовал.\n\n{_person_card_text(person)}", reply_markup=_person_card_kb(person.id))


@router.callback_query(F.data.startswith("card:delnote:"))
async def handle_card_delnote_list(callback: CallbackQuery) -> None:
    person_id = int(callback.data.split(":")[2])
    async with session_scope() as session:
        person = await get_person(session, person_id)
    if not person.notes:
        await callback.answer("Заметок нет", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(text=n.text[:40], callback_data=f"card:delnoteok:{n.id}:{person_id}")]
        for n in person.notes
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"cardshow:{person_id}")])
    await callback.message.edit_text("Какую заметку удалить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("card:delnoteok:"))
async def handle_card_delnote_confirm(callback: CallbackQuery) -> None:
    _, _, note_id_s, person_id_s = callback.data.split(":")
    async with session_scope() as session:
        await delete_note(session, int(note_id_s))
        await session.commit()
        person = await get_person(session, int(person_id_s))
    await callback.answer("Удалено")
    await callback.message.edit_text(_person_card_text(person), reply_markup=_person_card_kb(person.id))


@router.callback_query(F.data.startswith("card:delperson:"))
async def handle_card_delperson(callback: CallbackQuery) -> None:
    person_id = int(callback.data.split(":")[2])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"card:delpersonok:{person_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"cardshow:{person_id}"),
            ]
        ]
    )
    await callback.message.edit_text("Точно удалить этого человека и все заметки?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("card:delpersonok:"))
async def handle_card_delperson_confirm(callback: CallbackQuery) -> None:
    person_id = int(callback.data.split(":")[2])
    async with session_scope() as session:
        person = await get_person(session, person_id)
        if person is not None:
            await birthdays.remove_birthday_reminders(session, person)
            await delete_person(session, person_id)
            await session.commit()
    await callback.answer("Удалено")
    await callback.message.edit_text("🗑 Человек удалён.")
