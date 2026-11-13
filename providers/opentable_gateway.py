"""OpenTable transport selection.

Use the official API whenever partner credentials are present. For internal
testing, callers may explicitly opt into the standard consumer-web transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .opentable import OpenTableClient
from .opentable_web import OpenTableWebClient, OpenTableWebConfig


@dataclass
class OpenTableTransportConfig:
    api_access_token: Optional[str] = None
    api_base_url: Optional[str] = None
    web_enabled: bool = False
    web_profile_dir: str = "/data/opentable-browser"
    web_headless: bool = True


def build_opentable_transport(config: OpenTableTransportConfig):
    if config.api_access_token and config.api_base_url:
        return OpenTableClient(
            access_token=config.api_access_token,
            base_url=config.api_base_url,
        )

    if config.web_enabled:
        return OpenTableWebClient(
            OpenTableWebConfig(
                profile_dir=config.web_profile_dir,
                headless=config.web_headless,
            )
        )

    raise RuntimeError(
        "OpenTable is not configured. Provide official API credentials or "
        "explicitly enable the internal consumer-web transport."
    )
