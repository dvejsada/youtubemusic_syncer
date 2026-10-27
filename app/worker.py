"""Background scheduler: a single daemon thread that runs queued / due playlist syncs one at a time."""
from __future__ import annotations

import logging
import os
import signal
import threading

from app import db, engine

log = logging.getLogger("playlist-sync.worker")

POLL_SECONDS = 30
FLUSH_SECONDS = 1.0

_lock = threading.Lock()
_thread: threading.Thread | None = None
_wake = threading.Event()
_stop = threading.Event()
_state = {"playlist_id": None, "run_id": None}


# ---------------------------------------------------------------- public API

def start() -> None:
    """Start the worker thread (idempotent: only one thread ever runs)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _wake.clear()
        try:
            n = db.mark_running_interrupted()
            if n:
                log.info("marked %d interrupted run(s)", n)
        except Exception:  # pragma: no cover
            log.exception("could not mark interrupted runs")
        _thread = threading.Thread(target=_loop, name="sync-worker", daemon=True)
        _thread.start()


def stop(timeout: float = 5.0) -> None:
    global _thread
    _stop.set()
    _wake.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout)
    with _lock:
        if _thread is t:
            _thread = None


def wake() -> None:
    _wake.set()


def is_alive() -> bool:
    return _thread is not None and _thread.is_alive()


def status() -> dict:
    return {"running_playlist_id": _state["playlist_id"], "running_run_id": _state["run_id"],
            "queue": db.queued_ids()}


def run_pending_once() -> int:
    """Run everything that is queued or due, one playlist at a time. Returns how many ran."""
    ran = 0
    seen: set[int] = set()
    while not _stop.is_set():
        pid = db.next_queued_id()
        trigger = None
        if pid is None:
            pid = db.next_due_id()
            trigger = "schedule"
        if pid is None or pid in seen:  # `seen` guards against a hot loop if a run could not clear its flag
            break
        seen.add(pid)
        run_playlist(pid, trigger)
        ran += 1
    return ran


# ------------------------------------------------------------------- internals

def _loop() -> None:
    try:
        while not _stop.is_set():
            try:
                run_pending_once()
            except Exception:
                log.exception("worker iteration failed")
            _wake.wait(POLL_SECONDS)
            _wake.clear()
    finally:
        if not _stop.is_set():
            # only something like SystemExit gets here (an iteration catches Exception). Without this thread
            # nothing syncs any more: shut down so the container's restart policy starts a fresh process.
            log.critical("sync worker died, shutting down so that the app is restarted")
            _exit_process()


def _exit_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)  # uvicorn shuts down gracefully on SIGTERM


class _LogSink:
    """Collects engine log lines and appends them to runs.log (flushed about every second)."""

    def __init__(self, run_id: int, redact):
        self.run_id = run_id
        self.redact = redact
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._t = threading.Thread(target=self._flusher, name="log-flusher", daemon=True)
        self._t.start()

    def __call__(self, msg="") -> None:
        with self._lock:
            self._buf.append(self.redact(str(msg)) + "\n")

    def flush(self) -> None:
        with self._lock:
            text, self._buf = "".join(self._buf), []
        if text:
            try:
                db.append_log(self.run_id, text)
            except Exception:  # pragma: no cover
                log.exception("log flush failed")

    def _flusher(self) -> None:
        while not self._done.wait(FLUSH_SECONDS):
            self.flush()

    def close(self) -> None:
        self._done.set()
        self._t.join(2)
        self.flush()


def _redactor(settings):
    secrets = [v for v in (settings.lidarr_api_key, settings.jellyfin_api_key) if v and len(v) >= 4]

    def redact(text: str) -> str:
        for s in secrets:
            text = text.replace(s, "***")
        return text
    return redact


def run_playlist(pid: int, trigger: str | None = None) -> str | None:
    """Sync one playlist now (blocking). Returns the run status, or None if the playlist is gone."""
    pl = db.get_playlist(pid)
    if not pl:
        return None
    if trigger is None:
        trigger = "added" if not pl["last_run_at"] and not pl["last_status"] else "manual"
    settings = db.engine_settings()
    run_id = db.create_run(pid, trigger)
    _state.update(playlist_id=pid, run_id=run_id)
    sink = _LogSink(run_id, _redactor(settings))
    result, error = None, ""
    try:
        result = engine.sync_playlist(
            settings, pl["ref"], jellyfin_name=(pl["jellyfin_name"] or None),
            do_lidarr=bool(pl["do_lidarr"]), do_jellyfin=bool(pl["do_jellyfin"]),
            jellyfin_mirror=bool(pl["jellyfin_mirror"]), jellyfin_playlist_id=pl["jellyfin_playlist_id"] or "",
            log=sink, should_stop=_stop.is_set)
    except engine.EngineError as e:
        error = sink.redact(str(e))
        sink(f"! {error}")
    except Exception as e:  # never kill the worker
        log.exception("sync of playlist %s crashed", pid)
        error = sink.redact(f"Unexpected error: {type(e).__name__}: {e}")
        sink(f"! {error}")
    finally:
        sink.close()

    try:
        return _record(pid, pl, run_id, result, error, sink)
    finally:
        _state.update(playlist_id=None, run_id=None)


def _record(pid, pl, run_id, result, error, sink) -> str:
    status, summary = "error", {}
    now = db.now_dt()
    interval = max(1, min(24 * 365, int(pl["interval_hours"] or 24)))  # an absurd value must not crash _record
    if result is not None:
        stopped = bool(getattr(result, "stopped", False)) and _stop.is_set()
        status = "interrupted" if stopped else result.status
        error = "; ".join(result.errors)
        summary = {
            "tracks": len(result.tracks), "lidarr_found": result.lidarr_found,
            "lidarr_failed": result.lidarr_failed, "jellyfin_matched": result.jellyfin_matched,
            "jellyfin_added": result.jellyfin_added, "jellyfin_duplicates": result.jellyfin_duplicates,
            "jellyfin_removed": result.jellyfin_removed, "jellyfin_moved": result.jellyfin_moved,
            "errors": result.errors,
        }
        if db.get_playlist(pid) and db.replace_tracks(pid, result.tracks):
            db.update_playlist(
                pid, name=result.playlist_name or pl["name"], is_mix=int(result.is_mix),
                track_count=len(result.tracks), cover_url=result.cover_url or pl["cover_url"] or "",
                lidarr_found=result.lidarr_found,
                jellyfin_matched=result.jellyfin_matched, jellyfin_duplicates=result.jellyfin_duplicates,
                jellyfin_playlist_id=result.jellyfin_playlist_id or pl["jellyfin_playlist_id"] or "")
    else:
        summary = {"errors": [error]}
    db.finish_run(run_id, status, summary)
    if db.get_playlist(pid):
        if status == "interrupted":
            # app is shutting down: leave it queued so it resumes on the next start
            db.update_playlist(pid, queued=1)
        else:
            db.update_playlist(pid, queued=0, last_run_at=now.isoformat(), last_status=status,
                               last_error=error or None, next_run_at=db.iso_in_hours(interval, now))
        db.prune_runs(pid)
    return status
