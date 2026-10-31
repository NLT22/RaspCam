"""FastAPI app: login, live MJPEG stream, status."""
from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .config import Config
from .frames import LatestFrame

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
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


def create_app(cfg: Config, latest: LatestFrame,
               recorder_status: Callable[[], dict]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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
                "last_frame_age": age, **recorder_status()}

    return app
