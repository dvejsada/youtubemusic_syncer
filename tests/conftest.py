import os
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

_real_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def _offline_and_fast(monkeypatch):
    """Tests never touch the network (only 127.0.0.1 mock servers) and never really sleep."""
    from app import engine

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"network access blocked in tests: {address!r}")
        return _real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(engine, "_sleep", lambda s: None)

    def no_ytm(settings):
        raise AssertionError("YouTube Music reached without install_fake_ytm()")
    monkeypatch.setattr(engine, "_make_ytm", no_ytm)
    # MusicBrainz answers "nothing" unless a test installs a FakeMusicBrainz
    monkeypatch.setattr(engine.MusicBrainz, "_search", lambda self, entity, query: {})
    monkeypatch.setattr(engine, "_MB_LAST", 0.0)
