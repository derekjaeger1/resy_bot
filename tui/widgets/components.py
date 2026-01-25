"""Reusable TUI components."""

from datetime import datetime
from textual.app import ComposeResult
from textual.widgets import Static, Label
from textual.containers import Horizontal, Vertical
from rich.text import Text
from rich.table import Table


class StatusIndicator(Static):
    """Shows daemon running status."""

    def __init__(self, running: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._running = running

    def compose(self) -> ComposeResult:
        yield Label(self._get_status_text())

    def _get_status_text(self) -> Text:
        if self._running:
            return Text.from_markup("[green]● Daemon Running[/green]")
        else:
            return Text.from_markup("[red]○ Daemon Stopped[/red]")

    def update_status(self, running: bool) -> None:
        self._running = running
        self.query_one(Label).update(self._get_status_text())


class SnipeCard(Static):
    """Card showing an upcoming snipe."""

    DEFAULT_CSS = """
    SnipeCard {
        background: $surface;
        border: solid $primary;
        padding: 1;
        margin: 1;
        height: auto;
    }

    SnipeCard .snipe-title {
        text-style: bold;
        color: $text;
    }

    SnipeCard .snipe-details {
        color: $text-muted;
    }

    SnipeCard .snipe-time {
        color: $warning;
    }
    """

    def __init__(
        self,
        restaurant_name: str,
        target_date: str,
        party_size: int,
        snipe_at: datetime,
        reservation_id: int,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.restaurant_name = restaurant_name
        self.target_date = target_date
        self.party_size = party_size
        self.snipe_at = snipe_at
        self.reservation_id = reservation_id

    def compose(self) -> ComposeResult:
        yield Label(self.restaurant_name, classes="snipe-title")
        yield Label(
            f"Date: {self.target_date} | Party: {self.party_size}",
            classes="snipe-details",
        )
        yield Label(
            f"Snipe at: {self.snipe_at.strftime('%Y-%m-%d %H:%M')}",
            classes="snipe-time",
        )


class ActivityLog(Static):
    """Shows recent activity/history."""

    DEFAULT_CSS = """
    ActivityLog {
        height: auto;
        max-height: 20;
        overflow-y: auto;
    }
    """

    def __init__(self, entries: list = None, **kwargs):
        super().__init__(**kwargs)
        self._entries = entries or []

    def compose(self) -> ComposeResult:
        yield Static(self._render_table())

    def _render_table(self) -> Table:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Time", style="dim", width=16)
        table.add_column("Restaurant", width=20)
        table.add_column("Status", width=10)
        table.add_column("Details", width=30)

        for entry in self._entries[:20]:  # Show last 20
            status_style = "green" if entry.get("success") else "red"
            status_text = "SUCCESS" if entry.get("success") else "FAILED"
            table.add_row(
                entry.get("time", ""),
                entry.get("restaurant", ""),
                f"[{status_style}]{status_text}[/{status_style}]",
                entry.get("details", ""),
            )

        return table

    def update_entries(self, entries: list) -> None:
        self._entries = entries
        self.query_one(Static).update(self._render_table())


class RestaurantCard(Static):
    """Card showing restaurant info."""

    DEFAULT_CSS = """
    RestaurantCard {
        background: $surface;
        border: solid $primary;
        padding: 1;
        margin: 1;
        height: auto;
    }

    RestaurantCard:hover {
        border: solid $accent;
    }

    RestaurantCard .restaurant-name {
        text-style: bold;
        color: $text;
    }

    RestaurantCard .restaurant-details {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        restaurant_id: int,
        name: str,
        venue_id: int,
        release_time: str,
        days_in_advance: int,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.restaurant_id = restaurant_id
        self.name = name
        self.venue_id = venue_id
        self.release_time = release_time
        self.days_in_advance = days_in_advance

    def compose(self) -> ComposeResult:
        yield Label(self.name, classes="restaurant-name")
        yield Label(
            f"Venue ID: {self.venue_id}",
            classes="restaurant-details",
        )
        yield Label(
            f"Releases at {self.release_time}, {self.days_in_advance} days ahead",
            classes="restaurant-details",
        )
