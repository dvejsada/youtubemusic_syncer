"""Engine tests: matching regression, full sync against mock Lidarr/Jellyfin, checks, errors.

Run:  pytest tests/test_engine.py
Nothing here touches the network: YouTube Music and MusicBrainz are stubbed, Lidarr and
Jellyfin are in-process HTTP mocks on 127.0.0.1 (see mock_servers.py, conftest.py).
"""
import ast
import json
import os
import re

import pytest

from app import engine
from app.engine import (EngineError, Settings, check_jellyfin, check_lidarr, check_ytm, clean, lidarr_options,
                        norm, parse_playlist_ref, read_playlist, resolve, sim, split_title, sync_playlist)
from mock_servers import (FakeMusicBrainz, FakeYTM, MockJellyfin, MockLidarr, install_fake_musicbrainz,
                          install_fake_ytm, yt_track)

HERE = os.path.dirname(os.path.abspath(__file__))
LK, JK = "LK-secret-123", "JK-secret-456"


# ===================================================================== matching

class TestMatchingRegression:
    """The heuristics were tuned on real data; outputs must stay identical to ytm_sync.py.
    matching_baseline.json was recorded by running the original script's functions."""

    BASELINE = json.load(open(os.path.join(HERE, "matching_baseline.json"), encoding="utf-8"))

    def test_clean_identical_to_original(self):
        assert len(self.BASELINE["clean"]) > 30
        for src, expected in self.BASELINE["clean"].items():
            assert clean(src) == expected, src

    def test_norm_identical_to_original(self):
        for src, expected in self.BASELINE["norm"].items():
            assert norm(src) == expected, src

    def test_sim_identical_to_original(self):
        assert len(self.BASELINE["sim"]) > 100
        for key, expected in self.BASELINE["sim"].items():
            a, b = key.split("||")
            assert sim(a, b) == pytest.approx(expected, abs=1e-12), key

    def test_split_title_identical_to_original(self):
        for key, expected in self.BASELINE["split"].items():
            title, artists = key.split("||")
            artists = json.loads(artists.replace("'", '"'))
            assert list(split_title(title, artists)) == list(expected), key

    @pytest.mark.parametrize("a,b", [
        ("Happy Nation (U.S. Version)", "Happy Nation"),
        ("True (Bonus Edition)", "True"),
        ("The Fame Monster (International Deluxe)", "The Fame Monster"),
        ("Don’t Wake Me Up", "Don't Wake Me Up"),
    ])
    def test_editions_and_quotes_are_equal(self, a, b):
        assert sim(a, b) == 1.0

    @pytest.mark.parametrize("a,b", [
        ("Play", "就在此刻 (Play 中文版)"),
        ("Feelings", "Falling"),
        ("The Album", "Hit Collection, Volume 1: The Album New Edition"),
    ])
    def test_near_misses_stay_below_threshold(self, a, b):
        assert sim(a, b) < 0.85

    def test_split_title_video_title(self):
        # Values captured from the original script ("| OFFICIAL VIDEO ..." tail is dropped entirely).
        assert split_title("I'm an Albatraoz | OFFICIAL VIDEO (feat. Little Sis Nora)", ["AronChupa"]) \
            == (None, "I'm an Albatraoz")
        assert split_title("AronChupa - I'm an Albatraoz", ["AronChupa"]) == (None, "I'm an Albatraoz")
        assert split_title("DJ X - Track (Official 4K Video)", ["Channel"]) == ("DJ X", "Track (Official Video)")

    def test_clean_directed_by(self):
        assert clean("Cynical (Directed by Specter Berlin)") == "Cynical"

    def test_thresholds_and_skip_types_unchanged(self):
        assert engine.MusicBrainz.SKIP_TYPES == {"Compilation", "DJ-mix", "Live", "Mixtape/Street", "Remix",
                                                 "Interview", "Spokenword"}
        assert engine.PIPE_SUFFIX.pattern == r"\s*[|｜].*$"
        assert engine.JUNK_WORDS.pattern == r"\b(4k|hd|hq|official (music )?video|lyric video)\b"


class TestParsePlaylistRef:
    @pytest.mark.parametrize("arg,expected", [
        ("https://music.youtube.com/playlist?list=PLabc-123_x", "PLabc-123_x"),
        ("https://www.youtube.com/watch?v=zzz&list=RDCLAK5uy_k&index=2", "RDCLAK5uy_k"),
        ("  PLplain  ", "PLplain"),
        ("RDEM1234", "RDEM1234"),
    ])
    def test_extracts_id(self, arg, expected):
        assert parse_playlist_ref(arg) == expected

    def test_empty_raises(self):
        with pytest.raises(EngineError):
            parse_playlist_ref("   ")


# ====================================================================== resolve

class _FakeLid:
    def __init__(self, by_id=None, lookup_result=None):
        self.by_id, self.lookup_result = by_id or {}, lookup_result

    def lookup(self, artists, album):
        return self.lookup_result

    def by_mbid(self, mbid):
        return self.by_id.get(mbid)


class _FakeMB:
    def __init__(self, album=None, song=()):
        self._album, self._song = album, list(song)

    def album(self, artists, album):
        return self._album

    def song(self, artists, title):
        return self._song


def _rel(artist, title="Sexy Chick", fid="x"):
    return {"title": title, "foreignAlbumId": fid, "artist": {"artistName": artist, "foreignArtistId": "a-" + fid}}


class TestResolve:
    group = {"artists": ["David Guetta"], "album": None, "titles": ["Sexy Chick"]}

    def test_rejects_various_artists_and_wrong_artist_when_match_exists(self):
        lid = _FakeLid({"mb-va": _rel("Various Artists", fid="mb-va"),
                        "mb-akon": _rel("Akon", fid="mb-akon"),
                        "mb-dg": _rel("David Guetta", fid="mb-dg")})
        found, how, err = resolve(lid, _FakeMB(song=["mb-va", "mb-akon", "mb-dg"]), self.group)
        assert found["foreignAlbumId"] == "mb-dg"
        assert how == " (via MusicBrainz, song 'Sexy Chick')" and err is None

    def test_wrong_artist_is_only_a_fallback(self):
        lid = _FakeLid({"mb-va": _rel("Various Artists", fid="mb-va"), "mb-akon": _rel("Akon", fid="mb-akon")})
        found, how, _ = resolve(lid, _FakeMB(song=["mb-va", "mb-akon"]), self.group)
        assert found["foreignAlbumId"] == "mb-akon"
        assert "different artist name" in how

    def test_only_various_artists_is_not_found(self):
        lid = _FakeLid({"mb-va": _rel("Various Artists", fid="mb-va")})
        assert resolve(lid, _FakeMB(song=["mb-va"]), self.group) == (None, "", None)

    def test_order_lidarr_album_then_song_then_musicbrainz(self):
        g = {"artists": ["A"], "album": "Alb", "titles": ["Song"]}
        hit = _rel("A", "Alb", "l")
        assert resolve(_FakeLid(lookup_result=hit), _FakeMB(), g) == (hit, "", None)
        # no album hit but a song hit -> "via song"
        calls = []

        class Lid(_FakeLid):
            def lookup(self, artists, album):
                calls.append(album)
                return hit if album == "Song" else None
        found, how, _ = resolve(Lid(), _FakeMB(), g)
        assert calls == ["Alb", "Song"] and how == " (via song 'Song')"
        # MusicBrainz album id resolved through Lidarr by mbid
        found, how, _ = resolve(_FakeLid({"m": hit}), _FakeMB(album="m"), g)
        assert how == " (via MusicBrainz)"

    def test_lookup_errors_are_reported_not_raised(self):
        class Boom(_FakeLid):
            def lookup(self, *a):
                raise RuntimeError("503 Server Error for url: http://x/api?term=y")
        found, how, err = resolve(Boom(), _FakeMB(), {"artists": ["A"], "album": "B", "titles": ["T"]})
        assert found is None and err == "503 Server Error"


class TestMusicBrainzClient:
    def _rec(self, artist, rgs, title="Sexy Chick"):
        return {"title": title, "artist-credit": [{"name": artist}],
                "releases": [{"status": "Official", "date": "2009-01-01", "release-group": rg} for rg in rgs]}

    def test_song_ranking_prefers_single_skips_compilations_and_wrong_artist(self, monkeypatch):
        fake = install_fake_musicbrainz(monkeypatch, FakeMusicBrainz(recordings=[
            self._rec("Akon", [{"id": "akon-single", "primary-type": "Single", "title": "Sexy Chick"}]),
            self._rec("David Guetta", [
                {"id": "dg-album", "primary-type": "Album", "title": "One Love"},
                {"id": "dg-comp", "primary-type": "Album", "secondary-types": ["Compilation"], "title": "Hits"},
                {"id": "dg-single", "primary-type": "Single", "title": "Sexy Chick"},
            ])]))
        assert engine.MusicBrainz().song(["David Guetta"], "Sexy Chick") == ["dg-single", "dg-album"]
        assert fake.queries[0][0] == "recording"

    def test_album_skips_live_versions(self, monkeypatch):
        install_fake_musicbrainz(monkeypatch, FakeMusicBrainz(release_groups=[
            {"id": "live", "title": "Motion", "secondary-types": ["Live"], "artist-credit": [{"name": "Calvin Harris"}]},
            {"id": "studio", "title": "Motion", "artist-credit": [{"name": "Calvin Harris"}]},
            {"id": "other", "title": "Motion", "artist-credit": [{"name": "Somebody Else"}]}]))
        assert engine.MusicBrainz().album(["Calvin Harris"], "Motion (Deluxe)") == "studio"


# ================================================================ read_playlist

class TestReadPlaylist:
    def test_normal_playlist(self, monkeypatch):
        ytm = install_fake_ytm(monkeypatch, FakeYTM({"PL1": {"title": "Mine", "tracks": [
            yt_track("Summer", ["Calvin Harris"], "Motion")]}}))
        name, is_mix, tracks = read_playlist(Settings(), "https://music.youtube.com/playlist?list=PL1")
        assert (name, is_mix) == ("Mine", False)
        assert tracks == [{"title": "Summer", "artists": ["Calvin Harris"], "album": "Motion", "thumb": "",
                          "video_id": ""}]
        assert ("get_watch_playlist", "PL1") not in ytm.calls

    def test_mix_falls_back_to_watch_playlist(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM(
            playlists={"RDabcdef": RuntimeError("Server returned HTTP 400")},
            watch={"RDabcdef": [yt_track("Hello", ["Adele"], "25")]}))
        lines = []
        name, is_mix, tracks = read_playlist(Settings(), "RDabcdef", log=lines.append)
        assert is_mix is True and name == "YouTube Music Mix abcdef"
        assert tracks[0]["title"] == "Hello"
        assert any("auto-generated" in ln for ln in lines)

    def test_empty_non_mix_playlist_is_a_clear_error(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PLx": {"title": "T", "tracks": []}}))
        with pytest.raises(EngineError, match="no tracks"):
            read_playlist(Settings(), "PLx")

    def test_private_playlist_error(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PLp": RuntimeError("Server returned HTTP 403: Forbidden")}))
        with pytest.raises(EngineError, match="Could not read playlist PLp.*private"):
            read_playlist(Settings(), "PLp")

    def test_broken_login_falls_back_to_anonymous(self, monkeypatch):
        from app import engine
        anon = FakeYTM({"PL1": {"title": "Mine", "tracks": [yt_track("Summer", ["Calvin Harris"], "Motion")]}})
        def make(settings):
            if settings.ytm_auth_path:
                raise RuntimeError("authorization header expired")
            return anon
        monkeypatch.setattr(engine, "_make_ytm", make)
        lines = []
        name, _, tracks = read_playlist(Settings(ytm_auth_path="/data/browser.json"), "PL1", log=lines.append)
        assert name == "Mine" and len(tracks) == 1
        assert any("login failed" in ln for ln in lines)
        ok, title, n = engine.check_ytm(Settings(ytm_auth_path="/data/browser.json"), "PL1")
        assert (ok, title, n) == (True, "Mine", 1)

    def test_no_login_errors_are_not_retried(self, monkeypatch):
        from app import engine
        made = []
        def make(settings):
            made.append(settings.ytm_auth_path)
            return FakeYTM({"PLp": RuntimeError("Server returned HTTP 403: Forbidden")})
        monkeypatch.setattr(engine, "_make_ytm", make)
        with pytest.raises(EngineError):
            read_playlist(Settings(), "PLp")
        assert made == [""]

    def test_video_without_album_gets_song_version_from_search(self, monkeypatch):
        def search(q, f):
            assert f == "songs"
            return [{"title": "I'm an Albatraoz", "artists": [{"name": "AronChupa"}], "album": {"name": "I'm an Albatraoz"}}]
        install_fake_ytm(monkeypatch, FakeYTM({"PLv": {"title": "V", "tracks": [
            yt_track("AronChupa - I'm an Albatraoz | OFFICIAL VIDEO (feat. Little Sis Nora)", ["AronChupa"])]}},
            search_results=search))
        _, _, tracks = read_playlist(Settings(), "PLv")
        assert tracks == [{"title": "I'm an Albatraoz", "artists": ["AronChupa"], "album": "I'm an Albatraoz", "thumb": "",
                          "video_id": ""}]

    def test_search_result_with_wrong_artist_is_ignored(self, monkeypatch):
        install_fake_ytm(monkeypatch, FakeYTM({"PLv": {"title": "V", "tracks": [yt_track("Song", ["Real Artist"])]}},
                         search_results=lambda q, f: [{"title": "Song", "artists": [{"name": "Other"}],
                                                       "album": {"name": "Wrong"}}]))
        _, _, tracks = read_playlist(Settings(), "PLv")
        assert tracks[0]["album"] is None


# ================================================================ full sync

@pytest.fixture
def env(monkeypatch):
    lidarr = MockLidarr(LK).start()
    jf = MockJellyfin(JK, users=("Daniel", "Other")).start()

    # Lidarr library: Daft Punk exists (Discovery unmonitored), Radiohead's OK Computer is complete
    dp = lidarr.add_library_artist("Daft Punk", "dp", monitored=False)
    lidarr.add_library_album(dp, "Discovery", "alb-disc", monitored=False)
    rh = lidarr.add_library_artist("Radiohead", "rh")
    lidarr.add_library_album(rh, "OK Computer", "alb-okc", monitored=True, percent=100)
    # what the metadata server knows
    motion = lidarr.add_release("Motion", "Calvin Harris", "alb-motion", "art-ch", terms=["Calvin Harris Motion", "Rihanna Motion"])
    lidarr.add_release("Discovery", "Daft Punk", "alb-disc", "dp")
    lidarr.add_release("OK Computer", "Radiohead", "alb-okc", "rh")
    lidarr.add_release("Levitating", "Dua Lipa", "alb-lev", "art-dl", "Single")
    assert motion

    jf.add_item("j-summer", "Summer", "Calvin Harris", "Motion")
    jf.add_item("j-pa", "Paranoid Android", "Radiohead", "OK Computer")

    ytm = install_fake_ytm(monkeypatch, FakeYTM({"PLtest": {"title": "Road Trip", "tracks": [
        yt_track("Summer", ["Calvin Harris"], "Motion"),                       # 1 new artist + album
        yt_track("Outside", ["Calvin Harris", "Ellie Goulding"], "Motion"),    # 2 same album group
        yt_track("One More Time", ["Daft Punk"], "Discovery"),                 # 3 existing artist, unmonitored album
        yt_track("Paranoid Android", ["Radiohead"], "OK Computer"),            # 4 already downloaded
        yt_track("Levitating", ["Dua Lipa"]),                                  # 5 no album -> via song
        yt_track("Where Is My Mind", ["Nobody"], "Nothing"),                   # 6 not found anywhere
        yt_track("We Found Love", ["Rihanna", "Calvin Harris"], "Motion"),     # 7 resolves to same release as 1
    ]}}))
    settings = Settings(lidarr_url=lidarr.url, lidarr_api_key=LK, jellyfin_url=jf.url, jellyfin_api_key=JK,
                        jellyfin_user="Daniel", finalize_timeout_min=1)
    yield type("Env", (), dict(lidarr=lidarr, jf=jf, ytm=ytm, settings=settings))
    lidarr.stop()
    jf.stop()


def run(env, **kw):
    lines = []
    kw.setdefault("log", lines.append)
    res = sync_playlist(env.settings, "https://music.youtube.com/playlist?list=PLtest", **kw)
    return res, lines


class TestFullSync:
    def test_lidarr_statuses_per_track(self, env):
        env.lidarr.busy_polls = 2
        res, lines = run(env, do_jellyfin=False)
        s = {t.position: t for t in res.tracks}
        assert res.playlist_name == "Road Trip" and res.is_mix is False and len(res.tracks) == 7

        # 1+2: new artist -> added; both tracks of the album group share the result
        assert s[1].lidarr_status == s[2].lidarr_status == "added"
        assert s[1].lidarr_release == s[2].lidarr_release == "Calvin Harris - Motion [Album]"
        # 3: existing artist, unmonitored album -> monitored
        assert s[3].lidarr_status == "monitored" and s[3].lidarr_release == "Daft Punk - Discovery [Album]"
        # 4: complete album
        assert s[4].lidarr_status == "downloaded"
        # 5: no album on YouTube, found as a single through the song title
        assert s[5].lidarr_status == "added" and s[5].lidarr_release == "Dua Lipa - Levitating [Single]"
        assert "via song 'Levitating'" in s[5].lidarr_note
        # 6: nowhere
        assert s[6].lidarr_status == "not_found" and "add manually" in s[6].lidarr_note
        # 7: same release as 1
        assert s[7].lidarr_status == "duplicate" and s[7].lidarr_release == s[1].lidarr_release
        assert all(t.jellyfin_status == "skipped" for t in res.tracks)

        assert (res.lidarr_found, res.lidarr_failed) == (6, 1)
        assert res.status == "warning" and res.errors == []

    def test_lidarr_writes(self, env):
        env.lidarr.busy_polls = 2
        run(env, do_jellyfin=False)
        lw = env.lidarr

        # new artist: added with monitor none, only the wanted album
        posts = [b for m, p, b in lw.writes if m == "POST" and p.endswith("/album")]
        assert len(posts) == 2  # Motion + Levitating (new artists); Discovery is an existing artist
        motion = next(b for b in posts if b["foreignAlbumId"] == "alb-motion")
        assert motion["monitored"] is True and motion["addOptions"] == {"searchForNewAlbum": True}
        art = motion["artist"]
        assert art["monitorNewItems"] == "none" and art["addOptions"]["monitor"] == "none"
        assert art["addOptions"]["albumsToMonitor"] == ["alb-motion"]
        assert (art["rootFolderPath"], art["qualityProfileId"], art["metadataProfileId"]) == ("/music", 1, 1)

        # existing artist album: monitored, artist monitored, AlbumSearch for it
        disc = lw.album_by_foreign("alb-disc")
        assert disc["monitored"] is True
        assert any(m == "PUT" and p.endswith("/album/monitor") and b == {"albumIds": [disc["id"]], "monitored": True}
                   for m, p, b in lw.writes)
        dp = next(a for a in lw.artists if a["foreignArtistId"] == "dp")
        assert dp["monitored"] is True
        assert disc["id"] in lw.searched_album_ids()

        # finalize: Lidarr's refresh had unmonitored the new albums -> re-monitored and searched
        for fid in ("alb-motion", "alb-lev"):
            alb = lw.album_by_foreign(fid)
            assert alb["monitored"] is True, fid
            assert alb["id"] in lw.searched_album_ids(), fid
        # the already-complete album triggered nothing
        assert lw.album_by_foreign("alb-okc")["id"] not in lw.searched_album_ids()
        # finalize polled until the refresh was done
        assert lw.busy_polls == 0

    def test_log_lines(self, env):
        res, lines = run(env)
        text = "\n".join(lines)
        assert "YouTube Music playlist 'Road Trip': 7 tracks" in text
        assert "== Lidarr: 6 albums/singles to check ==" in text   # Motion, Discovery, OK Computer, Levitating, Nothing, Rihanna/Motion
        assert "  ✓  Calvin Harris - Motion  ->  Calvin Harris - Motion [Album]: added" in text
        assert "  ?  Nobody - Nothing  - not on MusicBrainz, add manually" in text
        assert "  =  Rihanna - Motion  -> same release as above, skipped" in text
        assert "Waiting for Lidarr to finish importing new artists" in text
        assert "== Jellyfin: matching 7 tracks ==" in text
        assert "not in library yet" in text
        assert LK not in text and JK not in text

    def test_jellyfin_playlist_created_then_only_new_items_appended(self, env):
        res1, _ = run(env, do_lidarr=False)
        s = {t.position: t for t in res1.tracks}
        pl = env.jf.playlist_by_name("Road Trip")
        assert pl["items"] == ["j-summer", "j-pa"]
        assert (s[1].jellyfin_status, s[4].jellyfin_status) == ("added", "added")
        assert s[1].jellyfin_item_id == "j-summer"
        assert all(s[i].jellyfin_status == "missing" for i in (2, 3, 5, 6, 7))
        assert (res1.jellyfin_matched, res1.jellyfin_added, res1.jellyfin_playlist_id) == (2, 2, pl["Id"])
        assert all(t.lidarr_status == "skipped" for t in res1.tracks)
        assert res1.status == "warning"
        creates = env.jf.writes_to("POST", "/Playlists")
        assert len(creates) == 1 and creates[0][2]["Ids"] == ["j-summer", "j-pa"]

        # library grows (Lidarr downloaded more) -> second run only appends the new items
        env.jf.add_item("j-out", "Outside", "Calvin Harris", "Motion")
        env.jf.add_item("j-omt", "One More Time", "Daft Punk", "Discovery")
        env.jf.clear_writes()
        res2, lines = run(env, do_lidarr=False)
        s = {t.position: t for t in res2.tracks}
        assert [env.jf.playlist_by_name("Road Trip")["items"]] == [["j-summer", "j-pa", "j-out", "j-omt"]]
        assert len(env.jf.playlists) == 1
        assert (s[1].jellyfin_status, s[4].jellyfin_status) == ("in_playlist", "in_playlist")
        assert (s[2].jellyfin_status, s[3].jellyfin_status) == ("added", "added")
        assert (res2.jellyfin_matched, res2.jellyfin_added) == (4, 2)
        assert res2.jellyfin_playlist_id == pl["Id"]
        # exactly one write: append of the two new ids
        assert len(env.jf.writes) == 1
        assert env.jf.writes[0][1] == f"/Playlists/{pl['Id']}/Items"
        assert env.jf.writes[0][2]["ids"] == "j-out,j-omt"
        assert any("exists: added 2 new track(s)" in ln for ln in lines)

        # third run: nothing new, nothing written, nothing duplicated
        env.jf.clear_writes()
        res3, _ = run(env, do_lidarr=False)
        assert env.jf.writes == [] and res3.jellyfin_added == 0
        assert env.jf.playlist_by_name("Road Trip")["items"] == ["j-summer", "j-pa", "j-out", "j-omt"]

    def test_saved_playlist_is_used_even_after_a_rename_in_jellyfin(self, env):
        res1, _ = run(env, do_lidarr=False)
        pl_id = res1.jellyfin_playlist_id
        env.jf.playlists[pl_id]["Name"] = "Renamed in Jellyfin"
        env.jf.add_item("j-out", "Outside", "Calvin Harris", "Motion")
        res2, lines = run(env, do_lidarr=False, jellyfin_playlist_id=pl_id)
        assert list(env.jf.playlists) == [pl_id] and res2.jellyfin_playlist_id == pl_id
        assert env.jf.playlists[pl_id]["items"] == ["j-summer", "j-pa", "j-out"]
        assert f"  playlist id {pl_id} exists: added 1 new track(s)" in lines

    def test_gone_saved_playlist_falls_back_to_the_name(self, env):
        res1, _ = run(env, do_lidarr=False)
        old = env.jf.playlists.pop(res1.jellyfin_playlist_id)
        res2, _ = run(env, do_lidarr=False, jellyfin_playlist_id=old["Id"])
        assert res2.jellyfin_playlist_id not in ("", old["Id"])
        assert env.jf.playlist_by_name("Road Trip")["items"] == ["j-summer", "j-pa"]
        # an existing playlist with the name is found again instead of creating another one
        res3, _ = run(env, do_lidarr=False, jellyfin_playlist_id="no-such-id")
        assert res3.jellyfin_playlist_id == res2.jellyfin_playlist_id and len(env.jf.playlists) == 1

    def test_jellyfin_name_override(self, env):
        run(env, do_lidarr=False, jellyfin_name="My Custom Name")
        assert env.jf.playlist_by_name("My Custom Name") is not None
        assert env.jf.playlist_by_name("Road Trip") is None

    def test_full_second_run_is_idempotent(self, env):
        run(env)
        env.lidarr.clear_writes()
        res, _ = run(env)
        # nothing is added to Lidarr twice
        assert not env.lidarr.writes_to("POST", "/album")
        assert len(env.lidarr.artists) == 4  # dp, rh + the two artists added by run 1
        s = {t.position: t for t in res.tracks}
        assert s[1].lidarr_status == "monitored"       # added earlier, still being searched for
        assert len(env.jf.playlists) == 1

    def test_all_found_is_ok(self, env):
        env.ytm.playlists["PLtest"]["tracks"] = [yt_track("Paranoid Android", ["Radiohead"], "OK Computer")]
        res, _ = run(env)
        assert res.status == "ok"
        assert res.tracks[0].lidarr_status == "downloaded" and res.tracks[0].jellyfin_status == "added"

    def test_new_artist_not_visible_yet_is_noted(self, env):
        env.lidarr.hide_added_albums = True
        res, lines = run(env, do_jellyfin=False)
        assert any("not visible in Lidarr yet" in ln for ln in lines)
        assert "not visible in Lidarr yet" in res.tracks[0].lidarr_note

    def test_existing_artist_without_the_album_adds_it_to_that_artist(self, env):
        env.ytm.playlists["PLtest"]["tracks"] = [yt_track("Aerodynamic", ["Daft Punk"], "Homework")]
        env.lidarr.add_release("Homework", "Daft Punk", "alb-hw", "dp")
        res, _ = run(env, do_jellyfin=False)
        t = res.tracks[0]
        assert t.lidarr_status == "added" and "existing artist" in t.lidarr_note
        post = env.lidarr.writes_to("POST", "/album")[0][2]
        assert post["artistId"] == next(a["id"] for a in env.lidarr.artists if a["foreignArtistId"] == "dp")

    def test_track_without_artist_is_not_found(self, env):
        env.ytm.playlists["PLtest"]["tracks"] = [yt_track("Mystery", [], "Album")]
        res, _ = run(env, do_jellyfin=False)
        assert res.tracks[0].lidarr_status == "not_found" and res.status == "warning"

    def test_via_musicbrainz_when_lidarr_text_search_misses(self, env, monkeypatch):
        env.ytm.playlists["PLtest"]["tracks"] = [yt_track("Sexy Chick", ["David Guetta"], "One Love")]
        env.lidarr.add_release("One Love", "David Guetta", "mb-dg", "art-dg", terms=[])        # reachable by mbid only
        env.lidarr.add_release("Sexy Chick", "Akon", "mb-akon", "art-akon", "Single", terms=[])
        install_fake_musicbrainz(monkeypatch, FakeMusicBrainz(
            release_groups=[{"id": "mb-dg", "title": "One Love", "artist-credit": [{"name": "David Guetta"}]}]))
        res, _ = run(env, do_jellyfin=False)
        t = res.tracks[0]
        assert t.lidarr_status == "added" and t.lidarr_release == "David Guetta - One Love [Album]"
        assert t.lidarr_note == "via MusicBrainz"

    def test_via_musicbrainz_song_picks_matching_artist_release(self, env, monkeypatch):
        env.ytm.playlists["PLtest"]["tracks"] = [yt_track("Sexy Chick", ["David Guetta"])]
        env.lidarr.add_release("Sexy Chick", "Akon", "mb-akon", "art-akon", "Single", terms=[])
        env.lidarr.add_release("One Love", "David Guetta", "mb-dg", "art-dg", terms=[])
        install_fake_musicbrainz(monkeypatch, FakeMusicBrainz(recordings=[
            {"title": "Sexy Chick", "artist-credit": [{"name": "David Guetta"}],
             "releases": [{"status": "Official", "date": "2009", "release-group":
                           {"id": "mb-dg", "primary-type": "Album", "title": "One Love"}}]},
        ]))
        res, _ = run(env, do_jellyfin=False)
        assert res.tracks[0].lidarr_release == "David Guetta - One Love [Album]"
        assert "via MusicBrainz, song 'Sexy Chick'" in res.tracks[0].lidarr_note

    def test_per_track_lidarr_error_does_not_raise(self, env):
        orig = env.lidarr.handle

        def handle(method, path, q, body, headers):
            if method == "POST" and path.endswith("/album"):
                return 500, {"error": f"server says key {LK} is bad"}
            return orig(method, path, q, body, headers)
        env.lidarr.handle = handle
        res, lines = run(env, do_jellyfin=False)
        s = {t.position: t for t in res.tracks}
        assert s[1].lidarr_status == "error" and s[1].lidarr_note
        assert s[3].lidarr_status == "monitored"   # other albums unaffected
        assert res.lidarr_failed >= 1 and res.status == "warning"
        # the API key never leaks into notes or the log
        assert all(LK not in t.lidarr_note for t in res.tracks)
        assert LK not in "\n".join(lines)


class TestJellyfinMatching:
    def tracks(self, env, *tracks):
        env.ytm.playlists["PLtest"]["tracks"] = list(tracks)

    def test_different_song_by_the_same_artist_is_no_match(self, env):
        env.jf.add_item("j-again", "Outside Again", "Calvin Harris", "Other")
        self.tracks(env, yt_track("Outside", ["Calvin Harris"]))
        res, _ = run(env, do_lidarr=False)
        assert res.tracks[0].jellyfin_status == "missing" and res.jellyfin_matched == 0

    def test_cover_by_another_artist_is_no_match(self, env):
        self.tracks(env, yt_track("Paranoid Android", ["Brad Mehldau"]))
        res, _ = run(env, do_lidarr=False)
        assert res.tracks[0].jellyfin_status == "missing"

    def test_any_listed_artist_can_match(self, env):
        env.jf.add_item("j-wfl", "We Found Love", "Calvin Harris", "Motion")
        self.tracks(env, yt_track("We Found Love", ["Rihanna", "Calvin Harris"], "Motion"))
        res, _ = run(env, do_lidarr=False)
        assert res.tracks[0].jellyfin_item_id == "j-wfl" and res.tracks[0].jellyfin_status == "added"

    def test_tracks_matching_the_same_song_count_once(self, env):
        self.tracks(env, yt_track("Summer", ["Calvin Harris"], "Motion"),
                    yt_track("Paranoid Android", ["Radiohead"], "OK Computer"),
                    yt_track("Summer", ["Calvin Harris"]))
        res, lines = run(env, do_lidarr=False)
        s = {t.position: t for t in res.tracks}
        assert env.jf.playlist_by_name("Road Trip")["items"] == ["j-summer", "j-pa"]
        assert [s[i].jellyfin_status for i in (1, 2, 3)] == ["added", "added", "duplicate"]
        assert s[3].jellyfin_note == "same song as #1" and s[3].jellyfin_item_id == "j-summer"
        assert (res.jellyfin_matched, res.jellyfin_added, res.jellyfin_duplicates) == (2, 2, 1)
        assert res.status == "ok"
        assert "    = #3 Calvin Harris - Summer  (same song as #1)" in lines

        res2, _ = run(env, do_lidarr=False)  # still a duplicate once the song is in the playlist
        assert [t.jellyfin_status for t in res2.tracks] == ["in_playlist", "in_playlist", "duplicate"]
        assert (res2.jellyfin_matched, res2.jellyfin_duplicates) == (2, 1)

    def test_appends_jellyfin_does_not_keep_are_reported(self, env):
        run(env, do_lidarr=False)
        env.jf.add_item("j-out", "Outside", "Calvin Harris", "Motion")
        env.jf.drop_playlist_appends = True
        res, lines = run(env, do_lidarr=False)
        s = {t.position: t for t in res.tracks}
        assert s[2].jellyfin_status == "not_added" and s[1].jellyfin_status == "in_playlist"
        assert (res.jellyfin_matched, res.jellyfin_added) == (2, 0)
        assert res.status == "error"
        assert res.errors == ["Jellyfin accepted 1 track(s) for playlist 'Road Trip' but did not keep them"]
        assert "    - #2 Calvin Harris, Ellie Goulding - Outside" in lines


def _lookups(env) -> list[str]:
    return [p for m, p in env.lidarr.requests if "album/lookup" in p]


def _jf_searches(env) -> list[str]:
    return [p for m, p in env.jf.requests if "searchTerm" in p and "Audio" in p]


class TestIncrementalSync:
    def test_second_run_reuses_earlier_lookups(self, env, monkeypatch):
        mb = install_fake_musicbrainz(monkeypatch)
        cache = engine.Cache()
        res1, _ = run(env, cache=cache)
        assert _lookups(env) and mb.queries and _jf_searches(env)
        assert {k for k, _ in cache.data} == {"lidarr", "jellyfin"}
        env.lidarr.requests.clear(), env.jf.requests.clear(), mb.queries.clear()

        res2, lines = run(env, cache=cache)
        assert _lookups(env) == [] and mb.queries == []       # nothing looked up in Lidarr / MusicBrainz again
        searched = " ".join(_jf_searches(env))
        assert "Summer" not in searched and "Paranoid" not in searched   # matches confirmed in one request
        assert "Outside" in searched                                     # not in the library yet: searched
        s1, s2 = {t.position: t for t in res1.tracks}, {t.position: t for t in res2.tracks}
        assert [s2[i].lidarr_status for i in range(1, 8)] == \
            ["monitored", "monitored", "monitored", "downloaded", "monitored", "not_found", "duplicate"]
        assert all(s2[i].lidarr_release == s1[i].lidarr_release for i in range(1, 8))
        assert [s2[i].jellyfin_status for i in (1, 4)] == ["in_playlist", "in_playlist"]
        assert "  (5 of them found by an earlier sync, not looked up again)" in lines
        assert "  matched 2/7 (2 remembered from earlier syncs)" in lines
        assert any("Nobody - Nothing  - not on MusicBrainz, add manually (looked up before" in ln for ln in lines)

    def test_not_found_is_looked_up_again_after_a_week(self, env, monkeypatch):
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(engine, "_now", lambda: clock["t"])
        cache = engine.Cache()
        run(env, do_jellyfin=False, cache=cache)
        env.lidarr.requests.clear()
        clock["t"] += 6 * 86400
        run(env, do_jellyfin=False, cache=cache)
        assert not any("Nobody" in p for p in _lookups(env))
        clock["t"] += 2 * 86400
        run(env, do_jellyfin=False, cache=cache)
        assert any("Nobody" in p for p in _lookups(env))

    def test_album_removed_from_lidarr_is_added_again_by_its_id(self, env):
        cache = engine.Cache()
        run(env, do_jellyfin=False, cache=cache)
        env.lidarr.albums = [a for a in env.lidarr.albums if a["foreignAlbumId"] != "alb-lev"]
        env.lidarr.requests.clear()
        res, _ = run(env, do_jellyfin=False, cache=cache)
        assert any("lidarr%3Aalb-lev" in p or "lidarr:alb-lev" in p for p in _lookups(env))
        assert not any("Levitating" in p for p in _lookups(env))
        assert res.tracks[4].lidarr_status == "added" and env.lidarr.album_by_foreign("alb-lev")

    def test_jellyfin_item_gone_is_searched_again(self, env):
        cache = engine.Cache()
        run(env, do_lidarr=False, cache=cache)
        env.jf.items = [it for it in env.jf.items if it["Id"] != "j-summer"]
        res, _ = run(env, do_lidarr=False, cache=cache)
        assert res.tracks[0].jellyfin_status == "missing"

    def test_jellyfin_match_is_checked_again_when_the_track_changed(self, env):
        cache = engine.Cache()
        run(env, do_lidarr=False, cache=cache)
        env.ytm.playlists["PLtest"]["tracks"][0] = yt_track("Summer", ["Calvin Harris"], "18 Months")
        env.jf.requests.clear()
        res, _ = run(env, do_lidarr=False, cache=cache)
        assert "Summer" in " ".join(_jf_searches(env))   # album changed: searched, not reused
        assert res.tracks[0].jellyfin_status == "in_playlist"

    def test_jellyfin_match_is_checked_again_after_a_week_and_kept_if_nothing_better(self, env, monkeypatch):
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(engine, "_now", lambda: clock["t"])
        cache = engine.Cache()
        run(env, do_lidarr=False, cache=cache)
        env.jf.requests.clear()
        clock["t"] += 8 * 86400
        env.jf.items[0]["Name"] = "Sumer"   # renamed in Jellyfin: the search no longer finds it
        res, _ = run(env, do_lidarr=False, cache=cache)
        assert "Summer" in " ".join(_jf_searches(env))     # older than a week: searched again
        assert res.tracks[0].jellyfin_item_id == "j-summer" and res.tracks[0].jellyfin_status == "in_playlist"

    def test_cache_loads_only_the_entries_a_sync_needs(self, env):
        cache = engine.Cache()
        run(env, cache=cache)
        stored = dict(cache.data)
        calls = []

        def loader(kind, keys):
            calls.append((kind, list(keys)))
            return {k: v for (kd, k), v in stored.items() if kd == kind and k in keys}
        res, lines = run(env, cache=engine.Cache(loader=loader))
        assert [kind for kind, _ in calls] == ["lidarr", "jellyfin"]   # one batch per phase
        assert len(calls[1][1]) == 7 and "  matched 2/7 (2 remembered from earlier syncs)" in lines
        assert engine.Cache(loader=lambda k, keys: 1 / 0).prefetch("ytm", ["a"]) is None  # a broken store is ignored

    def test_entries_of_another_version_are_ignored(self):
        cache = engine.Cache({("jellyfin", "k"): {"id": "x", "v": engine.CACHE_VERSION - 1}})
        assert cache.get("jellyfin", "k") is None
        cache.put("jellyfin", "k", {"id": "y"})
        assert cache.get("jellyfin", "k")["id"] == "y" and cache.changed == {("jellyfin", "k")}

    def test_song_version_of_a_video_is_searched_once(self, monkeypatch):
        def search(query, flt):
            return [{"title": "I'm an Albatraoz", "artists": [{"name": "AronChupa"}],
                     "album": {"name": "I'm an Albatraoz"}}]
        tracks = [yt_track("AronChupa - I'm an Albatraoz | OFFICIAL VIDEO", ["AronChupa"], video_id="vid-1")]
        ytm = install_fake_ytm(monkeypatch, FakeYTM({"PLv": {"title": "V", "tracks": tracks}}, search_results=search))
        cache = engine.Cache()
        first = engine.read_playlist_full(Settings(), "PLv", cache=cache)[2]
        second = engine.read_playlist_full(Settings(), "PLv", cache=cache)[2]
        assert first == second and second[0]["album"] == "I'm an Albatraoz" and second[0]["video_id"] == "vid-1"
        assert [c for c in ytm.calls if c[0] == "search"] == [("search", "AronChupa I'm an Albatraoz")]
        tracks[0]["title"] = "AronChupa - I'm an Albatraoz (Remastered)"   # changed on YouTube: asked again
        engine.read_playlist_full(Settings(), "PLv", cache=cache)
        assert len([c for c in ytm.calls if c[0] == "search"]) == 2


class TestDeferredLidarrFinalize:
    def test_sync_does_not_wait_and_reports_pending_albums(self, env):
        env.lidarr.busy_polls = 5
        res, lines = run(env, do_jellyfin=False, wait_for_lidarr=False)
        assert res.lidarr_pending == ["alb-lev", "alb-motion"]
        assert not any("command" in p for m, p in env.lidarr.requests if m == "GET")   # no polling
        assert not any("Waiting for Lidarr" in ln for ln in lines)
        assert any("2 album(s) of new artists will be monitored and searched" in ln for ln in lines)

    def test_step_waits_while_lidarr_is_busy_then_monitors(self, env):
        res, _ = run(env, do_jellyfin=False, wait_for_lidarr=False)
        env.lidarr.busy_polls = 1
        env.lidarr.clear_writes()
        step = engine.lidarr_finalize_step(env.settings, res.lidarr_pending + ["alb-unknown"])
        assert step.busy and env.lidarr.writes == []
        step = engine.lidarr_finalize_step(env.settings, res.lidarr_pending + ["alb-unknown"])
        assert not step.busy and sorted(step.done) == ["alb-lev", "alb-motion"] and step.missing == ["alb-unknown"]
        assert step.unmonitored == 2
        assert env.lidarr.album_by_foreign("alb-motion")["monitored"]
        assert set(env.lidarr.searched_album_ids()) == {env.lidarr.album_by_foreign(f)["id"]
                                                        for f in ("alb-lev", "alb-motion")}

    def test_step_asks_lidarr_only_for_the_pending_albums(self, env):
        res, _ = run(env, do_jellyfin=False, wait_for_lidarr=False)
        env.lidarr.requests.clear()
        engine.lidarr_finalize_step(env.settings, res.lidarr_pending)
        album_gets = [p for m, p in env.lidarr.requests if m == "GET" and "/api/v1/album" in p]
        assert sorted(album_gets) == ["/api/v1/album?foreignAlbumId=alb-lev", "/api/v1/album?foreignAlbumId=alb-motion"]

    def test_forced_step_ignores_a_busy_lidarr(self, env):
        res, _ = run(env, do_jellyfin=False, wait_for_lidarr=False)
        env.lidarr.busy_polls = 5
        assert sorted(engine.lidarr_finalize_step(env.settings, res.lidarr_pending, force=True).done) == \
            ["alb-lev", "alb-motion"]


class TestJellyfinMirror:
    """Mirror mode on a playlist that already exists in Jellyfin (playlist "pl-x" named like the YouTube one)."""

    @pytest.fixture(autouse=True)
    def setup(self, env):
        env.jf.add_item("j-extra", "Song I Added", "Someone")
        env.jf.add_item("j-out", "Outside", "Calvin Harris", "Motion")
        self.env = env

    def given(self, items, *tracks):
        self.env.jf.playlists["pl-x"] = {"Id": "pl-x", "Name": "Road Trip", "items": list(items)}
        self.env.ytm.playlists["PLtest"]["tracks"] = list(tracks) or [
            yt_track("Summer", ["Calvin Harris"], "Motion"), yt_track("Paranoid Android", ["Radiohead"], "OK Computer")]

    def items(self):
        return self.env.jf.playlists["pl-x"]["items"]

    def test_removes_other_songs_and_duplicates_and_restores_the_order(self):
        self.given(["j-extra", "j-pa", "j-summer", "j-pa"])
        res, lines = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.items() == ["j-summer", "j-pa"]
        assert (res.jellyfin_removed, res.jellyfin_moved, res.jellyfin_added) == (2, 2, 0)
        assert [t.jellyfin_status for t in res.tracks] == ["in_playlist", "in_playlist"]
        assert res.status == "ok" and res.errors == []
        assert "    - removed 'Song I Added' (not in the YouTube playlist)" in lines
        assert any("mirror: removed 1 song(s) not in the YouTube playlist and 1 duplicate entry" in ln
                   for ln in lines)

    def test_a_correct_start_is_kept(self):
        self.given(["j-summer", "j-pa", "j-extra"])
        res, _ = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.items() == ["j-summer", "j-pa"]
        assert self.env.jf.writes == [("DELETE", "/Playlists/pl-x/Items", {"entryIds": "j-extra"})]
        assert (res.jellyfin_removed, res.jellyfin_moved) == (1, 0)

    def test_new_song_in_the_middle_is_put_in_place(self):
        self.given(["j-summer", "j-pa"], yt_track("Summer", ["Calvin Harris"], "Motion"),
                   yt_track("Outside", ["Calvin Harris"], "Motion"),
                   yt_track("Paranoid Android", ["Radiohead"], "OK Computer"))
        res, _ = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.items() == ["j-summer", "j-out", "j-pa"]
        assert [t.jellyfin_status for t in res.tracks] == ["in_playlist", "added", "in_playlist"]
        assert (res.jellyfin_removed, res.jellyfin_moved, res.jellyfin_added) == (0, 1, 1)

    def test_kept_song_repeated_later_is_not_lost(self):
        # Jellyfin removes every copy of a song, so the repeated song can't stay in the kept start
        self.given(["j-summer", "j-pa", "j-summer"])
        res, _ = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.items() == ["j-summer", "j-pa"]
        assert res.jellyfin_removed == 1 and res.status == "ok"

    def test_already_matching_playlist_is_not_touched(self):
        self.given(["j-summer", "j-pa"])
        res, _ = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.env.jf.writes == [] and (res.jellyfin_removed, res.jellyfin_moved) == (0, 0)

    def test_off_by_default(self):
        self.given(["j-extra", "j-pa", "j-summer", "j-pa"])
        res, _ = run(self.env, do_lidarr=False)
        assert self.items() == ["j-extra", "j-pa", "j-summer", "j-pa"] and res.jellyfin_removed == 0

    def test_skipped_when_a_lookup_fails(self):
        self.given(["j-summer", "j-pa", "j-extra"])
        self.env.jf.fail_search_terms = {"paranoid android"}
        res, lines = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.items() == ["j-summer", "j-pa", "j-extra"]
        assert not self.env.jf.writes_to("DELETE", "/Playlists")
        assert any("Mirror skipped this time" in ln for ln in lines)

    def test_never_empties_the_playlist(self):
        self.given(["j-summer", "j-pa"], yt_track("Where Is My Mind", ["Nobody"], "Nothing"))
        res, lines = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert self.items() == ["j-summer", "j-pa"] and self.env.jf.writes == []
        assert any("Mirror skipped: no track matched" in ln for ln in lines)

    def test_dry_run_reports_without_writing(self):
        self.given(["j-extra", "j-summer", "j-pa"])
        res, lines = run(self.env, do_lidarr=False, jellyfin_mirror=True, dry_run=True)
        assert self.env.jf.writes == [] and self.items() == ["j-extra", "j-summer", "j-pa"]
        assert any("mirror: would remove 1 song(s)" in ln for ln in lines)
        assert res.jellyfin_removed == 1

    def test_changes_jellyfin_ignores_are_an_error(self, monkeypatch):
        self.given(["j-summer", "j-pa", "j-extra"])
        monkeypatch.setattr(engine.Jellyfin, "req", _ignore_deletes(engine.Jellyfin.req))
        res, _ = run(self.env, do_lidarr=False, jellyfin_mirror=True)
        assert res.status == "error"
        assert res.errors == ["Jellyfin did not apply all mirror changes to playlist 'Road Trip'"]


def _ignore_deletes(req):
    def wrapped(self, method, path, **kw):
        return {} if method == "DELETE" else req(self, method, path, **kw)
    return wrapped


class TestDryRun:
    def test_makes_no_write_calls(self, env):
        res, lines = run(env, dry_run=True)
        assert env.lidarr.writes == [] and env.jf.writes == []
        assert not any("command" in p for m, p in env.lidarr.requests)   # no finalize polling either
        # but it still reports what it would do
        s = {t.position: t for t in res.tracks}
        assert s[1].lidarr_status == "added" and s[3].lidarr_status == "monitored" and s[6].lidarr_status == "not_found"
        assert s[1].jellyfin_status == "added" and s[2].jellyfin_status == "missing"
        assert any("[dry-run]" in ln for ln in lines)
        assert any("would create playlist 'Road Trip' with 2 tracks" in ln for ln in lines)
        assert env.jf.playlists == {}

    def test_dry_run_against_existing_playlist(self, env):
        run(env, do_lidarr=False)
        env.jf.add_item("j-out", "Outside", "Calvin Harris", "Motion")
        env.jf.clear_writes()
        res, lines = run(env, do_lidarr=False, dry_run=True)
        assert env.jf.writes == []
        assert any("would add 1 new track(s)" in ln for ln in lines)


class TestStopAndTimeouts:
    def test_should_stop_between_albums_aborts_cleanly(self, env):
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 2          # let two album groups through
        res, lines = run(env, should_stop=stop)
        assert res.stopped is True and res.status == "warning"
        assert "Stopped before completion." in res.errors
        assert any(t.lidarr_status == "pending" for t in res.tracks)
        assert len(env.lidarr.writes_to("POST", "/album")) <= 2
        assert env.jf.writes == [] and not any("Jellyfin: matching" in ln for ln in lines)

    def test_should_stop_during_finalize_wait(self, env):
        env.lidarr.busy_polls = 10 ** 6
        flag = {"stop": False}

        def log(line):
            if "refresh task(s) still running" in line:
                flag["stop"] = True
        res = sync_playlist(env.settings, "PLtest", do_jellyfin=False, log=log, should_stop=lambda: flag["stop"])
        assert res.stopped is True
        # nothing monitored yet: the next sync heals it
        disc_id = env.lidarr.album_by_foreign("alb-disc")["id"]
        assert [w[2]["albumIds"] for w in env.lidarr.writes_to("PUT", "album/monitor")] == [[disc_id]]
        assert env.lidarr.searched_album_ids() == [env.lidarr.album_by_foreign("alb-disc")["id"]]
        assert env.lidarr.album_by_foreign("alb-motion")["monitored"] is False

    def test_finalize_gives_up_waiting_after_timeout_then_still_monitors(self, env, monkeypatch):
        env.lidarr.busy_polls = 10 ** 6
        clock = {"t": 1000.0}
        monkeypatch.setattr(engine, "_now", lambda: clock["t"])
        monkeypatch.setattr(engine, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
        res, lines = run(env, do_jellyfin=False)
        polls = [ln for ln in lines if "refresh task(s) still running" in ln]
        assert 1 <= len(polls) <= 5            # 1 minute timeout / 15 s poll interval
        assert env.lidarr.album_by_foreign("alb-motion")["monitored"] is True
        assert res.stopped is False

    def test_all_http_calls_have_timeouts(self):
        tree = ast.parse(open(engine.__file__, encoding="utf-8").read())
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "post", "put", "request", "delete")):
                recv = ast.unparse(node.func.value)
                if recv in ("s", "self.s", "r.s") and not any(k.arg == "timeout" for k in node.keywords):
                    offenders.append(node.lineno)
        assert offenders == []


# ================================================================ fatal errors

class TestRedaction:
    def test_url_queries_are_stripped_but_titles_with_question_marks_survive(self):
        # QA regression: "What About Us?!" used to be logged as "What About Us"
        redact = engine._redactor(Settings(lidarr_api_key="LK-secret-123"))
        assert redact("  ✓  Pink - What About Us?!  ->  X") == "  ✓  Pink - What About Us?!  ->  X"
        assert redact("¿Quién?Sí") == "¿Quién?Sí"
        assert redact("url: /api/v1/album/lookup?term=Foo%20Bar&apikey=k (Caused by X)") == \
            "url: /api/v1/album/lookup (Caused by X)"
        assert redact("GET /album?apikey=LK-secret-123 failed") == "GET /album failed"
        assert redact("key LK-secret-123 leaked") == "key *** leaked"


class TestFatalErrors:
    def test_lidarr_bad_key(self, env):
        env.settings.lidarr_api_key = "wrong-key-xyz"
        with pytest.raises(EngineError) as ei:
            run(env)
        assert str(ei.value) == "Lidarr: 401 Unauthorized — check the API key"

    def test_jellyfin_bad_key(self, env):
        env.settings.jellyfin_api_key = "wrong-key-xyz"
        with pytest.raises(EngineError, match="Jellyfin: 401 Unauthorized"):
            run(env)
        assert env.lidarr.writes == []     # failed before doing any work

    def test_lidarr_unreachable(self, env):
        env.settings.lidarr_url = _dead_url()
        with pytest.raises(EngineError, match=r"Lidarr: cannot reach http://127\.0\.0\.1:\d+"):
            run(env)

    def test_wrong_url_404(self, env):
        env.settings.lidarr_url = env.jf.url      # a Jellyfin answering on the Lidarr URL
        with pytest.raises(EngineError, match="Lidarr: "):
            run(env)

    def test_wrong_service_json_shape_is_a_clear_error(self, env):
        # QA regression: a URL that answers JSON of the wrong shape used to surface as "Lidarr: 0"
        env.lidarr.root_folders = {"message": "hello"}
        with pytest.raises(EngineError, match=r"Lidarr: unexpected response from http://127\.0\.0\.1:\d+"):
            run(env)
        env.lidarr.root_folders = [{"id": 1, "path": "/music"}]
        env.lidarr.version = None
        env.lidarr.handle = lambda *a, **k: (200, ["not", "a", "dict"])   # status endpoint answers a list
        ok, msg = check_lidarr(env.settings)
        assert not ok and msg.startswith("unexpected response") and "attribute" not in msg

    def test_unknown_jellyfin_user(self, env):
        env.settings.jellyfin_user = "nobody"
        with pytest.raises(EngineError, match="Jellyfin user 'nobody' not found"):
            run(env)

    def test_unknown_quality_profile(self, env):
        env.settings.lidarr_quality = "Nope"
        with pytest.raises(EngineError, match="qualityprofile 'Nope' not found"):
            run(env)

    def test_missing_config(self, env):
        env.settings.lidarr_api_key = ""
        with pytest.raises(EngineError, match="Lidarr: URL and API key are not configured"):
            run(env)

    def test_playlist_unreadable(self, env):
        env.ytm.playlists["PLtest"] = RuntimeError("Server returned HTTP 403")
        with pytest.raises(EngineError, match="Could not read playlist"):
            run(env)

    def test_secrets_not_in_exception_messages(self, env):
        env.settings.lidarr_url = _dead_url()
        with pytest.raises(EngineError) as ei:
            run(env)
        assert LK not in str(ei.value) and "?" not in str(ei.value)

    def test_skipped_services_are_never_contacted(self, env):
        env.settings.lidarr_url = _dead_url()
        env.settings.jellyfin_url = _dead_url()
        res, _ = run(env, do_lidarr=False, do_jellyfin=False)
        assert all(t.lidarr_status == "skipped" and t.jellyfin_status == "skipped" for t in res.tracks)
        assert res.status == "ok" and res.lidarr_found == res.jellyfin_matched == 0

    def test_skipped_service_needs_no_credentials(self, env):
        env.settings.jellyfin_url = env.settings.jellyfin_api_key = ""
        res, _ = run(env, do_jellyfin=False)
        assert res.tracks[0].jellyfin_status == "skipped"

    def test_jellyfin_playlist_write_failure_is_status_error(self, env):
        env.jf.fail_playlist_writes = True
        res, _ = run(env, do_lidarr=False)
        assert res.status == "error" and res.errors and "playlist update failed" in res.errors[0]
        assert all(t.jellyfin_status in ("missing", "skipped") for t in res.tracks)

    def test_jellyfin_going_away_mid_run_gives_up(self, env):
        env.jf.fail_items = True
        res, _ = run(env, do_lidarr=False)
        assert res.status == "error" and "stopped after repeated errors" in res.errors[0]


def _dead_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ================================================================ check_* helpers

class TestChecks:
    def test_check_lidarr_ok(self, env):
        ok, msg = check_lidarr(env.settings)
        assert ok and msg == "Connected · v2.5.3.4341 · 2 artists"

    def test_check_lidarr_bad_key(self, env):
        env.settings.lidarr_api_key = "bad"
        ok, msg = check_lidarr(env.settings)
        assert not ok and "401" in msg and "API key" in msg and "bad" not in msg.replace("API key", "")

    def test_check_lidarr_unreachable_and_unconfigured(self, env):
        env.settings.lidarr_url = _dead_url()
        ok, msg = check_lidarr(env.settings)
        assert not ok and "cannot reach" in msg
        assert check_lidarr(Settings()) == (False, "Not configured")

    def test_lidarr_options(self, env):
        assert lidarr_options(env.settings) == {
            "root": ["/music", "/music2"], "quality": ["Any", "Lossless"], "metadata": ["Standard", "Albums only"]}
        env.settings.lidarr_api_key = "bad"
        assert lidarr_options(env.settings) == {"root": [], "quality": [], "metadata": []}
        assert lidarr_options(Settings()) == {"root": [], "quality": [], "metadata": []}

    def test_lidarr_options_are_usable_as_settings(self, env):
        env.settings.lidarr_root, env.settings.lidarr_quality, env.settings.lidarr_metadata = "/music2", "Lossless", "Albums only"
        run(env, do_jellyfin=False)
        post = env.lidarr.writes_to("POST", "/album")[0][2]["artist"]
        assert (post["rootFolderPath"], post["qualityProfileId"], post["metadataProfileId"]) == ("/music2", 2, 2)

    def test_metadata_profile_warning_is_logged(self, env):
        env.settings.lidarr_metadata = "Albums only"
        _, lines = run(env, do_jellyfin=False)
        assert any("doesn't allow ['EP', 'Single']" in ln for ln in lines)

    def test_check_jellyfin(self, env):
        assert check_jellyfin(env.settings) == (True, "Connected · user Daniel")
        env.settings.jellyfin_user = ""
        assert check_jellyfin(env.settings) == (True, "Connected · user Daniel")   # first user
        env.settings.jellyfin_user = "nobody"
        ok, msg = check_jellyfin(env.settings)
        assert not ok and "'nobody' not found" in msg
        env.settings.jellyfin_api_key = "bad"
        ok, msg = check_jellyfin(env.settings)
        assert not ok and "401" in msg
        env.settings.jellyfin_url = _dead_url()
        assert not check_jellyfin(env.settings)[0]
        assert check_jellyfin(Settings()) == (False, "Not configured")

    def test_check_ytm(self, env):
        assert check_ytm(env.settings, "https://music.youtube.com/playlist?list=PLtest") == (True, "Road Trip", 7)
        ok, msg, n = check_ytm(env.settings, "PLprivate")      # FakeYTM raises KeyError for unknown ids
        assert not ok and "PLprivate" in msg and n == 0
        env.ytm.playlists["PLp"] = RuntimeError("Server returned HTTP 403")
        ok, msg, n = check_ytm(env.settings, "PLp")
        assert not ok and "private" in msg.lower()
        assert not check_ytm(env.settings, "")[0]

    def test_check_ytm_does_not_search_per_track(self, env):
        env.ytm.playlists["PLtest"]["tracks"] = [yt_track("Levitating", ["Dua Lipa"])]
        check_ytm(env.settings, "PLtest")
        assert not any(c[0] == "search" for c in env.ytm.calls)

    def test_check_ytm_mix(self, env):
        env.ytm.playlists["RDmix123456"] = RuntimeError("nope")
        env.ytm.watch["RDmix123456"] = [yt_track("A", ["B"], "C")] * 3
        assert check_ytm(env.settings, "RDmix123456") == (True, "YouTube Music Mix 123456", 3)


class TestCli:
    def test_cli_runs_against_mocks(self, env, capsys):
        rc = engine.main(["PLtest", "--dry-run", "--only", "lidarr", "--lidarr-url", env.lidarr.url,
                          "--lidarr-key", LK])
        out = capsys.readouterr().out
        assert rc == 0 and "== Lidarr:" in out and "== Jellyfin" not in out and "Jellyfin: user" not in out
        assert env.lidarr.writes == []

    def test_cli_env_vars_and_name(self, env, monkeypatch, capsys):
        for k, v in dict(LIDARR_URL=env.lidarr.url, LIDARR_API_KEY=LK, JELLYFIN_URL=env.jf.url,
                         JELLYFIN_API_KEY=JK, JELLYFIN_USER="Daniel").items():
            monkeypatch.setenv(k, v)
        assert engine.main(["PLtest", "--only", "jellyfin", "--name", "CLI List"]) == 0
        assert env.jf.playlist_by_name("CLI List")["items"] == ["j-summer", "j-pa"]

    def test_cli_fatal_error_exit_code(self, env, monkeypatch, capsys):
        for k in ("LIDARR_URL", "LIDARR_API_KEY", "JELLYFIN_URL", "JELLYFIN_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        assert engine.main(["PLtest"]) == 1
        assert "not configured" in capsys.readouterr().err

    def test_settings_from_env(self):
        s = engine.settings_from_env({"LIDARR_URL": "http://l", "YTM_AUTH": "/x/browser.json", "FINALIZE_TIMEOUT_MIN": "7"})
        assert (s.lidarr_url, s.ytm_auth_path, s.finalize_timeout_min) == ("http://l", "/x/browser.json", 7)
