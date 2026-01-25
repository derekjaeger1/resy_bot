"""Main TUI application."""

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, TabbedContent, TabPane
from textual.binding import Binding

from storage.database import Database
from scheduler.daemon import SchedulerDaemon

from .screens.dashboard import DashboardScreen
from .screens.restaurants import RestaurantsScreen
from .screens.reservations import ReservationsScreen
from .screens.settings import SettingsScreen


class ResyBotApp(App):
    """The main Resy Bot TUI application."""

    TITLE = "Resy Bot"
    SUB_TITLE = "Reservation Sniper"

    CSS = """
    Screen {
        background: $surface;
    }

    TabbedContent {
        height: 100%;
    }

    TabPane {
        padding: 1;
    }

    .section-title {
        text-style: bold;
        color: $text;
        padding: 1;
        background: $primary-darken-2;
        width: 100%;
    }

    .action-button {
        margin: 1;
    }

    Input {
        margin: 1 0;
    }

    Button {
        margin: 1 1;
    }
    """

    BINDINGS = [
        Binding("d", "switch_tab('dashboard')", "Dashboard", show=True),
        Binding("r", "switch_tab('restaurants')", "Restaurants", show=True),
        Binding("s", "switch_tab('schedule')", "Schedule", show=True),
        Binding("c", "switch_tab('settings')", "Settings", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+d", "toggle_daemon", "Toggle Daemon", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.db = Database()
        self.daemon = SchedulerDaemon(
            self.db,
            on_snipe_complete=self._on_snipe_complete,
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardScreen(self.db, self.daemon)
            with TabPane("Restaurants", id="restaurants"):
                yield RestaurantsScreen(self.db)
            with TabPane("Schedule", id="schedule"):
                yield ReservationsScreen(self.db, self.daemon)
            with TabPane("Settings", id="settings"):
                yield SettingsScreen(self.db, self.daemon)
        yield Footer()

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to a specific tab."""
        self.query_one(TabbedContent).active = tab_id

    def action_toggle_daemon(self) -> None:
        """Toggle the daemon on/off."""
        if self.daemon.is_running():
            self.daemon.stop()
            self.notify("Daemon stopped", severity="warning")
        else:
            self.daemon.start()
            self.notify("Daemon started", severity="information")

        # Update dashboard status
        dashboard = self.query_one(DashboardScreen)
        dashboard.refresh_status()

    def _on_snipe_complete(self, reservation, result) -> None:
        """Handle snipe completion."""
        if result.success:
            self.notify(
                f"Booked {result.booked_time} at reservation!",
                title="Reservation Success!",
                severity="information",
            )
        else:
            self.notify(
                result.message,
                title="Snipe Failed",
                severity="error",
            )

        # Refresh dashboard
        dashboard = self.query_one(DashboardScreen)
        dashboard.refresh_data()

    def on_mount(self) -> None:
        """Called when app is mounted."""
        # Check for credentials
        creds = self.db.get_credentials()
        if not creds:
            self.notify(
                "No credentials configured. Go to Settings to set up.",
                title="Setup Required",
                severity="warning",
            )

    def on_unmount(self) -> None:
        """Called when app is unmounted."""
        if self.daemon.is_running():
            self.daemon.stop()


def run_app() -> None:
    """Run the TUI application."""
    app = ResyBotApp()
    app.run()
