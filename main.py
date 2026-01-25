#!/usr/bin/env python3
"""
Resy Bot - Reservation Sniper

A web application for sniping hard-to-get restaurant reservations on Resy.

Usage:
    python main.py              # Run the web UI (default)
    python main.py --daemon     # Run daemon only (headless)
    python main.py --test       # Test credentials
    python main.py --dry-run    # Test snipe flow without booking
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config import DATA_DIR
from storage.database import Database
from resy.client import ResyClient
from resy.models import ResyCredentials


def setup_logging(verbose: bool = False) -> None:
    """Set up logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(DATA_DIR / "resy_bot.log"),
        ],
    )


def run_web(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the web UI."""
    import uvicorn
    from web.app import create_app
    from scheduler.daemon import SchedulerDaemon

    db = Database()
    daemon = SchedulerDaemon(db)

    app = create_app(db, daemon)

    print(f"""
    ╔═══════════════════════════════════════════════════╗
    ║                                                   ║
    ║   🍽️  Resy Bot - Reservation Sniper               ║
    ║                                                   ║
    ║   Open in your browser:                           ║
    ║   http://{host}:{port}                            ║
    ║                                                   ║
    ║   Press Ctrl+C to stop                            ║
    ║                                                   ║
    ╚═══════════════════════════════════════════════════╝
    """)

    uvicorn.run(app, host=host, port=port, log_level="warning")


def run_daemon() -> None:
    """Run the scheduler daemon in headless mode."""
    from scheduler.daemon import run_daemon_standalone

    db = Database()
    creds = db.get_credentials()

    if not creds:
        print("ERROR: No credentials configured.")
        print("Run 'python main.py' first to set up credentials in the web UI.")
        sys.exit(1)

    run_daemon_standalone(db)


def test_credentials() -> None:
    """Test if stored credentials are valid."""
    db = Database()
    creds = db.get_credentials()

    if not creds:
        print("No credentials stored.")
        print("Run 'python main.py' to set up credentials in the web UI.")
        return

    print("Testing credentials...")

    resy_creds = ResyCredentials(
        api_key=creds.api_key,
        auth_token=creds.auth_token,
        payment_method_id=creds.payment_method_id,
    )

    with ResyClient(resy_creds) as client:
        if client.test_credentials():
            user_info = client.get_user_info()
            name = user_info.get("first_name", "Unknown")
            email = user_info.get("email_address", "")
            print(f"SUCCESS! Connected as {name} ({email})")
        else:
            print("FAILED: Invalid credentials")
            print("Your credentials may have expired. Get new ones from resy.com")


def dry_run(venue_id: int, party_size: int, date: str) -> None:
    """Test the snipe flow without actually booking."""
    from scheduler.sniper import test_snipe_dry_run

    db = Database()
    creds = db.get_credentials()

    if not creds:
        print("No credentials stored. Set up credentials first.")
        return

    resy_creds = ResyCredentials(
        api_key=creds.api_key,
        auth_token=creds.auth_token,
        payment_method_id=creds.payment_method_id,
    )

    test_snipe_dry_run(resy_creds, venue_id, party_size, date)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Resy Bot - Reservation Sniper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # Run the web UI
  python main.py --port 3000              # Run on custom port
  python main.py --daemon                 # Run daemon in background
  python main.py --test                   # Test your credentials
  python main.py --dry-run 12345 2 2024-03-15  # Test snipe at venue 12345

Getting Started:
  1. Run 'python main.py' to start the web server
  2. Open http://127.0.0.1:8080 in your browser
  3. Go to Settings and enter your Resy credentials
  4. Add restaurants and schedule your reservation snipes
  5. Start the daemon from the Dashboard

For production use:
  1. Start the daemon separately: python main.py --daemon
  2. The daemon runs in the background and executes snipes at scheduled times
        """,
    )

    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run the scheduler daemon in headless mode",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test if stored credentials are valid",
    )
    parser.add_argument(
        "--dry-run",
        nargs=3,
        metavar=("VENUE_ID", "PARTY_SIZE", "DATE"),
        help="Test snipe flow without booking (venue_id party_size YYYY-MM-DD)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8080,
        help="Port for web server (default: 8080)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for web server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.daemon:
        run_daemon()
    elif args.test:
        test_credentials()
    elif args.dry_run:
        venue_id = int(args.dry_run[0])
        party_size = int(args.dry_run[1])
        date = args.dry_run[2]
        dry_run(venue_id, party_size, date)
    else:
        run_web(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
