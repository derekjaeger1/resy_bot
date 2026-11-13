"""Mobile-first FastAPI web app for Resy Bot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app_time import now_local_naive
from config import (
    APP_PASSWORD,
    APP_SECRET_KEY,
    APP_TIMEZONE,
    COOKIE_SECURE,
    DATA_DIR,
    SESSION_COOKIE_NAME,
)
from resy.client import AuthenticationError, ResyClient, login as resy_login, search_venues
from resy.models import ResyCredentials
from scheduler.daemon import SchedulerDaemon
from storage.database import Database
from storage.models import ReleasePattern, ReservationStatus, MonitorStatus

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _session_secret() -> bytes:
    seed = APP_SECRET_KEY or APP_PASSWORD or "local-development-only"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _make_session_token() -> str:
    payload = "resybot:v2"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, _make_session_token())


def _redirect(path: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append(f"message={quote_plus(message)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    if params:
        path = f"{path}?{'&'.join(params)}"
    return RedirectResponse(path, status_code=303)


def _fmt_fire(dt: datetime) -> str:
    return dt.strftime("%a, %b %-d at %-I:%M %p")


def _parse_preferences(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def create_app(db: Database | None = None, daemon: SchedulerDaemon | None = None) -> FastAPI:
    db = db or Database()
    daemon = daemon or SchedulerDaemon(db)

    def _watch_matches_snipe(monitor, reservation) -> bool:
        if (
            monitor.restaurant_id != reservation.restaurant_id
            or monitor.target_date != reservation.target_date
            or monitor.party_size != reservation.party_size
        ):
            return False
        prefs = _parse_preferences(reservation.preferred_times)
        return bool(prefs and prefs[0].get("time") == monitor.preferred_time)

    def _on_snipe_complete(reservation, result) -> None:
        # If the high-speed drop snipe books first, shut off its paired watch
        # so the user can never be booked twice for the same table.
        if not result.success:
            return
        for monitor in db.get_active_monitors():
            if _watch_matches_snipe(monitor, reservation):
                db.cancel_monitor(monitor.id)

    daemon.on_snipe_complete = _on_snipe_complete

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # One-time cleanup of an old V2 default we previously saved for
        # Straker's. Resy's current published rule is two weeks at midnight.
        # Recalculate any still-pending legacy drop job so the old 9 AM value
        # cannot fire later.
        for restaurant in db.get_restaurants():
            if "straker" in restaurant.name.lower() and restaurant.days_in_advance == 14:
                corrected = restaurant
                if restaurant.release_time != dt_time(0, 0):
                    corrected = db.update_restaurant(restaurant.id, release_time=dt_time(0, 0))
                if corrected:
                    for reservation in db.get_pending_reservations():
                        if reservation.restaurant_id != corrected.id:
                            continue
                        corrected_fire = db.calculate_snipe_time(corrected, reservation.target_date)
                        if corrected_fire > now_local_naive():
                            db.update_reservation(reservation.id, snipe_at=corrected_fire)
                        else:
                            db.update_reservation_status(
                                reservation.id,
                                ReservationStatus.CANCELLED,
                                result_message="Cancelled legacy job after correcting Straker's release time",
                            )

        # V3 migration: the Watch record is now the source of truth.
        # Cancel any legacy scheduled snipe that is not paired with an active
        # watch so an old hidden job can never book unexpectedly.
        active_watches = db.get_active_monitors()
        for reservation in db.get_pending_reservations():
            if not any(_watch_matches_snipe(monitor, reservation) for monitor in active_watches):
                db.update_reservation_status(
                    reservation.id,
                    ReservationStatus.CANCELLED,
                    result_message="Cancelled legacy unpaired snipe during Watch migration",
                )

        if not daemon.is_running():
            daemon.start()
        try:
            yield
        finally:
            if daemon.is_running():
                daemon.stop()

    app = FastAPI(title="Reservation Sniper", docs_url=None, redoc_url=None, lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def clock(value):
        try:
            raw = str(value).split(" ")[-1][:5]
            return datetime.strptime(raw, "%H:%M").strftime("%-I:%M %p")
        except Exception:
            return str(value)

    templates.env.filters["clock"] = clock

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        public = (
            request.url.path == "/health"
            or request.url.path == "/login"
            or request.url.path == "/manifest.webmanifest"
            or request.url.path.startswith("/static/")
        )
        if public:
            return await call_next(request)
        if not APP_PASSWORD:
            return RedirectResponse("/login?setup=1", status_code=303)
        if not _valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    def common_context(request: Request, page: str, **kwargs):
        return {
            "request": request,
            "page": page,
            "timezone": APP_TIMEZONE,
            "daemon_running": daemon.is_running(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            **kwargs,
        }

    @app.get("/health")
    async def health():
        db.get_restaurants()
        return {
            "status": "ok",
            "scheduler": "running" if daemon.is_running() else "starting",
            "timezone": APP_TIMEZONE,
            "persistent_volume": bool(os.getenv("RAILWAY_VOLUME_MOUNT_PATH")),
        }

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse({
            "name": "Reservation Sniper",
            "short_name": "Sniper",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0b0c10",
            "theme_color": "#0b0c10",
            "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }, media_type="application/manifest+json")

    @app.get("/login", response_class=HTMLResponse)
    async def app_login_page(request: Request):
        if APP_PASSWORD and _valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "setup_required": not bool(APP_PASSWORD),
                "bad_password": request.query_params.get("bad") == "1",
            },
        )

    @app.post("/login")
    async def app_login(password: str = Form(...)):
        if not APP_PASSWORD:
            return RedirectResponse("/login?setup=1", status_code=303)
        if not hmac.compare_digest(password, APP_PASSWORD):
            return RedirectResponse("/login?bad=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            _make_session_token(),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def app_logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        restaurants = db.get_restaurants()
        restaurant_map = {r.id: r for r in restaurants}
        reservations = db.get_scheduled_reservations()
        upcoming = [r for r in reservations if r.status in {ReservationStatus.PENDING, ReservationStatus.IN_PROGRESS}]
        recent = [r for r in reversed(reservations) if r.status not in {ReservationStatus.PENDING, ReservationStatus.IN_PROGRESS}][:8]
        pref_map = {r.id: _parse_preferences(r.preferred_times) for r in reservations}

        monitors = db.get_monitors()
        active_monitors = [m for m in monitors if m.status == MonitorStatus.ACTIVE]
        recent_monitors = [m for m in monitors if m.status != MonitorStatus.ACTIVE][:8]

        drop_boost = {}
        for monitor in active_monitors:
            for reservation in upcoming:
                if _watch_matches_snipe(monitor, reservation):
                    drop_boost[monitor.id] = reservation
                    break

        # Keep legacy orphan snipes visible so nothing can run silently.
        paired_snipe_ids = {r.id for r in drop_boost.values()}
        orphan_snipes = [r for r in upcoming if r.id not in paired_snipe_ids]

        creds = db.get_credentials()
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=common_context(
                request,
                "home",
                active_monitors=active_monitors,
                recent_monitors=recent_monitors,
                drop_boost=drop_boost,
                orphan_snipes=orphan_snipes,
                recent=recent,
                restaurant_map=restaurant_map,
                pref_map=pref_map,
                has_credentials=bool(creds),
                credential_email=creds.email if creds else None,
                has_volume=bool(os.getenv("RAILWAY_VOLUME_MOUNT_PATH")),
                now=now_local_naive(),
            ),
        )

    @app.get("/new", response_class=HTMLResponse)
    async def new_snipe(request: Request):
        restaurants = db.get_restaurants()
        time_slots = []
        # Resy-like exact-time bricks from 11:00 AM through 11:45 PM.
        for total_minutes in range(11 * 60, 24 * 60, 15):
            hour, minute = divmod(total_minutes, 60)
            value = f"{hour:02d}:{minute:02d}"
            label = datetime.strptime(value, "%H:%M").strftime("%-I:%M %p")
            time_slots.append({"value": value, "label": label})
        return templates.TemplateResponse(
            request=request,
            name="new_snipe.html",
            context=common_context(
                request,
                "new",
                restaurants=restaurants,
                time_slots=time_slots,
                today=now_local_naive().date().isoformat(),
                has_credentials=bool(db.get_credentials()),
            ),
        )

    @app.get("/api/venues/search")
    async def venue_search(q: str = ""):
        q = q.strip()
        if len(q) < 2:
            return []
        results = search_venues(q)[:8]
        return [{
            "id": item.get("id"),
            "name": item.get("name") or "Unknown",
            "neighborhood": item.get("neighborhood") or "",
            "cuisine": item.get("cuisine") or "",
        } for item in results if item.get("id")]

    @app.post("/watches")
    @app.post("/snipes")
    async def create_watch(
        venue_id: int = Form(...),
        restaurant_name: str = Form(...),
        target_date: str = Form(...),
        party_size: int = Form(...),
        preferred_time: str = Form(...),
    ):
        if not db.get_credentials():
            return _redirect("/settings", error="Connect your Resy account before creating a watch.")
        if party_size < 1 or party_size > 20:
            raise HTTPException(400, "Invalid party size")

        try:
            target = datetime.strptime(target_date, "%Y-%m-%d").date()
            datetime.strptime(preferred_time, "%H:%M")
        except ValueError:
            raise HTTPException(400, "Invalid date or time")

        if target < now_local_naive().date():
            return _redirect("/new", error="That reservation date has already passed.")

        restaurant = db.get_restaurant_by_venue_id(venue_id)
        is_new_restaurant = restaurant is None
        if not restaurant:
            # Safe default for persistence. We do NOT assume this is the real
            # drop rule for a new restaurant, so no high-speed drop job is
            # armed from this guessed value.
            restaurant = db.add_restaurant(
                venue_id=venue_id,
                name=restaurant_name.strip(),
                release_time=dt_time(0, 0),
                days_in_advance=14,
                release_pattern=ReleasePattern.DAILY,
                city="New York",
            )

        # The watch is the source of truth and starts immediately. Exact means
        # exact: time_window=0, so no neighboring reservation can be booked.
        monitor = db.add_monitor(
            restaurant_id=restaurant.id,
            target_date=target,
            party_size=party_size,
            preferred_time=preferred_time,
            time_window=0,
            table_type=None,
        )

        daemon.poll_monitors_now()

        # For restaurants with a saved/known release rule, add a high-speed
        # drop boost if the official release is still in the future.
        snipe_at = db.calculate_snipe_time(restaurant, target)
        drop_armed = False
        if not is_new_restaurant and snipe_at > now_local_naive():
            preference = {
                "time": preferred_time,
                "window_minutes": 0,
                "table_type": None,
            }
            reservation = db.schedule_reservation(
                restaurant_id=restaurant.id,
                target_date=target,
                party_size=party_size,
                snipe_at=snipe_at,
                preferred_times=[preference],
            )
            daemon.schedule_job(reservation)
            drop_armed = True

        time_label = datetime.strptime(preferred_time, "%H:%M").strftime("%-I:%M %p")
        if drop_armed:
            message = (
                f"Watching {restaurant.name} for exactly {time_label}. "
                f"High-speed drop booking is also armed for {_fmt_fire(snipe_at)} ET."
            )
        else:
            message = (
                f"Watching {restaurant.name} for exactly {time_label}. "
                "If that table opens, I'll attempt to book it automatically."
            )
        return _redirect("/", message=message)

    @app.post("/snipes/{reservation_id}/cancel")
    async def cancel_snipe(reservation_id: int):
        db.update_reservation_status(
            reservation_id,
            ReservationStatus.CANCELLED,
            result_message="Cancelled by user",
        )
        daemon.cancel_job(reservation_id)
        return _redirect("/", message="Snipe cancelled.")

    @app.post("/snipes/{reservation_id}/delete")
    async def delete_snipe(reservation_id: int):
        daemon.cancel_job(reservation_id)
        db.delete_reservation(reservation_id)
        return _redirect("/", message="Removed.")

    @app.post("/monitors/{monitor_id}/cancel")
    async def cancel_monitor(monitor_id: int):
        monitor = next((m for m in db.get_monitors() if m.id == monitor_id), None)
        if monitor:
            for reservation in db.get_pending_reservations():
                if _watch_matches_snipe(monitor, reservation):
                    db.update_reservation_status(
                        reservation.id,
                        ReservationStatus.CANCELLED,
                        result_message="Cancelled with watch",
                    )
                    daemon.cancel_job(reservation.id)
        db.cancel_monitor(monitor_id)
        return _redirect("/", message="Watch stopped.")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        creds = db.get_credentials()
        restaurants = db.get_restaurants()
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=common_context(
                request,
                "settings",
                credentials=creds,
                restaurants=restaurants,
                data_dir=str(DATA_DIR),
                has_volume=bool(os.getenv("RAILWAY_VOLUME_MOUNT_PATH")),
            ),
        )

    @app.post("/settings/resy-login")
    async def settings_resy_login(email: str = Form(...), password: str = Form(...)):
        try:
            creds = resy_login(email.strip(), password)
            db.save_credentials(
                api_key=creds.api_key,
                auth_token=creds.auth_token,
                payment_method_id=creds.payment_method_id,
                email=email.strip(),
            )
            daemon.refresh_credentials()
            daemon.load_pending_jobs()
            return _redirect("/settings", message="Resy connected successfully.")
        except AuthenticationError as exc:
            return _redirect("/settings", error=str(exc))
        except Exception:
            return _redirect("/settings", error="Resy login failed. Check your credentials and try again.")

    @app.post("/settings/resy-test")
    async def settings_resy_test():
        stored = db.get_credentials()
        if not stored:
            return _redirect("/settings", error="No Resy account connected.")
        creds = ResyCredentials(
            api_key=stored.api_key,
            auth_token=stored.auth_token,
            payment_method_id=stored.payment_method_id,
        )
        try:
            with ResyClient(creds) as client:
                ok = client.test_credentials()
            return _redirect("/settings", message="Resy connection is healthy." if ok else None,
                             error=None if ok else "Resy session expired. Log in again.")
        except Exception:
            return _redirect("/settings", error="Could not reach Resy. Try again.")

    @app.post("/settings/resy-logout")
    async def settings_resy_logout():
        db.delete_credentials()
        daemon.refresh_credentials()
        return _redirect("/settings", message="Resy disconnected.")

    @app.post("/restaurants/{restaurant_id}/rule")
    async def update_release_rule(
        restaurant_id: int,
        release_days: int = Form(...),
        release_time: str = Form(...),
    ):
        restaurant = db.get_restaurant(restaurant_id)
        if not restaurant:
            raise HTTPException(404, "Restaurant not found")
        try:
            t = datetime.strptime(release_time, "%H:%M").time()
        except ValueError:
            raise HTTPException(400, "Invalid time")
        db.update_restaurant(restaurant_id, days_in_advance=release_days, release_time=t)
        updated_restaurant = db.get_restaurant(restaurant_id)
        for res in db.get_scheduled_reservations(ReservationStatus.PENDING):
            if res.restaurant_id == restaurant_id:
                new_fire = db.calculate_snipe_time(updated_restaurant, res.target_date)
                updated = db.update_reservation(res.id, snipe_at=new_fire)
                if updated and new_fire > now_local_naive():
                    daemon.schedule_job(updated)
        return _redirect("/settings", message=f"Updated release rule for {restaurant.name}.")

    return app
