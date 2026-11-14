"""Configuration settings for the Resy Bot."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

_volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DATA_DIR = Path(_volume_path) if _volume_path else BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = DATA_DIR / "resy_bot.db"

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/New_York")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}
SESSION_COOKIE_NAME = "resybot_session"

RESY_API_BASE = "https://api.resy.com"
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"

SNIPE_WINDOW_SECONDS = int(os.getenv("SNIPE_WINDOW_SECONDS", "10"))
SNIPE_EARLY_WAKE_SECONDS = int(os.getenv("SNIPE_EARLY_WAKE_SECONDS", "5"))
PRE_POLL_SECONDS = float(os.getenv("PRE_POLL_SECONDS", "1"))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "0.1"))
MONITOR_POLL_TICK_SECONDS = int(os.getenv("MONITOR_POLL_TICK_SECONDS", "15"))
MONITOR_NEAR_INTERVAL_SECONDS = int(os.getenv("MONITOR_NEAR_INTERVAL_SECONDS", "15"))
MONITOR_WEEK_INTERVAL_SECONDS = int(os.getenv("MONITOR_WEEK_INTERVAL_SECONDS", "30"))
MONITOR_FAR_INTERVAL_SECONDS = int(os.getenv("MONITOR_FAR_INTERVAL_SECONDS", "60"))

# OpenTable provider. Official API credentials override the browser fallback when present.
OPENTABLE_API_ACCESS_TOKEN = os.getenv("OPENTABLE_API_ACCESS_TOKEN", "")
OPENTABLE_API_BASE_URL = os.getenv("OPENTABLE_API_BASE_URL", "")
OPENTABLE_WEB_ENABLED = os.getenv("OPENTABLE_WEB_ENABLED", "true").lower() not in {"0", "false", "no"}
OPENTABLE_WEB_PROFILE_DIR = os.getenv(
    "OPENTABLE_WEB_PROFILE_DIR",
    str(DATA_DIR / "opentable-browser"),
)
OPENTABLE_WEB_HEADLESS = os.getenv("OPENTABLE_WEB_HEADLESS", "true").lower() not in {"0", "false", "no"}
