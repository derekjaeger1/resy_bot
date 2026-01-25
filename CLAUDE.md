# Resy Bot - Project Context

## Overview
A Python-based Resy reservation sniper with web UI for sniping hard-to-get restaurant reservations.

## Development Setup

```bash
# Always activate the virtual environment first
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the web server
python main.py

# Run tests
python test_booking.py --dry-run
```

## IMPORTANT: Running Python Commands

When running Python scripts or one-liners from the command line, ALWAYS use the venv Python:

```bash
# Correct - use venv python
./venv/bin/python -c "from storage.database import Database; ..."
./venv/bin/python main.py

# Wrong - will fail with missing modules
python3 -c "from storage.database import Database; ..."
poetry run python ...  # No poetry in this project
```

## Project Structure

```
resy_bot/
├── main.py              # Entry point (web server, daemon, CLI)
├── config.py            # Configuration constants
├── test_booking.py      # Test script for booking flow
├── requirements.txt     # Python dependencies
│
├── resy/                # Resy API client
│   ├── client.py        # HTTP client with find/book methods
│   └── models.py        # Pydantic models (Slot, TimePreference, etc.)
│
├── scheduler/           # Background job scheduling
│   ├── daemon.py        # APScheduler-based daemon
│   └── sniper.py        # Sniping logic (warmup, poll, book)
│
├── storage/             # SQLite database layer
│   ├── database.py      # Database operations
│   └── models.py        # SQLAlchemy models
│
├── web/                 # FastAPI web interface
│   ├── app.py           # Routes and API endpoints
│   └── templates/       # Jinja2 HTML templates (Tailwind CSS)
│
├── tui/                 # Legacy Textual TUI (unused)
│
├── data/                # Runtime data (created automatically)
│   └── resy_bot.db      # SQLite database
│
└── venv/                # Python virtual environment
```

## Key Configuration (config.py)

- `SNIPE_EARLY_WAKE_SECONDS = 5` - Wake up early to warm connections
- `PRE_POLL_SECONDS = 3` - Start polling BEFORE release (accounts for network latency)
- `POLL_INTERVAL_SECONDS = 0.1` - Aggressive 100ms polling
- `SNIPE_WINDOW_SECONDS = 10` - How long to retry after release
- `RESY_API_KEY` - Public Resy API key (same for all users)

## Sniping Strategy

1. Daemon wakes up 5 seconds before release time
2. Warms up HTTP connection pool
3. Starts polling 3 seconds BEFORE release (request in-flight when slots drop)
4. Polls aggressively every 100ms for 13 seconds total (-3s to +10s from release)
5. Uses fuzzy time matching (configurable ±window)
6. Immediately retries on booking failure (no delay)
7. All API calls logged to `snipe_logs` table for debugging

## API Flow

1. **Find** (`GET /4/find`) - Get available slots
2. **Details** (`POST /3/details`) - Get booking token for slot
   - Requires `Content-Type: application/json`
   - Use `config.token` (rgs://...) as config_id, not numeric ID
   - Returns 201 on success
3. **Book** (`POST /3/book`) - Complete the reservation
   - Requires `Content-Type: application/x-www-form-urlencoded` (form data)
   - Returns 201 on success
   - Returns 412 if user already has a reservation at this restaurant/date

## Database Models

- `credentials` - Resy API credentials (api_key, auth_token, payment_method_id)
- `restaurants` - Saved venues with release schedules
- `scheduled_reservations` - Pending/completed snipe jobs
- `reservation_history` - Historical snipe results
- `monitored_reservations` - Cancellation monitors (poll for openings on released dates)
- `snipe_logs` - Detailed API debugging logs (log_type, message, elapsed_ms, details)

## Running Commands

```bash
# Web UI (default)
python main.py

# Test credentials
python main.py --test

# Headless daemon mode
python main.py --daemon

# Test booking flow
python test_booking.py --dry-run    # Find slots only
python test_booking.py --book       # Actually book
```

## Web UI Routes

- `/` - Dashboard (upcoming snipes, recent activity, daemon control)
- `/restaurants` - Manage saved restaurants
- `/reservations` - Schedule and view snipes (duplicate, edit, cancel)
- `/monitors` - Cancellation monitors (poll for last-minute openings)
- `/availability` - Check current availability across restaurants
- `/settings` - Configure Resy credentials (login or manual tokens)
- `/logs` - Debug snipe API logs (view requests/responses/errors)
- `/my-reservations` - View and cancel Resy reservations on your account
- `/api/status` - JSON status endpoint
- `/api/search` - Venue search API
