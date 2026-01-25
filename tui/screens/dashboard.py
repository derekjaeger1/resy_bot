"""Dashboard screen showing overview and status."""

from datetime import datetime
from textual.app import ComposeResult
from textual.widgets import Static, Label, Button
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from rich.text import Text

from storage.database import Database
from storage.models import ReservationStatus
from scheduler.daemon import SchedulerDaemon
from ..widgets.components import StatusIndicator, SnipeCard, ActivityLog


class DashboardScreen(Container):
    """Main dashboard showing overview of the bot status."""

    DEFAULT_CSS = """
    DashboardScreen {
        layout: grid;
        grid-size: 2;
        grid-gutter: 1;
        padding: 1;
    }

    #status-section {
        column-span: 2;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1;
    }

    #upcoming-section {
        height: 100%;
        background: $surface;
        border: solid $primary;
        padding: 1;
    }

    #history-section {
        height: 100%;
        background: $surface;
        border: solid $primary;
        padding: 1;
    }

    .section-header {
        text-style: bold;
        background: $primary-darken-1;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, db: Database, daemon: SchedulerDaemon, **kwargs):
        super().__init__(**kwargs)
        self.db = db
        self.daemon = daemon

    def compose(self) -> ComposeResult:
        # Status section
        with Container(id="status-section"):
            yield Label("Status", classes="section-header")
            yield Horizontal(
                StatusIndicator(self.daemon.is_running(), id="daemon-status"),
                Label(self._get_stats_text(), id="stats-label"),
            )
            yield Horizontal(
                Button("Start Daemon", id="start-daemon", variant="success"),
                Button("Stop Daemon", id="stop-daemon", variant="error"),
                Button("Refresh", id="refresh-btn", variant="primary"),
            )

        # Upcoming snipes section
        with ScrollableContainer(id="upcoming-section"):
            yield Label("Upcoming Snipes", classes="section-header")
            yield Container(id="snipes-container")

        # History section
        with ScrollableContainer(id="history-section"):
            yield Label("Recent Activity", classes="section-header")
            yield ActivityLog(self._get_history_entries(), id="activity-log")

    def on_mount(self) -> None:
        """Called when the widget is mounted."""
        self._load_upcoming_snipes()

    def _get_stats_text(self) -> str:
        """Get statistics text."""
        pending = len(self.db.get_scheduled_reservations(ReservationStatus.PENDING))
        restaurants = len(self.db.get_restaurants())
        jobs = len(self.daemon.get_scheduled_jobs()) if self.daemon.is_running() else 0

        return f"Restaurants: {restaurants} | Pending: {pending} | Scheduled Jobs: {jobs}"

    def _get_history_entries(self) -> list:
        """Get history entries for the activity log."""
        history = self.db.get_history(limit=20)
        entries = []
        for h in history:
            entries.append({
                "time": h.attempted_at.strftime("%Y-%m-%d %H:%M"),
                "restaurant": h.restaurant_name,
                "success": h.success,
                "details": h.booked_time if h.success else (h.error_message or "Failed")[:30],
            })
        return entries

    def _load_upcoming_snipes(self) -> None:
        """Load upcoming snipes into the container."""
        container = self.query_one("#snipes-container")
        container.remove_children()

        reservations = self.db.get_upcoming_snipes(hours_ahead=48)

        if not reservations:
            container.mount(Label("No upcoming snipes scheduled"))
            return

        for res in reservations[:10]:  # Show first 10
            restaurant = self.db.get_restaurant(res.restaurant_id)
            if restaurant:
                card = SnipeCard(
                    restaurant_name=restaurant.name,
                    target_date=res.target_date.strftime("%Y-%m-%d"),
                    party_size=res.party_size,
                    snipe_at=res.snipe_at,
                    reservation_id=res.id,
                )
                container.mount(card)

    def refresh_status(self) -> None:
        """Refresh the daemon status display."""
        status = self.query_one("#daemon-status", StatusIndicator)
        status.update_status(self.daemon.is_running())

        stats = self.query_one("#stats-label", Label)
        stats.update(self._get_stats_text())

    def refresh_data(self) -> None:
        """Refresh all data on the dashboard."""
        self.refresh_status()
        self._load_upcoming_snipes()

        log = self.query_one("#activity-log", ActivityLog)
        log.update_entries(self._get_history_entries())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "start-daemon":
            if not self.daemon.is_running():
                creds = self.db.get_credentials()
                if not creds:
                    self.app.notify(
                        "Configure credentials first in Settings",
                        severity="error",
                    )
                    return
                self.daemon.start()
                self.app.notify("Daemon started", severity="information")
            self.refresh_status()

        elif event.button.id == "stop-daemon":
            if self.daemon.is_running():
                self.daemon.stop()
                self.app.notify("Daemon stopped", severity="warning")
            self.refresh_status()

        elif event.button.id == "refresh-btn":
            self.refresh_data()
            self.app.notify("Refreshed", severity="information")
