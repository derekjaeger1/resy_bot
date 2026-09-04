"""FastAPI web application for Resy Bot."""

import json
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from storage.database import Database
from storage.models import ReleasePattern, ReservationStatus, MonitorStatus, SnipeLogType
from resy.client import ResyClient, ResyClientError, login as resy_login, AuthenticationError, search_venues
from resy.models import ResyCredentials
from scheduler.daemon import SchedulerDaemon

try:
    from data.nyc_restaurants import NYC_RESTAURANTS
except ModuleNotFoundError:
    NYC_RESTAURANTS = []

# Paths
WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app(db: Database = None, daemon: SchedulerDaemon = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    if db is None:
        db = Database()

    if daemon is None:
        daemon = SchedulerDaemon(db)

    app = FastAPI(title="Resy Bot", description="Reservation Sniper")

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Templates
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Add custom template filters
    # Use Eastern Time for display (NYC)
    eastern = ZoneInfo("America/New_York")

    def to_eastern(dt):
        """Convert a naive UTC datetime to Eastern Time."""
        if dt is None:
            return None
        # Assume naive datetimes from DB are UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(eastern)

    @app.on_event("startup")
    async def setup_jinja():
        templates.env.filters["format_time"] = lambda t: t.strftime("%H:%M") if t else ""
        templates.env.filters["format_date"] = lambda d: d.strftime("%Y-%m-%d") if d else ""
        templates.env.filters["format_datetime"] = lambda dt: to_eastern(dt).strftime("%Y-%m-%d %H:%M") if dt else ""
        templates.env.filters["format_datetime_short"] = lambda dt: to_eastern(dt).strftime("%m/%d %H:%M") if dt else ""
        templates.env.filters["format_datetime_sec"] = lambda dt: to_eastern(dt).strftime("%m/%d %H:%M:%S") if dt else ""
        templates.env.filters["format_time_sec"] = lambda dt: to_eastern(dt).strftime("%H:%M:%S") if dt else ""

    # ==================== Dashboard ====================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Dashboard page."""
        # Get stats
        restaurants = db.get_restaurants()
        pending = db.get_scheduled_reservations(ReservationStatus.PENDING)
        upcoming = db.get_upcoming_snipes(hours_ahead=168)  # 1 week
        history = db.get_history(limit=10)
        active_monitors = db.get_active_monitors()

        # Get credentials status
        creds = db.get_credentials()

        # Create restaurant map for easy lookup
        restaurant_map = {r.id: r for r in restaurants}

        # Calculate next snipe info
        next_snipe = None
        next_snipe_restaurant = None
        if upcoming:
            next_snipe = upcoming[0]
            next_snipe_restaurant = restaurant_map.get(next_snipe.restaurant_id)

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "page": "dashboard",
            "daemon_running": daemon.is_running(),
            "restaurant_count": len(restaurants),
            "pending_count": len(pending),
            "monitor_count": len(active_monitors),
            "upcoming_snipes": upcoming[:5],  # Limit to 5 for display
            "history": history,
            "has_credentials": creds is not None,
            "restaurants": restaurants,
            "restaurant_map": restaurant_map,
            "next_snipe": next_snipe,
            "next_snipe_restaurant": next_snipe_restaurant,
            "now": datetime.now(),
        })

    @app.post("/daemon/start")
    async def start_daemon():
        """Start the scheduler daemon."""
        if not daemon.is_running():
            creds = db.get_credentials()
            if not creds:
                raise HTTPException(400, "No credentials configured")
            daemon.start()
        return RedirectResponse("/", status_code=303)

    @app.post("/daemon/stop")
    async def stop_daemon():
        """Stop the scheduler daemon."""
        if daemon.is_running():
            daemon.stop()
        return RedirectResponse("/", status_code=303)

    # ==================== Restaurants ====================

    @app.get("/restaurants", response_class=HTMLResponse)
    async def restaurants_page(request: Request):
        """Restaurants management page."""
        restaurants = db.get_restaurants()

        # Group preset restaurants by cuisine
        cuisines = {}
        for r in NYC_RESTAURANTS:
            cuisine = r.get("cuisine", "Other")
            if cuisine not in cuisines:
                cuisines[cuisine] = []
            cuisines[cuisine].append(r)

        return templates.TemplateResponse("restaurants.html", {
            "request": request,
            "page": "restaurants",
            "restaurants": restaurants,
            "preset_restaurants": NYC_RESTAURANTS,
            "preset_cuisines": cuisines,
        })

    @app.post("/restaurants/add")
    async def add_restaurant(
        name: str = Form(...),
        venue_id: int = Form(...),
        release_time: str = Form(...),
        days_in_advance: int = Form(...),
        release_pattern: str = Form(...),
        address: str = Form(""),
    ):
        """Add a new restaurant."""
        # Parse time
        parts = release_time.split(":")
        release_time_obj = time(int(parts[0]), int(parts[1]))

        pattern = ReleasePattern(release_pattern)

        db.add_restaurant(
            venue_id=venue_id,
            name=name,
            release_time=release_time_obj,
            days_in_advance=days_in_advance,
            release_pattern=pattern,
            address=address or None,
        )

        return RedirectResponse("/restaurants", status_code=303)

    @app.post("/restaurants/{restaurant_id}/delete")
    async def delete_restaurant(restaurant_id: int):
        """Delete a restaurant."""
        db.delete_restaurant(restaurant_id)
        return RedirectResponse("/restaurants", status_code=303)

    @app.post("/restaurants/{restaurant_id}/edit")
    async def edit_restaurant(
        restaurant_id: int,
        name: str = Form(...),
        venue_id: int = Form(...),
        release_time: str = Form(...),
        days_in_advance: int = Form(...),
        release_pattern: str = Form(...),
        address: str = Form(""),
    ):
        """Edit a restaurant."""
        parts = release_time.split(":")
        release_time_obj = time(int(parts[0]), int(parts[1]))
        pattern = ReleasePattern(release_pattern)

        db.update_restaurant(
            restaurant_id,
            name=name,
            venue_id=venue_id,
            release_time=release_time_obj,
            days_in_advance=days_in_advance,
            release_pattern=pattern,
            address=address or None,
        )

        return RedirectResponse("/restaurants", status_code=303)

    # ==================== Reservations ====================

    @app.get("/reservations", response_class=HTMLResponse)
    async def reservations_page(request: Request):
        """Reservations scheduling page."""
        reservations = db.get_scheduled_reservations()
        restaurants = db.get_restaurants()

        # Attach restaurant info to reservations
        restaurant_map = {r.id: r for r in restaurants}

        return templates.TemplateResponse("reservations.html", {
            "request": request,
            "page": "reservations",
            "reservations": reservations,
            "restaurants": restaurants,
            "restaurant_map": restaurant_map,
            "default_date": (date.today() + timedelta(days=14)).strftime("%Y-%m-%d"),
        })

    @app.post("/reservations/add")
    async def add_reservation(
        restaurant_id: int = Form(...),
        target_date: str = Form(...),
        party_size: int = Form(...),
        preferred_time: str = Form("19:00"),
        time_window: int = Form(60),
        table_type: str = Form(""),
    ):
        """Schedule a new reservation."""
        # Parse date
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()

        # Build time preference with window
        times_list = []
        if preferred_time:
            pref = {
                "time": preferred_time,
                "window_minutes": time_window,
            }
            if table_type:
                pref["table_type"] = table_type
            times_list.append(pref)

        # Get restaurant and calculate snipe time
        restaurant = db.get_restaurant(restaurant_id)
        if not restaurant:
            raise HTTPException(404, "Restaurant not found")

        snipe_at = db.calculate_snipe_time(restaurant, target_date_obj)

        if snipe_at <= datetime.now():
            raise HTTPException(400, f"Snipe time ({snipe_at}) is in the past")

        # Schedule
        reservation = db.schedule_reservation(
            restaurant_id=restaurant_id,
            target_date=target_date_obj,
            party_size=party_size,
            snipe_at=snipe_at,
            preferred_times=times_list,
        )

        # Add to daemon if running
        if daemon.is_running():
            daemon.schedule_job(reservation)

        return RedirectResponse("/reservations", status_code=303)

    @app.post("/reservations/{reservation_id}/cancel")
    async def cancel_reservation(reservation_id: int):
        """Cancel a scheduled reservation."""
        db.update_reservation_status(
            reservation_id,
            ReservationStatus.CANCELLED,
            result_message="Cancelled by user",
        )
        daemon.cancel_job(reservation_id)
        return RedirectResponse("/reservations", status_code=303)

    @app.post("/reservations/{reservation_id}/delete")
    async def delete_reservation(reservation_id: int):
        """Delete a reservation (for cancelled/failed/success status)."""
        db.delete_reservation(reservation_id)
        return RedirectResponse("/reservations", status_code=303)

    @app.post("/reservations/{reservation_id}/duplicate")
    async def duplicate_reservation(
        reservation_id: int,
        target_date: str = Form(...),
    ):
        """Duplicate a reservation with a new target date."""
        # Get the original reservation
        reservations = db.get_scheduled_reservations()
        original = next((r for r in reservations if r.id == reservation_id), None)
        if not original:
            raise HTTPException(404, "Reservation not found")

        # Parse new date
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()

        # Get restaurant to calculate snipe time
        restaurant = db.get_restaurant(original.restaurant_id)
        if not restaurant:
            raise HTTPException(404, "Restaurant not found")

        snipe_at = db.calculate_snipe_time(restaurant, target_date_obj)

        if snipe_at <= datetime.now():
            raise HTTPException(400, f"Snipe time ({snipe_at}) is in the past")

        # Parse original preferred times
        import json as json_module
        preferred_times = None
        if original.preferred_times:
            preferred_times = json_module.loads(original.preferred_times)

        # Create new reservation
        new_reservation = db.schedule_reservation(
            restaurant_id=original.restaurant_id,
            target_date=target_date_obj,
            party_size=original.party_size,
            snipe_at=snipe_at,
            preferred_times=preferred_times,
        )

        # Add to daemon if running
        if daemon.is_running():
            daemon.schedule_job(new_reservation)

        return RedirectResponse("/reservations", status_code=303)

    @app.get("/reservations/{reservation_id}/edit-form", response_class=HTMLResponse)
    async def get_edit_form(request: Request, reservation_id: int):
        """Get pre-filled edit form for htmx."""
        reservations = db.get_scheduled_reservations(ReservationStatus.PENDING)
        reservation = next((r for r in reservations if r.id == reservation_id), None)
        if not reservation:
            raise HTTPException(404, "Reservation not found or not pending")

        restaurant = db.get_restaurant(reservation.restaurant_id)

        # Parse preferred times
        preferred_time = "19:00"
        time_window = 60
        table_type = ""

        if reservation.preferred_times:
            import json as json_module
            try:
                prefs = json_module.loads(reservation.preferred_times)
                if prefs and len(prefs) > 0:
                    pref = prefs[0]
                    preferred_time = pref.get("time", "19:00")
                    time_window = pref.get("window_minutes", 60)
                    table_type = pref.get("table_type", "")
            except Exception:
                pass

        return templates.TemplateResponse("partials/edit_reservation_form.html", {
            "request": request,
            "reservation_id": reservation_id,
            "restaurant_name": restaurant.name if restaurant else "Unknown",
            "target_date": reservation.target_date.strftime("%Y-%m-%d"),
            "party_size": reservation.party_size,
            "preferred_time": preferred_time,
            "time_window": time_window,
            "table_type": table_type,
        })

    @app.post("/reservations/{reservation_id}/edit")
    async def edit_reservation(
        reservation_id: int,
        target_date: str = Form(...),
        party_size: int = Form(...),
        preferred_time: str = Form("19:00"),
        time_window: int = Form(60),
        table_type: str = Form(""),
    ):
        """Edit a pending reservation."""
        # Parse date
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()

        # Build time preference
        times_list = []
        if preferred_time:
            pref = {
                "time": preferred_time,
                "window_minutes": time_window,
            }
            if table_type:
                pref["table_type"] = table_type
            times_list.append(pref)

        # Get reservation to find restaurant
        reservations = db.get_scheduled_reservations(ReservationStatus.PENDING)
        reservation = next((r for r in reservations if r.id == reservation_id), None)
        if not reservation:
            raise HTTPException(404, "Reservation not found or not pending")

        restaurant = db.get_restaurant(reservation.restaurant_id)
        if not restaurant:
            raise HTTPException(404, "Restaurant not found")

        # Recalculate snipe time for new date
        snipe_at = db.calculate_snipe_time(restaurant, target_date_obj)

        if snipe_at <= datetime.now():
            raise HTTPException(400, f"Snipe time ({snipe_at}) is in the past")

        # Update the reservation
        db.update_reservation(
            reservation_id,
            target_date=target_date_obj,
            party_size=party_size,
            snipe_at=snipe_at,
            preferred_times=times_list,
        )

        # Reschedule in daemon
        if daemon.is_running():
            daemon.cancel_job(reservation_id)
            updated = db.get_scheduled_reservations(ReservationStatus.PENDING)
            updated_res = next((r for r in updated if r.id == reservation_id), None)
            if updated_res:
                daemon.schedule_job(updated_res)

        return RedirectResponse("/reservations", status_code=303)

    # ==================== Settings ====================

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, message: str = None, error: str = None):
        """Settings page."""
        creds = db.get_credentials()
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "page": "settings",
            "credentials": creds,
            "message": message,
            "error": error,
        })

    @app.post("/settings/credentials")
    async def save_credentials(
        api_key: str = Form(...),
        auth_token: str = Form(...),
        payment_method_id: str = Form(""),
        email: str = Form(""),
    ):
        """Save credentials."""
        payment_id = int(payment_method_id) if payment_method_id else None

        db.save_credentials(
            api_key=api_key,
            auth_token=auth_token,
            payment_method_id=payment_id,
            email=email or None,
        )

        daemon.refresh_credentials()

        return RedirectResponse("/settings?message=Credentials saved!", status_code=303)

    @app.post("/settings/test")
    async def test_credentials(
        api_key: str = Form(...),
        auth_token: str = Form(...),
    ):
        """Test credentials."""
        try:
            creds = ResyCredentials(api_key=api_key, auth_token=auth_token)
            with ResyClient(creds) as client:
                if client.test_credentials():
                    user_info = client.get_user_info()
                    name = user_info.get("first_name", "User")
                    return RedirectResponse(
                        f"/settings?message=Success! Connected as {name}",
                        status_code=303,
                    )
                else:
                    return RedirectResponse(
                        "/settings?error=Invalid credentials",
                        status_code=303,
                    )
        except Exception as e:
            return RedirectResponse(
                f"/settings?error=Connection failed: {str(e)[:50]}",
                status_code=303,
            )

    @app.post("/settings/clear")
    async def clear_credentials():
        """Clear credentials."""
        db.delete_credentials()
        return RedirectResponse("/settings?message=Credentials cleared", status_code=303)

    @app.post("/settings/login")
    async def login_with_password(
        email: str = Form(...),
        password: str = Form(...),
    ):
        """Log in with email/password to get credentials automatically."""
        try:
            creds = resy_login(email, password)

            # Save the credentials
            db.save_credentials(
                api_key=creds.api_key,
                auth_token=creds.auth_token,
                payment_method_id=creds.payment_method_id,
                email=email,
            )

            daemon.refresh_credentials()

            return RedirectResponse(
                f"/settings?message=Logged in successfully!",
                status_code=303,
            )
        except AuthenticationError as e:
            return RedirectResponse(
                f"/settings?error={str(e)}",
                status_code=303,
            )
        except Exception as e:
            return RedirectResponse(
                f"/settings?error=Login failed: {str(e)[:50]}",
                status_code=303,
            )

    # ==================== Availability Checker ====================

    @app.get("/availability", response_class=HTMLResponse)
    async def availability_page(request: Request):
        """Availability checker page."""
        restaurants = db.get_restaurants()
        return templates.TemplateResponse("availability.html", {
            "request": request,
            "page": "availability",
            "restaurants": restaurants,
            "results": None,
            "search_date": (date.today() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "search_time": "19:00",
            "search_window": 120,
            "party_size": 2,
        })

    @app.post("/availability/check", response_class=HTMLResponse)
    async def check_availability(
        request: Request,
        restaurant_ids: list[int] = Form(...),
        search_date: str = Form(...),
        search_time: str = Form("19:00"),
        time_window: int = Form(120),
        party_size: int = Form(2),
    ):
        """Check availability across multiple restaurants."""
        restaurants = db.get_restaurants()
        selected_restaurants = [r for r in restaurants if r.id in restaurant_ids]

        # Get credentials
        creds_db = db.get_credentials()
        if not creds_db:
            return templates.TemplateResponse("availability.html", {
                "request": request,
                "page": "availability",
                "restaurants": restaurants,
                "results": None,
                "error": "No credentials configured. Go to Settings to log in.",
                "search_date": search_date,
                "search_time": search_time,
                "search_window": time_window,
                "party_size": party_size,
            })

        creds = ResyCredentials(
            api_key=creds_db.api_key,
            auth_token=creds_db.auth_token,
            payment_method_id=creds_db.payment_method_id,
        )

        # Parse preferred time for filtering
        time_parts = search_time.split(":")
        pref_minutes = int(time_parts[0]) * 60 + int(time_parts[1])

        results = []
        with ResyClient(creds) as client:
            for restaurant in selected_restaurants:
                try:
                    slots = client.find_slots(
                        venue_id=restaurant.venue_id,
                        party_size=party_size,
                        date=search_date,
                    )

                    # Filter slots within time window
                    matching_slots = []
                    for slot in slots:
                        try:
                            # Parse slot time
                            slot_time_str = slot.time_slot
                            if " " in slot_time_str and "-" in slot_time_str.split(" ")[0]:
                                slot_time_str = slot_time_str.split(" ")[1]
                            parts = slot_time_str.split(":")
                            slot_minutes = int(parts[0]) * 60 + int(parts[1])

                            diff = abs(slot_minutes - pref_minutes)
                            if diff <= time_window:
                                matching_slots.append({
                                    "time": slot.time_slot,
                                    "table_type": slot.table_type or "Any",
                                    "diff_minutes": diff,
                                })
                        except Exception:
                            continue

                    # Sort by time difference
                    matching_slots.sort(key=lambda x: x["diff_minutes"])

                    results.append({
                        "restaurant": restaurant,
                        "slots": matching_slots,
                        "total_slots": len(slots),
                        "error": None,
                    })
                except Exception as e:
                    results.append({
                        "restaurant": restaurant,
                        "slots": [],
                        "total_slots": 0,
                        "error": str(e),
                    })

        return templates.TemplateResponse("availability.html", {
            "request": request,
            "page": "availability",
            "restaurants": restaurants,
            "results": results,
            "search_date": search_date,
            "search_time": search_time,
            "search_window": time_window,
            "party_size": party_size,
            "selected_ids": restaurant_ids,
        })

    # ==================== Cancellation Monitors ====================

    @app.get("/monitors", response_class=HTMLResponse)
    async def monitors_page(request: Request):
        """Cancellation monitors management page."""
        monitors = db.get_monitors()
        restaurants = db.get_restaurants()
        restaurant_map = {r.id: r for r in restaurants}

        return templates.TemplateResponse("monitors.html", {
            "request": request,
            "page": "monitors",
            "monitors": monitors,
            "restaurants": restaurants,
            "restaurant_map": restaurant_map,
            "default_date": (date.today() + timedelta(days=7)).strftime("%Y-%m-%d"),
        })

    @app.post("/monitors/add")
    async def add_monitor(
        restaurant_id: int = Form(...),
        target_date: str = Form(...),
        party_size: int = Form(2),
        preferred_time: str = Form("19:00"),
        time_window: int = Form(120),
        table_type: str = Form(""),
    ):
        """Add a new cancellation monitor."""
        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()

        if target_date_obj < date.today():
            raise HTTPException(400, "Target date cannot be in the past")

        db.add_monitor(
            restaurant_id=restaurant_id,
            target_date=target_date_obj,
            party_size=party_size,
            preferred_time=preferred_time,
            time_window=time_window,
            table_type=table_type or None,
        )

        return RedirectResponse("/monitors", status_code=303)

    @app.post("/monitors/{monitor_id}/cancel")
    async def cancel_monitor(monitor_id: int):
        """Cancel an active monitor."""
        db.cancel_monitor(monitor_id)
        return RedirectResponse("/monitors", status_code=303)

    @app.post("/monitors/{monitor_id}/delete")
    async def delete_monitor(monitor_id: int):
        """Delete a monitor."""
        db.delete_monitor(monitor_id)
        return RedirectResponse("/monitors", status_code=303)

    # ==================== Snipe Logs ====================

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, restaurant: str = None):
        """Snipe logs page for debugging."""
        logs = db.get_snipe_logs(limit=500)
        restaurants = db.get_restaurants()

        # Filter by restaurant if specified
        if restaurant:
            logs = [l for l in logs if l.restaurant_name == restaurant]

        # Get unique restaurant names for filter dropdown
        all_restaurant_names = sorted(set(l.restaurant_name for l in db.get_snipe_logs(limit=500)))

        # Group logs by snipe (reservation_id + restaurant + target_date)
        # Each group represents one snipe attempt
        from collections import OrderedDict
        grouped_logs = OrderedDict()
        for log in logs:
            # Create a key for grouping - use reservation_id if available, else combo
            if log.reservation_id:
                key = f"{log.reservation_id}"
            else:
                key = f"{log.restaurant_name}_{log.target_date}"

            if key not in grouped_logs:
                grouped_logs[key] = {
                    "restaurant_name": log.restaurant_name,
                    "target_date": log.target_date,
                    "reservation_id": log.reservation_id,
                    "logs": [],
                    "first_timestamp": log.timestamp,
                    "last_timestamp": log.timestamp,
                    "success": False,
                    "failure": False,
                    "total_elapsed_ms": 0,
                }
            grouped_logs[key]["logs"].append(log)
            grouped_logs[key]["last_timestamp"] = log.timestamp
            if log.log_type == SnipeLogType.SUCCESS:
                grouped_logs[key]["success"] = True
            if log.log_type == SnipeLogType.FAILURE:
                grouped_logs[key]["failure"] = True
            if log.elapsed_ms:
                grouped_logs[key]["total_elapsed_ms"] = max(
                    grouped_logs[key]["total_elapsed_ms"], log.elapsed_ms
                )

        # Sort logs within each group by timestamp (oldest first for reading order)
        for key in grouped_logs:
            grouped_logs[key]["logs"].sort(key=lambda x: x.timestamp)

        return templates.TemplateResponse("logs.html", {
            "request": request,
            "page": "logs",
            "grouped_logs": grouped_logs,
            "all_restaurant_names": all_restaurant_names,
            "selected_restaurant": restaurant,
            "SnipeLogType": SnipeLogType,
        })

    @app.post("/logs/clear")
    async def clear_logs(days: int = Form(7)):
        """Clear old snipe logs."""
        deleted = db.clear_old_snipe_logs(days=days)
        return RedirectResponse(f"/logs?message=Cleared {deleted} logs", status_code=303)

    # ==================== My Reservations ====================

    @app.get("/my-reservations", response_class=HTMLResponse)
    async def my_reservations_page(request: Request, message: str = None, error: str = None):
        """Page showing user's Resy reservations with cancel option."""
        creds = db.get_credentials()
        reservations = []
        has_credentials = False

        if creds and creds.auth_token:
            has_credentials = True
            try:
                resy_creds = ResyCredentials(
                    api_key=creds.api_key,
                    auth_token=creds.auth_token,
                    payment_method_id=creds.payment_method_id,
                )
                client = ResyClient(resy_creds)
                reservations = client.get_reservations()
                client.close()
            except Exception as e:
                error = f"Failed to fetch reservations: {str(e)}"

        return templates.TemplateResponse("my_reservations.html", {
            "request": request,
            "page": "my-reservations",
            "reservations": reservations,
            "has_credentials": has_credentials,
            "message": message,
            "error": error,
        })

    @app.post("/my-reservations/cancel")
    async def cancel_my_reservation(resy_token: str = Form(...)):
        """Cancel a Resy reservation."""
        creds = db.get_credentials()

        if not creds or not creds.auth_token:
            return RedirectResponse("/my-reservations?error=No credentials configured", status_code=303)

        try:
            resy_creds = ResyCredentials(
                api_key=creds.api_key,
                auth_token=creds.auth_token,
                payment_method_id=creds.payment_method_id,
            )
            client = ResyClient(resy_creds)
            client.cancel_reservation(resy_token)
            client.close()
            return RedirectResponse("/my-reservations?message=Reservation cancelled successfully", status_code=303)
        except ResyClientError as e:
            return RedirectResponse(f"/my-reservations?error={str(e)}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/my-reservations?error=Failed to cancel: {str(e)}", status_code=303)

    # ==================== API Endpoints ====================

    @app.get("/api/status")
    async def api_status():
        """Get current status."""
        # Calculate next snipe time
        upcoming = db.get_upcoming_snipes(hours_ahead=168)  # 1 week
        next_snipe = None
        next_snipe_restaurant = None
        if upcoming:
            next_snipe = upcoming[0].snipe_at
            restaurant = db.get_restaurant(upcoming[0].restaurant_id)
            next_snipe_restaurant = restaurant.name if restaurant else "Unknown"

        return {
            "daemon_running": daemon.is_running(),
            "pending_count": len(db.get_pending_reservations()),
            "restaurant_count": len(db.get_restaurants()),
            "monitor_count": len(db.get_active_monitors()),
            "next_snipe": next_snipe.isoformat() if next_snipe else None,
            "next_snipe_restaurant": next_snipe_restaurant,
        }

    @app.get("/api/search")
    async def api_search_venues(q: str):
        """Search for venues by name."""
        if not q or len(q) < 2:
            return {"venues": []}
        venues = search_venues(q)
        return {"venues": venues}

    return app
