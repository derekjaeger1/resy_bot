"""Reservations scheduling screen."""

import json
from datetime import datetime, date, timedelta
from textual.app import ComposeResult
from textual.widgets import Static, Label, Button, Input, Select
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen

from storage.database import Database
from storage.models import ScheduledReservation, ReservationStatus
from scheduler.daemon import SchedulerDaemon


class ScheduleReservationModal(ModalScreen):
    """Modal for scheduling a new reservation."""

    DEFAULT_CSS = """
    ScheduleReservationModal {
        align: center middle;
    }

    #modal-container {
        width: 70;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 2;
    }

    #modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    Input {
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, db: Database, daemon: SchedulerDaemon, **kwargs):
        super().__init__(**kwargs)
        self.db = db
        self.daemon = daemon

    def compose(self) -> ComposeResult:
        restaurants = self.db.get_restaurants()

        with Container(id="modal-container"):
            yield Label("Schedule Reservation", id="modal-title")

            yield Label("Restaurant:")
            if restaurants:
                options = [(str(r.id), r.name) for r in restaurants]
                yield Select(options, id="restaurant-select")
            else:
                yield Label("[red]No restaurants saved. Add one first.[/red]")

            yield Label("Target Date (YYYY-MM-DD):")
            # Default to 2 weeks from now
            default_date = (date.today() + timedelta(days=14)).strftime("%Y-%m-%d")
            yield Input(
                placeholder="e.g., 2024-03-15",
                id="date-input",
                value=default_date,
            )

            yield Label("Party Size:")
            yield Input(
                placeholder="e.g., 2",
                id="party-input",
                value="2",
            )

            yield Label("Preferred Times (comma-separated, 24-hour, e.g., 19:00,19:30,20:00):")
            yield Input(
                placeholder="e.g., 19:00, 19:30, 20:00",
                id="times-input",
                value="19:00, 19:30, 20:00",
            )

            yield Label("Preferred Table Type (optional):")
            yield Input(
                placeholder="e.g., Dining Room",
                id="table-type-input",
            )

            yield Horizontal(
                Button("Schedule", id="schedule-btn", variant="success"),
                Button("Cancel", id="cancel-btn", variant="error"),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)

        elif event.button.id == "schedule-btn":
            try:
                restaurant_select = self.query_one("#restaurant-select", Select)
                restaurant_id = int(restaurant_select.value)

                date_str = self.query_one("#date-input", Input).value.strip()
                party_size = int(self.query_one("#party-input", Input).value.strip())
                times_str = self.query_one("#times-input", Input).value.strip()
                table_type = self.query_one("#table-type-input", Input).value.strip()

                # Parse date
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

                # Parse preferred times
                preferred_times = []
                if times_str:
                    for t in times_str.split(","):
                        t = t.strip()
                        pref = {"time": t}
                        if table_type:
                            pref["table_type"] = table_type
                        preferred_times.append(pref)

                # Get restaurant and calculate snipe time
                restaurant = self.db.get_restaurant(restaurant_id)
                if not restaurant:
                    self.app.notify("Restaurant not found", severity="error")
                    return

                snipe_at = self.db.calculate_snipe_time(restaurant, target_date)

                # Check if snipe time is in the future
                if snipe_at <= datetime.now():
                    self.app.notify(
                        f"Snipe time ({snipe_at}) is in the past. "
                        "Choose a later target date.",
                        severity="error",
                    )
                    return

                # Schedule the reservation
                reservation = self.db.schedule_reservation(
                    restaurant_id=restaurant_id,
                    target_date=target_date,
                    party_size=party_size,
                    snipe_at=snipe_at,
                    preferred_times=preferred_times,
                )

                # Add to scheduler if daemon is running
                if self.daemon.is_running():
                    self.daemon.schedule_job(reservation)

                self.dismiss(True)

            except ValueError as e:
                self.app.notify(f"Invalid input: {e}", severity="error")


class ReservationsScreen(Container):
    """Screen for managing scheduled reservations."""

    DEFAULT_CSS = """
    ReservationsScreen {
        layout: grid;
        grid-size: 1;
        padding: 1;
    }

    #toolbar {
        height: auto;
        dock: top;
        padding: 1;
    }

    #reservations-list {
        height: 100%;
        background: $surface;
        border: solid $primary;
        padding: 1;
    }

    .reservation-item {
        height: auto;
        background: $surface-lighten-1;
        border: solid $primary-darken-1;
        margin: 1 0;
        padding: 1;
    }

    .reservation-pending {
        border-left: thick $warning;
    }

    .reservation-success {
        border-left: thick $success;
    }

    .reservation-failed {
        border-left: thick $error;
    }
    """

    def __init__(self, db: Database, daemon: SchedulerDaemon, **kwargs):
        super().__init__(**kwargs)
        self.db = db
        self.daemon = daemon

    def compose(self) -> ComposeResult:
        with Container(id="toolbar"):
            yield Horizontal(
                Button("Schedule New", id="schedule-btn", variant="success"),
                Button("Refresh", id="refresh-btn", variant="primary"),
            )

        with ScrollableContainer(id="reservations-list"):
            yield Label("Scheduled Reservations", classes="section-header")
            yield Container(id="list-container")

    def on_mount(self) -> None:
        """Called when the widget is mounted."""
        self._load_reservations()

    def _load_reservations(self) -> None:
        """Load reservations into the list."""
        container = self.query_one("#list-container")
        container.remove_children()

        reservations = self.db.get_scheduled_reservations()

        if not reservations:
            container.mount(
                Label("No reservations scheduled. Click 'Schedule New' to create one.")
            )
            return

        for res in reservations:
            restaurant = self.db.get_restaurant(res.restaurant_id)
            restaurant_name = restaurant.name if restaurant else "Unknown"

            status_class = f"reservation-{res.status.value}"

            with Container(classes=f"reservation-item {status_class}") as item:
                item.mount(Label(f"[bold]{restaurant_name}[/bold]"))
                item.mount(Label(f"Date: {res.target_date.strftime('%Y-%m-%d')} | Party: {res.party_size}"))
                item.mount(Label(f"Snipe at: {res.snipe_at.strftime('%Y-%m-%d %H:%M')}"))
                item.mount(Label(f"Status: [{self._status_color(res.status)}]{res.status.value}[/{self._status_color(res.status)}]"))

                if res.preferred_times:
                    try:
                        times = json.loads(res.preferred_times)
                        times_str = ", ".join(t.get("time", "") for t in times)
                        item.mount(Label(f"Preferred times: {times_str}"))
                    except Exception:
                        pass

                if res.result_message:
                    item.mount(Label(f"Result: {res.result_message[:50]}..."))

                if res.booked_time:
                    item.mount(Label(f"[green]Booked: {res.booked_time}[/green]"))

                # Only show cancel for pending
                if res.status == ReservationStatus.PENDING:
                    item.mount(
                        Horizontal(
                            Button("Cancel", id=f"cancel-{res.id}", variant="error"),
                        )
                    )

                container.mount(item)

    def _status_color(self, status: ReservationStatus) -> str:
        """Get color for status."""
        colors = {
            ReservationStatus.PENDING: "yellow",
            ReservationStatus.IN_PROGRESS: "blue",
            ReservationStatus.SUCCESS: "green",
            ReservationStatus.FAILED: "red",
            ReservationStatus.CANCELLED: "dim",
        }
        return colors.get(status, "white")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        if button_id == "schedule-btn":
            restaurants = self.db.get_restaurants()
            if not restaurants:
                self.app.notify(
                    "Add a restaurant first before scheduling",
                    severity="error",
                )
                return

            self.app.push_screen(
                ScheduleReservationModal(self.db, self.daemon),
                callback=self._on_modal_dismiss,
            )

        elif button_id == "refresh-btn":
            self._load_reservations()
            self.app.notify("Refreshed", severity="information")

        elif button_id.startswith("cancel-"):
            reservation_id = int(button_id.split("-")[1])
            self.db.update_reservation_status(
                reservation_id,
                ReservationStatus.CANCELLED,
                result_message="Cancelled by user",
            )
            self.daemon.cancel_job(reservation_id)
            self._load_reservations()
            self.app.notify("Reservation cancelled", severity="warning")

    def _on_modal_dismiss(self, result) -> None:
        if result:
            self._load_reservations()
            self.app.notify("Reservation scheduled", severity="information")
