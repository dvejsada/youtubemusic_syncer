"""The shared API client (app/engine/http.py): retries, errors, JSON handling. The session is replaced by a
stub that answers from a script, so nothing touches the network."""
import pytest

from app import engine
from app.engine.http import Http


class Resp:
    def __init__(self, status=200, body='{"ok": true}', reason="OK"):
        self.status_code, self.text, self.reason = status, body, reason
        self.ok = status < 400

    def json(self):
        import json
        return json.loads(self.text)


class Session:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params, timeout))
        return self.answers.pop(0)

    def request(self, method, url, json=None, params=None, timeout=None):
        self.calls.append((method, url, params, timeout))
        return self.answers.pop(0)


@pytest.fixture
def waits(monkeypatch):
    out = []
    monkeypatch.setattr(engine.clock, "sleep", out.append)
    return out


def client(*answers, **kw):
    http = Http("http://svc/api/", {"X-Key": "k"}, **kw)
    http.s = Session(*answers)
    return http


def test_busy_reads_are_retried_with_backoff(waits):
    http = client(Resp(503, "", "Service Unavailable"), Resp(502, "", "Bad Gateway"), Resp(200, "[1]"))
    assert http.get("album", {"q": "x"}) == [1]
    assert [c[1] for c in http.s.calls] == ["http://svc/api/album"] * 3 and waits == [3.0, 6.0]


def test_gives_up_after_the_last_attempt_with_a_short_error(waits):
    http = client(*[Resp(503, "<html>" + "x" * 500, "Service Unavailable")] * 3)
    with pytest.raises(engine.ApiError) as e:
        http.get("/album/lookup")
    assert str(e.value) == "GET /album/lookup -> 503 Service Unavailable" and e.value.status == 503
    assert len(http.s.calls) == 3 and waits == [3.0, 6.0]


def test_only_listed_statuses_are_retried(waits):
    http = client(Resp(500, "", "Internal Server Error"), retry_on=(503,))
    with pytest.raises(engine.ApiError):
        http.get("x")
    assert len(http.s.calls) == 1 and waits == []
    http = client(Resp(404, "", "Not Found"))
    with pytest.raises(engine.ApiError) as e:
        http.get("x")
    assert e.value.status == 404 and len(http.s.calls) == 1


def test_writes_are_never_retried_and_explain_the_error(waits):
    http = client(Resp(503, '{"error": "busy"}'))
    with pytest.raises(engine.ApiError) as e:
        http.send("POST", "album", json={"a": 1})
    assert str(e.value) == 'POST album -> 503: {"error": "busy"}' and len(http.s.calls) == 1 and waits == []
    assert client(Resp(201, "")).send("PUT", "x") == {}   # empty answer to a write is fine


def test_a_read_that_is_not_json_is_an_error(waits):
    for body in ("", "<html>not the API</html>"):
        with pytest.raises(ValueError):
            client(Resp(200, body)).get("system/status")


def test_hook_runs_before_every_attempt_and_timeouts_are_passed(waits):
    ticks = []
    http = client(Resp(503, ""), Resp(200, "{}"), before=lambda: ticks.append(1), timeout=(1, 2))
    http.get("x")
    assert ticks == [1, 1] and [c[3] for c in http.s.calls] == [(1, 2), (1, 2)]
    http = client(Resp(200, "{}"), timeout=(1, 2))
    http.get("x", timeout=(5, 6))
    assert http.s.calls[0][3] == (5, 6)


def test_single_attempt_clients(waits):
    http = client(Resp(503, ""), attempts=1)
    with pytest.raises(engine.ApiError):
        http.get("x")
    assert len(http.s.calls) == 1 and waits == []


def test_services_use_their_retry_rules():
    mb = engine.MusicBrainz()
    assert (mb.http.retry_on, mb.http.attempts, mb.http.backoff) == ((503,), 5, 5)
    assert mb.http.before is engine.musicbrainz._mb_throttle   # the rate limit covers retries too


def test_lidarr_and_jellyfin_clients_are_set_up_as_intended():
    from mock_servers import MockJellyfin, MockLidarr
    lidarr, jf = MockLidarr("lk").start(), MockJellyfin("jk").start()
    try:
        lid = engine.Lidarr(lidarr.url, "lk")
        assert (lid.http.retry_on, lid.http.attempts, lid.http.backoff, lid.http.timeout) ==             ((429, 500, 502, 503, 504), 3, 3.0, (10, 90))
        jfc = engine.Jellyfin(jf.url, "jk", "")
        assert (jfc.http.retry_on, jfc.http.attempts, jfc.http.backoff, jfc.http.timeout) ==             ((429, 502, 503, 504), 3, 1, (10, 60))   # busy answers only, short waits
    finally:
        lidarr.stop()
        jf.stop()
