"""OpenTable Consumer Booking API v2 client.

This client targets OpenTable's official partner API. Production use requires
OpenTable partner approval and credentials. It is intentionally isolated from
the current Resy implementation so adding OpenTable cannot destabilize the
working Resy watcher.

Docs: https://docs.opentable.com/
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

import httpx

from .models import BookingResult, ProviderAvailability, ProviderSlot, SlotLock


class OpenTableError(RuntimeError):
    pass


class OpenTableClient:
    provider = "opentable"

    def __init__(
        self,
        access_token: str,
        base_url: str,
        timeout: float = 12.0,
    ) -> None:
        if not access_token:
            raise ValueError("OpenTable access token is required")
        if not base_url:
            raise ValueError("OpenTable base URL is required")

        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def __enter__(self) -> "OpenTableClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        if method.upper() in {"POST", "PUT", "PATCH"}:
            headers = {**headers, "X-Request-Id": str(uuid4())}

        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise OpenTableError(
                f"OpenTable {method.upper()} {path} failed "
                f"({response.status_code}): {detail}"
            )

        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        raw = str(value).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def get_availability(
        self,
        rid: int | str,
        *,
        start_date_time: str,
        party_size: int,
        forward_minutes: int = 0,
        backward_minutes: int = 0,
        table_type: Optional[str] = None,
        include_credit_card_results: bool = True,
        include_experiences: bool = True,
    ) -> ProviderAvailability:
        params: dict[str, Any] = {
            "start_date_time": start_date_time,
            "party_size": party_size,
            "forward_minutes": forward_minutes,
            "backward_minutes": backward_minutes,
            "include_credit_card_results": str(include_credit_card_results).lower(),
            "include_experiences": str(include_experiences).lower(),
        }
        if table_type:
            params["require_attributes"] = table_type

        payload = self._request("GET", f"/v2/availability/{rid}", params=params)
        slots: list[ProviderSlot] = []

        for time_row in payload.get("times_available", []) or []:
            time_value = time_row.get("time")
            parsed = self._parse_datetime(time_value)
            if parsed is None:
                # Some partner responses provide a local HH:MM-style value.
                # Preserve the raw row rather than inventing a date.
                continue

            availability_types = time_row.get("availability_types", []) or [{}]
            for option in availability_types:
                dining_areas = option.get("dining_area") or [{}]
                cancellation_policy = option.get("cancellation_policy")
                option_type = str(option.get("type") or "")
                requires_payment = bool(cancellation_policy)

                for area in dining_areas:
                    attrs = area.get("attributes") or []
                    normalized_table_type = attrs[0] if attrs else None
                    slots.append(
                        ProviderSlot(
                            provider=self.provider,
                            restaurant_id=str(rid),
                            date_time=parsed,
                            party_size=party_size,
                            table_type=normalized_table_type,
                            dining_area_id=(
                                str(area.get("id"))
                                if area.get("id") is not None
                                else None
                            ),
                            dining_area_name=area.get("name"),
                            environment=area.get("environment"),
                            experience_id=(
                                str((area.get("experience_ids") or [None])[0])
                                if area.get("experience_ids")
                                else None
                            ),
                            requires_payment=requires_payment,
                            cancellation_policy=cancellation_policy,
                            raw={
                                "time_row": time_row,
                                "availability_type": option,
                                "dining_area": area,
                                "type": option_type,
                            },
                        )
                    )

        return ProviderAvailability(
            provider=self.provider,
            restaurant_id=str(rid),
            slots=slots,
            no_availability_reasons=payload.get("no_availability_reasons", []) or [],
            raw=payload,
        )

    def get_availability_metadata(self, rid: int | str) -> dict[str, Any]:
        return self._request("GET", f"/v2/availability-metadata/{rid}")

    def get_active_experiences(self, rid: int | str) -> dict[str, Any]:
        return self._request("GET", f"/v2/experiences/{rid}/active")

    def get_booking_policy(
        self,
        rid: int | str,
        *,
        date: str,
        time: str,
        party_size: int,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v2/booking-policies/{rid}/{date}/{time}/{party_size}",
            params={"dinerContext": "details"},
        )

    def get_cancellation_policy(
        self,
        rid: int | str,
        cancellation_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v2/cancellation-policies/{rid}/{cancellation_id}",
        )

    def create_slot_lock(
        self,
        rid: int | str,
        *,
        party_size: int,
        date_time: str,
        reservation_attribute: str = "default",
        dining_area_id: Optional[int | str] = None,
        environment: Optional[str] = None,
        experience: Optional[dict[str, Any]] = None,
    ) -> SlotLock:
        body: dict[str, Any] = {
            "party_size": party_size,
            "date_time": date_time,
            "reservation_attribute": reservation_attribute,
        }
        if dining_area_id is not None:
            body["dining_area_id"] = int(dining_area_id)
        if environment:
            body["environment"] = environment
        if experience:
            body["experience"] = experience

        payload = self._request("POST", f"/v2/booking/{rid}/slot_locks", json=body)
        return SlotLock(
            provider=self.provider,
            restaurant_id=str(rid),
            reservation_token=str(payload.get("reservation_token") or ""),
            expires_at=self._parse_datetime(payload.get("expires_at")),
            payment_method=payload.get("payment_method"),
            raw=payload,
        )

    def release_slot_lock(
        self,
        rid: int | str,
        reservation_token: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/v2/booking/{rid}/slot_locks/{reservation_token}",
        )

    def make_reservation(
        self,
        rid: int | str,
        *,
        reservation_token: str,
        first_name: str,
        last_name: str,
        email_address: str,
        phone: dict[str, Any],
        reservation_attribute: str = "default",
        special_request: Optional[str] = None,
        dining_area_id: Optional[int | str] = None,
        environment: Optional[str] = None,
        experience: Optional[dict[str, Any]] = None,
        credit_card: Optional[dict[str, Any]] = None,
    ) -> BookingResult:
        body: dict[str, Any] = {
            "reservation_token": reservation_token,
            "first_name": first_name,
            "last_name": last_name,
            "email_address": email_address,
            "phone": phone,
            "reservation_attribute": reservation_attribute,
        }
        if special_request:
            body["special_request"] = special_request[:75]
        if dining_area_id is not None:
            body["dining_area_id"] = int(dining_area_id)
        if environment:
            body["environment"] = environment
        if experience:
            body["experience"] = experience
        if credit_card:
            body["credit_card"] = credit_card

        payload = self._request("POST", f"/v2/booking/{rid}/reservations", json=body)
        return BookingResult(
            provider=self.provider,
            restaurant_id=str(rid),
            confirmation_number=(
                str(payload.get("confirmation_number"))
                if payload.get("confirmation_number") is not None
                else None
            ),
            date_time=self._parse_datetime(payload.get("date_time")),
            party_size=payload.get("party_size"),
            manage_reservation_url=payload.get("manage_reservation_url"),
            cancel_cutoff_date_utc=self._parse_datetime(
                payload.get("cancel_cutoff_date_utc")
            ),
            message=payload.get("message"),
            payment=payload.get("payment"),
            raw=payload,
        )
