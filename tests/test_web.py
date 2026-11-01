import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from raspcam.auth import hash_password
from raspcam.config import parse_config
from raspcam.db import Segment, SegmentStore
from raspcam.frames import LatestFrame
from raspcam.web import create_app, mjpeg_stream

STATUS = {"ffmpeg_running": True, "ffmpeg_uptime": 1.0, "restarts": 2, "last_error": None}


@pytest.fixture
def latest():
    return LatestFrame()


@pytest.fixture
def store(tmp_path):
    return SegmentStore(tmp_path / "db.sqlite")


@pytest.fixture
def seg_dir(tmp_path):
    d = tmp_path / "segments"
    d.mkdir()
    return d


@pytest.fixture
def thumbs_dir(tmp_path):
    d = tmp_path / "thumbs"
    d.mkdir()
    return d


@pytest.fixture
def client(latest, store, seg_dir, thumbs_dir):
    cfg = parse_config({
        "camera": {"device": "testsrc"},
        "web": {"password_hash": hash_password("secret"), "secret_key": "k" * 32},
    })
    return TestClient(create_app(cfg, latest, lambda: STATUS, store, seg_dir, thumbs_dir=thumbs_dir))


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


def add_segment(store, seg_dir, when="2026-11-01 10:00:00", data=b"0123456789"):
    start = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
    name = time.strftime("%Y%m%d-%H%M%S.mp4", time.localtime(start))
    (seg_dir / name).write_bytes(data)
    store.add(Segment(name, start, start + 60, len(data)))
    return name


def test_recordings_endpoints_require_login(client):
    assert client.get("/recordings", follow_redirects=False).status_code == 303
    for url in ["/api/days", "/api/segments?day=2026-11-01", "/media/20261101-100000.mp4"]:
        assert client.get(url).status_code == 401


def test_days_and_segments(client, store, seg_dir):
    name = add_segment(store, seg_dir)
    login(client)
    assert client.get("/api/days").json() == ["2026-11-01"]
    [s] = client.get("/api/segments", params={"day": "2026-11-01"}).json()
    assert s["name"] == name and s["duration"] == 60 and s["size"] == 10
    assert s["end"] - s["start"] == 60


def test_media_supports_range_requests(client, store, seg_dir):
    name = add_segment(store, seg_dir)
    login(client)
    full = client.get(f"/media/{name}")
    assert full.status_code == 200 and full.content == b"0123456789"
    assert full.headers["content-type"] == "video/mp4"
    part = client.get(f"/media/{name}", headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.content == b"2345"


def test_media_rejects_unknown_and_malicious_names(client, store, seg_dir):
    login(client)
    assert client.get("/media/20261101-100000.mp4").status_code == 404
    assert client.get("/media/..%2Fdb.sqlite").status_code == 404


def test_status_includes_storage(client, store, seg_dir):
    add_segment(store, seg_dir)
    login(client)
    st = client.get("/api/status").json()["storage"]
    assert st["used_bytes"] == 10 and st["quota_bytes"] == 5 * 10**9 and st["free_bytes"] > 0


def test_recordings_page_renders(client):
    login(client)
    r = client.get("/recordings")
    assert r.status_code == 200 and "Xem lại" in r.text and "<video" in r.text


def test_on_shutdown_runs_when_the_app_stops(latest, store, seg_dir):
    # uvicorn re-raises SIGTERM after shutdown, so code after uvicorn.run()
    # never runs under systemd; cleanup must happen in the lifespan instead
    cfg = parse_config({"camera": {"device": "testsrc"}, "web": {"password_hash": "scrypt$x"}})
    calls = []
    app = create_app(cfg, latest, lambda: STATUS, store, seg_dir, on_shutdown=lambda: calls.append(1))
    with TestClient(app):
        assert calls == []
    assert calls == [1]


def test_thumbnails_require_login_and_are_served(client, store, seg_dir, thumbs_dir):
    name = add_segment(store, seg_dir)
    thumb = name.replace(".mp4", ".jpg")
    (thumbs_dir / thumb).write_bytes(b"\xff\xd8jpg\xff\xd9")
    assert client.get(f"/thumbs/{thumb}").status_code == 401
    login(client)
    r = client.get(f"/thumbs/{thumb}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers["cache-control"]


def test_thumbnail_names_are_validated(client, thumbs_dir):
    login(client)
    (thumbs_dir / "secret.jpg").write_bytes(b"x")
    assert client.get("/thumbs/secret.jpg").status_code == 404
    assert client.get("/thumbs/20261101-100000.jpg").status_code == 404


def test_media_download_sets_a_readable_filename(client, store, seg_dir):
    name = add_segment(store, seg_dir, when="2026-11-01 03:42:00")
    login(client)
    r = client.get(f"/media/{name}", params={"download": 1})
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="raspcam-2026-11-01_03-42-00.mp4"'
    assert "content-disposition" not in client.get(f"/media/{name}").headers


def test_static_files_are_public(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    assert client.get("/static/fonts/be-vietnam-pro-vietnamese-400-normal.woff2").status_code == 200
