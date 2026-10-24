"""HTTP routes, one APIRouter per area. main.py registers them on the app."""
from app.routes import art, auth, playlists, settings

routers = [auth.ar, playlists.ar, settings.ar, art.ar]
