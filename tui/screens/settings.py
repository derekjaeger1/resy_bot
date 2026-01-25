"""Settings/credentials screen."""

from textual.app import ComposeResult
from textual.widgets import Static, Label, Button, Input
from textual.containers import Container, Horizontal, Vertical

from storage.database import Database
from resy.client import ResyClient
from resy.models import ResyCredentials
from scheduler.daemon import SchedulerDaemon


class SettingsScreen(Container):
    """Screen for managing credentials and settings."""

    DEFAULT_CSS = """
    SettingsScreen {
        layout: grid;
        grid-size: 1;
        padding: 1;
    }

    #credentials-section {
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 2;
        margin-bottom: 1;
    }

    #help-section {
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 2;
    }

    .section-header {
        text-style: bold;
        margin-bottom: 1;
    }

    Input {
        margin: 1 0;
    }

    .help-text {
        color: $text-muted;
    }
    """

    def __init__(self, db: Database, daemon: SchedulerDaemon, **kwargs):
        super().__init__(**kwargs)
        self.db = db
        self.daemon = daemon

    def compose(self) -> ComposeResult:
        creds = self.db.get_credentials()

        with Container(id="credentials-section"):
            yield Label("Resy API Credentials", classes="section-header")

            yield Label("API Key:")
            yield Input(
                placeholder="Paste your API key here",
                id="api-key-input",
                value=creds.api_key if creds else "",
                password=True,
            )

            yield Label("Auth Token:")
            yield Input(
                placeholder="Paste your auth token here",
                id="auth-token-input",
                value=creds.auth_token if creds else "",
                password=True,
            )

            yield Label("Payment Method ID (optional):")
            yield Input(
                placeholder="e.g., 12345",
                id="payment-id-input",
                value=str(creds.payment_method_id) if creds and creds.payment_method_id else "",
            )

            yield Label("Email (for reference):")
            yield Input(
                placeholder="your@email.com",
                id="email-input",
                value=creds.email if creds else "",
            )

            yield Horizontal(
                Button("Save Credentials", id="save-btn", variant="success"),
                Button("Test Connection", id="test-btn", variant="primary"),
                Button("Clear", id="clear-btn", variant="error"),
            )

            yield Label("", id="status-label")

        with Container(id="help-section"):
            yield Label("How to Get Credentials", classes="section-header")
            yield Label(
                "1. Log into resy.com in your browser",
                classes="help-text",
            )
            yield Label(
                "2. Open Developer Tools (F12 or Cmd+Option+I)",
                classes="help-text",
            )
            yield Label(
                "3. Go to Network tab",
                classes="help-text",
            )
            yield Label(
                "4. Navigate to any restaurant page",
                classes="help-text",
            )
            yield Label(
                "5. Look for requests to api.resy.com",
                classes="help-text",
            )
            yield Label(
                "6. Find headers:",
                classes="help-text",
            )
            yield Label(
                "   - Authorization: Copy after 'ResyAPI api_key=\"'",
                classes="help-text",
            )
            yield Label(
                "   - X-Resy-Auth-Token: Copy the full value",
                classes="help-text",
            )
            yield Label("", classes="help-text")
            yield Label(
                "To find Venue IDs:",
                classes="help-text",
            )
            yield Label(
                "Look at the venue_id parameter in /find or /config requests",
                classes="help-text",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        status_label = self.query_one("#status-label", Label)

        if button_id == "save-btn":
            api_key = self.query_one("#api-key-input", Input).value.strip()
            auth_token = self.query_one("#auth-token-input", Input).value.strip()
            payment_id_str = self.query_one("#payment-id-input", Input).value.strip()
            email = self.query_one("#email-input", Input).value.strip()

            if not api_key or not auth_token:
                status_label.update("[red]API Key and Auth Token are required[/red]")
                return

            payment_id = int(payment_id_str) if payment_id_str else None

            self.db.save_credentials(
                api_key=api_key,
                auth_token=auth_token,
                payment_method_id=payment_id,
                email=email or None,
            )

            # Refresh daemon credentials
            self.daemon.refresh_credentials()

            status_label.update("[green]Credentials saved![/green]")
            self.app.notify("Credentials saved", severity="information")

        elif button_id == "test-btn":
            api_key = self.query_one("#api-key-input", Input).value.strip()
            auth_token = self.query_one("#auth-token-input", Input).value.strip()

            if not api_key or not auth_token:
                status_label.update("[red]Enter credentials first[/red]")
                return

            status_label.update("[yellow]Testing...[/yellow]")

            try:
                creds = ResyCredentials(api_key=api_key, auth_token=auth_token)
                with ResyClient(creds) as client:
                    if client.test_credentials():
                        # Get user info
                        user_info = client.get_user_info()
                        name = user_info.get("first_name", "User")
                        status_label.update(f"[green]Success! Connected as {name}[/green]")
                        self.app.notify("Connection successful!", severity="information")
                    else:
                        status_label.update("[red]Invalid credentials[/red]")
                        self.app.notify("Invalid credentials", severity="error")
            except Exception as e:
                status_label.update(f"[red]Error: {str(e)[:50]}[/red]")
                self.app.notify(f"Connection failed: {e}", severity="error")

        elif button_id == "clear-btn":
            self.db.delete_credentials()
            self.query_one("#api-key-input", Input).value = ""
            self.query_one("#auth-token-input", Input).value = ""
            self.query_one("#payment-id-input", Input).value = ""
            self.query_one("#email-input", Input).value = ""
            status_label.update("[yellow]Credentials cleared[/yellow]")
            self.app.notify("Credentials cleared", severity="warning")
