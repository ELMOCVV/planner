"""Small shared UI helpers: a "close" button that deletes the message
entirely (not just its keyboard), and an empty keyboard constant used to
strip buttons off a message after a final action, so stale menus don't
keep accumulating live buttons in the chat."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CLOSE_CALLBACK = "ui:close"
CLOSE_BUTTON = InlineKeyboardButton(text="✖️ Закрыть", callback_data=CLOSE_CALLBACK)

# Passed explicitly (rather than omitting reply_markup) so the keyboard is
# reliably cleared: Telegram's editMessageText leaves the markup unchanged
# if the field is absent from the request, so "don't pass it" is not the
# same as "remove it".
EMPTY_KB = InlineKeyboardMarkup(inline_keyboard=[])
