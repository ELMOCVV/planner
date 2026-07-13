"""Lightweight per-user rolling conversation history.

Used to give the intent classifier just enough context to resolve short
follow-up replies ("сегодня", "в 12", "да") to a prior bot question,
without needing a full database-backed chat log. In-memory is enough here:
these clarifications happen within a single short exchange, not across
bot restarts.
"""

import re
from collections import deque

MAX_TURNS = 6

_history: dict[int, deque] = {}

CANCEL_WORDS = re.compile(
    r"\b(отмени\w*|удали\w*|убер\w*|не надо|отмена|стоп)\b", re.IGNORECASE
)


def record_user(user_id: int, text: str) -> None:
    _history.setdefault(user_id, deque(maxlen=MAX_TURNS)).append(("Пользователь", text))


def record_bot(user_id: int, text: str) -> None:
    _history.setdefault(user_id, deque(maxlen=MAX_TURNS)).append(("Бот", text))


def get_context_text(user_id: int) -> str | None:
    hist = _history.get(user_id)
    if not hist:
        return None
    return "\n".join(f"{role}: {text}" for role, text in hist)


def clear(user_id: int) -> None:
    _history.pop(user_id, None)


def is_explicit_cancel(text: str) -> bool:
    """True only for explicit cancel/delete wording, per the fix for the
    classifier treating unrelated phrases (e.g. birthday congratulations)
    as a cancellation. Checked in code, deterministically, before any LLM
    call — so this never depends on model judgement.

    A cancel word must either open the message or appear in a short
    message (≤4 words): a longer sentence merely *containing* one — e.g.
    "напомни, что не надо покупать хлеб" — is a normal request, not a
    cancellation."""
    match = CANCEL_WORDS.search(text)
    if not match:
        return False
    return match.start() == 0 or len(text.split()) <= 4
