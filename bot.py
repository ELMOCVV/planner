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
from handlers.common import AccessControlMiddleware
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

    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    dp.include_router(reminders.router)
    dp.include_router(people.router)
    dp.include_router(settings.router)
    dp.include_router(common.router)

    init_scheduler(bot)
    await sync_all_birthday_reminders()
    await restore_missing_jobs()

    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
