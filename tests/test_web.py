import asyncio

import pytest
from fastapi.testclient import TestClient

from raspcam.auth import hash_password
from raspcam.config import parse_config
from raspcam.frames import LatestFrame
from raspcam.web import create_app, mjpeg_stream

STATUS = {"ffmpeg_running": True, "ffmpeg_uptime": 1.0, "restarts": 2, "last_error": None}


@pytest.fixture
def latest():
    return LatestFrame()


@pytest.fixture
def client(latest):
    cfg = parse_config({
        "camera": {"device": "testsrc"},
        "web": {"password_hash": hash_password("secret"), "secret_key": "k" * 32},
    })
    return TestClient(create_app(cfg, latest, lambda: STATUS))


def login(client, password="secret"):
    return client.post("/login", data={"username": "admin", "password": password},
                       follow_redirects=False)


def test_index_redirects_to_login_when_logged_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_protected_endpoints_return_401_when_logged_out(client):
    assert client.get("/live.mjpg").status_code == 401
    assert client.get("/api/status").status_code == 401


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Đăng nhập" in r.text


def test_wrong_password_is_rejected(client):
    r = login(client, "nope")
    assert r.status_code == 401
    assert "Sai tên đăng nhập hoặc mật khẩu" in r.text
    assert client.get("/api/status").status_code == 401


def test_login_gives_access_to_index_and_status(client, latest):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "/live.mjpg" in client.get("/").text
    s = client.get("/api/status").json()
    assert s["camera_online"] is False and s["last_frame_age"] is None
    assert s["restarts"] == 2
    latest.set(b"\xff\xd8x\xff\xd9")
    assert client.get("/api/status").json()["camera_online"] is True


def test_logout_ends_session(client):
    login(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/status").status_code == 401


def test_mjpeg_stream_yields_each_new_frame_once(latest):
    latest.set(b"\xff\xd8x\xff\xd9")
    answers = iter([False, False, True])

    async def is_disconnected():
        return next(answers)

    async def collect():
        return [c async for c in mjpeg_stream(latest, fps=50, is_disconnected=is_disconnected)]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert chunks[0].startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n")
    assert chunks[0].endswith(b"\xff\xd8x\xff\xd9\r\n")
