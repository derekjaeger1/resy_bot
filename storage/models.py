"""SQLAlchemy models for database tables."""

from datetime import datetime, date, time
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Date,
    Time,
    Boolean,
    Text,
    ForeignKey,
    Enum as SQLEnum,
)
from sqlalchemy.orm import DeclarativeBase, relationship
import enum


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class ReleasePattern(enum.Enum):
    """Pattern for when reservations are released."""
    DAILY = "daily"  # Released every day at a specific time
    WEEKLY = "weekly"  # Released on a specific day of the week


class ReservationStatus(enum.Enum):
    """Status of a scheduled reservation attempt."""
    PENDING = "pending"  # Waiting to be executed
    IN_PROGRESS = "in_progress"  # Currently attempting to book
    SUCCESS = "success"  # Successfully booked
    FAILED = "failed"  # Failed to book
    CANCELLED = "cancelled"  # User cancelled


class Credentials(Base):
    """Store Resy API credentials."""
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    api_key = Column(String(500), nullable=False)
    auth_token = Column(String(500), nullable=False)
    payment_method_id = Column(Integer, nullable=True)
    email = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Restaurant(Base):
    """Store restaurant/venue information."""
    __tablename__ = "restaurants"

    id = Column(Integer, primary_key=True)
    venue_id = Column(Integer, unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    address = Column(String(500), nullable=True)
    city = Column(String(100), nullable=True)

    # Release schedule
    release_pattern = Column(
        SQLEnum(ReleasePattern),
        default=ReleasePattern.DAILY,
        nullable=False,
    )
    release_time = Column(Time, nullable=False)  # Time when reservations drop
    days_in_advance = Column(Integer, default=14)  # How many days out reservations open
    release_day_of_week = Column(Integer, nullable=True)  # 0=Monday, 6=Sunday (for weekly)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    scheduled_reservations = relationship(
        "ScheduledReservation",
        back_populates="restaurant",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Restaurant {self.name} (venue_id={self.venue_id})>"


class ScheduledReservation(Base):
    """A scheduled reservation attempt."""
    __tablename__ = "scheduled_reservations"

    id = Column(Integer, primary_key=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id"), nullable=False)
    target_date = Column(Date, nullable=False)  # Date we want the reservation for
    party_size = Column(Integer, nullable=False, default=2)

    # Preferred times as JSON string (list of TimePreference)
    preferred_times = Column(Text, nullable=True)  # JSON: [{"time": "19:00", "table_type": "Dining Room"}]

    # Scheduling
    snipe_at = Column(DateTime, nullable=False)  # When to attempt the snipe
    status = Column(
        SQLEnum(ReservationStatus),
        default=ReservationStatus.PENDING,
        nullable=False,
    )

    # Result
    result_message = Column(Text, nullable=True)
    booked_time = Column(String(50), nullable=True)  # The time slot that was booked
    resy_token = Column(String(255), nullable=True)  # Confirmation token from Resy

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    restaurant = relationship("Restaurant", back_populates="scheduled_reservations")

    def __repr__(self) -> str:
        return f"<ScheduledReservation {self.target_date} @ {self.snipe_at}>"


class ReservationHistory(Base):
    """Log of all reservation attempts."""
    __tablename__ = "reservation_history"

    id = Column(Integer, primary_key=True)
    restaurant_name = Column(String(255), nullable=False)
    venue_id = Column(Integer, nullable=False)
    target_date = Column(Date, nullable=False)
    party_size = Column(Integer, nullable=False)
    attempted_at = Column(DateTime, nullable=False)
    success = Column(Boolean, nullable=False)
    booked_time = Column(String(50), nullable=True)
    resy_token = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"<ReservationHistory {self.restaurant_name} {self.target_date} - {status}>"


class MonitorStatus(enum.Enum):
    """Status of a cancellation monitor."""
    ACTIVE = "active"  # Actively polling for cancellations
    SUCCESS = "success"  # Found and booked a slot
    EXPIRED = "expired"  # Target date passed without booking
    CANCELLED = "cancelled"  # User cancelled the monitor


class MonitoredReservation(Base):
    """A restaurant/date to monitor for cancellations."""
    __tablename__ = "monitored_reservations"

    id = Column(Integer, primary_key=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id"), nullable=False)
    target_date = Column(Date, nullable=False)  # Date we want the reservation for
    party_size = Column(Integer, nullable=False, default=2)

    # Time preferences
    preferred_time = Column(String(10), nullable=False, default="19:00")  # HH:MM format
    time_window = Column(Integer, nullable=False, default=120)  # Minutes before/after preferred time
    table_type = Column(String(100), nullable=True)  # Optional table type filter

    # Status
    status = Column(
        SQLEnum(MonitorStatus),
        default=MonitorStatus.ACTIVE,
        nullable=False,
    )

    # Polling info
    last_checked = Column(DateTime, nullable=True)  # Last time we polled for this
    check_count = Column(Integer, default=0)  # How many times we've checked

    # Result (if successful)
    booked_time = Column(String(50), nullable=True)
    resy_token = Column(String(255), nullable=True)
    booked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    restaurant = relationship("Restaurant")

    def __repr__(self) -> str:
        return f"<MonitoredReservation {self.target_date} status={self.status.value}>"


class SnipeLogType(enum.Enum):
    """Type of snipe log entry."""
    INFO = "info"
    API_CALL = "api_call"
    API_RESPONSE = "api_response"
    API_ERROR = "api_error"
    SLOT_FOUND = "slot_found"
    BOOKING_ATTEMPT = "booking_attempt"
    SUCCESS = "success"
    FAILURE = "failure"


class SnipeLog(Base):
    """Detailed log of snipe attempts for debugging."""
    __tablename__ = "snipe_logs"

    id = Column(Integer, primary_key=True)
    reservation_id = Column(Integer, ForeignKey("scheduled_reservations.id"), nullable=True)
    restaurant_name = Column(String(255), nullable=False)
    target_date = Column(Date, nullable=False)

    # Log entry details
    log_type = Column(SQLEnum(SnipeLogType), nullable=False)
    message = Column(Text, nullable=False)
    details = Column(Text, nullable=True)  # JSON string with extra data

    # Timing
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    elapsed_ms = Column(Integer, nullable=True)  # Milliseconds since snipe started

    def __repr__(self) -> str:
        return f"<SnipeLog {self.log_type.value}: {self.message[:50]}>"
