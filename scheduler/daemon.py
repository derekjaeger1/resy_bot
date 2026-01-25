"""Background scheduler daemon for running snipe jobs."""

import logging
import signal
import sys
from datetime import datetime, timedelta
from typing import Optional, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from resy.models import ResyCredentials, TimePreference
from resy.client import ResyClient
from storage.database import Database
from storage.models import ScheduledReservation, ReservationStatus, MonitorStatus
from .sniper import ReservationSniper, SnipeResult
from config import SNIPE_EARLY_WAKE_SECONDS, MONITOR_POLL_INTERVAL_MINUTES

logger = logging.getLogger(__name__)


class SchedulerDaemon:
    """Background daemon that runs scheduled reservation snipes."""

    def __init__(
        self,
        db: Database,
        on_snipe_complete: Optional[Callable[[ScheduledReservation, SnipeResult], None]] = None,
    ):
        self.db = db
        self.on_snipe_complete = on_snipe_complete
        self.scheduler = BackgroundScheduler()
        self._running = False
        self._credentials: Optional[ResyCredentials] = None

        # Set up event listeners
        self.scheduler.add_listener(
            self._on_job_executed,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )

    def _get_credentials(self) -> Optional[ResyCredentials]:
        """Get credentials from database."""
        if self._credentials:
            return self._credentials

        creds = self.db.get_credentials()
        if creds:
            self._credentials = ResyCredentials(
                api_key=creds.api_key,
                auth_token=creds.auth_token,
                payment_method_id=creds.payment_method_id,
            )
        return self._credentials

    def refresh_credentials(self) -> None:
        """Force refresh of credentials from database."""
        self._credentials = None
        self._get_credentials()

    def _on_job_executed(self, event) -> None:
        """Handle job execution events."""
        if event.exception:
            logger.error(f"Job {event.job_id} failed: {event.exception}")
        else:
            logger.info(f"Job {event.job_id} completed successfully")

    def _execute_snipe(self, reservation_id: int) -> None:
        """Execute a snipe job."""
        credentials = self._get_credentials()
        if not credentials:
            logger.error("No credentials configured, cannot snipe")
            return

        # Reload reservation from database (session may have expired)
        with self.db.get_session() as session:
            reservation = (
                session.query(ScheduledReservation)
                .filter_by(id=reservation_id)
                .first()
            )

            if not reservation:
                logger.error(f"Reservation {reservation_id} not found")
                return

            if reservation.status != ReservationStatus.PENDING:
                logger.info(
                    f"Reservation {reservation_id} is not pending "
                    f"(status: {reservation.status}), skipping"
                )
                return

        sniper = ReservationSniper(self.db, credentials)
        result = sniper.snipe(reservation)

        logger.info(f"Snipe result: {result}")

        if self.on_snipe_complete:
            self.on_snipe_complete(reservation, result)

    def schedule_job(self, reservation: ScheduledReservation) -> str:
        """
        Schedule a snipe job for a reservation.

        The job wakes up SNIPE_EARLY_WAKE_SECONDS before the release time
        to warm up connections, then waits for exact release time to poll.

        Returns:
            Job ID
        """
        job_id = f"snipe_{reservation.id}"

        # Remove existing job if any
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        # Wake up early to warm up connections
        wake_time = reservation.snipe_at - timedelta(seconds=SNIPE_EARLY_WAKE_SECONDS)

        # Don't schedule in the past
        if wake_time < datetime.now():
            wake_time = datetime.now() + timedelta(seconds=1)

        # Schedule new job
        self.scheduler.add_job(
            self._execute_snipe,
            trigger=DateTrigger(run_date=wake_time),
            args=[reservation.id],
            id=job_id,
            name=f"Snipe for reservation {reservation.id}",
            replace_existing=True,
        )

        logger.info(
            f"Scheduled job {job_id}: wake at {wake_time}, "
            f"release at {reservation.snipe_at}"
        )
        return job_id

    def load_pending_jobs(self) -> int:
        """
        Load all pending reservations and schedule them.

        Returns:
            Number of jobs scheduled
        """
        reservations = self.db.get_pending_reservations()
        count = 0

        for reservation in reservations:
            # Only schedule if snipe time is in the future
            if reservation.snipe_at > datetime.now():
                self.schedule_job(reservation)
                count += 1
            else:
                # Mark as failed if snipe time has passed
                self.db.update_reservation_status(
                    reservation.id,
                    ReservationStatus.FAILED,
                    result_message="Snipe time passed while daemon was not running",
                )

        logger.info(f"Loaded {count} pending jobs")
        return count

    def start(self) -> None:
        """Start the scheduler daemon."""
        if self._running:
            logger.warning("Daemon is already running")
            return

        # Verify credentials
        credentials = self._get_credentials()
        if not credentials:
            logger.warning("No credentials configured. Set up credentials first.")

        # Load pending jobs
        self.load_pending_jobs()

        # Start scheduler
        self.scheduler.start()
        self._running = True

        # Start monitor polling
        self.start_monitor_polling()

        logger.info("Scheduler daemon started")

    def stop(self) -> None:
        """Stop the scheduler daemon."""
        if not self._running:
            return

        self.scheduler.shutdown(wait=True)
        self._running = False
        logger.info("Scheduler daemon stopped")

    def is_running(self) -> bool:
        """Check if the daemon is running."""
        return self._running

    def get_scheduled_jobs(self) -> list:
        """Get list of currently scheduled jobs."""
        return self.scheduler.get_jobs()

    def cancel_job(self, reservation_id: int) -> bool:
        """Cancel a scheduled job."""
        job_id = f"snipe_{reservation_id}"
        job = self.scheduler.get_job(job_id)
        if job:
            self.scheduler.remove_job(job_id)
            logger.info(f"Cancelled job {job_id}")
            return True
        return False

    # ==================== Cancellation Monitor ====================

    def _poll_monitors(self) -> None:
        """Poll active monitors for cancellations and auto-book if found."""
        credentials = self._get_credentials()
        if not credentials:
            logger.debug("No credentials, skipping monitor poll")
            return

        # Expire old monitors first
        expired = self.db.expire_old_monitors()
        if expired:
            logger.info(f"Expired {expired} monitors with past target dates")

        # Get active monitors
        monitors = self.db.get_active_monitors()
        if not monitors:
            return

        logger.info(f"Polling {len(monitors)} active monitors for cancellations")

        with ResyClient(credentials) as client:
            for monitor in monitors:
                try:
                    self._check_monitor(client, monitor)
                except Exception as e:
                    logger.error(f"Error checking monitor {monitor.id}: {e}")

    def _check_monitor(self, client: ResyClient, monitor) -> None:
        """Check a single monitor for availability and book if found."""
        # Get restaurant info
        restaurant = self.db.get_restaurant(monitor.restaurant_id)
        if not restaurant:
            logger.warning(f"Monitor {monitor.id} has invalid restaurant_id")
            return

        # Update last checked
        self.db.update_monitor_checked(monitor.id)

        # Search for slots
        date_str = monitor.target_date.strftime("%Y-%m-%d")
        try:
            slots = client.find_slots(
                venue_id=restaurant.venue_id,
                party_size=monitor.party_size,
                date=date_str,
            )
        except Exception as e:
            logger.debug(f"No availability for {restaurant.name} on {date_str}: {e}")
            return

        if not slots:
            logger.debug(f"No slots for {restaurant.name} on {date_str}")
            return

        # Create time preference for matching
        time_pref = TimePreference(
            time=monitor.preferred_time,
            table_type=monitor.table_type,
            window_minutes=monitor.time_window,
        )

        # Find best matching slot
        matching_slots = [s for s in slots if time_pref.matches(s)]
        if not matching_slots:
            logger.debug(
                f"Found {len(slots)} slots for {restaurant.name} but none match "
                f"preferences (time={monitor.preferred_time}, window=±{monitor.time_window}min)"
            )
            return

        # Sort by time difference (closest to preferred time first)
        matching_slots.sort(key=lambda s: time_pref.time_difference_minutes(s) or 999)
        best_slot = matching_slots[0]

        logger.info(
            f"MATCH FOUND! {restaurant.name} on {date_str} at {best_slot.time_slot} - "
            f"attempting to book..."
        )

        # Attempt to book
        try:
            details = client.get_slot_details(best_slot)
            result = client.book_reservation(details)
            resy_token = result.get("resy_token", "")

            # Mark as success
            self.db.update_monitor_success(
                monitor.id,
                booked_time=best_slot.time_slot,
                resy_token=resy_token,
            )

            # Add to history
            self.db.add_history(
                restaurant_name=restaurant.name,
                venue_id=restaurant.venue_id,
                target_date=monitor.target_date,
                party_size=monitor.party_size,
                success=True,
                booked_time=best_slot.time_slot,
                resy_token=resy_token,
            )

            logger.info(
                f"SUCCESS! Booked {restaurant.name} on {date_str} at "
                f"{best_slot.time_slot} via cancellation monitor!"
            )

        except Exception as e:
            logger.error(f"Failed to book cancellation slot: {e}")
            # Don't mark as failed - slot might have been taken, keep monitoring

    def start_monitor_polling(self) -> None:
        """Start the periodic monitor polling job."""
        job_id = "monitor_poll"

        # Remove if exists
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        # Schedule periodic polling
        self.scheduler.add_job(
            self._poll_monitors,
            trigger=IntervalTrigger(minutes=MONITOR_POLL_INTERVAL_MINUTES),
            id=job_id,
            name="Cancellation monitor polling",
            replace_existing=True,
        )
        logger.info(f"Started monitor polling every {MONITOR_POLL_INTERVAL_MINUTES} minutes")

    def stop_monitor_polling(self) -> None:
        """Stop the monitor polling job."""
        job_id = "monitor_poll"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            logger.info("Stopped monitor polling")


def run_daemon_standalone(db: Database) -> None:
    """Run the daemon as a standalone process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    daemon = SchedulerDaemon(db)

    def signal_handler(signum, frame):
        logger.info("Received shutdown signal")
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    daemon.start()

    print("Resy Bot Daemon running. Press Ctrl+C to stop.")
    print(f"Scheduled jobs: {len(daemon.get_scheduled_jobs())}")

    # Keep the main thread alive
    try:
        while True:
            import time
            time.sleep(60)

            # Periodically check for new jobs
            daemon.load_pending_jobs()
    except KeyboardInterrupt:
        daemon.stop()
