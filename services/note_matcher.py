"""Fuzzy duplicate detection for a person's notes.

Saving must stay one-step — this never blocks or asks for confirmation,
it only flags a likely-duplicate so the user notices ("любимый цвет
чёрный" vs "любит чёрный цвет" is the real example that motivated this).
"""

from rapidfuzz import fuzz

SIMILARITY_THRESHOLD = 85


def find_similar_note(existing_texts: list[str], new_text: str, threshold: float = SIMILARITY_THRESHOLD) -> str | None:
    """Return the existing note text most similar to new_text, if any
    clears the threshold — used to warn (not block) on likely duplicates."""
    best_score = 0.0
    best_text = None
    for text in existing_texts:
        score = fuzz.token_set_ratio(new_text.lower(), text.lower())
        if score > best_score:
            best_score = score
            best_text = text
    return best_text if best_score >= threshold else None


def find_duplicate_pairs(notes: list, threshold: float = SIMILARITY_THRESHOLD) -> list[tuple]:
    """All pairs of notes (objects with a `.text` attribute) whose text is
    similar enough to likely be redundant, sorted by similarity descending.
    Used by the "Почистить дубли" person-card button."""
    pairs = []
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            score = fuzz.token_set_ratio(notes[i].text.lower(), notes[j].text.lower())
            if score >= threshold:
                pairs.append((notes[i], notes[j], score))
    pairs.sort(key=lambda p: p[2], reverse=True)
    return pairs
