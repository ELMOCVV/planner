"""Small shared UI helpers: a "close" button that deletes the message
entirely (not just its keyboard), and an empty keyboard constant used to
strip buttons off a message after a final action, so stale menus don't
keep accumulating live buttons in the chat."""

import datetime as dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Genitive month names for friendly Russian dates ("12 июля", not "12.07").
MONTHS_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_date_ru(when: dt.date) -> str:
    return f"{when.day} {MONTHS_RU_GEN[when.month - 1]}"


def format_birthday_ru(month: int, day: int, year: int | None = None) -> str:
    base = f"{day} {MONTHS_RU_GEN[month - 1]}"
    return f"{base} ({year})" if year else base

CLOSE_CALLBACK = "ui:close"
CLOSE_BUTTON = InlineKeyboardButton(text="✖️ Закрыть", callback_data=CLOSE_CALLBACK)

# Passed explicitly (rather than omitting reply_markup) so the keyboard is
# reliably cleared: Telegram's editMessageText leaves the markup unchanged
# if the field is absent from the request, so "don't pass it" is not the
# same as "remove it".
EMPTY_KB = InlineKeyboardMarkup(inline_keyboard=[])
