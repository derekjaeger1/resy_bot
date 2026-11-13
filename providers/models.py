"""Normalized models shared across reservation providers."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ProviderSlot:
    provider: str
    restaurant_id: str
    date_time: datetime
    party_size: int
    table_type: Optional[str] = None
    dining_area_id: Optional[str] = None
    dining_area_name: Optional[str] = None
    environment: Optional[str] = None
    experience_id: Optional[str] = None
    requires_payment: bool = False
    cancellation_policy: Optional[dict[str, Any]] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderAvailability:
    provider: str
    restaurant_id: str
    slots: list[ProviderSlot] = field(default_factory=list)
    no_availability_reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlotLock:
    provider: str
    restaurant_id: str
    reservation_token: str
    expires_at: Optional[datetime] = None
    payment_method: Optional[dict[str, Any]] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BookingResult:
    provider: str
    restaurant_id: str
    confirmation_number: Optional[str]
    date_time: Optional[datetime]
    party_size: Optional[int]
    manage_reservation_url: Optional[str] = None
    cancel_cutoff_date_utc: Optional[datetime] = None
    message: Optional[str] = None
    payment: Optional[dict[str, Any]] = None
    raw: dict[str, Any] = field(default_factory=dict)
