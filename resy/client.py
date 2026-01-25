"""Resy API client for making reservation requests."""

import httpx
from typing import Optional
from datetime import datetime, time

from .models import (
    ResyCredentials,
    Slot,
    ReservationDetails,
    ReservationRequest,
    TimePreference,
)
from config import RESY_API_BASE, REQUEST_TIMEOUT_SECONDS, USER_AGENT, RESY_API_KEY


class ResyClientError(Exception):
    """Base exception for Resy client errors."""
    pass


class AuthenticationError(ResyClientError):
    """Raised when authentication fails."""
    pass


class BookingError(ResyClientError):
    """Raised when booking fails."""
    pass


def login(email: str, password: str) -> ResyCredentials:
    """
    Log in to Resy with email/password and return credentials.

    Args:
        email: Resy account email
        password: Resy account password

    Returns:
        ResyCredentials with api_key and auth_token

    Raises:
        AuthenticationError: If login fails
    """
    headers = {
        "Authorization": f'ResyAPI api_key="{RESY_API_KEY}"',
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://resy.com",
        "Referer": "https://resy.com/",
    }

    data = {
        "email": email,
        "password": password,
    }

    with httpx.Client(base_url=RESY_API_BASE, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = client.post("/3/auth/password", data=data, headers=headers)

        if response.status_code != 200:
            error_msg = "Invalid email or password"
            try:
                error_data = response.json()
                error_msg = error_data.get("message", error_msg)
            except Exception:
                pass
            raise AuthenticationError(error_msg)

        result = response.json()
        auth_token = result.get("token")

        if not auth_token:
            raise AuthenticationError("No auth token in response")

        # Get payment method ID if available
        payment_method_id = result.get("payment_method_id")

        return ResyCredentials(
            api_key=RESY_API_KEY,
            auth_token=auth_token,
            payment_method_id=payment_method_id,
        )


def search_venues(query: str, location: str = "new york") -> list[dict]:
    """
    Search for venues by name (no authentication required).

    Args:
        query: Restaurant name to search for
        location: City to search in (default: new york)

    Returns:
        List of venue dicts with id, name, neighborhood, cuisine
    """
    headers = {
        "Authorization": f'ResyAPI api_key="{RESY_API_KEY}"',
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://resy.com",
        "Referer": "https://resy.com/",
    }

    # Use the venue search endpoint with POST
    json_data = {
        "query": query,
        "geo": {"latitude": 40.7128, "longitude": -74.0060},
        "types": ["venue"],
        "per_page": 10,
    }

    with httpx.Client(base_url=RESY_API_BASE, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = client.post("/3/venuesearch/search", json=json_data, headers=headers)

        if response.status_code != 200:
            # Try alternative endpoint
            params = {"query": query, "lat": "40.7128", "long": "-74.0060"}
            response = client.get("/2/search", params=params, headers=headers)

            if response.status_code != 200:
                return []

            data = response.json()
            venues = []
            for venue in data.get("search", {}).get("venues", []):
                venues.append({
                    "id": venue.get("venue_id") or venue.get("id"),
                    "name": venue.get("name"),
                    "neighborhood": venue.get("neighborhood") or venue.get("location", {}).get("name", ""),
                    "cuisine": venue.get("cuisine", ""),
                    "url_slug": venue.get("url_slug", ""),
                })
            return venues

        data = response.json()
        venues = []

        for hit in data.get("search", {}).get("hits", []):
            # Data is directly in hit, not in _source
            venue_id = hit.get("id", {})
            if isinstance(venue_id, dict):
                venue_id = venue_id.get("resy")

            cuisine = hit.get("cuisine", [])
            if isinstance(cuisine, list) and cuisine:
                cuisine = cuisine[0]
            else:
                cuisine = ""

            venues.append({
                "id": venue_id,
                "name": hit.get("name"),
                "neighborhood": hit.get("neighborhood", ""),
                "cuisine": cuisine,
                "url_slug": hit.get("url_slug", ""),
            })

        return venues


class ResyClient:
    """Client for interacting with the Resy API."""

    def __init__(self, credentials: ResyCredentials):
        self.credentials = credentials
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=RESY_API_BASE,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers=self._get_headers(),
            )
        return self._client

    def _get_headers(self) -> dict:
        """Get headers for API requests."""
        return {
            "Authorization": f'ResyAPI api_key="{self.credentials.api_key}"',
            "X-Resy-Auth-Token": self.credentials.auth_token,
            "X-Resy-Universal-Auth": self.credentials.auth_token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
        }

    def test_credentials(self) -> bool:
        """Test if the credentials are valid by making a simple API call."""
        try:
            response = self.client.get("/2/user")
            return response.status_code == 200
        except Exception:
            return False

    def get_user_info(self) -> dict:
        """Get information about the authenticated user."""
        response = self.client.get("/2/user")
        if response.status_code != 200:
            raise AuthenticationError(f"Failed to get user info: {response.text}")
        return response.json()

    def find_slots(
        self,
        venue_id: int,
        party_size: int,
        date: str,
    ) -> list[Slot]:
        """
        Find available reservation slots for a venue.

        Args:
            venue_id: The venue's unique identifier
            party_size: Number of guests
            date: Date in YYYY-MM-DD format

        Returns:
            List of available slots
        """
        params = {
            "lat": "0",
            "long": "0",
            "day": date,
            "party_size": str(party_size),
            "venue_id": str(venue_id),
        }

        response = self.client.get("/4/find", params=params)

        if response.status_code != 200:
            raise ResyClientError(f"Failed to find slots: {response.text}")

        data = response.json()
        slots = []

        # Parse the response structure
        results = data.get("results", {})
        venues = results.get("venues", [])

        for venue in venues:
            for slot_data in venue.get("slots", []):
                try:
                    config = slot_data.get("config", {})
                    slot = Slot(
                        config_id=str(config.get("id", "")),
                        token=config.get("token", ""),
                        date=date,
                        time_slot=slot_data.get("date", {}).get("start", ""),
                        party_size=party_size,
                        table_type=config.get("type", ""),
                        venue_id=venue_id,
                    )
                    slots.append(slot)
                except Exception:
                    continue

        return slots

    def get_slot_details(self, slot: Slot) -> ReservationDetails:
        """
        Get details for a specific slot needed to complete booking.

        Args:
            slot: The slot to get details for

        Returns:
            Reservation details including book token
        """
        # API requires POST with JSON body and the full token as config_id
        data = {
            "config_id": slot.token,  # Use the full rgs:// token, not the numeric ID
            "day": slot.date,
            "party_size": slot.party_size,
        }

        response = self.client.post("/3/details", json=data)

        if response.status_code not in (200, 201):
            raise ResyClientError(f"Failed to get slot details: {response.text}")

        data = response.json()
        book_token = data.get("book_token", {}).get("value", "")

        if not book_token:
            raise ResyClientError("No book token in response")

        # Get payment method if required
        payment_method_id = None
        payment_methods = data.get("user", {}).get("payment_methods", [])
        if payment_methods:
            payment_method_id = payment_methods[0].get("id")

        return ReservationDetails(
            book_token=book_token,
            payment_method_id=payment_method_id or self.credentials.payment_method_id,
        )

    def book_reservation(self, details: ReservationDetails) -> dict:
        """
        Book a reservation using the book token.

        Args:
            details: Reservation details with book token

        Returns:
            Booking confirmation response
        """
        import json as json_module

        data = {
            "book_token": details.book_token,
            "source_id": "resy.com-venue-details",
        }

        if details.payment_method_id:
            data["struct_payment_method"] = json_module.dumps({"id": details.payment_method_id})

        # Book endpoint requires form data, not JSON
        headers = self._get_headers().copy()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        response = self.client.post("/3/book", data=data, headers=headers)

        if response.status_code == 412:
            # Already have a reservation at this restaurant on this date
            existing = response.json()
            raise BookingError(
                f"Conflict: You already have a reservation at {existing.get('venue', {}).get('name', 'this restaurant')} "
                f"on {existing.get('specs', {}).get('day', 'this date')} at {existing.get('specs', {}).get('time_slot', 'another time')}"
            )

        if response.status_code != 201:
            raise BookingError(f"Failed to book reservation: {response.text}")

        return response.json()

    def find_best_slot(
        self,
        venue_id: int,
        party_size: int,
        date: str,
        preferred_times: list[TimePreference],
    ) -> Optional[Slot]:
        """
        Find the best available slot based on time preferences.

        Uses fuzzy matching within each preference's time window (default ±60 min).
        Returns the slot closest to the preferred time.

        Args:
            venue_id: Venue identifier
            party_size: Number of guests
            date: Date in YYYY-MM-DD format
            preferred_times: List of time preferences in priority order

        Returns:
            Best matching slot or None if no slots available
        """
        slots = self.find_slots(venue_id, party_size, date)

        if not slots:
            return None

        # If no preferences, return first available
        if not preferred_times:
            return slots[0]

        # Find the best match across all preferences
        # Score by: (preference priority * 1000) + time difference
        # This ensures earlier preferences win ties
        best_slot = None
        best_score = float('inf')

        for pref_index, pref in enumerate(preferred_times):
            for slot in slots:
                if pref.matches(slot):  # Within time window AND matches table type
                    diff = pref.time_difference_minutes(slot)
                    if diff is not None:
                        score = (pref_index * 1000) + diff
                        if score < best_score:
                            best_score = score
                            best_slot = slot

        # Return best match, or first slot if nothing matched within windows
        return best_slot if best_slot else slots[0]

    def get_reservations(self) -> list[dict]:
        """
        Get all upcoming reservations for the authenticated user.

        Returns:
            List of reservation dicts with details
        """
        import re

        response = self.client.get("/3/user/reservations")

        if response.status_code != 200:
            raise ResyClientError(f"Failed to get reservations: {response.text}")

        data = response.json()
        reservations = []

        for res in data.get("reservations", []):
            venue = res.get("venue", {})
            config = res.get("config", {})

            # Extract venue name from share.generic_message
            # Format: "Please RSVP for Ozakaya on February 8 at 7:00PM"
            venue_name = "Unknown"
            share = res.get("share", {})
            generic_msg = share.get("generic_message", "")
            if generic_msg:
                match = re.search(r"for (.+?) on ", generic_msg)
                if match:
                    venue_name = match.group(1)

            # Get cancellation cutoff date
            cancellation = res.get("cancellation", {})
            cutoff = cancellation.get("date_refund_cut_off")
            if cutoff:
                # Format: "2026-02-08T18:00:00Z" -> "Feb 8 at 1:00 PM"
                try:
                    from datetime import datetime as dt
                    cutoff_dt = dt.fromisoformat(cutoff.replace("Z", "+00:00"))
                    cutoff = cutoff_dt.strftime("%b %d at %I:%M %p")
                except Exception:
                    pass

            reservations.append({
                "resy_token": res.get("resy_token"),
                "venue_name": venue_name,
                "venue_id": venue.get("id"),
                "date": res.get("day"),
                "time": res.get("time_slot"),
                "party_size": res.get("num_seats"),
                "table_type": config.get("type", ""),
                "reservation_id": res.get("reservation_id"),
                "cancellation_allowed": cancellation.get("allowed", False),
                "cancellation_cutoff": cutoff,
            })

        return reservations

    def cancel_reservation(self, resy_token: str) -> bool:
        """
        Cancel a reservation by its resy_token.

        Args:
            resy_token: The unique token for the reservation

        Returns:
            True if cancellation was successful

        Raises:
            ResyClientError: If cancellation fails
        """
        # Cancel endpoint requires form data
        headers = self._get_headers().copy()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        data = {"resy_token": resy_token}

        response = self.client.post("/3/cancel", data=data, headers=headers)

        if response.status_code not in (200, 201, 204):
            error_msg = f"Failed to cancel reservation: {response.text}"
            try:
                error_data = response.json()
                error_msg = error_data.get("message", error_msg)
            except Exception:
                pass
            raise ResyClientError(error_msg)

        return True

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
