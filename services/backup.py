"""Automatic daily JSON backups into DATA_DIR/backups, keeping the last 7
per user. A cheap safety net on top of the persistent volume: if the DB
file is ever corrupted or a bad migration slips through, the last week of
full exports survives next to it.
"""

import datetime as dt
import json
import logging

from config import ALLOWED_USER_IDS, DATA_DIR

logger = logging.getLogger(__name__)

BACKUP_DIR = DATA_DIR / "backups"
KEEP_LAST = 7


async def run_daily_backup() -> int:
    from db.repo import export_user_data, session_scope

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d")
    written = 0
    for user_id in sorted(ALLOWED_USER_IDS):
        try:
            async with session_scope() as session:
                data = await export_user_data(session, user_id)
            path = BACKUP_DIR / f"backup_{user_id}_{stamp}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            written += 1
            # prune: keep the newest KEEP_LAST for this user
            files = sorted(BACKUP_DIR.glob(f"backup_{user_id}_*.json"))
            for old in files[:-KEEP_LAST]:
                old.unlink(missing_ok=True)
        except Exception:
            logger.exception("Daily backup failed for user %s", user_id)
    logger.info("Daily backup: wrote %d file(s) to %s", written, BACKUP_DIR)
    return written
