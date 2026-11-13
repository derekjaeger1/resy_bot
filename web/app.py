"""Mobile-first FastAPI web app for Resy Bot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta
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
from release_rules import resolve_release_rule
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


def _has_verified_drop_rule(restaurant) -> bool:
    """True only for an exact, restaurant-specific release rule."""
    return bool(getattr(restaurant, "release_verified", False))


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _hhmm(total: int) -> str:
    total = max(0, min(23 * 60 + 59, int(total)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _clock_label(value: str) -> str:
    return datetime.strptime(value, "%H:%M").strftime("%-I:%M %p")


def _time_key(value) -> str:
    """Normalize Resy/history time formats to HH:MM for matching."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        if " " in raw:
            raw = raw.split(" ")[-1]
        if "T" in raw:
            raw = raw.split("T")[-1]
        return raw[:5]
    except Exception:
        return raw[:5]


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
        if not prefs:
            return False
        pref = prefs[0]
        return (
            pref.get("time") == monitor.preferred_time
            and int(pref.get("window_minutes", 0)) == int(monitor.time_window or 0)
        )

    def _cancel_group_after_booking(group_id: str | None, keep_monitor_id: int | None = None) -> None:
        if not group_id:
            return
        group_monitors = [m for m in db.get_monitors() if m.watch_group_id == group_id]
        group_dates = {m.target_date for m in group_monitors}
        if not group_monitors:
            return
        restaurant_id = group_monitors[0].restaurant_id
        party_size = group_monitors[0].party_size

        for monitor in group_monitors:
            if monitor.id != keep_monitor_id and monitor.status == MonitorStatus.ACTIVE:
                db.cancel_monitor(monitor.id)

        for reservation in db.get_pending_reservations():
            if (
                reservation.restaurant_id == restaurant_id
                and reservation.party_size == party_size
                and reservation.target_date in group_dates
            ):
                db.update_reservation_status(
                    reservation.id,
                    ReservationStatus.CANCELLED,
                    result_message="Cancelled automatically after another date/time in this watch booked",
                )
                daemon.cancel_job(reservation.id)

    def _on_snipe_complete(reservation, result) -> None:
        if not result.success:
            return
        for monitor in db.get_active_monitors():
            if _watch_matches_snipe(monitor, reservation):
                group_id = monitor.watch_group_id
                db.cancel_monitor(monitor.id)
                _cancel_group_after_booking(group_id, keep_monitor_id=monitor.id)
                break

    daemon.on_snipe_complete = _on_snipe_complete

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Refresh restaurant-specific release intelligence from our verified
        # registry / public Resy listing parser. Never invent a release time.
        for restaurant in db.get_restaurants():
            rule = resolve_release_rule(restaurant.name, getattr(restaurant, "url_slug", None))
            updates = {
                "release_verified": bool(rule.verified and rule.snipeable),
                "release_source": rule.source,
                "release_note": rule.note,
            }
            if rule.verified and rule.snipeable and rule.days_in_advance is not None and rule.release_time:
                updates.update({
                    "days_in_advance": rule.days_in_advance,
                    "release_time": rule.release_time,
                    "release_pattern": ReleasePattern.DAILY,
                })
            db.update_restaurant(restaurant.id, **updates)

        # The Watch record is the source of truth. Remove stale legacy drop
        # jobs, and remove any drop job whose restaurant no longer has a
        # verified exact release rule.
        active_watches = db.get_active_monitors()
        for reservation in db.get_pending_reservations():
            restaurant = db.get_restaurant(reservation.restaurant_id)
            paired = any(_watch_matches_snipe(monitor, reservation) for monitor in active_watches)
            if not restaurant or not _has_verified_drop_rule(restaurant) or not paired:
                db.update_reservation_status(
                    reservation.id,
                    ReservationStatus.CANCELLED,
                    result_message="Cancelled stale or unverified drop boost; watch remains source of truth",
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
        recent_monitors = [
            m for m in monitors
            if m.status not in {MonitorStatus.ACTIVE, MonitorStatus.SUCCESS}
        ][:12]

        # The live Resy account is the source of truth for upcoming
        # reservations. This intentionally includes reservations booked
        # outside this app.
        history = db.get_history(limit=200)
        successful_history = [h for h in history if h.success and h.resy_token]

        live_reservations = []
        live_resy_ok = False
        creds = db.get_credentials()
        if creds:
            try:
                with ResyClient(ResyCredentials(
                    api_key=creds.api_key,
                    auth_token=creds.auth_token,
                    payment_method_id=creds.payment_method_id,
                )) as client:
                    live_reservations = client.get_reservations()
                    live_resy_ok = True
            except Exception:
                live_reservations = []

        def _matching_history(live):
            live_date = str(live.get("date") or "")
            live_time = _time_key(live.get("time"))
            live_venue = str(live.get("venue_id") or "")
            live_party = int(live.get("party_size") or 0)
            live_token = live.get("resy_token")

            for h in successful_history:
                if live_token and h.resy_token == live_token:
                    return h
                if (
                    str(h.venue_id) == live_venue
                    and h.target_date.isoformat() == live_date
                    and int(h.party_size or 0) == live_party
                    and _time_key(h.booked_time) == live_time
                ):
                    return h
            return None

        account_reservations = []
        today = now_local_naive().date()
        for live in live_reservations:
            try:
                parsed_date = datetime.strptime(str(live.get("date")), "%Y-%m-%d").date()
            except Exception:
                continue

            # Resy's reservations endpoint can include historical rows.
            # The dashboard is an upcoming reservation wallet only.
            if parsed_date < today:
                continue

            history_match = _matching_history(live)
            account_reservations.append({
                "live": live,
                "history": history_match,
                "date_obj": parsed_date,
                "time_label": clock(live.get("time")),
                "booked_by_sniper": history_match is not None,
                "can_cancel": bool(live.get("reservation_id") and live.get("resy_token")),
            })

        account_reservations.sort(
            key=lambda item: (
                item["date_obj"],
                _time_key(item["live"].get("time")),
                item["live"].get("venue_name") or "",
            )
        )

        # Keep bot booking history only for cancelled/past items that are no
        # longer present in the live upcoming Resy account.
        live_tokens = {r.get("resy_token") for r in live_reservations if r.get("resy_token")}
        booking_history = [
            h for h in successful_history
            if h.resy_token not in live_tokens
        ][:20]

        grouped = {}
        for monitor in active_monitors:
            key = monitor.watch_group_id or f"legacy-{monitor.id}"
            grouped.setdefault(key, []).append(monitor)

        watch_groups = []
        paired_snipe_ids = set()
        for group_id, rows in grouped.items():
            rows.sort(key=lambda m: m.target_date)
            first = rows[0]
            boosts = []
            for reservation in upcoming:
                if any(_watch_matches_snipe(m, reservation) for m in rows):
                    boosts.append(reservation)
                    paired_snipe_ids.add(reservation.id)

            start_time = first.time_start
            end_time = first.time_end
            if not start_time or not end_time:
                center = _minutes(first.preferred_time)
                start_time = _hhmm(center - int(first.time_window or 0))
                end_time = _hhmm(center + int(first.time_window or 0))

            checked = [m.last_checked for m in rows if m.last_checked]
            watch_groups.append({
                "id": group_id,
                "restaurant": restaurant_map.get(first.restaurant_id),
                "party_size": first.party_size,
                "date_start": rows[0].target_date,
                "date_end": rows[-1].target_date,
                "time_start": start_time,
                "time_end": end_time,
                "time_start_label": _clock_label(start_time),
                "time_end_label": _clock_label(end_time),
                "checks": sum(m.check_count or 0 for m in rows),
                "last_checked": max(checked) if checked else None,
                "boosts": sorted(boosts, key=lambda r: r.snipe_at),
                "date_count": len(rows),
            })

        watch_groups.sort(key=lambda g: (g["date_start"], g["restaurant"].name if g["restaurant"] else ""))
        orphan_snipes = [r for r in upcoming if r.id not in paired_snipe_ids]

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=common_context(
                request,
                "home",
                watch_groups=watch_groups,
                recent_monitors=recent_monitors,
                account_reservations=account_reservations,
                booking_history=booking_history,
                live_resy_ok=live_resy_ok,
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
            "url_slug": item.get("url_slug") or "",
        } for item in results if item.get("id")]

    @app.get("/api/watch-groups/{group_id}/availability")
    async def watch_group_availability(group_id: str):
        monitors = [m for m in db.get_active_monitors() if m.watch_group_id == group_id]
        if not monitors:
            return {"status": "inactive", "slots": []}

        slots = daemon.get_watch_group_availability(group_id)
        cleaned = []
        for slot in slots:
            raw = str(slot.get("time") or "")
            time_value = raw
            try:
                if " " in raw and "-" in raw.split(" ")[0]:
                    time_value = raw.split(" ", 1)[1][:5]
                else:
                    time_value = raw[:5]
                label = _clock_label(time_value)
            except Exception:
                label = raw
            cleaned.append({
                "date": slot.get("date"),
                "time": time_value,
                "label": label,
                "type": slot.get("type"),
            })
        return {
            "status": "watching",
            "checked_at": now_local_naive().strftime("%-I:%M:%S %p"),
            "slots": cleaned,
        }

    @app.post("/watches")
    @app.post("/snipes")
    async def create_watch(
        venue_id: int = Form(...),
        restaurant_name: str = Form(...),
        venue_slug: str = Form(""),
        date_start: str = Form(...),
        date_end: str = Form(...),
        party_size: int = Form(...),
        time_start: str = Form(...),
        time_end: str = Form(...),
    ):
        if not db.get_credentials():
            return _redirect("/settings", error="Connect your Resy account before creating a watch.")
        if party_size < 1 or party_size > 20:
            raise HTTPException(400, "Invalid party size")

        try:
            start_date = datetime.strptime(date_start, "%Y-%m-%d").date()
            end_date = datetime.strptime(date_end, "%Y-%m-%d").date()
            start_minutes = _minutes(time_start)
            end_minutes = _minutes(time_end)
        except ValueError:
            raise HTTPException(400, "Invalid date or time")

        today = now_local_naive().date()
        if start_date < today:
            return _redirect("/new", error="The first watch date has already passed.")
        if end_date < start_date:
            return _redirect("/new", error="End date must be on or after the start date.")
        date_count = (end_date - start_date).days + 1
        if date_count > 14:
            return _redirect("/new", error="A single watch can cover up to 14 consecutive dates.")
        if end_minutes < start_minutes:
            return _redirect("/new", error="End time must be later than the start time.")

        midpoint = (start_minutes + end_minutes) // 2
        window = max(midpoint - start_minutes, end_minutes - midpoint)
        preferred_time = _hhmm(midpoint)
        group_id = uuid.uuid4().hex[:16]

        # Resolve the restaurant's release rule automatically. User never
        # enters or guesses it.
        rule = resolve_release_rule(restaurant_name.strip(), venue_slug.strip() or None)
        restaurant = db.get_restaurant_by_venue_id(venue_id)
        updates = {
            "name": restaurant_name.strip(),
            "url_slug": venue_slug.strip() or None,
            "release_verified": bool(rule.verified and rule.snipeable),
            "release_source": rule.source,
            "release_note": rule.note,
        }
        if rule.verified and rule.snipeable and rule.days_in_advance is not None and rule.release_time:
            updates.update({
                "days_in_advance": rule.days_in_advance,
                "release_time": rule.release_time,
                "release_pattern": ReleasePattern.DAILY,
            })

        if restaurant:
            restaurant = db.update_restaurant(restaurant.id, **updates)
        else:
            restaurant = db.add_restaurant(
                venue_id=venue_id,
                name=restaurant_name.strip(),
                release_time=rule.release_time or dt_time(0, 0),
                days_in_advance=rule.days_in_advance or 14,
                release_pattern=ReleasePattern.DAILY,
                city="New York",
                release_verified=bool(rule.verified and rule.snipeable),
                release_source=rule.source,
                release_note=rule.note,
                url_slug=venue_slug.strip() or None,
            )

        created_monitors = []
        drop_count = 0
        current_date = start_date
        while current_date <= end_date:
            monitor = db.add_monitor(
                restaurant_id=restaurant.id,
                target_date=current_date,
                party_size=party_size,
                preferred_time=preferred_time,
                time_window=window,
                table_type=None,
                watch_group_id=group_id,
                time_start=time_start,
                time_end=time_end,
            )
            created_monitors.append(monitor)

            if _has_verified_drop_rule(restaurant):
                snipe_at = db.calculate_snipe_time(restaurant, current_date)
                if snipe_at > now_local_naive():
                    reservation = db.schedule_reservation(
                        restaurant_id=restaurant.id,
                        target_date=current_date,
                        party_size=party_size,
                        snipe_at=snipe_at,
                        preferred_times=[{
                            "time": preferred_time,
                            "window_minutes": window,
                            "table_type": None,
                        }],
                    )
                    daemon.schedule_job(reservation)
                    drop_count += 1

            current_date += timedelta(days=1)

        # Search synchronously before returning the dashboard. The user should
        # never see an ambiguous "checking" state when the answer is already
        # available from Resy.
        immediate = daemon.check_watch_group_now(group_id)

        date_label = (
            start_date.strftime("%b %-d")
            if start_date == end_date
            else f"{start_date.strftime('%b %-d')}–{end_date.strftime('%b %-d')}"
        )
        time_label = f"{_clock_label(time_start)}–{_clock_label(time_end)}"

        if immediate.get("status") == "booked":
            raw_booked = str(immediate.get("time") or "")
            try:
                booked_clock = raw_booked.split(" ")[1][:5] if " " in raw_booked and "-" in raw_booked.split(" ")[0] else raw_booked[:5]
                booked_label = _clock_label(booked_clock)
            except Exception:
                booked_label = raw_booked
            booked_date = datetime.strptime(immediate["date"], "%Y-%m-%d").strftime("%a, %b %-d")
            return _redirect(
                "/",
                message=f"BOOKED — {restaurant.name}, {booked_date} at {booked_label} for {party_size}.",
            )

        if immediate.get("status") == "error":
            return _redirect(
                "/",
                error=f"Could not complete the immediate Resy check: {immediate.get('message', 'unknown error')}. The watch remains saved.",
            )

        if drop_count:
            message = (
                f"No qualifying table is available right now for {restaurant.name}, {date_label}, "
                f"{time_label}. Watching continuously; {drop_count} verified drop "
                f"{'attempt is' if drop_count == 1 else 'attempts are'} also armed."
            )
        else:
            message = (
                f"No qualifying table is available right now for {restaurant.name}, "
                f"{date_label}, {time_label}. Watching continuously."
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

    @app.post("/resy-reservations/{reservation_id}/cancel")
    async def cancel_live_resy_reservation(reservation_id: str):
        stored = db.get_credentials()
        if not stored:
            return _redirect("/", error="Reconnect your Resy account before cancelling.")

        creds = ResyCredentials(
            api_key=stored.api_key,
            auth_token=stored.auth_token,
            payment_method_id=stored.payment_method_id,
        )

        try:
            with ResyClient(creds) as client:
                current = client.get_reservations()
                live = next(
                    (
                        r for r in current
                        if str(r.get("reservation_id")) == str(reservation_id)
                    ),
                    None,
                )
                if not live:
                    return _redirect(
                        "/",
                        error="That reservation is no longer active in your Resy account.",
                    )

                if not live.get("cancellation_allowed", False):
                    cutoff = live.get("cancellation_cutoff")
                    detail = f" Cancellation cutoff: {cutoff}." if cutoff else ""
                    return _redirect(
                        "/",
                        error=f"Resy is not allowing cancellation for this reservation right now.{detail}",
                    )

                token = live.get("resy_token")
                if not token:
                    return _redirect("/", error="Resy did not provide a cancellation token.")

                client.cancel_reservation(token)

            # Preserve bot history and mark matching bot-created booking
            # cancelled when this live reservation originated here.
            live_date = str(live.get("date") or "")
            live_time = _time_key(live.get("time"))
            for h in db.get_history(limit=200):
                if not h.success:
                    continue
                if (
                    h.resy_token == token
                    or (
                        str(h.venue_id) == str(live.get("venue_id") or "")
                        and h.target_date.isoformat() == live_date
                        and int(h.party_size or 0) == int(live.get("party_size") or 0)
                        and _time_key(h.booked_time) == live_time
                    )
                ):
                    db.mark_history_cancelled(
                        h.id,
                        message="Cancelled directly from Reservation Sniper",
                    )

            return _redirect(
                "/",
                message=(
                    f"Cancelled — {live.get('venue_name', 'reservation')} "
                    f"on {live.get('date')} at {clock(live.get('time'))}."
                ),
            )
        except Exception as exc:
            return _redirect("/", error=f"Resy cancellation failed: {str(exc)}")

    @app.post("/bookings/{history_id}/cancel")
    async def cancel_booked_reservation(history_id: int):
        history = db.get_history_by_id(history_id)
        if not history or not history.success or not history.resy_token:
            return _redirect("/", error="That booking could not be found.")
        if history.cancelled_at:
            return _redirect("/", message=f"{history.restaurant_name} is already marked cancelled.")

        stored = db.get_credentials()
        if not stored:
            return _redirect("/", error="Reconnect your Resy account before cancelling.")

        creds = ResyCredentials(
            api_key=stored.api_key,
            auth_token=stored.auth_token,
            payment_method_id=stored.payment_method_id,
        )
        try:
            with ResyClient(creds) as client:
                current = client.get_reservations()
                live = next(
                    (r for r in current if r.get("resy_token") == history.resy_token),
                    None,
                )
                if not live:
                    return _redirect(
                        "/",
                        error=(
                            f"{history.restaurant_name} is not currently listed as an active "
                            "upcoming reservation in Resy. It may already be cancelled or changed."
                        ),
                    )
                if not live.get("cancellation_allowed", False):
                    cutoff = live.get("cancellation_cutoff")
                    detail = f" Cancellation cutoff: {cutoff}." if cutoff else ""
                    return _redirect(
                        "/",
                        error=f"Resy is not allowing cancellation for this reservation right now.{detail}",
                    )

                client.cancel_reservation(history.resy_token)

            db.mark_history_cancelled(
                history_id,
                message="Cancelled directly from Reservation Sniper",
            )
            return _redirect(
                "/",
                message=f"Cancelled — {history.restaurant_name} on {history.target_date.strftime('%b %-d')}.",
            )
        except Exception as exc:
            return _redirect(
                "/",
                error=f"Resy cancellation failed: {str(exc)}",
            )

    @app.post("/watch-groups/{group_id}/cancel")
    async def cancel_watch_group(group_id: str):
        monitors = [m for m in db.get_monitors() if m.watch_group_id == group_id]
        for monitor in monitors:
            if monitor.status == MonitorStatus.ACTIVE:
                db.cancel_monitor(monitor.id)
        dates = {m.target_date for m in monitors}
        if monitors:
            restaurant_id = monitors[0].restaurant_id
            party_size = monitors[0].party_size
            for reservation in db.get_pending_reservations():
                if (
                    reservation.restaurant_id == restaurant_id
                    and reservation.party_size == party_size
                    and reservation.target_date in dates
                ):
                    db.update_reservation_status(
                        reservation.id,
                        ReservationStatus.CANCELLED,
                        result_message="Cancelled with range watch",
                    )
                    daemon.cancel_job(reservation.id)
        return _redirect("/", message="Watch stopped.")

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
