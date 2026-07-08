"""Eval harness for Russian-diminutive-aware person dedup matching
(services/person_matcher.py). Pure local logic — no LLM calls, no
Telegram objects — just: create person A, then check that querying a
diminutive/variant of A's name surfaces A as a match at the CREATE-flow
threshold (and, for a couple of cases, that unrelated names do NOT match).

Run with: python3 scripts/eval_dedup.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

TEST_DATA_DIR = Path(__file__).resolve().parent / "_eval_dedup_scratch"
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("TIMEZONE", "Europe/Kyiv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repo import init_db, session_scope, create_person  # noqa: E402
from services.person_matcher import CREATE_MATCH_THRESHOLD, find_matches  # noqa: E402

# (existing person name already in the DB, query name typed by the user,
#  should_match: True if `existing` is expected among the candidates)
CASES = [
    ("Валерчик", "Валера", True),
    ("Валерчик", "Валеныч", True),
    ("Валера", "Валеныч", True),
    ("Александр", "Саша", True),
    ("Дмитрий", "Дима", True),
    ("Владимир", "Вова", True),
    ("Сергей", "Серёга", True),
    ("Екатерина", "Катя", True),
    ("Николай", "Коля", True),
    ("Валерий", "Валерка", True),
    ("Иван", "Ваня", True),
    ("Ольга", "Оля", True),
    # negative controls: unrelated names must NOT surface as candidates
    ("Валера", "Игорь", False),
    ("Дмитрий", "Николай", False),
]


async def main() -> None:
    await init_db()
    user_id = 1

    rows = []
    for existing_name, query_name, should_match in CASES:
        async with session_scope() as session:
            person = await create_person(session, user_id, existing_name)
            await session.commit()
            person_id = person.id

        async with session_scope() as session:
            matches = await find_matches(session, user_id, query_name, threshold=CREATE_MATCH_THRESHOLD)

        found = any(m.person.id == person_id for m in matches)
        passed = found == should_match
        score = next((m.score for m in matches if m.person.id == person_id), None)
        rows.append((existing_name, query_name, should_match, found, score, passed))

    label_w = max(len(r[0]) + len(r[1]) for r in rows) + 20
    print(f"{'EXISTING → QUERY':<{label_w}}{'EXPECTED':<12}{'FOUND':<10}{'SCORE':<8}{'RESULT'}")
    print("-" * (label_w + 40))
    passed_count = 0
    for existing_name, query_name, should_match, found, score, passed in rows:
        passed_count += int(passed)
        label = f"{existing_name} → {query_name}"
        score_s = f"{score:.0f}" if score is not None else "-"
        status = "PASS" if passed else "FAIL"
        print(f"{label:<{label_w}}{str(should_match):<12}{str(found):<10}{score_s:<8}{status}")

    print("-" * (label_w + 40))
    print(f"\n{passed_count}/{len(rows)} passed")

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    if passed_count != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
