"""Mobile-first FastAPI web app for Resy Bot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
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
from storage.models import ReleasePattern, ReservationStatus

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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
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
        creds = db.get_credentials()
        pref_map = {r.id: _parse_preferences(r.preferred_times) for r in reservations}
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=common_context(
                request,
                "home",
                upcoming=upcoming,
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
        return templates.TemplateResponse(
            request=request,
            name="new_snipe.html",
            context=common_context(
                request,
                "new",
                restaurants=restaurants,
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

    @app.post("/snipes")
    async def create_snipe(
        venue_id: int = Form(...),
        restaurant_name: str = Form(...),
        target_date: str = Form(...),
        party_size: int = Form(...),
        preferred_time: str = Form(...),
        window_minutes: int = Form(60),
        table_type: str = Form(""),
        release_days: int = Form(14),
        release_time: str = Form("09:00"),
    ):
        if not db.get_credentials():
            return _redirect("/settings", error="Connect your Resy account before arming a snipe.")
        if party_size < 1 or party_size > 20:
            raise HTTPException(400, "Invalid party size")
        if window_minutes not in {0, 15, 30, 45, 60, 90, 120}:
            raise HTTPException(400, "Invalid time window")

        try:
            target = datetime.strptime(target_date, "%Y-%m-%d").date()
            release_t = datetime.strptime(release_time, "%H:%M").time()
            datetime.strptime(preferred_time, "%H:%M")
        except ValueError:
            raise HTTPException(400, "Invalid date or time")

        restaurant = db.get_restaurant_by_venue_id(venue_id)
        if not restaurant:
            restaurant = db.add_restaurant(
                venue_id=venue_id,
                name=restaurant_name.strip(),
                release_time=release_t,
                days_in_advance=release_days,
                release_pattern=ReleasePattern.DAILY,
                city="New York",
            )

        snipe_at = db.calculate_snipe_time(restaurant, target)
        if snipe_at <= now_local_naive():
            return _redirect(
                "/new",
                error=f"This drop already passed ({_fmt_fire(snipe_at)} ET). Check the restaurant's release rule.",
            )

        preference = {
            "time": preferred_time,
            "window_minutes": window_minutes,
            "table_type": table_type.strip() or None,
        }
        reservation = db.schedule_reservation(
            restaurant_id=restaurant.id,
            target_date=target,
            party_size=party_size,
            snipe_at=snipe_at,
            preferred_times=[preference],
        )
        daemon.schedule_job(reservation)
        return _redirect(
            "/",
            message=f"Armed for {restaurant.name}. Fires {_fmt_fire(snipe_at)} ET.",
        )

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
