import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from db.repo import init_db
from handlers import common, people, reminders, settings
from handlers.common import AccessControlMiddleware, ErrorGuardMiddleware
from services.birthdays import sync_all_birthday_reminders
from services.scheduler import init_scheduler, restore_missing_jobs


def setup_logging() -> None:
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")
    if not config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY is not set — message parsing will fail.")

    logger.info("DATA_DIR resolved to: %s", config.DATA_DIR)
    logger.info("Main DB path: %s", config.DB_PATH)
    logger.info("Scheduler jobstore DB path: %s", config.SCHEDULER_DB_PATH)

    await init_db()

    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Order matters: the error guard wraps everything (outermost), then
    # access control filters unauthorized users before any handler runs.
    dp.message.middleware(ErrorGuardMiddleware())
    dp.callback_query.middleware(ErrorGuardMiddleware())
    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    dp.include_router(reminders.router)
    dp.include_router(people.router)
    dp.include_router(settings.router)
    dp.include_router(common.router)

    init_scheduler(bot)
    await sync_all_birthday_reminders()
    await restore_missing_jobs()

    from services.backup import run_daily_backup
    from services.scheduler import scheduler as _sched

    _sched.add_job(
        run_daily_backup,
        trigger="cron",
        hour=3,
        minute=30,
        id="daily_backup",
        replace_existing=True,
        misfire_grace_time=86400,
    )

    logger.info("Bot started")
    try:
        # aiogram handles SIGINT/SIGTERM itself (Railway sends SIGTERM on
        # redeploy): polling stops and in-flight handlers finish before
        # start_polling returns, then we release everything else.
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down: scheduler, DB engine, bot session")
        _sched.shutdown(wait=False)
        from db.repo import engine

        await engine.dispose()
        await bot.session.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
