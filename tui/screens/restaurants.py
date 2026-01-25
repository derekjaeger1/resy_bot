"""Restaurants management screen."""

from datetime import time
from textual.app import ComposeResult
from textual.widgets import Static, Label, Button, Input, Select
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen

from storage.database import Database
from storage.models import Restaurant, ReleasePattern
from ..widgets.components import RestaurantCard


class AddRestaurantModal(ModalScreen):
    """Modal for adding a new restaurant."""

    DEFAULT_CSS = """
    AddRestaurantModal {
        align: center middle;
    }

    #modal-container {
        width: 60;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 2;
    }

    #modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .form-row {
        height: auto;
        margin: 1 0;
    }

    .form-label {
        width: 20;
    }

    Input {
        width: 100%;
    }
    """

    def __init__(self, db: Database, restaurant: Restaurant = None, **kwargs):
        super().__init__(**kwargs)
        self.db = db
        self.restaurant = restaurant
        self.is_edit = restaurant is not None

    def compose(self) -> ComposeResult:
        with Container(id="modal-container"):
            title = "Edit Restaurant" if self.is_edit else "Add Restaurant"
            yield Label(title, id="modal-title")

            yield Label("Restaurant Name:")
            yield Input(
                placeholder="e.g., Carbone",
                id="name-input",
                value=self.restaurant.name if self.is_edit else "",
            )

            yield Label("Venue ID:")
            yield Input(
                placeholder="e.g., 12345",
                id="venue-input",
                value=str(self.restaurant.venue_id) if self.is_edit else "",
            )

            yield Label("Release Time (HH:MM, 24-hour):")
            yield Input(
                placeholder="e.g., 09:00",
                id="time-input",
                value=(
                    self.restaurant.release_time.strftime("%H:%M")
                    if self.is_edit else "09:00"
                ),
            )

            yield Label("Days in Advance:")
            yield Input(
                placeholder="e.g., 14",
                id="days-input",
                value=str(self.restaurant.days_in_advance) if self.is_edit else "14",
            )

            yield Label("Release Pattern:")
            yield Select(
                [(ReleasePattern.DAILY.value, "Daily"), (ReleasePattern.WEEKLY.value, "Weekly")],
                id="pattern-select",
                value=(
                    self.restaurant.release_pattern.value
                    if self.is_edit else ReleasePattern.DAILY.value
                ),
            )

            yield Label("Address (optional):")
            yield Input(
                placeholder="e.g., 181 Thompson St, New York",
                id="address-input",
                value=self.restaurant.address if self.is_edit else "",
            )

            yield Horizontal(
                Button("Save", id="save-btn", variant="success"),
                Button("Cancel", id="cancel-btn", variant="error"),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)

        elif event.button.id == "save-btn":
            try:
                name = self.query_one("#name-input", Input).value.strip()
                venue_id = int(self.query_one("#venue-input", Input).value.strip())
                time_str = self.query_one("#time-input", Input).value.strip()
                days = int(self.query_one("#days-input", Input).value.strip())
                pattern_val = self.query_one("#pattern-select", Select).value
                address = self.query_one("#address-input", Input).value.strip()

                # Parse time
                parts = time_str.split(":")
                release_time = time(int(parts[0]), int(parts[1]))

                # Get pattern
                pattern = ReleasePattern(pattern_val)

                if not name or not venue_id:
                    self.app.notify("Name and Venue ID are required", severity="error")
                    return

                if self.is_edit:
                    self.db.update_restaurant(
                        self.restaurant.id,
                        name=name,
                        venue_id=venue_id,
                        release_time=release_time,
                        days_in_advance=days,
                        release_pattern=pattern,
                        address=address or None,
                    )
                else:
                    self.db.add_restaurant(
                        venue_id=venue_id,
                        name=name,
                        release_time=release_time,
                        days_in_advance=days,
                        release_pattern=pattern,
                        address=address or None,
                    )

                self.dismiss(True)

            except ValueError as e:
                self.app.notify(f"Invalid input: {e}", severity="error")


class RestaurantsScreen(Container):
    """Screen for managing restaurants."""

    DEFAULT_CSS = """
    RestaurantsScreen {
        layout: grid;
        grid-size: 1;
        padding: 1;
    }

    #toolbar {
        height: auto;
        dock: top;
        padding: 1;
    }

    #restaurants-list {
        height: 100%;
        background: $surface;
        border: solid $primary;
        padding: 1;
    }

    .restaurant-item {
        height: auto;
        background: $surface-lighten-1;
        border: solid $primary-darken-1;
        margin: 1 0;
        padding: 1;
    }

    .restaurant-item:hover {
        background: $surface-lighten-2;
    }
    """

    def __init__(self, db: Database, **kwargs):
        super().__init__(**kwargs)
        self.db = db

    def compose(self) -> ComposeResult:
        with Container(id="toolbar"):
            yield Horizontal(
                Button("Add Restaurant", id="add-btn", variant="success"),
                Button("Refresh", id="refresh-btn", variant="primary"),
            )

        with ScrollableContainer(id="restaurants-list"):
            yield Label("Saved Restaurants", classes="section-header")
            yield Container(id="list-container")

    def on_mount(self) -> None:
        """Called when the widget is mounted."""
        self._load_restaurants()

    def _load_restaurants(self) -> None:
        """Load restaurants into the list."""
        container = self.query_one("#list-container")
        container.remove_children()

        restaurants = self.db.get_restaurants()

        if not restaurants:
            container.mount(Label("No restaurants saved. Click 'Add Restaurant' to add one."))
            return

        for r in restaurants:
            with Container(classes="restaurant-item") as item:
                item.mount(Label(f"[bold]{r.name}[/bold]"))
                item.mount(Label(f"Venue ID: {r.venue_id}"))
                item.mount(
                    Label(
                        f"Releases: {r.release_time.strftime('%H:%M')} | "
                        f"{r.days_in_advance} days ahead | "
                        f"{r.release_pattern.value}"
                    )
                )
                if r.address:
                    item.mount(Label(f"Address: {r.address}"))
                item.mount(
                    Horizontal(
                        Button("Edit", id=f"edit-{r.id}", variant="primary"),
                        Button("Delete", id=f"delete-{r.id}", variant="error"),
                    )
                )
                container.mount(item)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        if button_id == "add-btn":
            self.app.push_screen(
                AddRestaurantModal(self.db),
                callback=self._on_modal_dismiss,
            )

        elif button_id == "refresh-btn":
            self._load_restaurants()
            self.app.notify("Refreshed", severity="information")

        elif button_id.startswith("edit-"):
            restaurant_id = int(button_id.split("-")[1])
            restaurant = self.db.get_restaurant(restaurant_id)
            if restaurant:
                self.app.push_screen(
                    AddRestaurantModal(self.db, restaurant),
                    callback=self._on_modal_dismiss,
                )

        elif button_id.startswith("delete-"):
            restaurant_id = int(button_id.split("-")[1])
            self.db.delete_restaurant(restaurant_id)
            self._load_restaurants()
            self.app.notify("Restaurant deleted", severity="warning")

    def _on_modal_dismiss(self, result) -> None:
        if result:
            self._load_restaurants()
            self.app.notify("Restaurant saved", severity="information")
