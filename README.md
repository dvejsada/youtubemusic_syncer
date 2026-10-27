# Playlist Sync

A small self-hosted web app that keeps **YouTube Music playlists** in sync with
**Lidarr** (downloads the albums and singles) and **Jellyfin** (a playlist with the same tracks),
and refreshes every playlist on a schedule. It replaces the old `ytm_sync.py` script; the
matching logic is the same, now with a UI, run history and a background scheduler.

* FastHTML + htmx UI styled with Pico CSS, stdlib SQLite, one Python process, no external services or CDNs
  (htmx and Pico are vendored in `app/static`).
* Single user, protected by one password.

## Quick start

```bash
cp .env.example .env          # set APP_PASSWORD (and TZ)
docker compose up -d           # pulls georgx22/youtubesync:latest
```

To build the image yourself instead: `docker build -t georgx22/youtubesync:latest .` before `docker compose up -d`.

Open <http://your-server:5001>, sign in, open **Settings**, enter the Lidarr and Jellyfin URL and API key
(use *Test connection*), then paste a playlist link on the dashboard. The first sync starts immediately.

`LIDARR_*`, `JELLYFIN_*` and `DEFAULT_INTERVAL_HOURS` in `.env` only pre-fill Settings on the very first
start; afterwards the values saved in the UI are used. Data (database, login secret, optional YouTube
login, cached playlist/track artwork) lives in `./data`, mounted at `/data`. The container starts as root only to fix the ownership of
`/data` and then runs the app as an unprivileged user.

Without Docker (Python 3.13): `pip install -r requirements.txt`, then
`APP_PASSWORD=... uvicorn app.main:app --port 5001` (keep a single worker: the sync thread must exist once).
Tests: `pip install -r requirements-dev.txt && python -m pytest -q`.
The old CLI still works: `python -m app.engine "<playlist url>" --dry-run` (settings from env vars).

## Code layout

```
app/
  main.py        FastHTML app setup: headers, auth Beforeware, toasts, static files, registers the routers
  config.py      constants and small input helpers shared by the routes
  routes/        HTTP handlers, one APIRouter per area (auth, playlists, settings, art)
  ui/            FastHTML components: core.py = shared building blocks (layout, buttons, badges, fields),
                 one module per area for the page-specific parts
  db.py          SQLite persistence
  worker.py      background scheduler thread
  engine/        the sync itself (YouTube Music -> Lidarr / Jellyfin), no web or database code:
                 one module per service (ytm, musicbrainz, lidarr, jellyfin), sync.py ties them together,
                 matching.py (normalisation / similarity), cache.py (remembered lookups), http.py (the
                 shared API client: timeouts, retries, ApiError), cli.py; app.engine re-exports the API
  static/        htmx, Pico CSS, app.css (only what Pico lacks), favicon
```

The UI is plain FastHTML + htmx without custom JavaScript: messages are FastHTML toasts (`add_toast`, which
also survive redirects), live views poll with `hx-trigger="every Ns"` and the server answers HTTP 286 to stop
polling, and responses send `HX-Trigger: playlists-changed` to refresh the library and sidebar.
Artwork from YouTube Music is fetched by the server (`/art/...`, allow-listed Google image hosts only) and
cached in `data/art`, so the browser never loads anything from external hosts.

## How matching works

* Each YouTube Music track is looked up in Lidarr by album (and artist); when that fails, by song title,
  and as a last resort MusicBrainz is searched. Titles are normalised first (edition, remaster and
  similar noise is stripped).
* Candidates are scored by title and artist similarity; a result with the wrong artist is rejected
  instead of downloading a random album.
* Tracks of the same album are grouped: the album is added to Lidarr once, monitored and a search is
  triggered. Lidarr's import of a new artist unmonitors its albums, so albums of new artists are monitored
  and searched again once Lidarr has finished importing. The sync doesn't wait for that: a background check
  every 30 seconds does it (up to the Lidarr import wait in Settings, default 20 minutes) and adds a
  `[later]` line to the run's log. The CLI still waits.
* Syncs are incremental: what earlier syncs found (the song version of a video, the Lidarr release of an
  album, the Jellyfin item of a track) is remembered, so a sync only looks up tracks that are new or not
  resolved yet. Albums in Lidarr's library come from a single request; remembered Jellyfin matches are
  confirmed in one request. Lookups that found nothing are tried again after a week. *Re-check all tracks*
  (playlist Settings) makes the next sync of that playlist look everything up again.
* In Jellyfin each track is matched against the library of the configured user: the title and one of the
  artists both have to match on their own (the album only breaks ties), so a different song by the same artist
  or a cover by someone else is not taken. Only tracks that are already in the library are added, in playlist order.
* A track that matches the same Jellyfin song as an earlier track shows as "Same song" and is counted once,
  because the Jellyfin playlist holds each song once. After adding, the playlist is read back; tracks Jellyfin
  did not keep show as "Not added" and the run fails.
* By default the Jellyfin playlist is **append-only**: tracks are added, never removed or reordered, so edits
  you make in Jellyfin are kept. Re-running a sync after Lidarr has finished downloading picks up the new files.
* With **Mirror in Jellyfin** (playlist Settings, or `--mirror` on the CLI) every sync makes the Jellyfin playlist
  match the YouTube one instead: songs no track maps to (earlier wrong matches, songs added by hand) and
  duplicate entries are removed, and the YouTube order is restored. Jellyfin doesn't let an API key move
  playlist items, so the order is fixed by keeping the part of the playlist that is already right and removing
  and re-adding everything after it. The mirror is skipped for a sync in which any Jellyfin lookup failed or no
  track matched, so a hiccup never empties a playlist.

## Lidarr and Jellyfin notes

* The Lidarr **metadata profile** must allow **Singles** and **EPs**, otherwise tracks that only exist as
  singles can't be added.
* Lidarr's **root folder must be the folder your Jellyfin music library points to**.
* To see new downloads in Jellyfin quickly, enable real-time monitoring on the Jellyfin music library, or use
  Lidarr, Settings, Connect, Jellyfin/Emby to trigger library updates.
* The Jellyfin API key is created in Jellyfin under Dashboard, API Keys; the Lidarr key under
  Settings, General, Security.
* **Lidarr webhook** (optional, Settings, Lidarr webhook, Turn on): in Lidarr, Settings, Connect, +, Webhook,
  tick *On Release Import*, method POST, and enter the shown URL and password (any username). After an album
  import, the playlists that are waiting for it get a Jellyfin-only update two minutes after the first import
  of a burst (one more try ten minutes later if that update found nothing new, e.g. Jellyfin hadn't scanned the
  files yet), instead of waiting for the next scheduled sync. The password may also be sent as `?token=` in
  the URL, but then it shows up in access logs. *New URL* replaces the password.

## Private playlists

Public and unlisted playlists and auto-generated mixes need no login. For private playlists or Liked Music,
open Settings, YouTube Music login and paste the request headers of an authenticated request to
`music.youtube.com` (browser developer tools, Network tab, any `/browse` request, copy the request headers).
They are stored in `/data/browser.json` and only used by this server. Remove them from the same page.
The headers expire when you sign out of YouTube in that browser; paste fresh ones if private playlists
start failing. Public playlists and mixes keep syncing without the login if it expires (the run log says so).

## Limitations

* Mixes (`RD...` playlists, e.g. "Workout Supermix") change over time and may be personalised to the
  account that opened them; each sync reads the current contents. Tracks that disappear from a mix stay in
  the Jellyfin playlist unless Mirror in Jellyfin is on.
* Tracks that are not on MusicBrainz / in Lidarr's metadata can't be downloaded automatically and show up
  under "Not found in Lidarr"; add those manually.
* Matching is heuristic. Remixes, live versions and very generic titles can occasionally pick the wrong release.
* Deleting a playlist in the app removes only its records here, not the Jellyfin playlist or Lidarr albums.
* There is one password and no CSRF tokens (the session cookie is `SameSite=Lax`); put the app behind your
  LAN/VPN or a reverse proxy with HTTPS.
