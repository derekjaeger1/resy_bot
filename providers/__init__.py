"""Reservation provider abstractions.

The app UI and watch engine should consume normalized provider models rather
than provider-specific Resy/OpenTable payloads.
"""

from .models import (
    BookingResult,
    ProviderAvailability,
    ProviderSlot,
    SlotLock,
)

__all__ = [
    "BookingResult",
    "ProviderAvailability",
    "ProviderSlot",
    "SlotLock",
]
