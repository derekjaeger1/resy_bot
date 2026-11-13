"""Browser-backed OpenTable fallback for internal/private use.

This transport uses OpenTable's normal visible consumer reservation flow.
It deliberately does NOT implement stealth patches, CAPTCHA solving,
fingerprint spoofing, or other anti-bot bypasses. If OpenTable presents a
challenge, the caller receives OpenTableChallenge and should ask the user to
re-authenticate normally.

The browser dependency is imported lazily so the existing app can continue to
run without Playwright until this transport is explicitly enabled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from .models import ProviderAvailability, ProviderSlot


class OpenTableWebError(RuntimeError):
    pass


class OpenTableChallenge(OpenTableWebError):
    pass


@dataclass
class OpenTableWebConfig:
    profile_dir: str
    headless: bool = True
    timeout_ms: int = 15000


class OpenTableWebClient:
    provider = "opentable"
    base_url = "https://www.opentable.com"

    def __init__(self, config: OpenTableWebConfig):
        self.config = config

    @staticmethod
    def reservation_url(
        rid: int | str,
        *,
        date_time: str,
        party_size: int,
    ) -> str:
        """Build OpenTable's normal consumer reservation hand-off URL."""
        params = {
            "rid": str(rid),
            "datetime": date_time,
            "covers": str(party_size),
        }
        return f"{OpenTableWebClient.base_url}/restref/client/?{urlencode(params)}"

    @staticmethod
    def _parse_time_label(label: str, day: str) -> Optional[datetime]:
        raw = " ".join((label or "").split()).upper()
        match = re.search(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", raw)
        if not match:
            return None
        try:
            return datetime.strptime(
                f"{day} {match.group(1)}:{match.group(2)} {match.group(3)}",
                "%Y-%m-%d %I:%M %p",
            )
        except ValueError:
            return None

    @staticmethod
    def _challenge_present(page) -> bool:
        """Detect visible challenge pages without attempting to bypass them."""
        checks = [
            "verify you are human",
            "are you a human",
            "captcha",
            "access denied",
            "unusual traffic",
            "security check",
        ]
        try:
            body = (page.locator("body").inner_text(timeout=1500) or "").lower()
        except Exception:
            return False
        return any(term in body for term in checks)

    def get_availability(
        self,
        rid: int | str,
        *,
        day: str,
        party_size: int,
        pivot_time: str = "19:00",
    ) -> ProviderAvailability:
        """Read visible reservation-time buttons from the normal consumer page.

        This is intentionally a standard browser flow. It does not call or
        reverse-engineer OpenTable's private frontend APIs.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise OpenTableWebError(
                "Browser transport is not installed. Install Playwright and "
                "Chromium before enabling OpenTable web mode."
            ) from exc

        profile = Path(self.config.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        url = self.reservation_url(
            rid,
            date_time=f"{day}T{pivot_time}",
            party_size=party_size,
        )

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.config.headless,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)

                if self._challenge_present(page):
                    raise OpenTableChallenge(
                        "OpenTable requires normal user verification or "
                        "re-authentication for this browser session."
                    )

                # OpenTable exposes bookable times as visible controls in its
                # consumer UI. Parse only visible text that looks like a time.
                candidates = page.locator("button:visible, a:visible")
                slots: list[ProviderSlot] = []
                seen: set[tuple[str, str]] = set()

                for idx in range(candidates.count()):
                    node = candidates.nth(idx)
                    try:
                        label = node.inner_text(timeout=500)
                    except Exception:
                        continue
                    parsed = self._parse_time_label(label, day)
                    if parsed is None:
                        continue
                    key = (parsed.isoformat(), label.strip())
                    if key in seen:
                        continue
                    seen.add(key)

                    slots.append(
                        ProviderSlot(
                            provider=self.provider,
                            restaurant_id=str(rid),
                            date_time=parsed,
                            party_size=party_size,
                            raw={
                                "source": "consumer-web",
                                "label": label.strip(),
                                "url": url,
                            },
                        )
                    )

                return ProviderAvailability(
                    provider=self.provider,
                    restaurant_id=str(rid),
                    slots=sorted(slots, key=lambda slot: slot.date_time),
                    raw={"source": "consumer-web", "url": url},
                )
            finally:
                context.close()

    def booking_url(
        self,
        rid: int | str,
        *,
        day: str,
        time_24h: str,
        party_size: int,
    ) -> str:
        """Return the normal OpenTable checkout hand-off for a chosen slot."""
        return self.reservation_url(
            rid,
            date_time=f"{day}T{time_24h}",
            party_size=party_size,
        )
