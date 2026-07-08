from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Person
from db.repo import list_people

# Default threshold used for "strict" lookups (add_person_info, query_person,
# add_alias, search) where a wrong match is more costly (silently attaching
# a note/birthday to the wrong person).
MATCH_THRESHOLD = 80

# Looser threshold used only in the person-CREATE flow. A false positive
# there just means one extra candidate button in a list the user reviews
# before anything is written — nothing is attached silently — so it's safe
# to be much more liberal about surfacing Russian diminutives.
CREATE_MATCH_THRESHOLD = 60

# A family-match (query and candidate both belong to the same name family
# below) is a strong signal — score it high enough to clear both the create
# and the strict thresholds.
FAMILY_MATCH_SCORE = 92

# A merely-shared-prefix signal is weaker — only meant to clear the loose
# create threshold, not the strict one.
PREFIX_MATCH_SCORE = 65
PREFIX_MIN_LEN = 4

# Common Russian given-name families: canonical form -> diminutives/variants.
# Not exhaustive — a deliberately small, high-value builtin list; the prefix
# heuristic below catches many other cases (Валерчик/Валеныч/Валера all
# share "Вале...").
NAME_FAMILIES: dict[str, set[str]] = {
    "александр": {"саша", "сашка", "санёк", "санек", "саня", "шурик"},
    "дмитрий": {"дима", "димон", "митя", "димка"},
    "валерий": {"валера", "валерка", "валерчик", "валеныч", "валерочка", "валерок"},
    "владимир": {"вова", "вовка", "вован", "володя", "вольдемар"},
    "сергей": {"серёга", "серега", "серёжа", "сережа", "серж"},
    "николай": {"коля", "коляныч", "николаич"},
    "евгений": {"женя", "женёк", "женек"},
    "андрей": {"андрюха", "андрюша"},
    "михаил": {"миша", "мишка", "мишаня"},
    "иван": {"ваня", "ванька", "ванюша"},
    "екатерина": {"катя", "катюша", "катенька"},
    "елена": {"лена", "леночка", "аленка"},
    "анна": {"аня", "анюта", "анька"},
    "мария": {"маша", "машенька", "машка"},
    "татьяна": {"таня", "танюша", "танька"},
    "ольга": {"оля", "оленька", "олька"},
    "наталья": {"наташа", "натаха", "натуля"},
    "виктор": {"витя", "витёк", "витек"},
    "юрий": {"юра", "юрец", "юрка"},
    "константин": {"костя", "костик"},
    "павел": {"паша", "пашка", "пашок"},
    "алексей": {"лёша", "леша", "лёха", "леха"},
    "антон": {"антоха", "антоша"},
    "денис": {"дэн", "деня"},
    "игорь": {"игорёк", "игорек"},
    "роман": {"рома", "ромка", "ромыч"},
    "максим": {"макс", "максимка"},
    "артём": {"артем", "тёма", "тема"},
    "виталий": {"виталик", "виталя"},
    "вячеслав": {"слава", "славик"},
    "геннадий": {"гена", "геныч"},
    "григорий": {"гриша", "гришка"},
}

_VARIANT_TO_FAMILY: dict[str, str] = {}
for _canonical, _variants in NAME_FAMILIES.items():
    _VARIANT_TO_FAMILY[_canonical] = _canonical
    for _variant in _variants:
        _VARIANT_TO_FAMILY[_variant] = _canonical


def normalize(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


def name_family(name: str) -> str | None:
    """Canonical name family for a (possibly diminutive) Russian first
    name, or None if it's not in the builtin dictionary."""
    return _VARIANT_TO_FAMILY.get(normalize(name))


def _family_of(normalized_name: str) -> str | None:
    return _VARIANT_TO_FAMILY.get(normalized_name)


def _shared_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def combined_score(query_norm: str, candidate_norm: str) -> float:
    """Fuzzy score, boosted by two Russian-diminutive-aware signals:
    a builtin name-family match (strong signal) and a shared-prefix
    heuristic (weak signal, e.g. "Валерчик"/"Валеныч"/"Валера" all start
    with "Вале...")."""
    score = fuzz.token_set_ratio(query_norm, candidate_norm)

    family_q = _family_of(query_norm)
    family_c = _family_of(candidate_norm)
    if family_q and family_q == family_c:
        score = max(score, FAMILY_MATCH_SCORE)
    elif _shared_prefix_len(query_norm, candidate_norm) >= PREFIX_MIN_LEN:
        score = max(score, PREFIX_MATCH_SCORE)

    return score


@dataclass
class PersonMatch:
    person: Person
    score: float


async def find_matches(
    session: AsyncSession, user_id: int, name: str, threshold: float = MATCH_THRESHOLD
) -> list[PersonMatch]:
    people = await list_people(session, user_id)
    query = normalize(name)
    matches: list[PersonMatch] = []
    for person in people:
        candidates = [person.name] + [a.alias for a in person.aliases]
        best = max(combined_score(query, normalize(c)) for c in candidates)
        if best >= threshold:
            matches.append(PersonMatch(person=person, score=best))
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
