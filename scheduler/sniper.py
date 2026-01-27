"""Reservation sniping logic."""

import json
import time as time_module
from datetime import datetime, timedelta
from typing import Optional
import logging

from resy.client import ResyClient, ResyClientError, BookingError
from resy.models import ResyCredentials, TimePreference, Slot
from storage.database import Database
from storage.models import ScheduledReservation, ReservationStatus, SnipeLogType
from config import SNIPE_WINDOW_SECONDS, POLL_INTERVAL_SECONDS, SNIPE_EARLY_WAKE_SECONDS, PRE_POLL_SECONDS

logger = logging.getLogger(__name__)


class SnipeResult:
    """Result of a snipe attempt."""

    def __init__(
        self,
        success: bool,
        message: str,
        booked_time: Optional[str] = None,
        resy_token: Optional[str] = None,
    ):
        self.success = success
        self.message = message
        self.booked_time = booked_time
        self.resy_token = resy_token

    def __repr__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"<SnipeResult {status}: {self.message}>"


class ReservationSniper:
    """Handles the sniping logic for reservations."""

    def __init__(self, db: Database, credentials: ResyCredentials):
        self.db = db
        self.credentials = credentials
        self._snipe_start_time: Optional[float] = None
        self._current_reservation_id: Optional[int] = None
        self._current_restaurant_name: Optional[str] = None
        self._current_target_date = None

    def _log(
        self,
        log_type: SnipeLogType,
        message: str,
        details: Optional[str] = None,
    ) -> None:
        """Log a snipe event to the database."""
        elapsed_ms = None
        if self._snipe_start_time:
            elapsed_ms = int((time_module.time() - self._snipe_start_time) * 1000)

        try:
            self.db.add_snipe_log(
                restaurant_name=self._current_restaurant_name or "Unknown",
                target_date=self._current_target_date,
                log_type=log_type,
                message=message,
                reservation_id=self._current_reservation_id,
                details=details,
                elapsed_ms=elapsed_ms,
            )
        except Exception as e:
            logger.warning(f"Failed to write snipe log: {e}")

    def snipe(self, reservation: ScheduledReservation) -> SnipeResult:
        """
        Attempt to snipe a reservation.

        This method will:
        1. Wake up early and warm up the HTTP connection
        2. Wait until exact release time
        3. Poll aggressively for available slots
        4. Find the best matching slot based on preferences
        5. Attempt to book immediately

        Args:
            reservation: The scheduled reservation to attempt

        Returns:
            SnipeResult with success/failure and details
        """
        # Set up logging context
        self._current_reservation_id = reservation.id
        self._current_target_date = reservation.target_date
        self._snipe_start_time = time_module.time()

        # Update status to in progress
        self.db.update_reservation_status(
            reservation.id,
            ReservationStatus.IN_PROGRESS,
        )

        # Parse preferences
        preferred_times = []
        if reservation.preferred_times:
            try:
                prefs_data = json.loads(reservation.preferred_times)
                preferred_times = [TimePreference(**p) for p in prefs_data]
            except Exception as e:
                logger.warning(f"Failed to parse preferred times: {e}")

        # Get restaurant info
        restaurant = self.db.get_restaurant(reservation.restaurant_id)
        if not restaurant:
            result = SnipeResult(False, "Restaurant not found in database")
            self._record_result(reservation, result, restaurant)
            return result

        # Set restaurant name for logging
        self._current_restaurant_name = restaurant.name

        venue_id = restaurant.venue_id
        party_size = reservation.party_size
        target_date = reservation.target_date.strftime("%Y-%m-%d")

        logger.info(
            f"Starting snipe for {restaurant.name} on {target_date} "
            f"(party of {party_size})"
        )
        self._log(
            SnipeLogType.INFO,
            f"Starting snipe for {restaurant.name}",
            json.dumps({
                "venue_id": venue_id,
                "party_size": party_size,
                "target_date": target_date,
                "snipe_at": reservation.snipe_at.isoformat(),
            }),
        )

        # Create client
        with ResyClient(self.credentials) as client:
            # WARM UP: Test credentials and establish connection pool
            logger.info("Warming up connection...")
            self._log(SnipeLogType.API_CALL, "Testing credentials (warmup)")
            try:
                client.test_credentials()
                logger.info("Connection warmed up, credentials valid")
                self._log(SnipeLogType.API_RESPONSE, "Credentials valid, connection warmed up")
            except Exception as e:
                logger.warning(f"Warmup failed (continuing anyway): {e}")
                self._log(SnipeLogType.API_ERROR, f"Warmup failed: {e}")

            # Calculate when to start polling (PRE_POLL_SECONDS before release)
            release_time = reservation.snipe_at
            poll_start_time = release_time - timedelta(seconds=PRE_POLL_SECONDS)
            now = datetime.now()

            if now < poll_start_time:
                wait_seconds = (poll_start_time - now).total_seconds()
                if wait_seconds > 0:
                    logger.info(
                        f"Waiting {wait_seconds:.1f}s until pre-poll time "
                        f"({PRE_POLL_SECONDS}s before release at {release_time})"
                    )
                    # Sleep in small increments so we don't overshoot
                    while datetime.now() < poll_start_time:
                        remaining = (poll_start_time - datetime.now()).total_seconds()
                        if remaining > 1:
                            time_module.sleep(0.5)
                        elif remaining > 0:
                            time_module.sleep(0.01)  # Fine-grained sleep near start
                        else:
                            break

            logger.info(
                f"Starting pre-poll ({PRE_POLL_SECONDS}s before release) - "
                f"release time is {release_time}"
            )
            self._log(
                SnipeLogType.INFO,
                f"Starting pre-poll {PRE_POLL_SECONDS}s before release",
                json.dumps({"release_time": release_time.isoformat()}),
            )

            start_time = time_module.time()
            # Poll for PRE_POLL_SECONDS + SNIPE_WINDOW_SECONDS total
            # This ensures we poll before AND after release time
            total_poll_window = PRE_POLL_SECONDS + SNIPE_WINDOW_SECONDS
            end_time = start_time + total_poll_window

            last_error = None
            attempts = 0
            failed_tokens = set()  # Track tokens that failed so we try different slots

            while time_module.time() < end_time:
                attempts += 1

                try:
                    # Get all matching slots so we can skip failed ones
                    all_slots = client.find_slots(
                        venue_id=venue_id,
                        party_size=party_size,
                        date=target_date,
                    )

                    # Filter to matching slots that we haven't tried yet
                    slot = None
                    for s in all_slots:
                        # Skip slots we already tried and failed
                        if s.token in failed_tokens:
                            continue
                        # Check if it matches preferences
                        if not preferred_times:
                            slot = s
                            break
                        for pref in preferred_times:
                            if pref.matches(s):
                                slot = s
                                break
                        if slot:
                            break

                    # If all matching slots failed, try any slot we haven't tried
                    if not slot:
                        for s in all_slots:
                            if s.token not in failed_tokens:
                                slot = s
                                break

                    if slot:
                        logger.info(f"Found slot: {slot.time_slot} ({slot.table_type})")
                        self._log(
                            SnipeLogType.SLOT_FOUND,
                            f"Found slot: {slot.time_slot}",
                            json.dumps({
                                "time_slot": slot.time_slot,
                                "table_type": slot.table_type,
                                "token": slot.token[:100] if slot.token else None,
                                "attempt": attempts,
                                "skipped_failed": len(failed_tokens),
                            }),
                        )

                        # Try to book it
                        result = self._attempt_booking(client, slot, restaurant)
                        if result.success:
                            self._log(
                                SnipeLogType.SUCCESS,
                                f"Successfully booked {slot.time_slot}",
                                json.dumps({
                                    "booked_time": result.booked_time,
                                    "resy_token": result.resy_token,
                                }),
                            )
                            self._record_result(reservation, result, restaurant)
                            return result

                        # Booking failed - mark this token as failed and try a different slot
                        failed_tokens.add(slot.token)
                        last_error = result.message
                        logger.warning(f"Booking attempt failed: {result.message}, will try different slot")
                        self._log(
                            SnipeLogType.API_ERROR,
                            f"Booking attempt failed, trying different slot",
                            json.dumps({"error": result.message, "attempt": attempts, "failed_tokens": len(failed_tokens)}),
                        )
                        continue  # Skip delay, try again immediately with different slot
                    else:
                        logger.debug(f"No slots found (attempt {attempts})")
                        # Only log every 10th attempt to avoid log spam
                        if attempts % 10 == 0:
                            self._log(
                                SnipeLogType.API_RESPONSE,
                                f"No slots found (attempt {attempts})",
                            )

                except ResyClientError as e:
                    last_error = str(e)
                    logger.warning(f"API error: {e}")
                    self._log(
                        SnipeLogType.API_ERROR,
                        f"API error during polling",
                        json.dumps({"error": str(e), "attempt": attempts}),
                    )
                    # Back off on rate limit errors
                    if "429" in str(e) or "Rate Limit" in str(e):
                        logger.info("Rate limited, backing off 500ms")
                        time_module.sleep(0.5)
                        continue

                # Small delay before retrying (only when no slots found)
                time_module.sleep(POLL_INTERVAL_SECONDS)

            # Snipe window expired
            elapsed = time_module.time() - start_time
            message = (
                f"Failed after {attempts} attempts over {elapsed:.1f}s. "
                f"Last error: {last_error or 'No slots available'}"
            )
            self._log(
                SnipeLogType.FAILURE,
                f"Snipe failed after {attempts} attempts",
                json.dumps({
                    "attempts": attempts,
                    "elapsed_seconds": round(elapsed, 1),
                    "last_error": last_error,
                }),
            )
            result = SnipeResult(False, message)
            self._record_result(reservation, result, restaurant)
            return result

    def _attempt_booking(
        self,
        client: ResyClient,
        slot: Slot,
        restaurant,
    ) -> SnipeResult:
        """Attempt to book a specific slot."""
        self._log(
            SnipeLogType.BOOKING_ATTEMPT,
            f"Attempting to book {slot.time_slot}",
            json.dumps({"time_slot": slot.time_slot, "table_type": slot.table_type}),
        )
        try:
            # Get booking details
            self._log(SnipeLogType.API_CALL, "Getting slot details")
            details = client.get_slot_details(slot)
            logger.info("Got booking details, attempting to book...")
            self._log(SnipeLogType.API_RESPONSE, "Got slot details, booking...")

            # Book it
            self._log(SnipeLogType.API_CALL, "Booking reservation")
            response = client.book_reservation(details)

            resy_token = response.get("resy_token", "")
            message = f"Successfully booked {slot.time_slot} at {restaurant.name}"
            logger.info(message)
            self._log(
                SnipeLogType.API_RESPONSE,
                "Booking successful",
                json.dumps({"resy_token": resy_token}),
            )

            return SnipeResult(
                success=True,
                message=message,
                booked_time=slot.time_slot,
                resy_token=resy_token,
            )

        except BookingError as e:
            self._log(SnipeLogType.API_ERROR, f"BookingError: {e}")
            return SnipeResult(False, f"Booking failed: {e}")
        except ResyClientError as e:
            self._log(SnipeLogType.API_ERROR, f"ResyClientError: {e}")
            return SnipeResult(False, f"API error: {e}")
        except Exception as e:
            self._log(SnipeLogType.API_ERROR, f"Unexpected error: {e}")
            return SnipeResult(False, f"Unexpected error: {e}")

    def _record_result(
        self,
        reservation: ScheduledReservation,
        result: SnipeResult,
        restaurant,
    ) -> None:
        """Record the snipe result in the database."""
        # Update reservation status
        status = ReservationStatus.SUCCESS if result.success else ReservationStatus.FAILED
        self.db.update_reservation_status(
            reservation.id,
            status,
            result_message=result.message,
            booked_time=result.booked_time,
            resy_token=result.resy_token,
        )

        # Add to history
        if restaurant:
            self.db.add_history(
                restaurant_name=restaurant.name,
                venue_id=restaurant.venue_id,
                target_date=reservation.target_date,
                party_size=reservation.party_size,
                success=result.success,
                booked_time=result.booked_time,
                resy_token=result.resy_token,
                error_message=None if result.success else result.message,
            )


def test_snipe_dry_run(
    credentials: ResyCredentials,
    venue_id: int,
    party_size: int,
    date: str,
) -> None:
    """
    Test the snipe flow without actually booking.
    Useful for verifying credentials and slot availability.
    """
    with ResyClient(credentials) as client:
        print(f"Testing credentials...")
        if not client.test_credentials():
            print("ERROR: Invalid credentials")
            return

        print("Credentials valid!")
        print(f"\nSearching for slots at venue {venue_id} on {date}...")

        slots = client.find_slots(venue_id, party_size, date)

        if not slots:
            print("No slots available")
            return

        print(f"\nFound {len(slots)} slots:")
        for slot in slots[:10]:  # Show first 10
            print(f"  - {slot.time_slot} ({slot.table_type or 'Any'})")

        if len(slots) > 10:
            print(f"  ... and {len(slots) - 10} more")
