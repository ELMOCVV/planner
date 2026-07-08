import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "bot.db"
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"

# APScheduler uses its own synchronous SQLite connection for the jobstore.
# Keeping it in a separate file (rather than sharing bot.db) avoids SQLite
# file-lock contention with the async engine used for the rest of the app.
SCHEDULER_DB_PATH = DATA_DIR / "scheduler.db"
SCHEDULER_DB_URL = f"sqlite:///{SCHEDULER_DB_PATH}"

LOG_FILE = DATA_DIR / "bot.log"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")

_allowed_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {
    int(x.strip()) for x in _allowed_raw.split(",") if x.strip().isdigit()
}

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
