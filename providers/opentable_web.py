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
from typing import Any, Optional
from urllib.parse import quote_plus, urlencode

from .models import BookingResult, ProviderAvailability, ProviderSlot


class OpenTableWebError(RuntimeError):
    pass


class OpenTableChallenge(OpenTableWebError):
    pass


class OpenTableUserActionRequired(OpenTableWebError):
    """The normal consumer flow needs a user step we will not bypass."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


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

    def search_restaurants(
        self,
        query: str,
        *,
        latitude: float = 40.7128,
        longitude: float = -74.0060,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Search OpenTable's visible consumer results and return restaurant IDs."""
        q = (query or "").strip()
        if len(q) < 2:
            return []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise OpenTableWebError("Playwright/Chromium is not installed.") from exc

        today = datetime.now().strftime("%Y-%m-%d")
        params = {
            "term": q,
            "covers": "2",
            "dateTime": f"{today}T19:00:00",
            "latitude": str(latitude),
            "longitude": str(longitude),
        }
        url = f"{self.base_url}/s?{urlencode(params)}"

        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.config.headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(1600)

                if self._challenge_present(page):
                    raise OpenTableChallenge(
                        "OpenTable requires normal verification before search can continue."
                    )

                # Preferred path: restaurant result cards exposing a data-rid.
                cards = page.locator("[data-rid]")
                for idx in range(min(cards.count(), 60)):
                    card = cards.nth(idx)
                    rid = card.get_attribute("data-rid")
                    if not rid or not str(rid).isdigit() or str(rid) in seen:
                        continue
                    try:
                        text = " ".join((card.inner_text(timeout=400) or "").split())
                    except Exception:
                        text = ""
                    if not text:
                        continue
                    name = ""
                    try:
                        heading = card.locator("h1,h2,h3,[role='heading']").first
                        if heading.count():
                            name = " ".join((heading.inner_text(timeout=250) or "").split())
                    except Exception:
                        pass
                    if not name:
                        try:
                            first_link = card.locator("a").first
                            if first_link.count():
                                name = " ".join((first_link.inner_text(timeout=250) or "").split())
                        except Exception:
                            pass
                    name = (name or text)[:160]
                    if q.lower() not in text.lower() and q.lower() not in name.lower():
                        continue
                    seen.add(str(rid))
                    results.append({
                        "id": str(rid),
                        "name": name[:160],
                        "neighborhood": "",
                        "cuisine": "",
                        "url_slug": "",
                        "provider": self.provider,
                    })
                    if len(results) >= limit:
                        break

                # Fallback: reservation links often carry rid in the query string.
                if len(results) < limit:
                    anchors = page.locator("a[href]")
                    for idx in range(min(anchors.count(), 250)):
                        a = anchors.nth(idx)
                        href = a.get_attribute("href") or ""
                        match = re.search(r"[?&]rid=(\d+)", href)
                        if not match:
                            continue
                        rid = match.group(1)
                        if rid in seen:
                            continue
                        try:
                            text = " ".join((a.inner_text(timeout=250) or "").split())
                        except Exception:
                            text = ""
                        if not text or q.lower() not in text.lower():
                            continue
                        seen.add(rid)
                        results.append({
                            "id": rid,
                            "name": text[:160],
                            "neighborhood": "",
                            "cuisine": "",
                            "url_slug": href,
                            "provider": self.provider,
                        })
                        if len(results) >= limit:
                            break
            finally:
                context.close()
                browser.close()

        return results

    @staticmethod
    def _fill_first(page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            try:
                node = page.locator(selector).first
                if node.count() and node.is_visible():
                    node.fill(value)
                    return True
            except Exception:
                continue
        return False

    def book_visible_slot(
        self,
        rid: int | str,
        *,
        day: str,
        time_24h: str,
        party_size: int,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
    ) -> BookingResult:
        """Book an ordinary table through OpenTable's visible consumer checkout.

        No anti-bot bypass is attempted. Secured/prepaid flows and verification
        challenges are surfaced as user-action-required so the watch can stay active.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise OpenTableWebError("Playwright/Chromium is not installed.") from exc

        profile = Path(self.config.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        url = self.booking_url(
            rid,
            day=day,
            time_24h=time_24h,
            party_size=party_size,
        )
        target_label = datetime.strptime(time_24h, "%H:%M").strftime("%-I:%M %p")

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
                        "OpenTable requested human verification for this session."
                    )

                # If the hand-off lands on a slot list, select the exact requested time.
                time_re = re.compile(
                    rf"^\s*{re.escape(target_label)}(?:\s|$)",
                    re.IGNORECASE,
                )
                clicked_time = False
                for role in ("button", "link"):
                    try:
                        candidates = page.get_by_role(role, name=time_re)
                        if candidates.count():
                            candidates.first.click()
                            clicked_time = True
                            page.wait_for_timeout(900)
                            break
                    except Exception:
                        continue

                if self._challenge_present(page):
                    raise OpenTableChallenge(
                        "OpenTable requested human verification during checkout."
                    )

                body = ""
                try:
                    body = (page.locator("body").inner_text(timeout=1500) or "").lower()
                except Exception:
                    pass

                # We deliberately do not auto-submit a financial commitment in the
                # browser fallback. Official API mode can support those later.
                payment_terms = (
                    "credit card required",
                    "card required",
                    "deposit",
                    "prepaid",
                    "pre-pay",
                    "non-refundable",
                )
                if any(term in body for term in payment_terms):
                    raise OpenTableUserActionRequired(
                        "payment",
                        "This OpenTable reservation requires a card, deposit, or prepayment.",
                    )

                # Prefer guest checkout when OpenTable offers it.
                for label in ("Continue as guest", "Book as guest", "Guest"):
                    try:
                        guest = page.get_by_role("button", name=re.compile(label, re.I))
                        if guest.count() and guest.first.is_visible():
                            guest.first.click()
                            page.wait_for_timeout(500)
                            break
                    except Exception:
                        continue

                filled_first = self._fill_first(page, [
                    "input[autocomplete='given-name']",
                    "input[name*='first' i]",
                    "input[id*='first' i]",
                ], first_name)
                filled_last = self._fill_first(page, [
                    "input[autocomplete='family-name']",
                    "input[name*='last' i]",
                    "input[id*='last' i]",
                ], last_name)
                filled_email = self._fill_first(page, [
                    "input[type='email']",
                    "input[autocomplete='email']",
                    "input[name*='email' i]",
                ], email)
                filled_phone = self._fill_first(page, [
                    "input[type='tel']",
                    "input[autocomplete='tel']",
                    "input[name*='phone' i]",
                ], phone)

                if not all((filled_first, filled_last, filled_email, filled_phone)):
                    body_now = ""
                    try:
                        body_now = (page.locator("body").inner_text(timeout=1000) or "").lower()
                    except Exception:
                        pass
                    if "sign in" in body_now or "log in" in body_now:
                        raise OpenTableUserActionRequired(
                            "login",
                            "OpenTable requires an authenticated session for this checkout.",
                        )
                    raise OpenTableWebError(
                        "OpenTable checkout fields were not available in the expected guest flow."
                    )

                confirm_re = re.compile(
                    r"(complete|confirm|make|book|reserve).*(reservation|table)|"
                    r"(reservation|table).*(complete|confirm|book|reserve)",
                    re.IGNORECASE,
                )
                confirm = page.get_by_role("button", name=confirm_re)
                if not confirm.count():
                    raise OpenTableWebError("Could not find OpenTable's confirmation button.")

                confirm.first.click()
                page.wait_for_timeout(2200)

                if self._challenge_present(page):
                    raise OpenTableChallenge(
                        "OpenTable requested human verification when confirming the reservation."
                    )

                confirmation_text = ""
                try:
                    confirmation_text = page.locator("body").inner_text(timeout=1500) or ""
                except Exception:
                    pass

                lower = confirmation_text.lower()
                success_terms = (
                    "reservation confirmed",
                    "you're booked",
                    "you’re booked",
                    "booking confirmed",
                    "reservation is confirmed",
                )
                if not any(term in lower for term in success_terms):
                    raise OpenTableWebError(
                        "OpenTable did not return a visible booking confirmation; watch remains active."
                    )

                conf_match = re.search(
                    r"confirmation(?:\s+number)?\s*[:#]?\s*([A-Z0-9-]{4,})",
                    confirmation_text,
                    re.IGNORECASE,
                )
                confirmation = conf_match.group(1) if conf_match else None

                return BookingResult(
                    provider=self.provider,
                    restaurant_id=str(rid),
                    confirmation_number=confirmation,
                    date_time=datetime.strptime(
                        f"{day} {time_24h}", "%Y-%m-%d %H:%M"
                    ),
                    party_size=party_size,
                    manage_reservation_url=page.url,
                    message="Booked through OpenTable consumer checkout",
                    raw={
                        "source": "consumer-web",
                        "clicked_time": clicked_time,
                        "checkout_url": page.url,
                    },
                )
            finally:
                context.close()

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
