"""Automatic reservation release-rule intelligence.

The app only arms high-speed drop snipes when a restaurant-specific rule has
been verified. Unknown or ambiguous rules fall back to continuous availability
watching instead of guessing.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import time
from typing import Optional

import httpx

from config import REQUEST_TIMEOUT_SECONDS, USER_AGENT


@dataclass
class ReleaseRule:
    verified: bool
    snipeable: bool
    pattern: str = "daily"
    days_in_advance: Optional[int] = None
    release_time: Optional[time] = None
    source: Optional[str] = None
    note: Optional[str] = None


# Hand-verified rules for restaurants we've already researched.
# These take precedence over automatic Resy-page parsing.
KNOWN_RULES = {
    "iara": ReleaseRule(
        verified=True,
        snipeable=True,
        days_in_advance=14,
        release_time=time(12, 0),
        source="https://resy.com/cities/new-york-ny/venues/iara",
        note="Reservations release daily 14 days ahead at noon ET.",
    ),
    "straker's nyc": ReleaseRule(
        verified=True,
        snipeable=True,
        days_in_advance=14,
        release_time=time(0, 0),
        source="https://blog.resy.com/2026/10/strakers-nyc/",
        note="Reservations release daily two weeks ahead at midnight ET.",
    ),
    "straker's": ReleaseRule(
        verified=True,
        snipeable=True,
        days_in_advance=14,
        release_time=time(0, 0),
        source="https://blog.resy.com/2026/10/strakers-nyc/",
        note="Reservations release daily two weeks ahead at midnight ET.",
    ),
    "ambassadors clubhouse new york": ReleaseRule(
        verified=True,
        snipeable=False,
        source="https://ambassadorsclubhouse.com/newyork/faqs/",
        note=(
            "The upcoming month is released in one block approximately two weeks "
            "before the first of that month; no fixed public release hour is published."
        ),
    ),
    "ambassadors clubhouse": ReleaseRule(
        verified=True,
        snipeable=False,
        source="https://ambassadorsclubhouse.com/newyork/faqs/",
        note=(
            "The upcoming month is released in one block approximately two weeks "
            "before the first of that month; no fixed public release hour is published."
        ),
    ),
}


def _normalize_name(name: str) -> str:
    return " ".join((name or "").lower().replace("’", "'").split())


def _parse_clock(text: str) -> Optional[time]:
    value = html.unescape(text).strip().lower()
    value = re.sub(r"\s+(eastern time|et|est|edt)\b.*$", "", value).strip()
    value = value.rstrip(".,; ")

    if value.startswith("noon"):
        return time(12, 0)
    if value.startswith("midnight"):
        return time(0, 0)

    match = re.search(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?", value)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        ampm = match.group(3)
        if ampm == "p" and hour != 12:
            hour += 12
        if ampm == "a" and hour == 12:
            hour = 0
        return time(hour, minute)

    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", value)
    if match:
        return time(int(match.group(1)), int(match.group(2)))
    return None


def _extract_daily_rule(page_text: str, source: str) -> Optional[ReleaseRule]:
    compact = " ".join(html.unescape(page_text).replace("\\u2019", "'").split())

    patterns = [
        r"(?:up to\s+)?(\d+)\s+days?\s+in advance.{0,120}?released daily at\s+([^.<\"}]{2,45})",
        r"reservations (?:drop|are released|open).{0,40}?(\d+)\s+days?\s+(?:in advance|ahead).{0,40}?at\s+([^.<\"}]{2,45})",
        r"(\d+)\s+days?\s+(?:in advance|ahead).{0,40}?(?:at|beginning at)\s+([^.<\"}]{2,45})",
    ]

    for pattern in patterns:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if not match:
            continue
        days = int(match.group(1))
        clock = _parse_clock(match.group(2))
        if clock is None:
            continue
        return ReleaseRule(
            verified=True,
            snipeable=True,
            days_in_advance=days,
            release_time=clock,
            source=source,
            note=f"Automatically verified from the restaurant's public Resy listing: {days} days ahead.",
        )
    return None


def resolve_release_rule(name: str, url_slug: str | None = None) -> ReleaseRule:
    """Resolve a restaurant-specific release rule without user input.

    Order:
    1) hand-verified rule registry,
    2) automatic parsing of the restaurant's public Resy page,
    3) safe watch-only fallback.
    """
    normalized = _normalize_name(name)
    if normalized in KNOWN_RULES:
        return KNOWN_RULES[normalized]

    # Allow suffix/prefix variants such as "Iara New York".
    for key, rule in KNOWN_RULES.items():
        if key in normalized or normalized in key:
            return rule

    if url_slug:
        source = f"https://resy.com/cities/new-york-ny/venues/{url_slug}"
        try:
            response = httpx.get(
                source,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                follow_redirects=True,
            )
            if response.status_code == 200:
                parsed = _extract_daily_rule(response.text, source)
                if parsed:
                    return parsed
        except Exception:
            pass

    return ReleaseRule(
        verified=False,
        snipeable=False,
        source=None,
        note="Exact release timing is not verified yet; continuous watch only.",
    )
