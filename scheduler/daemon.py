"""Background scheduler daemon for running snipe jobs."""

import logging
import signal
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Optional, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from resy.models import ResyCredentials, TimePreference
from resy.client import ResyClient
from providers.opentable_web import (
    OpenTableChallenge,
    OpenTableUserActionRequired,
    OpenTableWebClient,
    OpenTableWebConfig,
    OpenTableWebError,
)
from storage.database import Database
from storage.models import ScheduledReservation, ReservationStatus, MonitorStatus
from .sniper import ReservationSniper, SnipeResult
from config import (
    SNIPE_EARLY_WAKE_SECONDS,
    MONITOR_POLL_TICK_SECONDS,
    MONITOR_NEAR_INTERVAL_SECONDS,
    MONITOR_WEEK_INTERVAL_SECONDS,
    MONITOR_FAR_INTERVAL_SECONDS,
    OPENTABLE_WEB_ENABLED,
    OPENTABLE_WEB_HEADLESS,
    OPENTABLE_WEB_PROFILE_DIR,
)

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
        # monitor_id -> {checked_at, slots, error}. This is the UI's read-only
        # view of what the watcher already saw; reading it never calls Resy.
        self._availability_cache: dict[int, dict] = {}

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
            # Always recalculate from the restaurant's CURRENT release rule.
            # This prevents stale timestamps from surviving rule corrections,
            # migrations, or long periods while the app was offline.
            restaurant = self.db.get_restaurant(reservation.restaurant_id)
            if restaurant:
                expected_fire = self.db.calculate_snipe_time(
                    restaurant,
                    reservation.target_date,
                )
                if expected_fire != reservation.snipe_at:
                    updated = self.db.update_reservation(
                        reservation.id,
                        snipe_at=expected_fire,
                    )
                    if updated:
                        reservation = updated
                        logger.info(
                            f"Recalculated reservation {reservation.id} fire time "
                            f"to {expected_fire}"
                        )

            # Only schedule if snipe time is in the future.
            if reservation.snipe_at > datetime.now():
                self.schedule_job(reservation)
                count += 1
            else:
                self.db.update_reservation_status(
                    reservation.id,
                    ReservationStatus.FAILED,
                    result_message="Official drop time has already passed; use an active watch instead",
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
        """Poll active monitors and route each one to its reservation provider."""
        # Expire old monitors first.
        expired = self.db.expire_old_monitors()
        if expired:
            logger.info(f"Expired {expired} monitors with past target dates")

        monitors = self.db.get_active_monitors()
        if not monitors:
            return

        logger.info(f"Polling {len(monitors)} active monitors for cancellations")

        now = datetime.now()
        today = now.date()
        credentials = self._get_credentials()

        # A single Resy client is reused for every Resy monitor in this tick.
        # OpenTable uses its own provider transport only for OpenTable rows.
        with ExitStack() as stack:
            resy_client = (
                stack.enter_context(ResyClient(credentials))
                if credentials
                else None
            )

            for monitor in monitors:
                active_ids = {m.id for m in self.db.get_active_monitors()}
                if monitor.id not in active_ids:
                    continue

                days_away = (monitor.target_date - today).days
                if days_away <= 1:
                    min_interval = MONITOR_NEAR_INTERVAL_SECONDS
                elif days_away <= 7:
                    min_interval = MONITOR_WEEK_INTERVAL_SECONDS
                else:
                    min_interval = MONITOR_FAR_INTERVAL_SECONDS

                if monitor.last_checked:
                    elapsed = (now - monitor.last_checked).total_seconds()
                    if elapsed < min_interval:
                        continue

                restaurant = self.db.get_restaurant(monitor.restaurant_id)
                if not restaurant:
                    logger.warning(f"Monitor {monitor.id} has invalid restaurant_id")
                    continue

                provider = (getattr(restaurant, "provider", None) or "resy").lower()

                try:
                    if provider == "opentable":
                        self._check_opentable_monitor(monitor, restaurant)
                    elif provider == "resy":
                        if not resy_client:
                            self._cache_monitor_availability(
                                monitor, [], error="Resy account is not connected."
                            )
                            continue
                        self._check_monitor(resy_client, monitor)
                    else:
                        self._cache_monitor_availability(
                            monitor, [], error=f"Unsupported provider: {provider}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error checking {provider} monitor {monitor.id}: {e}"
                    )

    @staticmethod
    def _slot_minutes(slot) -> int | None:
        """Parse Resy or normalized provider slot time into minutes after midnight."""
        if getattr(slot, "date_time", None) is not None:
            dt = slot.date_time
            return dt.hour * 60 + dt.minute

        raw = str(getattr(slot, "time_slot", "") or "").strip()
        try:
            if " " in raw and "-" in raw.split(" ")[0]:
                raw = raw.split(" ", 1)[1]
            upper = raw.upper()
            if upper.endswith(" AM") or upper.endswith(" PM"):
                parsed = datetime.strptime(raw, "%I:%M %p")
                return parsed.hour * 60 + parsed.minute
            parts = raw.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return None

    def _cache_monitor_availability(
        self,
        monitor,
        matching_slots: list,
        error: str | None = None,
    ) -> None:
        """Cache the result of the actual watcher poll for the dashboard."""
        cached_slots = []
        for slot in matching_slots:
            raw_time = getattr(slot, "time_slot", None)
            if raw_time is None and getattr(slot, "date_time", None) is not None:
                raw_time = slot.date_time.strftime("%H:%M")
            cached_slots.append({
                "date": monitor.target_date.isoformat(),
                "time": str(raw_time or ""),
                "type": (
                    getattr(slot, "type", None)
                    or getattr(slot, "table_type", None)
                    or getattr(slot, "dining_area_name", None)
                ),
            })

        self._availability_cache[monitor.id] = {
            "checked_at": datetime.now(),
            "error": error,
            "slots": cached_slots,
        }

    def _finalize_monitor_success(
        self,
        monitor,
        restaurant,
        *,
        booked_time: str,
        booking_token: str = "",
        provider: str = "resy",
        provider_confirmation: str | None = None,
        manage_reservation_url: str | None = None,
    ) -> None:
        """Mark the winning monitor successful and cancel sibling attempts."""
        self.db.update_monitor_success(
            monitor.id,
            booked_time=booked_time,
            resy_token=booking_token,
        )

        group_rows = [monitor]
        if getattr(monitor, "watch_group_id", None):
            group_rows = [
                m for m in self.db.get_monitors()
                if m.watch_group_id == monitor.watch_group_id
            ]
            for sibling in group_rows:
                if (
                    sibling.id != monitor.id
                    and sibling.status == MonitorStatus.ACTIVE
                ):
                    self.db.cancel_monitor(sibling.id)

        group_dates = {m.target_date for m in group_rows}
        for reservation in self.db.get_pending_reservations():
            if (
                reservation.restaurant_id == monitor.restaurant_id
                and reservation.party_size == monitor.party_size
                and reservation.target_date in group_dates
            ):
                self.db.update_reservation_status(
                    reservation.id,
                    ReservationStatus.CANCELLED,
                    result_message=(
                        "Cancelled automatically because this watch already booked"
                    ),
                )
                self.cancel_job(reservation.id)

        self.db.add_history(
            restaurant_name=restaurant.name,
            venue_id=restaurant.venue_id,
            target_date=monitor.target_date,
            party_size=monitor.party_size,
            success=True,
            booked_time=booked_time,
            resy_token=booking_token or None,
            provider=provider,
            provider_confirmation=provider_confirmation,
            manage_reservation_url=manage_reservation_url,
        )

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
            self._cache_monitor_availability(monitor, [], error=str(e))
            logger.debug(f"No availability for {restaurant.name} on {date_str}: {e}")
            return

        if not slots:
            self._cache_monitor_availability(monitor, [])
            logger.debug(f"No slots for {restaurant.name} on {date_str}")
            return

        # Match against the user's exact stored range when available.
        # Legacy rows fall back to center ± window behavior.
        time_pref = TimePreference(
            time=monitor.preferred_time,
            table_type=monitor.table_type,
            window_minutes=monitor.time_window,
        )

        if monitor.time_start and monitor.time_end:
            try:
                start_dt = datetime.strptime(monitor.time_start, "%H:%M")
                end_dt = datetime.strptime(monitor.time_end, "%H:%M")
                start_minutes = start_dt.hour * 60 + start_dt.minute
                end_minutes = end_dt.hour * 60 + end_dt.minute
            except Exception:
                start_minutes = None
                end_minutes = None

            matching_slots = []
            if start_minutes is not None and end_minutes is not None:
                for slot in slots:
                    slot_minutes = self._slot_minutes(slot)
                    if slot_minutes is None:
                        continue
                    if (
                        start_minutes <= slot_minutes <= end_minutes
                        and time_pref.matches_table_type(slot)
                    ):
                        matching_slots.append(slot)
            else:
                matching_slots = [s for s in slots if time_pref.matches(s)]
        else:
            matching_slots = [s for s in slots if time_pref.matches(s)]

        # Cache exactly what qualifies for this watch. The dashboard reads
        # this cache; it never performs its own Resy availability search.
        self._cache_monitor_availability(monitor, matching_slots)

        if not matching_slots:
            logger.debug(
                f"Found {len(slots)} slots for {restaurant.name} but none match watch "
                f"(range={monitor.time_start or monitor.preferred_time}-"
                f"{monitor.time_end or monitor.preferred_time})"
            )
            return

        # First qualifying table wins. Prefer the earliest slot in the allowed range.
        matching_slots.sort(key=lambda s: self._slot_minutes(s) or 9999)
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

            self._finalize_monitor_success(
                monitor,
                restaurant,
                booked_time=str(best_slot.time_slot),
                booking_token=resy_token,
                provider="resy",
            )

            logger.info(
                f"SUCCESS! Booked {restaurant.name} on {date_str} at "
                f"{best_slot.time_slot} via cancellation monitor!"
            )

        except Exception as e:
            logger.error(f"Failed to book cancellation slot: {e}")
            # Don't mark as failed - slot might have been taken, keep monitoring

    def _check_opentable_monitor(self, monitor, restaurant) -> None:
        """Check and auto-book an ordinary OpenTable table via consumer web flow."""
        if not OPENTABLE_WEB_ENABLED:
            self._cache_monitor_availability(
                monitor, [], error="OpenTable internal transport is disabled."
            )
            return

        profile = self.db.get_diner_profile()
        if not profile:
            self._cache_monitor_availability(
                monitor,
                [],
                error="Add diner details in Settings before OpenTable can auto-book.",
            )
            return

        rid = getattr(restaurant, "provider_venue_id", None)
        if not rid:
            self._cache_monitor_availability(
                monitor, [], error="OpenTable restaurant ID is missing."
            )
            return

        self.db.update_monitor_checked(monitor.id)
        date_str = monitor.target_date.strftime("%Y-%m-%d")

        client = OpenTableWebClient(
            OpenTableWebConfig(
                profile_dir=OPENTABLE_WEB_PROFILE_DIR,
                headless=OPENTABLE_WEB_HEADLESS,
            )
        )

        try:
            availability = client.get_availability(
                rid,
                day=date_str,
                party_size=monitor.party_size,
                pivot_time=monitor.preferred_time,
            )
        except (OpenTableChallenge, OpenTableUserActionRequired) as exc:
            self._cache_monitor_availability(monitor, [], error=str(exc))
            return
        except OpenTableWebError as exc:
            self._cache_monitor_availability(monitor, [], error=str(exc))
            return

        start_minutes = None
        end_minutes = None
        try:
            if monitor.time_start and monitor.time_end:
                start_dt = datetime.strptime(monitor.time_start, "%H:%M")
                end_dt = datetime.strptime(monitor.time_end, "%H:%M")
                start_minutes = start_dt.hour * 60 + start_dt.minute
                end_minutes = end_dt.hour * 60 + end_dt.minute
        except Exception:
            pass

        matching_slots = []
        for slot in availability.slots:
            slot_minutes = self._slot_minutes(slot)
            if slot_minutes is None:
                continue
            if start_minutes is not None and end_minutes is not None:
                if not (start_minutes <= slot_minutes <= end_minutes):
                    continue
            matching_slots.append(slot)

        self._cache_monitor_availability(monitor, matching_slots)
        if not matching_slots:
            return

        matching_slots.sort(key=lambda slot: slot.date_time)
        best_slot = matching_slots[0]
        time_24h = best_slot.date_time.strftime("%H:%M")

        logger.info(
            f"MATCH FOUND! {restaurant.name} on {date_str} at {time_24h} "
            f"via OpenTable - attempting to book..."
        )

        try:
            result = client.book_visible_slot(
                rid,
                day=date_str,
                time_24h=time_24h,
                party_size=monitor.party_size,
                first_name=profile.first_name,
                last_name=profile.last_name,
                email=profile.email,
                phone=profile.phone,
            )
        except OpenTableUserActionRequired as exc:
            self._cache_monitor_availability(
                monitor,
                matching_slots,
                error=(
                    f"OpenTable found {time_24h}, but needs user action: {exc}"
                ),
            )
            return
        except OpenTableChallenge as exc:
            self._cache_monitor_availability(
                monitor,
                matching_slots,
                error=f"OpenTable verification required: {exc}",
            )
            return
        except Exception as exc:
            logger.error(f"OpenTable booking failed for monitor {monitor.id}: {exc}")
            self._cache_monitor_availability(
                monitor,
                matching_slots,
                error=f"OpenTable booking attempt failed: {exc}",
            )
            return

        confirmation = result.confirmation_number or ""
        token = f"opentable:{confirmation}" if confirmation else "opentable:confirmed"
        self._finalize_monitor_success(
            monitor,
            restaurant,
            booked_time=time_24h,
            booking_token=token,
            provider="opentable",
            provider_confirmation=confirmation or None,
            manage_reservation_url=result.manage_reservation_url,
        )
        logger.info(
            f"SUCCESS! Booked {restaurant.name} on {date_str} at {time_24h} "
            f"via OpenTable."
        )

    def get_watch_group_availability(self, group_id: str) -> dict:
        """Return cached results from watcher polls; never makes a Resy request."""
        monitors = [
            m for m in self.db.get_active_monitors()
            if getattr(m, "watch_group_id", None) == group_id
        ]
        if not monitors:
            return {"slots": [], "checked_at": None, "errors": []}

        slots = []
        checked = []
        errors = []
        for monitor in monitors:
            cached = self._availability_cache.get(monitor.id)
            if not cached:
                continue
            if cached.get("checked_at"):
                checked.append(cached["checked_at"])
            if cached.get("error"):
                errors.append(cached["error"])
            slots.extend(cached.get("slots") or [])

        # Remove duplicate slot rows while preserving date/time/type.
        unique = {}
        for slot in slots:
            key = (slot.get("date"), slot.get("time"), slot.get("type"))
            unique[key] = slot

        feed = sorted(
            unique.values(),
            key=lambda item: (item.get("date") or "", item.get("time") or ""),
        )
        return {
            "slots": feed,
            "checked_at": max(checked) if checked else None,
            "errors": errors,
        }

    def check_watch_group_now(self, group_id: str) -> dict:
        """Synchronously check/book a newly-created watch before returning UI control."""
        monitors = [
            m for m in self.db.get_active_monitors()
            if getattr(m, "watch_group_id", None) == group_id
        ]
        monitors.sort(key=lambda m: m.target_date)
        if not monitors:
            return {"status": "error", "message": "Watch could not be loaded."}

        restaurant = self.db.get_restaurant(monitors[0].restaurant_id)
        if not restaurant:
            return {"status": "error", "message": "Restaurant could not be loaded."}
        provider = (getattr(restaurant, "provider", None) or "resy").lower()

        if provider == "opentable":
            for monitor in monitors:
                self._check_opentable_monitor(monitor, restaurant)
                refreshed = next(
                    (m for m in self.db.get_monitors() if m.id == monitor.id),
                    None,
                )
                if refreshed and refreshed.status == MonitorStatus.SUCCESS:
                    return {
                        "status": "booked",
                        "date": refreshed.target_date.isoformat(),
                        "time": refreshed.booked_time,
                        "provider": "opentable",
                    }
            return {"status": "watching", "provider": "opentable"}

        credentials = self._get_credentials()
        if not credentials:
            return {"status": "error", "message": "Resy is not connected."}

        with ResyClient(credentials) as client:
            for monitor in monitors:
                self._check_monitor(client, monitor)
                refreshed = next(
                    (m for m in self.db.get_monitors() if m.id == monitor.id),
                    None,
                )
                if refreshed and refreshed.status == MonitorStatus.SUCCESS:
                    return {
                        "status": "booked",
                        "date": refreshed.target_date.isoformat(),
                        "time": refreshed.booked_time,
                        "provider": "resy",
                    }

        return {"status": "watching", "provider": "resy"}

    def poll_monitors_now(self) -> None:
        """Queue an immediate monitor check without blocking the web request."""
        if not self._running:
            return
        job_id = f"monitor_now_{int(datetime.now().timestamp() * 1000)}"
        self.scheduler.add_job(
            self._poll_monitors,
            trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=1)),
            id=job_id,
            name="Immediate cancellation watch check",
            replace_existing=False,
        )

    def start_monitor_polling(self) -> None:
        """Start the periodic monitor polling job."""
        job_id = "monitor_poll"

        # Remove if exists
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        # Schedule periodic polling
        self.scheduler.add_job(
            self._poll_monitors,
            trigger=IntervalTrigger(seconds=MONITOR_POLL_TICK_SECONDS),
            id=job_id,
            name="Cancellation monitor polling",
            replace_existing=True,
        )
        logger.info(
            f"Started adaptive monitor polling tick every {MONITOR_POLL_TICK_SECONDS}s "
            f"(near={MONITOR_NEAR_INTERVAL_SECONDS}s, week={MONITOR_WEEK_INTERVAL_SECONDS}s, "
            f"far={MONITOR_FAR_INTERVAL_SECONDS}s)"
        )

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
