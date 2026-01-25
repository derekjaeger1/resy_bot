"""Database operations for the Resy Bot."""

import json
from datetime import datetime, date, time, timedelta
from typing import Optional
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Base,
    Credentials,
    Restaurant,
    ScheduledReservation,
    ReservationHistory,
    MonitoredReservation,
    SnipeLog,
    ReleasePattern,
    ReservationStatus,
    MonitorStatus,
    SnipeLogType,
)
from config import DATABASE_PATH


class Database:
    """Database manager for the Resy Bot."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DATABASE_PATH)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Create tables if they don't exist
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Session:
        """Get a new database session."""
        return self.SessionLocal()

    # ==================== Credentials ====================

    def get_credentials(self) -> Optional[Credentials]:
        """Get stored credentials."""
        with self.get_session() as session:
            return session.query(Credentials).first()

    def save_credentials(
        self,
        api_key: str,
        auth_token: str,
        payment_method_id: Optional[int] = None,
        email: Optional[str] = None,
    ) -> Credentials:
        """Save or update credentials."""
        with self.get_session() as session:
            creds = session.query(Credentials).first()
            if creds:
                creds.api_key = api_key
                creds.auth_token = auth_token
                creds.payment_method_id = payment_method_id
                creds.email = email
            else:
                creds = Credentials(
                    api_key=api_key,
                    auth_token=auth_token,
                    payment_method_id=payment_method_id,
                    email=email,
                )
                session.add(creds)
            session.commit()
            session.refresh(creds)
            return creds

    def delete_credentials(self) -> None:
        """Delete stored credentials."""
        with self.get_session() as session:
            session.query(Credentials).delete()
            session.commit()

    # ==================== Restaurants ====================

    def get_restaurants(self) -> list[Restaurant]:
        """Get all restaurants."""
        with self.get_session() as session:
            return session.query(Restaurant).all()

    def get_restaurant(self, restaurant_id: int) -> Optional[Restaurant]:
        """Get a restaurant by ID."""
        with self.get_session() as session:
            return session.query(Restaurant).filter_by(id=restaurant_id).first()

    def get_restaurant_by_venue_id(self, venue_id: int) -> Optional[Restaurant]:
        """Get a restaurant by venue ID."""
        with self.get_session() as session:
            return session.query(Restaurant).filter_by(venue_id=venue_id).first()

    def add_restaurant(
        self,
        venue_id: int,
        name: str,
        release_time: time,
        days_in_advance: int = 14,
        release_pattern: ReleasePattern = ReleasePattern.DAILY,
        release_day_of_week: Optional[int] = None,
        address: Optional[str] = None,
        city: Optional[str] = None,
    ) -> Restaurant:
        """Add a new restaurant."""
        with self.get_session() as session:
            restaurant = Restaurant(
                venue_id=venue_id,
                name=name,
                release_time=release_time,
                days_in_advance=days_in_advance,
                release_pattern=release_pattern,
                release_day_of_week=release_day_of_week,
                address=address,
                city=city,
            )
            session.add(restaurant)
            session.commit()
            session.refresh(restaurant)
            return restaurant

    def update_restaurant(
        self,
        restaurant_id: int,
        **kwargs,
    ) -> Optional[Restaurant]:
        """Update a restaurant."""
        with self.get_session() as session:
            restaurant = session.query(Restaurant).filter_by(id=restaurant_id).first()
            if restaurant:
                for key, value in kwargs.items():
                    if hasattr(restaurant, key):
                        setattr(restaurant, key, value)
                session.commit()
                session.refresh(restaurant)
            return restaurant

    def delete_restaurant(self, restaurant_id: int) -> bool:
        """Delete a restaurant."""
        with self.get_session() as session:
            restaurant = session.query(Restaurant).filter_by(id=restaurant_id).first()
            if restaurant:
                session.delete(restaurant)
                session.commit()
                return True
            return False

    # ==================== Scheduled Reservations ====================

    def get_scheduled_reservations(
        self,
        status: Optional[ReservationStatus] = None,
    ) -> list[ScheduledReservation]:
        """Get scheduled reservations, optionally filtered by status."""
        with self.get_session() as session:
            query = session.query(ScheduledReservation)
            if status:
                query = query.filter_by(status=status)
            return query.order_by(ScheduledReservation.snipe_at).all()

    def get_pending_reservations(self) -> list[ScheduledReservation]:
        """Get all pending reservations."""
        return self.get_scheduled_reservations(status=ReservationStatus.PENDING)

    def get_upcoming_snipes(self, hours_ahead: int = 24) -> list[ScheduledReservation]:
        """Get reservations that will snipe in the next N hours."""
        cutoff = datetime.now() + timedelta(hours=hours_ahead)
        with self.get_session() as session:
            return (
                session.query(ScheduledReservation)
                .filter(
                    ScheduledReservation.status == ReservationStatus.PENDING,
                    ScheduledReservation.snipe_at <= cutoff,
                )
                .order_by(ScheduledReservation.snipe_at)
                .all()
            )

    def schedule_reservation(
        self,
        restaurant_id: int,
        target_date: date,
        party_size: int,
        snipe_at: datetime,
        preferred_times: Optional[list[dict]] = None,
    ) -> ScheduledReservation:
        """Schedule a new reservation attempt."""
        with self.get_session() as session:
            reservation = ScheduledReservation(
                restaurant_id=restaurant_id,
                target_date=target_date,
                party_size=party_size,
                snipe_at=snipe_at,
                preferred_times=json.dumps(preferred_times) if preferred_times else None,
                status=ReservationStatus.PENDING,
            )
            session.add(reservation)
            session.commit()
            session.refresh(reservation)
            return reservation

    def update_reservation_status(
        self,
        reservation_id: int,
        status: ReservationStatus,
        result_message: Optional[str] = None,
        booked_time: Optional[str] = None,
        resy_token: Optional[str] = None,
    ) -> Optional[ScheduledReservation]:
        """Update a reservation's status."""
        with self.get_session() as session:
            reservation = (
                session.query(ScheduledReservation)
                .filter_by(id=reservation_id)
                .first()
            )
            if reservation:
                reservation.status = status
                if result_message:
                    reservation.result_message = result_message
                if booked_time:
                    reservation.booked_time = booked_time
                if resy_token:
                    reservation.resy_token = resy_token
                session.commit()
                session.refresh(reservation)
            return reservation

    def update_reservation(
        self,
        reservation_id: int,
        target_date: date = None,
        party_size: int = None,
        snipe_at: datetime = None,
        preferred_times: list[dict] = None,
    ) -> Optional[ScheduledReservation]:
        """Update a pending reservation's details."""
        with self.get_session() as session:
            reservation = (
                session.query(ScheduledReservation)
                .filter_by(id=reservation_id)
                .first()
            )
            if reservation and reservation.status == ReservationStatus.PENDING:
                if target_date is not None:
                    reservation.target_date = target_date
                if party_size is not None:
                    reservation.party_size = party_size
                if snipe_at is not None:
                    reservation.snipe_at = snipe_at
                if preferred_times is not None:
                    reservation.preferred_times = json.dumps(preferred_times)
                session.commit()
                session.refresh(reservation)
            return reservation

    def delete_reservation(self, reservation_id: int) -> bool:
        """Delete a scheduled reservation."""
        with self.get_session() as session:
            reservation = (
                session.query(ScheduledReservation)
                .filter_by(id=reservation_id)
                .first()
            )
            if reservation:
                session.delete(reservation)
                session.commit()
                return True
            return False

    # ==================== Reservation History ====================

    def add_history(
        self,
        restaurant_name: str,
        venue_id: int,
        target_date: date,
        party_size: int,
        success: bool,
        booked_time: Optional[str] = None,
        resy_token: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> ReservationHistory:
        """Add a reservation attempt to history."""
        with self.get_session() as session:
            history = ReservationHistory(
                restaurant_name=restaurant_name,
                venue_id=venue_id,
                target_date=target_date,
                party_size=party_size,
                attempted_at=datetime.now(),
                success=success,
                booked_time=booked_time,
                resy_token=resy_token,
                error_message=error_message,
            )
            session.add(history)
            session.commit()
            session.refresh(history)
            return history

    def get_history(self, limit: int = 50) -> list[ReservationHistory]:
        """Get recent reservation history."""
        with self.get_session() as session:
            return (
                session.query(ReservationHistory)
                .order_by(ReservationHistory.attempted_at.desc())
                .limit(limit)
                .all()
            )

    def calculate_snipe_time(
        self,
        restaurant: Restaurant,
        target_date: date,
    ) -> datetime:
        """
        Calculate when to attempt the snipe based on restaurant's release pattern.

        For daily releases: snipe_time = target_date - days_in_advance + release_time
        For weekly releases: more complex calculation based on day of week
        """
        snipe_date = target_date - timedelta(days=restaurant.days_in_advance)

        if restaurant.release_pattern == ReleasePattern.WEEKLY:
            # Find the right day of the week
            if restaurant.release_day_of_week is not None:
                days_until_release = (
                    restaurant.release_day_of_week - snipe_date.weekday()
                ) % 7
                if days_until_release == 0 and datetime.now().date() > snipe_date:
                    days_until_release = 7
                snipe_date = snipe_date + timedelta(days=days_until_release)

        return datetime.combine(snipe_date, restaurant.release_time)

    # ==================== Cancellation Monitors ====================

    def get_active_monitors(self) -> list[MonitoredReservation]:
        """Get all active cancellation monitors."""
        with self.get_session() as session:
            return (
                session.query(MonitoredReservation)
                .filter_by(status=MonitorStatus.ACTIVE)
                .all()
            )

    def get_monitors(self, status: MonitorStatus = None) -> list[MonitoredReservation]:
        """Get monitors, optionally filtered by status."""
        with self.get_session() as session:
            query = session.query(MonitoredReservation)
            if status:
                query = query.filter_by(status=status)
            return query.order_by(MonitoredReservation.created_at.desc()).all()

    def add_monitor(
        self,
        restaurant_id: int,
        target_date: date,
        party_size: int,
        preferred_time: str = "19:00",
        time_window: int = 120,
        table_type: str = None,
    ) -> MonitoredReservation:
        """Add a new cancellation monitor."""
        with self.get_session() as session:
            monitor = MonitoredReservation(
                restaurant_id=restaurant_id,
                target_date=target_date,
                party_size=party_size,
                preferred_time=preferred_time,
                time_window=time_window,
                table_type=table_type,
                status=MonitorStatus.ACTIVE,
            )
            session.add(monitor)
            session.commit()
            session.refresh(monitor)
            return monitor

    def update_monitor_checked(self, monitor_id: int) -> None:
        """Update the last_checked timestamp and increment check_count."""
        with self.get_session() as session:
            monitor = (
                session.query(MonitoredReservation)
                .filter_by(id=monitor_id)
                .first()
            )
            if monitor:
                monitor.last_checked = datetime.now()
                monitor.check_count += 1
                session.commit()

    def update_monitor_success(
        self,
        monitor_id: int,
        booked_time: str,
        resy_token: str = None,
    ) -> None:
        """Mark a monitor as successfully booked."""
        with self.get_session() as session:
            monitor = (
                session.query(MonitoredReservation)
                .filter_by(id=monitor_id)
                .first()
            )
            if monitor:
                monitor.status = MonitorStatus.SUCCESS
                monitor.booked_time = booked_time
                monitor.resy_token = resy_token
                monitor.booked_at = datetime.now()
                session.commit()

    def cancel_monitor(self, monitor_id: int) -> bool:
        """Cancel a monitor."""
        with self.get_session() as session:
            monitor = (
                session.query(MonitoredReservation)
                .filter_by(id=monitor_id)
                .first()
            )
            if monitor and monitor.status == MonitorStatus.ACTIVE:
                monitor.status = MonitorStatus.CANCELLED
                session.commit()
                return True
            return False

    def delete_monitor(self, monitor_id: int) -> bool:
        """Delete a monitor."""
        with self.get_session() as session:
            monitor = (
                session.query(MonitoredReservation)
                .filter_by(id=monitor_id)
                .first()
            )
            if monitor:
                session.delete(monitor)
                session.commit()
                return True
            return False

    def expire_old_monitors(self) -> int:
        """Mark monitors with past target dates as expired. Returns count."""
        with self.get_session() as session:
            today = date.today()
            monitors = (
                session.query(MonitoredReservation)
                .filter_by(status=MonitorStatus.ACTIVE)
                .filter(MonitoredReservation.target_date < today)
                .all()
            )
            for monitor in monitors:
                monitor.status = MonitorStatus.EXPIRED
            session.commit()
            return len(monitors)

    # ==================== Snipe Logs ====================

    def add_snipe_log(
        self,
        restaurant_name: str,
        target_date: date,
        log_type: SnipeLogType,
        message: str,
        reservation_id: int = None,
        details: str = None,
        elapsed_ms: int = None,
    ) -> SnipeLog:
        """Add a snipe log entry."""
        with self.get_session() as session:
            log = SnipeLog(
                reservation_id=reservation_id,
                restaurant_name=restaurant_name,
                target_date=target_date,
                log_type=log_type,
                message=message,
                details=details,
                elapsed_ms=elapsed_ms,
            )
            session.add(log)
            session.commit()
            session.refresh(log)
            return log

    def get_snipe_logs(
        self,
        reservation_id: int = None,
        limit: int = 100,
    ) -> list[SnipeLog]:
        """Get snipe logs, optionally filtered by reservation."""
        with self.get_session() as session:
            query = session.query(SnipeLog)
            if reservation_id:
                query = query.filter_by(reservation_id=reservation_id)
            return (
                query.order_by(SnipeLog.timestamp.desc())
                .limit(limit)
                .all()
            )

    def get_snipe_logs_for_date(
        self,
        target_date: date,
        restaurant_name: str = None,
    ) -> list[SnipeLog]:
        """Get snipe logs for a specific target date."""
        with self.get_session() as session:
            query = session.query(SnipeLog).filter_by(target_date=target_date)
            if restaurant_name:
                query = query.filter_by(restaurant_name=restaurant_name)
            return query.order_by(SnipeLog.timestamp).all()

    def clear_old_snipe_logs(self, days: int = 7) -> int:
        """Delete snipe logs older than N days. Returns count deleted."""
        with self.get_session() as session:
            cutoff = datetime.now() - timedelta(days=days)
            result = (
                session.query(SnipeLog)
                .filter(SnipeLog.timestamp < cutoff)
                .delete()
            )
            session.commit()
            return result
