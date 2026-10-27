"""Background scheduler: a single daemon thread that runs queued / due playlist syncs one at a time."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from app import db, engine

log = logging.getLogger("playlist-sync.worker")

POLL_SECONDS = 30
FLUSH_SECONDS = 1.0
FINALIZE_DELAY = 15        # seconds: Lidarr starts refreshing a new artist right after it was added
GIVE_UP_HOURS = 24         # Lidarr unreachable this long: forget pending albums (the next sync retries)
CACHE_MAX_AGE_DAYS = 180   # remembered lookups older than this are forgotten (looked up again when needed)
PRUNE_EVERY = 24 * 3600    # seconds between cache prunes

_lock = threading.Lock()
_thread: threading.Thread | None = None
_wake = threading.Event()
_stop = threading.Event()
_state = {"playlist_id": None, "run_id": None}
_last_prune = 0.0


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
            try:
                finalize_pending_once()
            except Exception:
                log.exception("finishing Lidarr albums failed")
            try:
                prune_cache_if_due()
            except Exception:
                log.exception("pruning the lookup cache failed")
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
    # remembered lookups of earlier syncs (loaded as needed); a full re-check starts empty and overwrites
    cache = engine.Cache() if pl["full_recheck"] else engine.Cache(loader=db.load_cache_entries)
    if pl["full_recheck"]:
        sink("Full re-check: looking up every track again.")
    try:
        result = engine.sync_playlist(
            settings, pl["ref"], jellyfin_name=(pl["jellyfin_name"] or None),
            do_lidarr=bool(pl["do_lidarr"]), do_jellyfin=bool(pl["do_jellyfin"]),
            jellyfin_mirror=bool(pl["jellyfin_mirror"]), jellyfin_playlist_id=pl["jellyfin_playlist_id"] or "",
            cache=cache, wait_for_lidarr=False,
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
    # never let these keep the run from being recorded (it would stay "running" with a stale worker state)
    try:  # also after a failed run: what was looked up stays useful
        db.save_cache({k: cache.data[k] for k in cache.changed})
    except Exception:
        log.exception("could not save the lookup cache")
    if result is not None and result.lidarr_pending:
        try:
            db.add_lidarr_pending(result.lidarr_pending, engine.lidarr_base_url(settings), pid, run_id)
        except Exception:
            log.exception("could not save the Lidarr albums to finish (the next sync handles them)")

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
            # only a finished full re-check clears its flag: one requested during a normal run stays queued
            done_recheck = {"full_recheck": 0} if pl["full_recheck"] and not stopped else {}
            db.update_playlist(
                pid, name=result.playlist_name or pl["name"], is_mix=int(result.is_mix),
                track_count=len(result.tracks), cover_url=result.cover_url or pl["cover_url"] or "",
                lidarr_found=result.lidarr_found,
                jellyfin_matched=result.jellyfin_matched, jellyfin_duplicates=result.jellyfin_duplicates,
                jellyfin_playlist_id=result.jellyfin_playlist_id or pl["jellyfin_playlist_id"] or "",
                **done_recheck)
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


# --------------------------------------------------- Lidarr albums to finish

def finalize_pending_once() -> int:
    """Monitor and search the albums of newly added artists once Lidarr has imported them (a sync doesn't
    wait for that, see engine.lidarr_finalize_step). Never blocks: while Lidarr is busy the albums stay
    pending for the next tick; after finalize_timeout_min the visible ones are monitored anyway and the rest
    is left to the next sync. Notes go into the log of the run that added the albums. Returns how many
    albums were monitored and searched."""
    rows = db.lidarr_pending()
    if not rows:
        return 0
    settings = db.engine_settings()
    now = db.now_dt()

    def age(r) -> float:
        return (now - (db.parse_iso(r["added_at"]) or now)).total_seconds()

    other = [r for r in rows if r["lidarr_url"] != engine.lidarr_base_url(settings) or not settings.lidarr_api_key]
    if other:  # Lidarr was changed or removed in Settings meanwhile
        _note(other, "Lidarr settings changed: albums of new artists not monitored here; the next sync handles them")
        db.remove_lidarr_pending([r["foreign_album_id"] for r in other])
    due = [r for r in rows if r not in other and age(r) >= FINALIZE_DELAY]
    if not due:
        return 0
    timeout = settings.finalize_timeout_min * 60
    # albums past the import wait are monitored even while Lidarr is busy; the others keep waiting for it
    # (monitoring those now would be undone when Lidarr finishes importing their artist)
    overdue = [r for r in due if age(r) >= timeout]
    waiting = [r for r in due if age(r) < timeout]
    handled = 0
    for rows_, force in ((overdue, True), (waiting, False)):
        if rows_:
            handled += _finish(settings, rows_, force, age)
    return handled


def _finish(settings, rows: list[dict], force: bool, age) -> int:
    try:
        step = engine.lidarr_finalize_step(settings, [r["foreign_album_id"] for r in rows], force=force)
    except Exception as e:
        log.warning("Lidarr: could not finish albums of new artists: %s", _redactor(settings)(str(e)))
        old = [r for r in rows if age(r) >= GIVE_UP_HOURS * 3600]
        if old:
            _note(old, "Lidarr unreachable: albums of new artists not monitored; the next sync retries")
            db.remove_lidarr_pending([r["foreign_album_id"] for r in old])
        return 0
    if step.busy:
        return 0
    done = set(step.done)
    handled = [r for r in rows if r["foreign_album_id"] in done]
    gone = [r for r in rows if r["foreign_album_id"] not in done and force]
    _note(handled, "Lidarr finished importing the new artists: album(s) monitored and searched")
    _note(gone, f"Lidarr: album(s) still not visible after {settings.finalize_timeout_min} min; "
                "the next sync picks them up")
    db.remove_lidarr_pending([r["foreign_album_id"] for r in handled + gone])
    return len(handled)


def prune_cache_if_due() -> int:
    """Once a day: forget remembered lookups of another cache version or older than CACHE_MAX_AGE_DAYS."""
    global _last_prune
    if time.monotonic() - _last_prune < PRUNE_EVERY and _last_prune:
        return 0
    _last_prune = time.monotonic()
    n = db.prune_cache(engine.CACHE_VERSION, CACHE_MAX_AGE_DAYS)
    if n:
        log.info("forgot %d old remembered lookup(s)", n)
    return n


def _note(rows: list[dict], text: str) -> None:
    """Append "text (n)" to the log of each run these pending albums came from."""
    by_run: dict = {}
    for r in rows:
        by_run[r["run_id"]] = by_run.get(r["run_id"], 0) + 1
    for run_id, n in by_run.items():
        if run_id:
            db.append_log(run_id, f"\n[later] {text} ({n})\n")
