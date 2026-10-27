"""Web app tests (Starlette TestClient). Nothing touches the network: YouTube Music is a fake,
Lidarr / Jellyfin are the in-process mock servers from mock_servers.py."""
import html
import os
import re
import tempfile
import threading

# app.main reads DATA_DIR / SECRET_KEY at import time: point them at a throw-away dir first.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ps-test-import-")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["APP_PASSWORD"] = "pw-123"

import pytest
from starlette.testclient import TestClient

from app import config, db, engine, main, worker
from app.routes import auth
from app.ui import core as ui_core
from app.ui import playlists as ui_playlists
from mock_servers import MockJellyfin, MockLidarr, FakeYTM, install_fake_ytm, yt_track

HX = {"HX-Request": "true"}
LK, JK = "LK-secret-123", "JK-secret-456"

FAKE_HEADERS = """accept: */*
accept-language: en-US,en;q=0.9
authorization: SAPISIDHASH 1700000000_FAKE
content-type: application/json
cookie: SAPISID=FAKE/abc; __Secure-3PAPISID=FAKE/abc; VISITOR_INFO1_LIVE=FAKE
origin: https://music.youtube.com
x-goog-authuser: 0
x-goog-visitor-id: FAKEVISITORID
user-agent: Mozilla/5.0"""


# ------------------------------------------------------------------ fixtures

@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PASSWORD", "pw-123")
    monkeypatch.setattr(auth, "LOGIN_DELAY", 0)
    worker._stop.clear()
    worker._state.update(playlist_id=None, run_id=None)
    db.init_db()
    yield tmp_path
    worker.stop()


@pytest.fixture
def anon():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture
def client(anon):
    r = anon.post("/login", data={"password": "pw-123"})
    assert r.status_code == 303
    return anon


@pytest.fixture
def servers():
    lidarr = MockLidarr(LK).start()
    jf = MockJellyfin(JK, users=("Daniel",)).start()
    lidarr.add_release("Motion", "Calvin Harris", "alb-motion", "art-ch")
    lidarr.add_release("Discovery", "Daft Punk", "alb-disc", "dp")
    jf.add_item("j-summer", "Summer", "Calvin Harris", "Motion")
    db.set_settings({"lidarr_url": lidarr.url, "lidarr_api_key": LK, "jellyfin_url": jf.url,
                     "jellyfin_api_key": JK, "jellyfin_user": "Daniel", "finalize_timeout_min": "1"})
    yield type("S", (), dict(lidarr=lidarr, jf=jf))
    lidarr.stop()
    jf.stop()


def fake_ytm(monkeypatch):
    return install_fake_ytm(monkeypatch, FakeYTM({"PLroad": {"title": "Road Trip", "tracks": [
        yt_track("Summer", ["Calvin Harris"], "Motion"),
        yt_track("One More Time", ["Daft Punk"], "Discovery"),
        yt_track("Where Is My Mind", ["Nobody"], "Nothing"),
    ]}}))


def add_pl(ref="PLroad", name="Road Trip", **kw):
    pid = db.add_playlist(ref, f"https://music.youtube.com/playlist?list={ref}", name, 3, kw.pop("interval", 24))
    if kw:
        db.update_playlist(pid, **kw)
    return pid


def toasts(r) -> list[tuple[str, str]]:
    """(type, message) of the FastHTML toasts in a response."""
    return [(typ, html.unescape(msg)) for typ, msg in
            re.findall(r'class="fh-toast fh-toast-(\w+)">\s*<span>(.*?)</span>', r.text)]


# --------------------------------------------------------------------- auth

class TestDataDir:
    def test_unusable_data_dir_gives_a_readable_error(self, tmp_path, monkeypatch):
        # QA regression: a bind mount the app user can't write to died with a bare PermissionError /
        # "unable to open database file". A path that is a file reproduces it even when tests run as root.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        monkeypatch.setenv("DATA_DIR", str(blocker))
        with pytest.raises(RuntimeError, match=r"Cannot use the data directory .*not-a-dir.*writable"):
            db.check_data_dir()
        with pytest.raises(RuntimeError, match="Cannot use the data directory"):
            db.init_db()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_database_file_is_private(self, data_dir):
        # QA: the settings table holds the API keys in plain text; do not leave the file world-readable
        os.chmod(db.db_path(), 0o644)
        db.init_db()
        assert db.db_path().stat().st_mode & 0o077 == 0

    def test_fresh_data_dir_is_created_and_left_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "new" / "data"))
        assert db.check_data_dir().is_dir()
        assert list((tmp_path / "new" / "data").iterdir()) == []


class TestMobileCss:
    def test_long_unbroken_strings_cannot_widen_the_page(self, client):
        # QA regression: a 300-char unbroken playlist name / track artist overflowed the 375 px layout
        css = (main.STATIC / "app.css").read_text()
        assert "minmax(0, 1fr)" in css and ".trk-title, .trk-artist, .trk-album, .trk-note { overflow: hidden;" in css
        assert ".hero-title" in css and "overflow-wrap: anywhere" in css
        pid = add_pl(name="N" * 300)
        db.replace_tracks(pid, [engine.TrackResult(1, "T" * 300, ["A" * 300], None)])
        page = client.get(f"/playlists/{pid}").text
        assert "T" * 300 in page and "A" * 300 in page  # rendered in ellipsis/anywhere-wrapping elements, no table


class TestAuth:
    @pytest.mark.parametrize("path", ["/", "/settings", "/playlists/1", "/ui/connections", "/ui/playlists"])
    def test_redirects_to_login(self, anon, path):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"

    def test_htmx_request_gets_hx_redirect(self, anon):
        r = anon.get("/ui/playlists", headers=HX)
        assert r.headers.get("hx-redirect") == "/login"

    def test_post_requires_login(self, anon):
        assert anon.post("/playlists", data={"url": "PLx"}).status_code == 303
        assert anon.post("/settings", data={"lidarr_url": "http://x"}).status_code == 303

    def test_login_page(self, anon):
        r = anon.get("/login")
        assert r.status_code == 200
        assert "<title>Sign in · Playlist Sync</title>" in r.text
        assert 'type="password"' in r.text

    def test_wrong_password(self, anon):
        r = anon.post("/login", data={"password": "nope"})
        assert r.status_code == 401 and "Incorrect password" in r.text
        assert anon.get("/").status_code == 303

    def test_failed_logins_are_serialised_without_blocking_threads(self, monkeypatch):
        # QA regression: a burst of wrong passwords used to sleep in the thread pool (36 guesses/s, /health
        # blocked). Now failures are queued one per LOGIN_DELAY and the queue is bounded.
        import time
        from concurrent.futures import ThreadPoolExecutor
        monkeypatch.setattr(auth, "LOGIN_DELAY", 0.3)
        monkeypatch.setattr(auth, "_next_fail_slot", 0.0)

        def bad(_):
            return TestClient(main.app, follow_redirects=False).post("/login", data={"password": "nope"}).status_code
        t0 = time.monotonic()
        with ThreadPoolExecutor(4) as ex:
            codes = list(ex.map(bad, range(4)))
        assert codes == [401] * 4
        assert time.monotonic() - t0 >= 0.9   # 4 guesses can not finish in less than ~3 x 0.3 s
        # the owner is never queued behind guesses, and a runaway queue is refused outright
        c = TestClient(main.app, follow_redirects=False)
        t0 = time.monotonic()
        assert c.post("/login", data={"password": "pw-123"}).status_code == 303
        assert time.monotonic() - t0 < 0.25
        monkeypatch.setattr(auth, "LOGIN_DELAY", 40)
        monkeypatch.setattr(auth, "_next_fail_slot", 0.0)
        assert c.post("/login", data={"password": "nope"}).status_code == 429
        monkeypatch.setattr(auth, "_next_fail_slot", 0.0)

    def test_right_password_and_logout(self, anon):
        r = anon.post("/login", data={"password": "pw-123"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "httponly" in r.headers["set-cookie"].lower()
        assert "max-age=2592000" in r.headers["set-cookie"].lower()  # 30 days
        assert anon.get("/").status_code == 200
        assert anon.get("/login").status_code == 303  # already signed in
        assert anon.post("/logout").status_code == 303
        assert anon.get("/").status_code == 303

    def test_password_unset_refuses(self, anon, monkeypatch):
        monkeypatch.delenv("APP_PASSWORD")
        page = anon.get("/login").text
        assert "APP_PASSWORD is not set" in page and "Set APP_PASSWORD in the container environment" in page
        assert 'type="password"' not in page
        r = anon.post("/login", data={"password": ""})
        assert r.status_code == 503
        assert anon.get("/").status_code == 303

    def test_health_is_open_and_reports_the_worker(self, anon):
        worker.start()
        r = anon.get("/health")
        assert r.status_code == 200 and r.text == "ok"
        worker.stop()
        r = anon.get("/health")
        assert r.status_code == 503 and r.text == "sync worker is not running"

    def test_static_open_and_no_traversal(self, anon):
        assert anon.get("/static/app.css").status_code == 200
        assert anon.get("/static/htmx.min.js").status_code == 200
        for bad in ("/static/%2e%2e%2fmain.py", "/static/..%2fmain.py", "/static/../main.py",
                    "/static/%2e%2e/db.py", "/static/..%5cmain.py"):
            r = anon.get(bad)
            assert r.status_code in (404, 400), bad
            assert "import" not in r.text, bad
        # the old built-in extension route is gone too
        assert anon.get("/app.css").status_code in (303, 404)

    def test_secret_key_file_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SECRET_KEY")
        k1 = main._secret_key()
        assert k1 and (tmp_path / ".secret_key").read_text() == k1
        assert main._secret_key() == k1


# ---------------------------------------------------------------- dashboard

class TestDashboard:
    def test_empty_state(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "<title>Library · Playlist Sync</title>" in r.text
        assert "No playlists yet" in r.text and "Add playlist" in r.text
        assert "autofocus" in r.text

    def test_no_external_resources(self, client):
        for path in ("/", "/settings", "/login"):
            html = client.get(path).text
            assert "http://" not in html.replace("http://www.w3.org", "") or "placeholder" in html
            for host in ("cdn.", "googleapis", "jsdelivr", "unpkg", "cloudflare"):
                assert host not in html

    def test_add_playlist(self, client, monkeypatch):
        monkeypatch.setattr(engine, "check_ytm", lambda s, ref: (True, "Workout Supermix", 82))
        r = client.post("/playlists", data={"url": "https://music.youtube.com/playlist?list=RDCLAK5uy_abc"},
                        headers=HX)
        assert r.status_code == 200
        assert r.headers["hx-trigger"] == "playlists-changed"
        assert toasts(r) == [("success", "Added 'Workout Supermix' · 82 tracks · first sync started")]
        pl = db.get_playlist_by_ref("RDCLAK5uy_abc")
        assert pl["name"] == "Workout Supermix" and pl["queued"] == 1 and pl["is_mix"] == 1
        assert pl["interval_hours"] == 24 and pl["track_count"] == 82
        assert "Workout Supermix" in client.get("/").text

    def test_add_uses_default_interval_setting(self, client, monkeypatch):
        monkeypatch.setattr(engine, "check_ytm", lambda s, ref: (True, "X", 5))
        db.set_settings({"default_interval_hours": "6"})
        client.post("/playlists", data={"url": "PLabcdefgh"}, headers=HX)
        assert db.get_playlist_by_ref("PLabcdefgh")["interval_hours"] == 6

    def test_add_honours_interval_chosen_in_form(self, client, monkeypatch):
        # QA regression: the interval <select> of the add form was ignored (always the default)
        monkeypatch.setattr(engine, "check_ytm", lambda s, ref: (True, "X", 5))
        db.set_settings({"default_interval_hours": "24"})
        client.post("/playlists", data={"url": "PLabcdefgh", "interval_hours": "6"}, headers=HX)
        client.post("/playlists", data={"url": "PLabcdefgi", "interval_hours": "168"}, headers=HX)
        client.post("/playlists", data={"url": "PLabcdefgj", "interval_hours": "-5"}, headers=HX)
        assert db.get_playlist_by_ref("PLabcdefgh")["interval_hours"] == 6
        assert db.get_playlist_by_ref("PLabcdefgi")["interval_hours"] == 168
        assert db.get_playlist_by_ref("PLabcdefgj")["interval_hours"] == 24
        assert 'name="interval_hours"' in client.get("/").text

    def test_add_unreadable_playlist_shows_error(self, client, monkeypatch):
        monkeypatch.setattr(engine, "check_ytm", lambda s, ref: (False, "Playlist is private. Add a login.", 0))
        r = client.post("/playlists", data={"url": "PLprivate123"}, headers=HX)
        assert r.status_code == 200 and "Playlist is private" in r.text
        assert "hx-trigger" not in r.headers
        assert db.list_playlists() == []

    def test_add_validation_and_duplicates(self, client, monkeypatch):
        monkeypatch.setattr(engine, "check_ytm", lambda s, ref: (True, "Same", 3))
        assert "does not look like" in client.post("/playlists", data={"url": "not a link!"}, headers=HX).text
        assert "No playlist URL" in client.post("/playlists", data={"url": "  "}, headers=HX).text
        client.post("/playlists", data={"url": "PLsame12345"}, headers=HX)
        r = client.post("/playlists", data={"url": "https://music.youtube.com/playlist?list=PLsame12345"}, headers=HX)
        assert "already in your list" in r.text
        assert len(db.list_playlists()) == 1

    def test_list_fragment_and_row_polling(self, client):
        pid = add_pl()  # queued -> polls
        html = client.get("/ui/playlists").text
        assert "Road Trip" in html and f'hx-get="/ui/playlists/{pid}/row"' in html and "every 3s" in html
        assert "Queued" in html
        db.update_playlist(pid, queued=0, last_status="ok", last_run_at=db.now_iso(),
                           next_run_at=db.iso_in_hours(9), lidarr_found=2, jellyfin_matched=1, track_count=3)
        row = client.get(f"/ui/playlists/{pid}/row").text
        assert "every 3s" not in row  # idle rows stop polling
        assert "Synced" in row and "Lidarr" in row and "2/3" in row and "1/3" in row
        assert "in 9 h" in row and "just now" in row
        # a vanished row answers with an empty fragment
        assert client.get("/ui/playlists/999/row").text == ""

    def test_state_badges(self, client):
        a = add_pl("PLaaaaaa", "A", queued=0, last_status="warning")
        b = add_pl("PLbbbbbb", "B", queued=0, last_status="error", last_error="Lidarr: bad key")
        c = add_pl("PLcccccc", "C", queued=0, enabled=0)
        html = client.get("/ui/playlists").text
        assert "Needs attention" in html and "Error" in html and "Paused" in html
        worker._state.update(playlist_id=a, run_id=1)
        assert "Syncing" in client.get(f"/ui/playlists/{a}/row").text
        assert b and c
        # QA: an Error row says why, right on the dashboard (escaped, with the full text as a tooltip)
        assert 'class="pl-error"' in html and "Lidarr: bad key" in html
        db.update_playlist(b, last_error="<script>alert(1)</script>")
        row = client.get(f"/ui/playlists/{b}/row").text
        assert "<script>alert(1)</script>" not in row and "&lt;script&gt;" in row
        # a healthy row has no error line
        db.update_playlist(a, last_status="ok", last_error=None)
        assert "pl-error" not in client.get(f"/ui/playlists/{a}/row").text

    def test_connections_fragment(self, client, servers):
        r = client.get("/ui/connections")
        assert "Connected" in r.text and "Lidarr" in r.text and "Jellyfin" in r.text and 'class="dot ok"' in r.text

    def test_connections_not_configured_and_error(self, client, servers):
        db.set_settings({"jellyfin_url": "", "jellyfin_api_key": "", "lidarr_api_key": "wrong"})
        html = client.get("/ui/connections").text
        assert "not set up" in html and 'href="/settings"' in html
        assert 'class="dot err"' in html and "Error:" in html
        assert LK not in html

    def test_sync_now_pause_resume(self, client):
        pid = add_pl(queued=0, last_status="ok", last_run_at=db.now_iso(), next_run_at=db.iso_in_hours(5))
        r = client.post(f"/playlists/{pid}/sync", headers={**HX, "HX-Target": f"pl-{pid}"})
        assert r.status_code == 200 and db.get_playlist(pid)["queued"] == 1 and "Queued" in r.text
        assert toasts(r) == [("success", "Sync queued for 'Road Trip'")]
        r = client.post(f"/playlists/{pid}/sync", headers={**HX, "HX-Target": f"pl-{pid}"})
        assert toasts(r) == [("info", "Already syncing or queued")]
        db.update_playlist(pid, queued=0)
        r = client.post(f"/playlists/{pid}/toggle", headers={**HX, "HX-Target": f"pl-{pid}"})
        assert db.get_playlist(pid)["enabled"] == 0 and "Paused" in r.text
        r = client.post(f"/playlists/{pid}/toggle", headers={**HX, "HX-Target": f"pl-{pid}"})
        assert db.get_playlist(pid)["enabled"] == 1 and "Synced" in r.text

    def test_sync_now_from_detail_returns_header(self, client):
        pid = add_pl(queued=0)
        r = client.post(f"/playlists/{pid}/sync", headers={**HX, "HX-Target": "detail-head"})
        assert 'id="detail-head"' in r.text and "every 3s" in r.text

    def test_sync_all(self, client):
        a = add_pl("PLaaaaaa", "A", queued=0)
        b = add_pl("PLbbbbbb", "B", queued=0, enabled=0)
        r = client.post("/playlists/sync-all", headers=HX)
        assert r.status_code == 200
        assert db.get_playlist(a)["queued"] == 1 and db.get_playlist(b)["queued"] == 0
        assert toasts(r) == [("success", "Queued 1 playlist")]
        assert r.headers["hx-trigger"] == "playlists-changed"

    def test_delete(self, client, monkeypatch):
        pid = add_pl()
        db.replace_tracks(pid, [engine.TrackResult(1, "T", ["A"], None)])
        rid = db.create_run(pid, "manual")
        db.finish_run(rid, "ok")
        worker._state.update(playlist_id=pid, run_id=rid)
        r = client.post(f"/playlists/{pid}/delete", headers=HX)
        assert "finished" in toasts(r)[0][1]
        assert db.get_playlist(pid)
        worker._state.update(playlist_id=None, run_id=None)
        r = client.post(f"/playlists/{pid}/delete", headers=HX)
        assert r.headers["hx-redirect"] == "/"
        assert db.get_playlist(pid) is None and db.list_tracks(pid) == [] and db.list_runs(pid) == []
        assert "Deleted" in client.get("/").text  # flash toast
        assert client.get(f"/playlists/{pid}").status_code == 200  # friendly not-found page
        assert "not found" in client.get(f"/playlists/{pid}").text.lower()


class TestShellAndArtwork:
    def test_sidebar_lists_playlists_with_status_titles(self, client):
        a = add_pl("PLaaaaaa", "Alpha", queued=0, last_status="ok")
        b = add_pl("PLbbbbbb", "Beta", queued=0, last_status="error", last_error="x")
        html = client.get(f"/playlists/{a}").text
        assert 'id="side-pls"' in html and "Alpha" in html and "Beta" in html
        assert 'class="dot ok" title="Synced"' in html and 'class="dot err" title="Error"' in html
        assert 'aria-current="page"' in html and f'href="/playlists/{b}"' in html

    def test_sidebar_fragment_refreshes_on_playlists_changed(self, client):
        pid = add_pl("PLaaaaaa", "Alpha", queued=0)
        html = client.get(f"/ui/sidebar-playlists?current={pid}").text
        assert "Alpha" in html and "playlists-changed from:body" in html and f"current={pid}" in html

    def test_library_summary_and_card_is_the_polling_element(self, client):
        add_pl("PLaaaaaa", "Alpha", queued=0, last_status="warning", track_count=4)
        add_pl("PLbbbbbb", "Beta", queued=1, track_count=2)
        html = client.get("/ui/playlists").text
        assert "2 playlists · 6 tracks · 1 needs attention" in html
        assert html.count("every 3s") == 1  # only the queued card polls

    def test_art_is_layered_over_the_gradient_only_when_known(self, client):
        a = add_pl("PLaaaaaa", "Alpha", queued=0, cover_url="https://lh3.googleusercontent.com/x")
        b = add_pl("PLbbbbbb", "Beta", queued=0)
        html = client.get("/ui/playlists").text
        assert f'src="/art/playlist/{a}"' in html and f'src="/art/playlist/{b}"' not in html
        assert 'loading="lazy"' in html and 'alt=""' in html and "onerror" not in html

    def test_mix_without_cover_gets_a_mosaic_of_track_art(self, client):
        pid = add_pl("RDmix00001", "Mix", queued=0, is_mix=1)
        db.replace_tracks(pid, [engine.TrackResult(i, f"T{i}", ["A"], None, thumb_url="https://i.ytimg.com/t.jpg")
                                for i in range(1, 6)])
        html = client.get("/ui/playlists").text
        assert html.count("/art/track/") == 4 and 'class="mosaic"' in html

    def test_track_rows_show_status_icons_with_tooltips(self, client):
        pid = add_pl(queued=0)
        db.replace_tracks(pid, [engine.TrackResult(1, "Song", ["Artist"], None)])
        html = client.get(f"/playlists/{pid}").text
        assert 'class="trk"' in html and 'title="Pending"' in html
        assert "<table" not in html


# -------------------------------------------------------------------- worker

class TestRobustness:
    HUGE = "99999999999999999999"

    def test_out_of_range_ids_are_not_found_not_500(self, client):
        # QA regression: ids beyond sqlite's 64-bit range raised OverflowError -> HTTP 500
        add_pl()
        for path in (f"/playlists/{self.HUGE}", f"/ui/playlists/{self.HUGE}/row", f"/ui/playlists/{self.HUGE}/head",
                     f"/ui/runs/{self.HUGE}/log", "/playlists/-1", "/playlists/0"):
            assert client.get(path).status_code in (200, 303, 404), path
        for path in ("sync", "toggle", "delete", "settings"):
            assert client.post(f"/playlists/{self.HUGE}/{path}", headers=HX).status_code in (200, 303), path
        assert db.get_playlist(2**63) is None and db.get_run(2**63) is None

    def test_absurd_default_interval_is_clamped(self, client, monkeypatch):
        # QA regression: a huge default interval made every sync end in OverflowError (and retry forever)
        client.post("/settings", data={"default_interval_hours": "99999999999999999999999"})
        assert db.get_settings()["default_interval_hours"] == str(config.MAX_INTERVAL_HOURS)
        monkeypatch.setattr(engine, "check_ytm", lambda s, ref: (True, "X", 5))
        client.post("/playlists", data={"url": "PLabcdefgh", "interval_hours": "99999999"}, headers=HX)
        assert db.get_playlist_by_ref("PLabcdefgh")["interval_hours"] == config.MAX_INTERVAL_HOURS

    def test_worker_survives_absurd_interval_in_db(self, monkeypatch):
        monkeypatch.setattr(engine, "sync_playlist", lambda s, r, **kw: engine.SyncResult("N", False, []))
        pid = add_pl()
        db.update_playlist(pid, interval_hours=2**62)
        assert worker.run_playlist(pid, "manual") == "ok"
        pl = db.get_playlist(pid)
        assert pl["queued"] == 0 and pl["next_run_at"]


class TestWorker:
    def test_run_stores_tracks_and_run(self, client, servers, monkeypatch):
        fake_ytm(monkeypatch)
        pid = add_pl()
        assert worker.run_pending_once() == 1
        pl = db.get_playlist(pid)
        assert pl["queued"] == 0 and pl["last_status"] == "warning"  # "Where Is My Mind" is nowhere
        assert pl["track_count"] == 3 and pl["name"] == "Road Trip" and pl["lidarr_found"] == 2
        assert pl["jellyfin_matched"] == 1 and pl["jellyfin_playlist_id"]
        assert db.parse_iso(pl["next_run_at"]) > db.now_dt()
        tracks = db.list_tracks(pid)
        assert [t["title"] for t in tracks] == ["Summer", "One More Time", "Where Is My Mind"]
        assert tracks[0]["artists"] == ["Calvin Harris"]
        by = {t["title"]: t for t in tracks}
        assert by["Where Is My Mind"]["lidarr_status"] == "not_found"
        assert by["Summer"]["jellyfin_status"] == "added"  # first sync creates the playlist
        assert by["One More Time"]["jellyfin_status"] == "missing"
        runs = db.list_runs(pid)
        assert len(runs) == 1 and runs[0]["status"] == "warning" and runs[0]["trigger"] == "added"
        assert runs[0]["finished_at"] and runs[0]["summary"]["tracks"] == 3
        assert "Road Trip" in runs[0]["log"]
        assert LK not in runs[0]["log"] and JK not in runs[0]["log"]
        assert worker.status() == {"running_playlist_id": None, "running_run_id": None, "queue": []}
        # nothing is due any more
        assert worker.run_pending_once() == 0
        # a second sync finds the track already in the Jellyfin playlist
        db.queue_playlist(pid)
        worker.run_pending_once()
        assert {t["title"]: t["jellyfin_status"] for t in db.list_tracks(pid)}["Summer"] == "in_playlist"
        assert [r["trigger"] for r in db.list_runs(pid)] == ["manual", "added"]

    def test_due_playlists_run_on_schedule(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        pid = add_pl(queued=0, next_run_at=db.iso_in_hours(-1), last_run_at=db.iso_in_hours(-25),
                     last_status="ok")
        later = add_pl("PLlater1", "Later", queued=0, next_run_at=db.iso_in_hours(5))
        paused = add_pl("PLpaused", "Paused", queued=0, next_run_at=db.iso_in_hours(-1), enabled=0)
        assert worker.run_pending_once() == 1
        assert db.list_runs(pid)[0]["trigger"] == "schedule"
        assert db.list_runs(later) == [] and db.list_runs(paused) == []

    def test_queued_playlists_run_even_when_paused(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        pid = add_pl(queued=1, enabled=0, last_status="ok", last_run_at=db.now_iso())
        assert worker.run_pending_once() == 1
        assert db.list_runs(pid)[0]["trigger"] == "manual"
        assert db.get_playlist(pid)["queued"] == 0

    def test_engine_error_is_recorded(self, monkeypatch):
        db.set_settings({"lidarr_api_key": "SUPER-SECRET-KEY"})

        def boom(settings, ref, **kw):
            kw["log"]("starting with SUPER-SECRET-KEY")
            raise engine.EngineError("Lidarr: could not connect to http://x")
        monkeypatch.setattr(engine, "sync_playlist", boom)
        pid = add_pl()
        worker.run_pending_once()
        pl = db.get_playlist(pid)
        assert pl["last_status"] == "error" and "could not connect" in pl["last_error"] and pl["queued"] == 0
        run = db.list_runs(pid)[0]
        assert run["status"] == "error" and "could not connect" in run["log"]
        assert "SUPER-SECRET-KEY" not in run["log"] and "***" in run["log"]
        assert db.parse_iso(pl["next_run_at"]) > db.now_dt()

    def test_unexpected_exception_does_not_kill_worker(self, monkeypatch):
        def boom(settings, ref, **kw):
            raise ValueError("kaboom")
        monkeypatch.setattr(engine, "sync_playlist", boom)
        pid = add_pl()
        assert worker.run_pending_once() == 1
        run = db.list_runs(pid)[0]
        assert run["status"] == "error" and "kaboom" in run["log"]

    def test_stop_during_run_requeues(self, monkeypatch):
        def stopped(settings, ref, **kw):
            worker._stop.set()
            return engine.SyncResult("Road Trip", False, [], status="warning", stopped=True,
                                     errors=["Stopped before completion."])
        monkeypatch.setattr(engine, "sync_playlist", stopped)
        pid = add_pl()
        worker.run_pending_once()
        assert db.list_runs(pid)[0]["status"] == "interrupted"
        assert db.get_playlist(pid)["queued"] == 1

    def test_deleted_while_running(self, monkeypatch):
        pid = add_pl()

        def res(settings, ref, **kw):
            db.delete_playlist(pid)
            return engine.SyncResult("Road Trip", False, [engine.TrackResult(1, "T", ["A"], None)])
        monkeypatch.setattr(engine, "sync_playlist", res)
        worker.run_pending_once()
        assert db.list_tracks(pid) == []

    def test_keeps_30_runs(self, monkeypatch):
        monkeypatch.setattr(engine, "sync_playlist",
                            lambda s, r, **kw: engine.SyncResult("N", False, []))
        pid = add_pl()
        for _ in range(35):
            worker.run_playlist(pid, "manual")
        assert len(db.list_runs(pid, 100)) == 30

    def test_log_is_written_while_running(self, monkeypatch):
        seen = {}

        def slow(settings, ref, **kw):
            kw["log"]("line one")
            for _ in range(60):
                run = db.get_run(worker.status()["running_run_id"])
                if "line one" in run["log"]:
                    seen["live"] = run["log"]
                    break
                threading.Event().wait(0.1)
            return engine.SyncResult("N", False, [])
        monkeypatch.setattr(engine, "sync_playlist", slow)
        monkeypatch.setattr(worker, "FLUSH_SECONDS", 0.1)
        add_pl()
        worker.run_pending_once()
        assert "line one" in seen.get("live", "")

    def test_start_is_idempotent_and_marks_interrupted(self):
        pid = add_pl()
        rid = db.create_run(pid, "manual")
        worker.start()
        worker.start()
        assert [t.name for t in threading.enumerate()].count("sync-worker") == 1
        assert db.get_run(rid)["status"] == "interrupted"
        worker.stop()
        assert "sync-worker" not in [t.name for t in threading.enumerate()]

    def test_lifespan_starts_worker_once(self):
        with TestClient(main.app) as c:
            assert c.get("/health").text == "ok"
            assert [t.name for t in threading.enumerate()].count("sync-worker") == 1
        assert "sync-worker" not in [t.name for t in threading.enumerate()]

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_dead_worker_shuts_the_app_down(self, monkeypatch):
        died = threading.Event()
        monkeypatch.setattr(worker, "_exit_process", died.set)
        monkeypatch.setattr(worker, "run_pending_once", lambda: (_ for _ in ()).throw(SystemExit))
        worker.start()
        worker._thread.join(5)
        assert died.is_set() and not worker.is_alive()

    def test_normal_stop_does_not_shut_the_app_down(self, monkeypatch):
        died = threading.Event()
        monkeypatch.setattr(worker, "_exit_process", died.set)
        worker.start()
        worker.stop()
        assert not worker.is_alive() and not died.is_set()

    def test_wake_runs_queued_playlist(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        monkeypatch.setattr(worker, "POLL_SECONDS", 60)
        worker.start()
        pid = add_pl()
        worker.wake()
        for _ in range(100):
            if db.get_playlist(pid)["last_run_at"]:
                break
            threading.Event().wait(0.1)
        assert db.get_playlist(pid)["last_status"] == "warning"


# --------------------------------------------------------------- detail page

class TestDetail:
    @pytest.fixture
    def synced(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        pid = add_pl()
        worker.run_pending_once()
        return pid

    def test_tracks_tab(self, client, synced):
        r = client.get(f"/playlists/{synced}")
        assert r.status_code == 200
        assert "<title>Road Trip · Playlist Sync</title>" in r.text
        for text in ("Summer", "Calvin Harris", "Motion", "Where Is My Mind", "Not found", "Added",
                     "Not in library", "in Lidarr", "in Jellyfin playlist", "Open in YouTube Music",
                     "Not found in Lidarr", "Not in Jellyfin yet", "Sync now"):
            assert text in r.text, text
        assert f"https://music.youtube.com/playlist?list=PLroad" in r.text
        assert LK not in r.text and JK not in r.text

    def test_track_filters(self, client, synced):
        t = client.get(f"/playlists/{synced}?tab=tracks&filter=lidarr").text
        assert "Where Is My Mind" in t and ">Summer<" not in t
        t = client.get(f"/playlists/{synced}?tab=tracks&filter=jellyfin").text
        assert "One More Time" in t and ">Summer<" not in t

    def test_runs_tab(self, client, synced):
        r = client.get(f"/playlists/{synced}?tab=runs")
        assert r.status_code == 200
        assert "First sync" in r.text and "Warnings" in r.text and "class=\"log\"" in r.text
        assert "YouTube Music playlist" in r.text  # engine log line

    def test_running_run_polls_its_log(self, client):
        pid = add_pl()
        rid = db.create_run(pid, "manual")
        db.append_log(rid, "step 1\n")
        html = client.get(f"/playlists/{pid}?tab=runs").text
        assert f'hx-get="/ui/runs/{rid}/log"' in html and "every 2s" in html and "<details" in html
        assert " open" in html
        r = client.get(f"/ui/runs/{rid}/log", headers=HX)
        assert r.status_code == 200 and r.text == "step 1"
        db.append_log(rid, "<b>x</b>\n")
        db.finish_run(rid, "ok")
        r = client.get(f"/ui/runs/{rid}/log", headers=HX)
        assert r.status_code == 286 and "&lt;b&gt;" in r.text  # 286 stops the htmx polling
        assert client.get("/ui/runs/9999/log").status_code == 404

    def test_head_polling_refreshes_page_when_done(self, client):
        pid = add_pl()
        r = client.get(f"/ui/playlists/{pid}/head?live=1", headers=HX)
        assert "hx-refresh" not in r.headers and "every 3s" in r.text
        db.update_playlist(pid, queued=0, last_status="ok")
        r = client.get(f"/ui/playlists/{pid}/head?live=1", headers=HX)
        assert r.headers.get("hx-refresh") == "true"
        r = client.get(f"/ui/playlists/{pid}/head", headers=HX)
        assert "hx-refresh" not in r.headers

    def test_settings_tab_save(self, client):
        pid = add_pl(queued=0)
        r = client.get(f"/playlists/{pid}?tab=settings")
        assert "Jellyfin playlist name" in r.text and "Download with Lidarr" in r.text
        assert "Delete playlist" in r.text and "hx-confirm" in r.text and "NOT deleted" in r.text
        r = client.post(f"/playlists/{pid}/settings",
                        data={"jellyfin_name": " My Mix ", "interval_hours": "6", "do_jellyfin": "on",
                              "enabled": "on"})
        assert r.status_code == 303 and r.headers["location"] == f"/playlists/{pid}?tab=settings"
        pl = db.get_playlist(pid)
        assert (pl["jellyfin_name"], pl["interval_hours"], pl["enabled"], pl["do_lidarr"], pl["do_jellyfin"]) == \
            ("My Mix", 6, 1, 0, 1)
        assert pl["jellyfin_mirror"] == 0
        assert "Playlist settings saved" in client.get(f"/playlists/{pid}?tab=settings").text
        client.post(f"/playlists/{pid}/settings", data={"do_jellyfin": "on", "jellyfin_mirror": "on"})
        assert db.get_playlist(pid)["jellyfin_mirror"] == 1
        assert "Mirrored in Jellyfin" in client.get(f"/playlists/{pid}").text

    def test_saved_jellyfin_playlist_survives_a_rename(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        pid = add_pl()
        worker.run_pending_once()
        pl_id = db.get_playlist(pid)["jellyfin_playlist_id"]
        servers.jf.playlists[pl_id]["Name"] = "Renamed in Jellyfin"
        db.queue_playlist(pid)
        worker.run_pending_once()
        assert list(servers.jf.playlists) == [pl_id] and db.get_playlist(pid)["jellyfin_playlist_id"] == pl_id

    def test_new_jellyfin_name_forgets_the_saved_playlist(self, client):
        pid = add_pl(queued=0, jellyfin_playlist_id="pl-old", jellyfin_name="Old")
        client.post(f"/playlists/{pid}/settings", data={"jellyfin_name": "Old", "do_jellyfin": "on"})
        assert db.get_playlist(pid)["jellyfin_playlist_id"] == "pl-old"
        client.post(f"/playlists/{pid}/settings", data={"jellyfin_name": "New", "do_jellyfin": "on"})
        assert db.get_playlist(pid)["jellyfin_playlist_id"] == ""

    def test_mirror_runs_from_the_worker(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        servers.jf.playlists["pl-x"] = {"Id": "pl-x", "Name": "Road Trip", "items": ["j-other", "j-summer"]}
        pid = add_pl(jellyfin_mirror=1)
        worker.run_pending_once()
        assert servers.jf.playlists["pl-x"]["items"] == ["j-summer"]
        run = db.list_runs(pid)[0]
        assert run["summary"]["jellyfin_removed"] == 1 and "1 removed from playlist" in ui_playlists.run_summary_text(run)

    def test_error_notice(self, client):
        pid = add_pl(queued=0, last_status="error", last_error="Lidarr: bad key")
        assert "Lidarr: bad key" in client.get(f"/playlists/{pid}").text

    def test_jellyfin_name_override_is_used(self, servers, monkeypatch):
        fake_ytm(monkeypatch)
        pid = add_pl(jellyfin_name="Custom Name")
        worker.run_pending_once()
        assert servers.jf.playlist_by_name("Custom Name") is not None

    def test_a_repeated_song_counts_once(self, client):
        pid = add_pl(queued=0, track_count=2, jellyfin_matched=1, jellyfin_duplicates=1)
        db.replace_tracks(pid, [
            engine.TrackResult(1, "Summer", ["Calvin Harris"], "Motion", jellyfin_status="in_playlist"),
            engine.TrackResult(2, "Summer", ["Calvin Harris"], None, jellyfin_status="duplicate",
                               jellyfin_note="same song as #1")])
        assert db.track_stats(pid)["jf_ok"] == 1 and db.track_stats(pid)["jf_dup"] == 1
        t = client.get(f"/playlists/{pid}").text
        for text in ("Same song", "same song as #1", "repeat an earlier song", "<strong>1/1</strong>"):
            assert text in t, text
        assert "<strong>1/1</strong>" in client.get("/").text  # library card meter


# ------------------------------------------------------------------ settings

class TestSettings:
    def test_page_renders_masked(self, client, servers):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "<title>Settings · Playlist Sync</title>" in r.text
        for text in ("Lidarr", "Jellyfin", "API key", "Test connection", "YouTube Music login",
                     "Metadata profile", "Singles and EPs"):
            assert text in r.text, text
        assert LK not in r.text and JK not in r.text
        assert "Saved. Leave blank to keep" in r.text
        assert servers.lidarr.url in r.text  # URL is not secret

    def test_blank_key_keeps_existing(self, client, servers):
        r = client.post("/settings", data={
            "lidarr_url": servers.lidarr.url + "/", "lidarr_api_key": "", "jellyfin_url": servers.jf.url,
            "jellyfin_api_key": "   ", "jellyfin_user": "Daniel", "default_interval_hours": "12",
            "finalize_timeout_min": "30", "lidarr_root": "/music", "lidarr_quality": "", "lidarr_metadata": ""})
        assert r.status_code == 303
        s = db.get_settings()
        assert s["lidarr_api_key"] == LK and s["jellyfin_api_key"] == JK
        assert s["lidarr_url"] == servers.lidarr.url  # trailing slash stripped
        assert (s["default_interval_hours"], s["finalize_timeout_min"], s["lidarr_root"]) == ("12", "30", "/music")
        assert "Settings saved" in client.get("/settings").text
        assert "Settings saved" not in client.get("/settings").text  # flash shows once

    def test_new_key_replaces(self, client, servers):
        client.post("/settings", data={"lidarr_url": servers.lidarr.url, "lidarr_api_key": "new-key-999",
                                       "jellyfin_api_key": "", "default_interval_hours": "24",
                                       "finalize_timeout_min": "20"})
        s = db.get_settings()
        assert s["lidarr_api_key"] == "new-key-999" and s["jellyfin_api_key"] == JK
        assert "new-key-999" not in client.get("/settings").text

    def test_test_connection_uses_form_values_and_saved_key(self, client, servers):
        r = client.post("/settings/test/lidarr", headers=HX,
                        data={"lidarr_url": servers.lidarr.url, "lidarr_api_key": ""})
        assert "Connected" in r.text and "test-res ok" in r.text
        r = client.post("/settings/test/lidarr", headers=HX,
                        data={"lidarr_url": servers.lidarr.url, "lidarr_api_key": "wrong"})
        assert "test-res err" in r.text and "wrong" not in r.text
        r = client.post("/settings/test/jellyfin", headers=HX,
                        data={"jellyfin_url": servers.jf.url, "jellyfin_user": "Daniel"})
        assert "Connected" in r.text and "Daniel" in r.text
        r = client.post("/settings/test/jellyfin", headers=HX, data={"jellyfin_url": "", "jellyfin_api_key": ""})
        db.set_settings({"jellyfin_api_key": ""})
        r = client.post("/settings/test/jellyfin", headers=HX, data={"jellyfin_url": ""})
        assert "Enter a URL" in r.text
        assert client.post("/settings/test/other", headers=HX).status_code == 404

    def test_lidarr_options_become_selects(self, client, servers):
        db.set_settings({"lidarr_quality": "Lossless"})
        r = client.get("/ui/lidarr-options", headers=HX)
        assert r.status_code == 200 and "<select" in r.text
        for name in ("/music2", "Lossless", "Any", "Standard", "Albums only", "Default (first in Lidarr)"):
            assert name in r.text, name
        assert 'value="Lossless" selected' in r.text

    def test_lidarr_options_fallback_to_inputs(self, client):
        assert client.get("/ui/lidarr-options", headers=HX).status_code == 204
        page = client.get("/settings").text
        assert 'name="lidarr_root"' in page and "<select" in page  # selects only for the interval

    def test_ytm_login_roundtrip(self, client, data_dir):
        f = data_dir / "browser.json"
        assert "Login configured" not in client.get("/settings").text
        r = client.post("/settings/ytm", data={"headers": ""})
        assert r.status_code == 303 and not f.exists()
        assert "Paste the request headers" in client.get("/settings").text
        client.post("/settings/ytm", data={"headers": "cookie: a=b"})
        assert not f.exists() and "SAPISIDHASH" in client.get("/settings").text
        client.post("/settings/ytm", data={"headers": "authorization: SAPISIDHASH 1_x\ncookie: a=b"})
        assert not f.exists() and "Could not use these headers" in client.get("/settings").text
        r = client.post("/settings/ytm", data={"headers": FAKE_HEADERS})
        assert r.status_code == 303 and f.exists()
        if os.name != "nt":  # POSIX permissions
            assert oct(f.stat().st_mode & 0o777) == "0o600"
        assert not (data_dir / "browser.json.tmp").exists()
        page = client.get("/settings").text
        assert "Login configured" in page and "FAKE/abc" not in page
        assert db.engine_settings().ytm_auth_path == str(f)
        client.post("/settings/ytm/remove")
        assert not f.exists() and db.engine_settings().ytm_auth_path == ""

    def test_ytm_path_is_passed_to_engine(self, client, servers, monkeypatch, data_dir):
        client.post("/settings/ytm", data={"headers": FAKE_HEADERS})
        seen = {}

        def spy(settings, ref, **kw):
            seen["path"] = settings.ytm_auth_path
            return engine.SyncResult("N", False, [])
        monkeypatch.setattr(engine, "sync_playlist", spy)
        add_pl()
        worker.run_pending_once()
        assert seen["path"] == str(data_dir / "browser.json")


# ------------------------------------------------------------------------ db

class TestDb:
    def test_env_seeds_settings_once(self, tmp_path, monkeypatch):
        d = tmp_path / "fresh"
        monkeypatch.setenv("DATA_DIR", str(d))
        monkeypatch.setenv("LIDARR_URL", "http://seed:8686")
        monkeypatch.setenv("DEFAULT_INTERVAL_HOURS", "12")
        db.init_db()
        s = db.get_settings()
        assert s["lidarr_url"] == "http://seed:8686" and s["default_interval_hours"] == "12"
        db.set_settings({"lidarr_url": "http://edited:1"})
        monkeypatch.setenv("LIDARR_URL", "http://other")
        db.init_db()
        assert db.get_settings()["lidarr_url"] == "http://edited:1"

    def test_wal_mode(self):
        with db.connect() as c:
            assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_unknown_column_rejected(self):
        pid = add_pl()
        with pytest.raises(ValueError):
            db.update_playlist(pid, bogus=1)


# --------------------------------------------------------------------- views

class TestViews:
    def test_rel_text(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

        def at(**kw):
            return (now + timedelta(**kw)).isoformat()
        assert ui_core.rel_text(at(seconds=-10), now) == "just now"
        assert ui_core.rel_text(at(minutes=-5), now) == "5 min ago"
        assert ui_core.rel_text(at(hours=-3), now) == "3 h ago"
        assert ui_core.rel_text(at(hours=9), now) == "in 9 h"
        assert ui_core.rel_text(at(days=-4), now) == "4 d ago"
        assert ui_core.rel_text(None, now) == "never"

    def test_duration(self):
        assert ui_core.duration_text("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:42+00:00") == "42 s"
        assert ui_core.duration_text("2026-01-01T00:00:00+00:00", "2026-01-01T00:03:05+00:00") == "3 min 05 s"
        assert ui_core.duration_text("2026-01-01T00:00:00+00:00", None) == "-"
