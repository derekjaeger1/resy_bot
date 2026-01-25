"""Pydantic models for Resy API requests and responses."""

from datetime import date, time, datetime
from typing import Optional
from pydantic import BaseModel, Field


class ResyCredentials(BaseModel):
    """Credentials for Resy API authentication."""
    api_key: str
    auth_token: str
    payment_method_id: Optional[int] = None


class Slot(BaseModel):
    """Represents an available reservation slot."""
    config_id: str = Field(alias="config_id")
    token: str = Field(alias="token")
    date: str
    time_slot: str = Field(alias="time_slot")
    party_size: int
    table_type: Optional[str] = None
    venue_id: int

    class Config:
        populate_by_name = True


class ReservationDetails(BaseModel):
    """Details needed to complete a booking."""
    book_token: str
    payment_method_id: Optional[int] = None


class VenueInfo(BaseModel):
    """Basic venue information."""
    venue_id: int
    name: str
    address: Optional[str] = None
    locality: Optional[str] = None  # city


class FindResponse(BaseModel):
    """Response from the /find endpoint."""
    slots: list[Slot] = []


class BookRequest(BaseModel):
    """Request payload for booking a reservation."""
    book_token: str
    struct_payment_method: Optional[str] = None
    source_id: str = "resy.com-venue-details"


class BookResponse(BaseModel):
    """Response from the /book endpoint."""
    resy_token: str
    reservation_id: Optional[int] = None


class TimePreference(BaseModel):
    """A preferred time slot with optional table type."""
    time: str  # HH:MM format (24-hour)
    table_type: Optional[str] = None  # e.g., "Dining Room", "Patio"
    window_minutes: int = 60  # How many minutes before/after to consider (default ±1 hour)

    def to_time(self) -> time:
        """Convert string time to time object."""
        parts = self.time.split(":")
        return time(int(parts[0]), int(parts[1]))

    def to_minutes(self) -> int:
        """Convert time to minutes since midnight for easier comparison."""
        t = self.to_time()
        return t.hour * 60 + t.minute

    def matches_table_type(self, slot: Slot) -> bool:
        """Check if table type matches."""
        if self.table_type and slot.table_type:
            if self.table_type.lower() not in slot.table_type.lower():
                return False
        return True

    def time_difference_minutes(self, slot: Slot) -> Optional[int]:
        """
        Calculate the absolute difference in minutes between preference and slot.
        Returns None if slot time can't be parsed.
        """
        try:
            # Parse slot time (format: "2024-01-15 19:00:00" or "19:00:00" or "7:00 PM")
            slot_time_str = slot.time_slot
            if " " in slot_time_str and "-" in slot_time_str.split(" ")[0]:
                # Format: "2024-01-15 19:00:00"
                slot_time_str = slot_time_str.split(" ")[1]

            # Handle HH:MM:SS or HH:MM
            parts = slot_time_str.split(":")
            slot_minutes = int(parts[0]) * 60 + int(parts[1])

            pref_minutes = self.to_minutes()
            return abs(slot_minutes - pref_minutes)
        except Exception:
            return None

    def is_within_window(self, slot: Slot) -> bool:
        """Check if slot time is within the preferred time window."""
        diff = self.time_difference_minutes(slot)
        if diff is None:
            return False
        return diff <= self.window_minutes

    def matches(self, slot: Slot) -> bool:
        """Check if this preference matches a slot (time window + table type)."""
        return self.is_within_window(slot) and self.matches_table_type(slot)


class ReservationRequest(BaseModel):
    """Configuration for a reservation attempt."""
    venue_id: int
    party_size: int
    target_date: date
    preferred_times: list[TimePreference] = []

    def get_date_string(self) -> str:
        """Get date in YYYY-MM-DD format."""
        return self.target_date.strftime("%Y-%m-%d")
