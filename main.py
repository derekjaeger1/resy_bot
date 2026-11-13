#!/usr/bin/env python3
"""Resy Bot entry point."""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import APP_TIMEZONE, DATA_DIR
from storage.database import Database
from resy.client import ResyClient
from resy.models import ResyCredentials

def configure_timezone() -> None:
    os.environ["TZ"] = APP_TIMEZONE
    if hasattr(time, "tzset"):
        time.tzset()

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "resy_bot.log")],
    )

def run_web(host: str = "127.0.0.1", port: int = 8080) -> None:
    import uvicorn
    from web.app import create_app
    from scheduler.daemon import SchedulerDaemon
    db = Database()
    daemon = SchedulerDaemon(db)
    app = create_app(db, daemon)
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True)

def run_daemon() -> None:
    from scheduler.daemon import run_daemon_standalone
    run_daemon_standalone(Database())

def test_credentials() -> None:
    db = Database()
    creds = db.get_credentials()
    if not creds:
        print("No Resy account connected.")
        return
    rc = ResyCredentials(api_key=creds.api_key, auth_token=creds.auth_token, payment_method_id=creds.payment_method_id)
    with ResyClient(rc) as client:
        if client.test_credentials():
            info = client.get_user_info()
            print(f"Connected as {info.get('first_name', 'User')} ({info.get('email_address', '')})")
        else:
            print("Resy credentials are invalid or expired.")

def main() -> None:
    parser = argparse.ArgumentParser(description="Resy Bot")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("-p", "--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    configure_timezone()
    setup_logging(args.verbose)
    if args.daemon:
        run_daemon()
    elif args.test:
        test_credentials()
    else:
        run_web(host=args.host, port=args.port)

if __name__ == "__main__":
    main()
