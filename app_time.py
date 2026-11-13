"""Application time helpers.

Reservation release rules are interpreted in the configured local timezone
(default: America/New_York). SQLite stores timestamps as naive local wall-clock
values so existing databases remain compatible.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from config import APP_TIMEZONE

APP_TZ = ZoneInfo(APP_TIMEZONE)

def now_local() -> datetime:
    return datetime.now(APP_TZ)

def now_local_naive() -> datetime:
    return now_local().replace(tzinfo=None)

def localize_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(APP_TZ)
    return value.replace(tzinfo=APP_TZ)
