#!/usr/bin/env python3
"""Quick test script to verify the bot works."""

import sys
import argparse
from datetime import date, timedelta

from storage.database import Database
from resy.client import ResyClient, search_venues
from resy.models import ResyCredentials, TimePreference


def main():
    parser = argparse.ArgumentParser(description="Test Resy Bot booking flow")
    parser.add_argument("--book", action="store_true", help="Actually book the reservation (no confirmation prompt)")
    parser.add_argument("--dry-run", action="store_true", help="Find slots but don't book (default)")
    args = parser.parse_args()
    db = Database()

    # Get credentials
    creds_db = db.get_credentials()
    if not creds_db:
        print("❌ No credentials found. Go to http://localhost:8080/settings to log in.")
        return

    creds = ResyCredentials(
        api_key=creds_db.api_key,
        auth_token=creds_db.auth_token,
        payment_method_id=creds_db.payment_method_id,
    )

    print(f"✓ Using credentials for: {creds_db.email}")

    with ResyClient(creds) as client:
        # Test credentials
        print("\n1. Testing credentials...")
        if not client.test_credentials():
            print("❌ Credentials invalid or expired. Re-login at /settings")
            return
        print("✓ Credentials valid!")

        # Get user info
        user = client.get_user_info()
        print(f"✓ Logged in as: {user.get('first_name', '')} {user.get('last_name', '')}")

        # Search for a test restaurant
        print("\n2. Searching for restaurants with availability...")

        # Try a few less competitive restaurants
        test_venues = [
            ("L'Artusi", 443),
            ("Via Carota", 25973),
            ("Lilia", 1505),
            ("Bar Pitti", 834),
        ]

        today = date.today()
        # Start 7 days out so reservations can be cancelled for free
        test_dates = [
            today + timedelta(days=7),
            today + timedelta(days=8),
            today + timedelta(days=9),
            today + timedelta(days=10),
        ]

        found_slot = None
        found_venue = None
        found_date = None

        for venue_name, venue_id in test_venues:
            for test_date in test_dates:
                date_str = test_date.strftime("%Y-%m-%d")
                print(f"   Checking {venue_name} on {date_str}...", end=" ")

                try:
                    slots = client.find_slots(venue_id, party_size=2, date=date_str)
                    if slots:
                        print(f"✓ Found {len(slots)} slots!")
                        # Pick slot at index 5 (or last available) to avoid conflicts with earlier tests
                        slot_index = min(5, len(slots) - 1)
                        found_slot = slots[slot_index]
                        found_venue = venue_name
                        found_date = date_str
                        break
                    else:
                        print("no availability")
                except Exception as e:
                    print(f"error: {e}")

            if found_slot:
                break

        if not found_slot:
            print("\n❌ No availability found at test venues. Try a different restaurant.")
            print("   You can search manually:")
            print("   - Go to http://localhost:8080/restaurants")
            print("   - Add a restaurant")
            print("   - Schedule a snipe for a future date")
            return

        # Show what we found
        print(f"\n3. Found available slot!")
        print(f"   Restaurant: {found_venue}")
        print(f"   Date: {found_date}")
        print(f"   Time: {found_slot.time_slot}")
        print(f"   Table: {found_slot.table_type or 'Any'}")

        # Check if we should book
        if not args.book:
            print("\n" + "="*50)
            print("DRY RUN - Not booking. Use --book flag to actually book.")
            print("="*50)
            return

        # Actually book it
        print("\n4. Attempting to book...")
        try:
            details = client.get_slot_details(found_slot)
            print(f"   Got booking token: {details.book_token[:30]}...")

            result = client.book_reservation(details)
            resy_token = result.get("resy_token", "")

            print("\n" + "="*50)
            print("🎉 SUCCESS! Reservation booked!")
            print(f"   Resy Token: {resy_token}")
            print("   Check your Resy app to confirm.")
            print("="*50)

        except Exception as e:
            print(f"\n❌ Booking failed: {e}")
            print("   This could mean the slot was taken or there was an API error.")


if __name__ == "__main__":
    main()
