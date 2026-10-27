"""Artwork: thumbnail extraction (engine), migration V2 + storage (db, worker) and the /art proxy routes.
Everything stays on 127.0.0.1: the image host is the mock image server from mock_servers.py."""
import os
import sqlite3
import tempfile
import threading

# app.main reads DATA_DIR / SECRET_KEY at import time: point them at a throw-away dir first.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ps-test-art-import-"))
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["APP_PASSWORD"] = "pw-123"

import pytest
from starlette.testclient import TestClient

from app import db, engine, main, worker
from app.engine import Settings, read_playlist, read_playlist_full
from app.routes import art, auth
from mock_servers import FakeYTM, MockImages, install_fake_ytm, yt_track


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
def images(monkeypatch):
    """Mock image host, allowed (over http) by the /art allow-list."""
    srv = MockImages().start()
    monkeypatch.setattr(art, "ALLOWED_HOSTS", (srv.host,))
    monkeypatch.setattr(art, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(art, "_failed", {})
    yield srv
    srv.stop()


@pytest.fixture
def anon():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture
def client(anon):
    assert anon.post("/login", data={"password": "pw-123"}).status_code == 303
    return anon


def add_pl(cover_url="", ref="PLart", **kw):
    pid = db.add_playlist(ref, f"https://music.youtube.com/playlist?list={ref}", "Art", 2, 24, cover_url=cover_url)
    if kw:
        db.update_playlist(pid, **kw)
    return pid


def add_tracks(pid, thumbs):
    db.replace_tracks(pid, [engine.TrackResult(i, f"T{i}", ["A"], None, thumb_url=t)
                            for i, t in enumerate(thumbs, 1)])
    return [t["id"] for t in db.list_tracks(pid)]


def th(url, w):
    return {"url": url, "width": w, "height": w}


# -------------------------------------------------------------------- engine

class TestExtraction:
    def test_playlist_cover_is_the_largest_and_tracks_get_the_smallest_thumb_of_at_least_60px(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PL1": {
            "title": "T", "thumbnails": [th("c-100", 100), th("c-544", 544), th("c-300", 300)],
            "tracks": [yt_track("A", ["X"], "Al", [th("t-30", 30), th("t-120", 120), th("t-60", 60)]),
                       yt_track("B", ["X"], "Al", [th("small-30", 30), th("small-40", 40)])]}}))
        name, is_mix, tracks, cover = read_playlist_full(Settings(), "PL1")
        assert (name, is_mix, cover) == ("T", False, "c-544")
        assert [t["thumb"] for t in tracks] == ["t-60", "small-40"]  # none >= 60px: fall back to the largest

    def test_read_playlist_keeps_its_three_item_shape(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PL1": {"title": "T", "thumbnails": [th("c", 200)],
                                                       "tracks": [yt_track("A", ["X"], "Al", [th("t", 60)])]}}))
        name, is_mix, tracks = read_playlist(Settings(), "PL1")
        assert tracks[0]["thumb"] == "t"

    def test_watch_playlist_uses_the_singular_thumbnail_key_and_a_mix_has_no_cover(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM(
            playlists={"RDmix123456": {"title": "Mix", "thumbnails": [th("ignored", 500)], "tracks": []}},
            watch={"RDmix123456": [yt_track("A", ["X"], "Al", [th("w-60", 60), th("w-120", 120)],
                                            thumb_key="thumbnail")]}))
        _, is_mix, tracks, cover = read_playlist_full(Settings(), "RDmix123456")
        assert is_mix and cover == ""
        assert tracks[0]["thumb"] == "w-60"

    @pytest.mark.parametrize("thumbs", [None, [], "nope", [None, {}, {"url": None}, {"url": ""}]])
    def test_missing_or_malformed_thumbnails_are_empty_strings(self, monkeypatch, thumbs):
        raw = yt_track("A", ["X"], "Al")
        raw["thumbnails"] = thumbs
        install_fake_ytm(monkeypatch, FakeYTM({"PL1": {"title": "T", "thumbnails": thumbs, "tracks": [raw]}}))
        _, _, tracks, cover = read_playlist_full(Settings(), "PL1")
        assert cover == "" and tracks[0]["thumb"] == ""

    def test_thumbnails_without_widths_still_resolve(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PL1": {"title": "T", "thumbnails": [{"url": "only"}],
                                                       "tracks": [yt_track("A", ["X"], "Al", [{"url": "u"}])]}}))
        _, _, tracks, cover = read_playlist_full(Settings(), "PL1")
        assert cover == "only" and tracks[0]["thumb"] == "u"

    def test_sync_result_carries_cover_and_track_thumbs(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PL1": {"title": "T", "thumbnails": [th("cover", 400)],
                                                       "tracks": [yt_track("A", ["X"], "Al", [th("t1", 60)]),
                                                                  yt_track("B", ["X"], "Al")]}}))
        res = engine.sync_playlist(Settings(), "PL1", do_lidarr=False, do_jellyfin=False, log=lambda m="": None)
        assert res.cover_url == "cover"
        assert [t.thumb_url for t in res.tracks] == ["t1", ""]

    def test_check_ytm_remembers_the_cover_once(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PL1": {"title": "T", "thumbnails": [th("cover", 400)],
                                                       "tracks": [yt_track("A", ["X"], "Al")]}}))
        assert engine.check_ytm(Settings(), "https://music.youtube.com/playlist?list=PL1") == (True, "T", 1)
        assert engine.take_cover("PL1") == "cover"
        assert engine.take_cover("PL1") == ""  # handed out once
        assert engine.take_cover("") == ""


# ------------------------------------------------------------------ database

class TestDatabase:
    def test_v1_database_upgrades_in_place(self, tmp_path):
        path = tmp_path / "old"
        path.mkdir()
        con = sqlite3.connect(path / "app.db")
        con.executescript(db.SCHEMA_V1)
        con.execute("INSERT INTO playlists(ref, name) VALUES ('PLold', 'Old one')")
        con.execute("INSERT INTO tracks(playlist_id, position, title) VALUES (1, 1, 'Old track')")
        con.execute("PRAGMA user_version=1")
        con.commit()
        con.close()

        os.environ["DATA_DIR"] = str(path)
        db.init_db()

        pl = db.get_playlist_by_ref("PLold")
        assert pl["name"] == "Old one" and pl["cover_url"] == "" and pl["jellyfin_duplicates"] == 0
        assert db.list_tracks(pl["id"])[0]["thumb_url"] == ""
        assert db.list_tracks(pl["id"])[0]["jellyfin_note"] == ""
        assert db.add_playlist("PLnew", "u", "New", 1, 24, cover_url="https://x/c")
        with db.connect() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) == 5
        db.init_db()  # idempotent

    def test_add_playlist_stores_the_cover_and_rows_expose_it(self):
        pid = add_pl("https://lh3.googleusercontent.com/c")
        assert db.get_playlist(pid)["cover_url"] == "https://lh3.googleusercontent.com/c"
        assert db.list_playlists()[0]["cover_url"] == "https://lh3.googleusercontent.com/c"
        assert db.get_playlist(add_pl(ref="PLnone"))["cover_url"] == ""

    def test_track_rows_expose_id_and_thumb_url(self):
        pid = add_pl()
        add_tracks(pid, ["https://x/1"])
        (t,) = db.list_tracks(pid)
        assert t["id"] > 0 and t["thumb_url"] == "https://x/1"
        assert db.get_track(t["id"])["thumb_url"] == "https://x/1"
        assert db.get_track(10**30) is None

    def test_mosaic_track_ids_are_the_first_tracks_with_a_thumbnail(self):
        pid = add_pl()
        ids = add_tracks(pid, ["", "u2", "u3", "", "u5", "u6", "u7"])
        assert db.mosaic_track_ids(pid) == [ids[1], ids[2], ids[4], ids[5]]
        assert db.mosaic_track_ids(pid, 2) == [ids[1], ids[2]]
        assert db.mosaic_track_ids(add_pl(ref="PLother")) == []

    def test_worker_persists_cover_and_thumbs_and_keeps_an_old_cover_when_none_is_returned(self, monkeypatch):
        fake = install_fake_ytm(monkeypatch, FakeYTM({"PL1": {
            "title": "T", "thumbnails": [th("cover-1", 400)],
            "tracks": [yt_track("A", ["X"], "Al", [th("t1", 60)])]}}))
        pid = add_pl(ref="PL1", do_lidarr=0, do_jellyfin=0)
        worker.run_playlist(pid)
        assert db.get_playlist(pid)["cover_url"] == "cover-1"
        assert [t["thumb_url"] for t in db.list_tracks(pid)] == ["t1"]
        assert len(db.mosaic_track_ids(pid)) == 1

        fake.playlists["PL1"]["thumbnails"] = None
        worker.run_playlist(pid)
        assert db.get_playlist(pid)["cover_url"] == "cover-1"


# -------------------------------------------------------------------- routes

class TestUrlHelpers:
    @pytest.mark.parametrize("url", [
        "https://lh3.googleusercontent.com/abc=w60-h60", "https://i.ytimg.com/vi/x/hq.jpg",
        "https://yt3.ggpht.com/x", "https://googleusercontent.com/x"])
    def test_allowed(self, url):
        assert art.is_allowed(url)

    @pytest.mark.parametrize("url", [
        "http://lh3.googleusercontent.com/abc", "https://evil.example/x", "https://googleusercontent.com.evil.io/x",
        "https://evilgoogleusercontent.com/x", "https://127.0.0.1/x", "ftp://i.ytimg.com/x", "", "https:///x"])
    def test_rejected(self, url):
        assert not art.is_allowed(url)

    def test_google_size_suffixes_are_rewritten_others_are_untouched(self):
        assert art.resize("https://lh3.googleusercontent.com/abc=w60-h60-l90-rj", 544) == \
            "https://lh3.googleusercontent.com/abc=w544-h544-l90-rj"
        assert art.resize("https://yt3.ggpht.com/abc=s88-c-k", 120) == "https://yt3.ggpht.com/abc=s120-c-k"
        assert art.resize("https://lh3.googleusercontent.com/plain", 120) == "https://lh3.googleusercontent.com/plain"
        assert art.resize("https://i.ytimg.com/vi/x/hq.jpg?a=w60-h60", 120) == "https://i.ytimg.com/vi/x/hq.jpg?a=w60-h60"


class TestArtRoutes:
    def test_playlist_cover_is_proxied_with_cache_headers(self, client, images):
        pid = add_pl(f"{images.url}/cover-1?s=500")
        r = client.get(f"/art/playlist/{pid}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.headers["cache-control"] == "private, max-age=604800"
        assert r.content.startswith(bytes.fromhex("89504e47"))

    def test_track_thumbnail_is_proxied_and_cached_on_disk(self, client, images, data_dir):
        pid = add_pl()
        (tid,) = add_tracks(pid, [f"{images.url}/thumb-1"])
        r = client.get(f"/art/track/{tid}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert len(list((data_dir / "art").glob("*.png"))) == 1

    def test_second_request_is_a_cache_hit(self, client, images):
        pid = add_pl(f"{images.url}/cover-1")
        first = client.get(f"/art/playlist/{pid}")
        second = client.get(f"/art/playlist/{pid}")
        assert first.content == second.content
        assert images.requests == ["/cover-1"]

    def test_different_images_look_different(self, client, images):
        a, b = add_pl(f"{images.url}/one"), add_pl(f"{images.url}/two", ref="PL2")
        assert client.get(f"/art/playlist/{a}").content != client.get(f"/art/playlist/{b}").content

    def test_disallowed_host_is_404_and_never_fetched(self, client, images, monkeypatch):
        pid = add_pl(f"{images.url}/cover-1")
        monkeypatch.setattr(art, "ALLOWED_HOSTS", ("googleusercontent.com",))
        assert client.get(f"/art/playlist/{pid}").status_code == 404
        assert images.requests == []

    def test_plain_http_is_404_by_default(self, client, images, monkeypatch):
        monkeypatch.setattr(art, "ALLOWED_SCHEMES", ("https",))
        pid = add_pl(f"{images.url}/cover-1")
        assert client.get(f"/art/playlist/{pid}").status_code == 404
        assert images.requests == []

    @pytest.mark.parametrize("path", ["text", "huge", "missing"])
    def test_non_image_oversize_or_failing_upstream_is_404_and_leaves_nothing_cached(
            self, client, images, data_dir, path):
        pid = add_pl(f"{images.url}/{path}")
        assert client.get(f"/art/playlist/{pid}").status_code == 404
        assert list((data_dir / "art").iterdir()) == []

    def test_a_failed_url_is_not_hammered(self, client, images):
        pid = add_pl(f"{images.url}/missing")
        client.get(f"/art/playlist/{pid}")
        client.get(f"/art/playlist/{pid}")
        assert images.requests == ["/missing"]

    def test_unreachable_host_is_404(self, client, images):
        url = images.url
        images.stop()
        pid = add_pl(f"{url}/x")
        assert client.get(f"/art/playlist/{pid}").status_code == 404

    def test_unknown_ids_and_missing_urls_are_404(self, client, images):
        pid = add_pl()
        (tid,) = add_tracks(pid, [""])
        for url in (f"/art/playlist/{pid}", f"/art/track/{tid}", "/art/playlist/999", "/art/track/999",
                    f"/art/playlist/{10 ** 30}"):
            assert client.get(url).status_code == 404

    def test_login_is_required(self, anon, images):
        pid = add_pl(f"{images.url}/cover-1")
        for url in (f"/art/playlist/{pid}", "/art/track/1"):
            r = anon.get(url)
            assert r.status_code in (302, 303, 307) and r.headers["location"] == "/login"
        assert images.requests == []

    def test_concurrent_first_requests_leave_a_single_intact_file(self, images, data_dir):
        pid = add_pl(f"{images.url}/cover-1")
        out, barrier = [], threading.Barrier(6)

        def hit():
            barrier.wait()
            out.append(art.playlist_art(pid))

        threads = [threading.Thread(target=hit) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert [r.status_code for r in out] == [200] * 6
        files = list((data_dir / "art").iterdir())
        assert [f.suffix for f in files] == [".png"]
        assert files[0].read_bytes() == images._png["/cover-1"]
