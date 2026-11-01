"""FastAPI app: login, live MJPEG stream, status."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import shutil
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .config import Config
from .db import EventStore, SegmentStore
from .frames import LatestFrame
from .retention import GB
from .segments import parse_start, thumb_name

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
OFFLINE_AFTER = 5.0  # seconds without a frame before the camera counts as offline
BOUNDARY = "frame"


async def mjpeg_stream(latest: LatestFrame, fps: int,
                       is_disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[bytes]:
    """Yield each new frame once as a multipart part until the client leaves."""
    last_seq = 0
    poll = 1 / (fps * 2)
    while not await is_disconnected():
        seq, jpeg = latest.get()
        if jpeg is None or seq == last_seq:
            await asyncio.sleep(poll)
            continue
        last_seq = seq
        yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
               f"Content-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"


def create_app(cfg: Config, latest: LatestFrame, recorder_status: Callable[[], dict],
               store: SegmentStore, segments_dir: Path, *,
               thumbs_dir: Path | None = None,
               events: EventStore | None = None, events_dir: Path | None = None,
               on_shutdown: Callable[[], None] | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # uvicorn re-raises SIGTERM after shutdown, so cleanup after uvicorn.run()
        # would never run under systemd; do it here, while we still can
        if on_shutdown:
            await asyncio.to_thread(on_shutdown)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # CSS, JS, fonts
    app.add_middleware(SessionMiddleware,
                       secret_key=cfg.web.secret_key or secrets.token_hex(32),
                       same_site="lax", max_age=30 * 24 * 3600)

    def logged_in(request: Request) -> bool:
        return request.session.get("user") == cfg.web.username

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if username == cfg.web.username and verify_password(password, cfg.web.password_hash):
            request.session["user"] = username
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": "Sai tên đăng nhập hoặc mật khẩu"}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return TEMPLATES.TemplateResponse(request, "live.html", {})

    @app.get("/live.mjpg")
    def live(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        return StreamingResponse(
            mjpeg_stream(latest, cfg.live.fps, request.is_disconnected),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        age = latest.age()
        return {"camera_online": age is not None and age < OFFLINE_AFTER,
                "last_frame_age": age, **recorder_status(),
                "storage": {"used_bytes": store.total_size(),
                            "quota_bytes": int(cfg.storage.quota_gb * GB),
                            "free_bytes": shutil.disk_usage(segments_dir).free}}

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return TEMPLATES.TemplateResponse(request, "recordings.html", {})

    @app.get("/api/days")
    def days(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        return store.days()

    @app.get("/api/segments")
    def segments(request: Request, day: str):
        if not logged_in(request):
            return Response(status_code=401)
        return [{"name": s.name, "start": s.start_ts, "end": s.end_ts,
                 "duration": s.duration, "size": s.size} for s in store.by_day(day)]

    @app.get("/media/{name}")
    def media(request: Request, name: str, download: bool = False):
        if not logged_in(request):
            return Response(status_code=401)
        # parse_start only accepts YYYYMMDD-HHMMSS.mp4, so no path tricks get through
        start = parse_start(name)
        if start is None or store.get(name) is None:
            return Response(status_code=404)
        path = segments_dir / name
        if not path.is_file():
            return Response(status_code=404)
        if download:
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(start))
            return FileResponse(path, media_type="video/mp4", filename=f"raspcam-{stamp}.mp4")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/thumbs/{name}")
    def thumb(request: Request, name: str):
        if not logged_in(request):
            return Response(status_code=401)
        segment = name.removesuffix(".jpg") + ".mp4"
        if not name.endswith(".jpg") or parse_start(segment) is None or thumbs_dir is None:
            return Response(status_code=404)
        path = thumbs_dir / thumb_name(segment)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/events")
    def events_api(request: Request, day: str):
        if not logged_in(request):
            return Response(status_code=401)
        if events is None:
            return []
        return [{"id": e.id, "start": e.start_ts, "end": e.end_ts, "peak": e.peak}
                for e in events.by_day(day)]

    @app.get("/events/{event_id}.jpg")
    def event_snapshot(request: Request, event_id: int):
        if not logged_in(request):
            return Response(status_code=401)
        path = events_dir / f"{event_id}.jpg" if events_dir else None
        if path is None or not path.is_file():
            return Response(status_code=404)
        # short cache: the snapshot is replaced while the event is still going
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=30"})

    return app
