from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Person
from db.repo import list_people

MATCH_THRESHOLD = 80


def normalize(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


@dataclass
class PersonMatch:
    person: Person
    score: float


async def find_matches(session: AsyncSession, user_id: int, name: str) -> list[PersonMatch]:
    people = await list_people(session, user_id)
    query = normalize(name)
    matches: list[PersonMatch] = []
    for person in people:
        candidates = [person.name] + [a.alias for a in person.aliases]
        best = max(fuzz.token_set_ratio(query, normalize(c)) for c in candidates)
        if best >= MATCH_THRESHOLD:
            matches.append(PersonMatch(person=person, score=best))
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
