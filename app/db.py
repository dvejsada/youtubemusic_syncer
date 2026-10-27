"""SQLite persistence (stdlib sqlite3). Short-lived connection per operation, WAL, busy timeout.

DATA_DIR is read lazily from the environment, so tests can point it at a temp dir.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import flag

KEEP_RUNS = 30

# setting key -> (env var used to seed it, default)
SETTING_DEFS: dict[str, tuple[str, str]] = {
    "lidarr_url": ("LIDARR_URL", ""),
    "lidarr_api_key": ("LIDARR_API_KEY", ""),
    "lidarr_root": ("LIDARR_ROOT", ""),
    "lidarr_quality": ("LIDARR_QUALITY", ""),
    "lidarr_metadata": ("LIDARR_METADATA", ""),
    "jellyfin_url": ("JELLYFIN_URL", ""),
    "jellyfin_api_key": ("JELLYFIN_API_KEY", ""),
    "jellyfin_user": ("JELLYFIN_USER", ""),
    "jellyfin_cover": ("JELLYFIN_COVER", "1"),
    "lidarr_link_url": ("LIDARR_LINK_URL", ""),
    "default_interval_hours": ("DEFAULT_INTERVAL_HOURS", "24"),
    "finalize_timeout_min": ("FINALIZE_TIMEOUT_MIN", "20"),
}
SECRET_KEYS = ("lidarr_api_key", "jellyfin_api_key")
WEBHOOK_TOKEN, WEBHOOK_LAST = "webhook_token", "webhook_last"  # get_meta / set_meta keys

PLAYLIST_COLUMNS = {
    "ref", "url", "name", "is_mix", "jellyfin_name", "interval_hours", "enabled", "do_lidarr",
    "do_jellyfin", "queued", "last_run_at", "next_run_at", "last_status", "last_error",
    "track_count", "lidarr_found", "jellyfin_matched", "jellyfin_playlist_id", "cover_url",
    "jellyfin_duplicates", "jellyfin_mirror", "full_recheck", "jellyfin_due_at", "jellyfin_due_try",
    "jellyfin_cover_set",
}

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,
    url TEXT,
    name TEXT,
    is_mix INTEGER NOT NULL DEFAULT 0,
    jellyfin_name TEXT,
    interval_hours INTEGER NOT NULL DEFAULT 24,
    enabled INTEGER NOT NULL DEFAULT 1,
    do_lidarr INTEGER NOT NULL DEFAULT 1,
    do_jellyfin INTEGER NOT NULL DEFAULT 1,
    queued INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    last_run_at TEXT,
    next_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    track_count INTEGER NOT NULL DEFAULT 0,
    lidarr_found INTEGER NOT NULL DEFAULT 0,
    jellyfin_matched INTEGER NOT NULL DEFAULT 0,
    jellyfin_playlist_id TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL,
    position INTEGER,
    title TEXT,
    artists TEXT,
    album TEXT,
    lidarr_status TEXT,
    lidarr_release TEXT,
    lidarr_note TEXT,
    jellyfin_status TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracks_playlist ON tracks(playlist_id, position);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL,
    trigger TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    summary TEXT,
    log TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_playlist ON runs(playlist_id, id);
"""

SCHEMA_V2 = """
ALTER TABLE playlists ADD COLUMN cover_url TEXT NOT NULL DEFAULT '';
ALTER TABLE tracks ADD COLUMN thumb_url TEXT NOT NULL DEFAULT '';
"""

SCHEMA_V3 = """
ALTER TABLE playlists ADD COLUMN jellyfin_duplicates INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tracks ADD COLUMN jellyfin_note TEXT NOT NULL DEFAULT '';
"""

SCHEMA_V4 = """
ALTER TABLE playlists ADD COLUMN jellyfin_mirror INTEGER NOT NULL DEFAULT 0;
"""

SCHEMA_V5 = """
ALTER TABLE playlists ADD COLUMN full_recheck INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS lookup_cache (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS lidarr_pending (
    foreign_album_id TEXT PRIMARY KEY,
    lidarr_url TEXT NOT NULL,
    playlist_id INTEGER,
    run_id INTEGER,
    added_at TEXT NOT NULL
);
"""

SCHEMA_V6 = """
ALTER TABLE playlists ADD COLUMN jellyfin_due_at TEXT;
ALTER TABLE playlists ADD COLUMN jellyfin_due_try INTEGER NOT NULL DEFAULT 0;
"""

SCHEMA_V7 = """
ALTER TABLE playlists ADD COLUMN jellyfin_cover_set TEXT NOT NULL DEFAULT '';
"""

# index + 1 == user_version after applying
MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, SCHEMA_V7]

_init_lock = threading.Lock()


# ------------------------------------------------------------------ helpers

def data_dir() -> Path:
    p = Path(os.environ.get("DATA_DIR") or "./data")
    p.mkdir(parents=True, exist_ok=True)
    return p


def check_data_dir() -> Path:
    """Fail early with a readable message if DATA_DIR can't be created or written to
    (typically a bind mount owned by another user)."""
    try:
        p = data_dir()
        probe = p / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise RuntimeError(
            f"Cannot use the data directory '{os.environ.get('DATA_DIR') or './data'}': {e.strerror or e}. "
            f"It must be writable by the user running the app (uid {getattr(os, 'getuid', lambda: '?')()}); "
            f"fix the permissions of the mounted volume or set DATA_DIR.") from None
    return p


def db_path() -> Path:
    return data_dir() / "app.db"


def now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_dt().isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso_in_hours(hours: float, base: datetime | None = None) -> str:
    return ((base or now_dt()) + timedelta(hours=hours)).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(str(db_path()), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        conn.close()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------- init

def init_db() -> None:
    """Create / migrate the schema and seed settings from env vars (once)."""
    check_data_dir()
    with _init_lock, connect() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[ver:], start=ver + 1):
            c.executescript(script)
            c.execute(f"PRAGMA user_version={i}")
        seeded = c.execute("SELECT 1 FROM settings WHERE key='_seeded'").fetchone()
        if not seeded:
            c.execute("BEGIN IMMEDIATE")
            for key, (env, default) in SETTING_DEFS.items():
                val = os.environ.get(env)
                c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                          (key, (val if val not in (None, "") else default).strip()))
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('_seeded', '1')")
            c.execute("COMMIT")
    try:  # the settings table holds the Lidarr / Jellyfin API keys: keep the file private (WAL/SHM inherit it)
        os.chmod(db_path(), 0o600)
    except OSError:  # pragma: no cover (e.g. Windows / odd mounts)
        pass


# ----------------------------------------------------------------- settings

def get_settings() -> dict[str, str]:
    out = {k: d for k, (_, d) in SETTING_DEFS.items()}
    with connect() as c:
        for r in c.execute("SELECT key, value FROM settings"):
            if r["key"] in out:
                out[r["key"]] = r["value"] or ""
    return out


def set_settings(values: dict[str, str]) -> None:
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for k, v in values.items():
            if k in SETTING_DEFS:
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, str(v).strip()))
        c.execute("COMMIT")


def get_meta(key: str) -> str:
    """App state kept in the settings table that is not a user setting (e.g. the webhook token)."""
    with connect() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (f"meta:{key}",)).fetchone()
    return (r["value"] if r else "") or ""


def set_meta(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (f"meta:{key}", value))


def ytm_auth_file() -> Path:
    return data_dir() / "browser.json"


def engine_settings(overrides: dict[str, str] | None = None):
    """Build an engine.Settings from the DB (optionally with form overrides)."""
    from app import engine

    s = get_settings()
    if overrides:
        s.update(overrides)
    try:
        timeout = max(1, int(s["finalize_timeout_min"]))
    except ValueError:
        timeout = 20
    auth = ytm_auth_file()
    return engine.Settings(
        lidarr_url=s["lidarr_url"], lidarr_api_key=s["lidarr_api_key"], lidarr_root=s["lidarr_root"],
        lidarr_quality=s["lidarr_quality"], lidarr_metadata=s["lidarr_metadata"],
        jellyfin_url=s["jellyfin_url"], jellyfin_api_key=s["jellyfin_api_key"],
        jellyfin_user=s["jellyfin_user"], jellyfin_cover=flag(s["jellyfin_cover"]),
        ytm_auth_path=str(auth) if auth.exists() else "",
        finalize_timeout_min=timeout)


# ---------------------------------------------------------------- playlists

def add_playlist(ref: str, url: str, name: str, track_count: int, interval_hours: int,
                 is_mix: bool = False, cover_url: str = "") -> int:
    """Insert a playlist, queued for its first sync. Raises sqlite3.IntegrityError if it exists."""
    with connect() as c:
        cur = c.execute(
            "INSERT INTO playlists(ref, url, name, is_mix, interval_hours, queued, created_at, track_count,"
            " cover_url) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (ref, url, name, int(is_mix), interval_hours, now_iso(), track_count, cover_url or ""))
        return cur.lastrowid


def _valid_id(i) -> bool:
    return isinstance(i, int) and 0 < i < 2**63  # beyond that sqlite raises OverflowError (HTTP 500)


def get_playlist(pid: int) -> dict | None:
    if not _valid_id(pid):
        return None
    with connect() as c:
        r = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def get_playlist_by_ref(ref: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM playlists WHERE ref=?", (ref,)).fetchone()
        return dict(r) if r else None


def list_playlists() -> list[dict]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM playlists ORDER BY created_at, id"))


def update_playlist(pid: int, **fields) -> None:
    bad = set(fields) - PLAYLIST_COLUMNS
    if bad:
        raise ValueError(f"unknown playlist columns: {sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE playlists SET {sets} WHERE id=?", (*fields.values(), pid))


def delete_playlist(pid: int) -> None:
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM tracks WHERE playlist_id=?", (pid,))
        c.execute("DELETE FROM runs WHERE playlist_id=?", (pid,))
        c.execute("DELETE FROM playlists WHERE id=?", (pid,))
        c.execute("COMMIT")


def queue_playlist(pid: int) -> None:
    update_playlist(pid, queued=1)


def next_queued_id() -> int | None:
    with connect() as c:
        r = c.execute("SELECT id FROM playlists WHERE queued=1 ORDER BY id LIMIT 1").fetchone()
        return r["id"] if r else None


def next_due_id() -> int | None:
    with connect() as c:
        r = c.execute(
            "SELECT id FROM playlists WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at<=?)"
            " ORDER BY COALESCE(next_run_at, ''), id LIMIT 1", (now_iso(),)).fetchone()
        return r["id"] if r else None


def queued_ids() -> list[int]:
    with connect() as c:
        return [r["id"] for r in c.execute("SELECT id FROM playlists WHERE queued=1 ORDER BY id")]


# ------------------------------------------------------------------- tracks

def replace_tracks(pid: int, tracks) -> bool:
    """Replace a playlist's tracks. Returns False (and writes nothing) if the playlist was deleted."""
    ts = now_iso()
    rows = [(pid, t.position, t.title, json.dumps(list(t.artists), ensure_ascii=False), t.album,
             t.lidarr_status, t.lidarr_release, t.lidarr_note, t.jellyfin_status, ts,
             getattr(t, "thumb_url", "") or "", getattr(t, "jellyfin_note", "") or "") for t in tracks]
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
            c.execute("ROLLBACK")
            return False
        c.execute("DELETE FROM tracks WHERE playlist_id=?", (pid,))
        c.executemany(
            "INSERT INTO tracks(playlist_id, position, title, artists, album, lidarr_status,"
            " lidarr_release, lidarr_note, jellyfin_status, updated_at, thumb_url, jellyfin_note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("COMMIT")
    return True


def list_tracks(pid: int) -> list[dict]:
    with connect() as c:
        out = _rows(c.execute("SELECT * FROM tracks WHERE playlist_id=? ORDER BY position, id", (pid,)))
    for t in out:
        try:
            t["artists"] = json.loads(t["artists"] or "[]")
        except ValueError:
            t["artists"] = []
    return out


def get_track(tid: int) -> dict | None:
    if not _valid_id(tid):
        return None
    with connect() as c:
        r = c.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None


def mosaic_track_ids(pid: int, n: int = 4) -> list[int]:
    """Ids of the first `n` tracks (by position) that have a thumbnail: the art of a cover-less mix."""
    with connect() as c:
        return [r["id"] for r in c.execute(
            "SELECT id FROM tracks WHERE playlist_id=? AND COALESCE(thumb_url, '') <> ''"
            " ORDER BY position, id LIMIT ?", (pid, max(0, n)))]


def track_stats(pid: int) -> dict:
    """Counts used by the detail header."""
    with connect() as c:
        r = c.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(lidarr_status IN ('added','monitored','downloaded','duplicate')) AS lidarr_ok,"
            " SUM(lidarr_status IN ('not_found','error')) AS lidarr_missing,"
            " SUM(jellyfin_status IN ('in_playlist','added')) AS jf_ok,"
            " SUM(jellyfin_status='missing') AS jf_missing,"
            " SUM(jellyfin_status='duplicate') AS jf_dup"
            " FROM tracks WHERE playlist_id=?", (pid,)).fetchone()
    return {k: (r[k] or 0) for k in r.keys()}


# ------------------------------------------------- Jellyfin after a Lidarr import

def playlists_waiting_for(artist: str, album: str) -> list[int]:
    """Playlists (with Jellyfin on) that have a track not in the Jellyfin library yet whose Lidarr release is
    this album ("Artist - Title [Type]", both from Lidarr's metadata). None without artist and album."""
    if not (artist and album):
        return []
    prefix = f"{artist} - {album} [".casefold()
    with connect() as c:
        rows = c.execute("SELECT DISTINCT t.playlist_id, t.lidarr_release FROM tracks t"
                         " JOIN playlists p ON p.id = t.playlist_id"
                         " WHERE p.do_jellyfin = 1 AND t.jellyfin_status = 'missing'"
                         " AND t.lidarr_release <> ''").fetchall()
    return sorted({r["playlist_id"] for r in rows if (r["lidarr_release"] or "").casefold().startswith(prefix)})


def schedule_jellyfin_update(pids: list[int], delay_seconds: float) -> None:
    """A Jellyfin-only update of these playlists in `delay_seconds`. An update already due sooner is kept, so
    a burst of imports leads to one update (timed from the first import); a later one (a pending retry) is
    brought forward and counts as a fresh update with its own retry."""
    if not pids:
        return
    due = (now_dt() + timedelta(seconds=delay_seconds)).isoformat()
    with connect() as c:
        c.executemany("UPDATE playlists SET jellyfin_due_at=?, jellyfin_due_try=0"
                      " WHERE id=? AND (jellyfin_due_at IS NULL OR jellyfin_due_at > ?)",
                      [(due, pid, due) for pid in pids])


def next_jellyfin_due_id() -> int | None:
    with connect() as c:
        r = c.execute("SELECT id FROM playlists WHERE jellyfin_due_at IS NOT NULL AND jellyfin_due_at<=?"
                      " ORDER BY jellyfin_due_at, id LIMIT 1", (now_iso(),)).fetchone()
        return r["id"] if r else None


# ------------------------------------------------------------ lookup cache

def load_cache_entries(kind: str, keys: list[str]) -> dict:
    """The remembered lookups of one kind for these keys, {key: value} (engine.Cache's loader)."""
    out = {}
    with connect() as c:
        for k in range(0, len(keys), 500):
            chunk = keys[k:k + 500]
            for r in c.execute(f"SELECT key, value FROM lookup_cache WHERE kind=? AND key IN "
                               f"({','.join('?' * len(chunk))})", (kind, *chunk)):
                try:
                    out[r["key"]] = json.loads(r["value"])
                except ValueError:
                    pass
    return out


def prune_cache(version: int, max_age_days: float) -> int:
    """Forget lookups of another cache version or older than max_age_days (they are looked up again when
    needed). Returns how many were removed."""
    cutoff = time.time() - max_age_days * 86400
    with connect() as c:
        cur = c.execute("DELETE FROM lookup_cache WHERE CASE WHEN json_valid(value) THEN"
                        " json_extract(value, '$.v') IS NOT ? OR COALESCE(json_extract(value, '$.at'), 0) < ?"
                        " ELSE 1 END", (version, cutoff))
        return cur.rowcount


def save_cache(entries: dict) -> None:
    if not entries:
        return
    rows = [(kind, key, json.dumps(v, ensure_ascii=False)) for (kind, key), v in entries.items()]
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.executemany("INSERT OR REPLACE INTO lookup_cache(kind, key, value) VALUES (?, ?, ?)", rows)
        c.execute("COMMIT")


# ---------------------------------------------------- Lidarr albums to finish

def add_lidarr_pending(foreign_album_ids, lidarr_url: str, playlist_id: int, run_id: int) -> None:
    ts = now_iso()
    with connect() as c:
        c.executemany("INSERT OR REPLACE INTO lidarr_pending(foreign_album_id, lidarr_url, playlist_id, run_id,"
                      " added_at) VALUES (?, ?, ?, ?, ?)",
                      [(f, lidarr_url, playlist_id, run_id, ts) for f in foreign_album_ids])


def lidarr_pending() -> list[dict]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM lidarr_pending ORDER BY added_at, foreign_album_id"))


def remove_lidarr_pending(foreign_album_ids) -> None:
    with connect() as c:
        c.executemany("DELETE FROM lidarr_pending WHERE foreign_album_id=?", [(f,) for f in foreign_album_ids])


# --------------------------------------------------------------------- runs

def create_run(pid: int, trigger: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO runs(playlist_id, trigger, started_at, status, log)"
                        " VALUES (?, ?, ?, 'running', '')", (pid, trigger, now_iso()))
        return cur.lastrowid


def append_log(run_id: int, text: str) -> None:
    if not text:
        return
    with connect() as c:
        c.execute("UPDATE runs SET log = log || ? WHERE id=?", (text, run_id))


def finish_run(run_id: int, status: str, summary: dict | None = None) -> None:
    with connect() as c:
        c.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE id=?",
                  (status, now_iso(), json.dumps(summary or {}), run_id))


def prune_runs(pid: int, keep: int = KEEP_RUNS) -> None:
    with connect() as c:
        c.execute("DELETE FROM runs WHERE playlist_id=? AND id NOT IN"
                  " (SELECT id FROM runs WHERE playlist_id=? ORDER BY id DESC LIMIT ?)", (pid, pid, keep))


def _run_row(r) -> dict:
    d = dict(r)
    try:
        d["summary"] = json.loads(d.get("summary") or "{}")
    except ValueError:
        d["summary"] = {}
    return d


def get_run(run_id: int) -> dict | None:
    if not _valid_id(run_id):
        return None
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run_row(r) if r else None


def list_runs(pid: int, limit: int = KEEP_RUNS) -> list[dict]:
    with connect() as c:
        return [_run_row(r) for r in
                c.execute("SELECT * FROM runs WHERE playlist_id=? ORDER BY id DESC LIMIT ?", (pid, limit))]


def last_run(pid: int) -> dict | None:
    runs = list_runs(pid, 1)
    return runs[0] if runs else None


def mark_running_interrupted() -> int:
    """On startup: runs left in 'running' by a previous process were interrupted."""
    with connect() as c:
        cur = c.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'",
                        (now_iso(),))
        return cur.rowcount
