"""Configuration settings for the Resy Bot."""

import os
from pathlib import Path

# Base directory for the application
BASE_DIR = Path(__file__).parent.resolve()

# Data directory for database and logs
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Database file path
DATABASE_PATH = DATA_DIR / "resy_bot.db"

# Resy API base URL
RESY_API_BASE = "https://api.resy.com"

# Default snipe window in seconds (how long to retry after release time)
SNIPE_WINDOW_SECONDS = 10

# How many seconds before release time to wake up and prepare
# Just enough to warm up the connection - Alkaar uses 2 seconds
SNIPE_EARLY_WAKE_SECONDS = 5

# How many seconds before release time to START polling
# This accounts for network latency - your request needs to be in-flight
# when slots drop, not sent after they drop
# Keep this low (1s) to reduce rate limiting risk on competitive restaurants
PRE_POLL_SECONDS = 1

# Polling interval during snipe window (in seconds)
# 100ms is aggressive but sniper has 429 backoff if rate limited
POLL_INTERVAL_SECONDS = 0.1

# Request timeout in seconds (increased from 10 to handle occasional slow responses)
REQUEST_TIMEOUT_SECONDS = 15

# User agent for requests
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Resy public API key (same for all users - obtained from their web client)
RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"

# Cancellation monitor polling interval (in minutes)
# Check every 10 minutes for cancellations
MONITOR_POLL_INTERVAL_MINUTES = 10
