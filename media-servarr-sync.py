"""
Media Servarr Sync
------------------
Webhook receiver for Sonarr & Radarr that triggers targeted Plex folder scans.

Logic:
1. Webhooks are immediately placed in a deduplicated Queue.
2. A background worker processes the queue after a configurable delay.
3. (Optional) Rclone VFS cache is cleared/refreshed for the specific path
   when USE_RCLONE=true. Skip entirely if you don't use rclone.
4. Plex is triggered to perform a partial scan with retries on timeout.
5. Health endpoint exposes queue depth, worker status, and recent history.
"""

import os
import time
import json
import re
import threading
import requests
import urllib.parse
import queue
import logging
import signal
import sys
import sqlite3
import ipaddress
import secrets as _secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import plexapi
from waitress import serve
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from plexapi.server import PlexServer
from plexapi.base import MediaContainer

# ---------------------------------------------------------------------------
# Timezone — resolved before logging so timestamps are correct from line 1
# ---------------------------------------------------------------------------
load_dotenv()

def _resolve_tz() -> timezone:
    tz_name = os.getenv("TZ", "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            print(f"WARNING: Unknown timezone '{tz_name}', falling back to UTC", flush=True)
    return timezone.utc

LOCAL_TZ = _resolve_tz()


def now_local() -> datetime:
    """Return the current time in the configured timezone."""
    return datetime.now(LOCAL_TZ)


# ---------------------------------------------------------------------------
# Logging — timezone-aware timestamps
# ---------------------------------------------------------------------------

class _TZFormatter(logging.Formatter):
    """Logging formatter that stamps records in LOCAL_TZ."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_TZFormatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

app = Flask(__name__)
csrf = CSRFProtect(app)


@app.template_filter('datetimeformat')
def _datetimeformat(ts):
    """Format a Unix timestamp as a local date/time string."""
    try:
        return datetime.fromtimestamp(int(ts), tz=LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(ts) if ts else ''

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_duration(duration_str) -> int:
    """Convert a duration string like '30s', '5m', '1h' to seconds."""
    if not duration_str:
        return 0
    s = str(duration_str).strip().lower()
    if s.isdigit():
        return int(s)
    match = re.match(r'^(\d+)([smhd])$', s)
    if not match:
        return 0
    value, unit = match.groups()
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return int(value) * multipliers[unit]


def normalize_path(path: str, is_dir: bool = True) -> str:
    if not path or not isinstance(path, str):
        return ""
    clean = path.strip().replace('\\', '/').rstrip('/')
    return clean + '/' if is_dir else clean


def parse_json_env(env_name: str) -> dict:
    raw = os.getenv(env_name, "{}").strip().strip("'")
    try:
        data = json.loads(raw)
        return {normalize_path(k, is_dir=False).lower(): v for k, v in data.items()}
    except Exception as exc:
        log.error("Failed to parse %s: %s", env_name, exc)
        return {}


def apply_path_mapping(path: str, mapping: dict, label: str, is_dir: bool = True) -> str:
    orig = normalize_path(path, is_dir=False)
    lower = orig.lower()
    for prefix in sorted(mapping.keys(), key=len, reverse=True):
        if lower.startswith(prefix):
            result = normalize_path(str(mapping[prefix]) + orig[len(prefix):], is_dir=is_dir)
            log.debug("[%s] Map: '%s' -> '%s'", label, orig, result)
            return result
    return normalize_path(orig, is_dir=is_dir)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PLEX_URL        = os.getenv("PLEX_URL", "http://127.0.0.1:32400").rstrip('/')
PLEX_TOKEN      = os.getenv("PLEX_TOKEN", "")
PLEX_TIMEOUT    = parse_duration(os.getenv("PLEX_TIMEOUT", "60")) or 60
PORT            = int(os.getenv("PORT", "5000"))
WEBHOOK_DELAY   = parse_duration(os.getenv("WEBHOOK_DELAY", "30"))
MINIMUM_AGE     = parse_duration(os.getenv("MINIMUM_AGE", "0"))
HISTORY_DAYS    = int(os.getenv("HISTORY_DAYS", "7"))
SYNC_COOLDOWN   = parse_duration(os.getenv("SYNC_COOLDOWN", "5m"))
MANUAL_USER     = os.getenv("MANUAL_USER", "admin")
MANUAL_PASS     = os.getenv("MANUAL_PASS", "password")
# Used to sign session cookies — set a long random string in your .env
SECRET_KEY      = os.getenv("SECRET_KEY", os.urandom(24).hex())
# Demo mode: enables /login/demo and populates pages with fake data for screenshots
DEMO_MODE       = os.getenv("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes")

# Optional Sonarr/Radarr API credentials — used to look up quality profile names.
# If unset, quality_profile badges are simply omitted.
SONARR_URL     = os.getenv("SONARR_URL", "").rstrip('/')
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
RADARR_URL     = os.getenv("RADARR_URL", "").rstrip('/')
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")

# Onboarding / offboarding links shown on the invite page
ONBOARD_WIKI_URL    = os.getenv("ONBOARD_WIKI_URL", "").rstrip('/')
ONBOARD_REQUEST_URL = os.getenv("ONBOARD_REQUEST_URL", "").rstrip('/')

# Rclone — set USE_RCLONE=false to skip all rclone calls entirely
USE_RCLONE        = os.getenv("USE_RCLONE", "false").strip().lower() in ("1", "true", "yes")
RCLONE_RC_URL     = os.getenv("RCLONE_RC_URL", "").rstrip('/')
RCLONE_RC_USER    = os.getenv("RCLONE_RC_USER", "")
RCLONE_RC_PASS    = os.getenv("RCLONE_RC_PASS", "")
RCLONE_MOUNT_ROOT = os.getenv("RCLONE_MOUNT_ROOT", "").rstrip('/')

plexapi.TIMEOUT = PLEX_TIMEOUT

# PlexAPI reads PLEXAPI_HEADER_IDENTIFIER from the environment automatically on import.
# We set it explicitly here as well so it's always applied regardless of import order.
PLEX_IDENTIFIER           = os.getenv("PLEXAPI_HEADER_IDENTIFIER", "media-servarr-sync")
plexapi.X_PLEX_IDENTIFIER = PLEX_IDENTIFIER
plexapi.X_PLEX_PRODUCT    = PLEX_IDENTIFIER

PATH_REPLACEMENTS        = parse_json_env("PATH_REPLACEMENTS")
RCLONE_PATH_REPLACEMENTS = parse_json_env("RCLONE_PATH_REPLACEMENTS")
SECTION_MAPPING          = parse_json_env("SECTION_MAPPING")

# Apply secret key now that config is loaded
app.secret_key = SECRET_KEY


# ---------------------------------------------------------------------------
# Quality profile lookup (Sonarr / Radarr API)
# ---------------------------------------------------------------------------

# Two-level cache: profile list (id→name) and per-series/movie id→profile_id.
# Both are loaded lazily on the first webhook and held in memory.
_qp_profiles:    dict[str, dict[int, str]] = {}   # arr_type → {profile_id: name}
_qp_series_map:  dict[str, dict[int, int]] = {}   # arr_type → {item_id: profile_id}
_qp_lock = threading.Lock()

_cf_cache: dict[str, list] = {}   # arr_type → [{id, name, ...}, ...]
_cf_lock  = threading.Lock()

# ---------------------------------------------------------------------------
# Geo-IP cache (server-side proxy to ip-api.com)
# ---------------------------------------------------------------------------
_geo_cache: dict[str, dict] = {}   # ip → {status, city, country, lat, lon, ...}
_geo_cache_lock = threading.Lock()


def _is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return True


def _get_quality_profile_name(arr_type: str, item_id: int) -> str:
    """Return the quality-profile name for a Sonarr series or Radarr movie.

    Returns "" if credentials are not configured or the API call fails.
    """
    url = SONARR_URL if arr_type == "sonarr" else RADARR_URL
    key = SONARR_API_KEY if arr_type == "sonarr" else RADARR_API_KEY
    if not url or not key:
        log.debug("[%s] Quality profile lookup skipped — %s_URL / %s_API_KEY not set",
                  arr_type.upper(), arr_type.upper(), arr_type.upper())
        return ""
    if not item_id:
        return ""

    headers = {"X-Api-Key": key}
    label   = arr_type.upper()

    with _qp_lock:
        # Ensure profile list is loaded
        if arr_type not in _qp_profiles:
            try:
                r = requests.get(f"{url}/api/v3/qualityprofile",
                                 headers=headers, timeout=5)
                r.raise_for_status()
                _qp_profiles[arr_type] = {p['id']: p['name'] for p in r.json()}
                log.info("[%s] Loaded %d quality profiles from API", label,
                         len(_qp_profiles[arr_type]))
            except Exception as exc:
                log.warning("[%s] Could not load quality profiles: %s", label, exc)
                _qp_profiles[arr_type] = {}

        if arr_type not in _qp_series_map:
            _qp_series_map[arr_type] = {}

        # Look up this item's profile_id if not yet cached
        if item_id not in _qp_series_map[arr_type]:
            endpoint = "series" if arr_type == "sonarr" else "movie"
            try:
                r = requests.get(f"{url}/api/v3/{endpoint}/{item_id}",
                                 headers=headers, timeout=5)
                r.raise_for_status()
                _qp_series_map[arr_type][item_id] = r.json().get('qualityProfileId', 0)
            except Exception as exc:
                log.warning("[%s] Could not fetch %s/%d for quality profile: %s",
                            label, endpoint, item_id, exc)
                return ""

        profile_id = _qp_series_map[arr_type].get(item_id, 0)
        return _qp_profiles[arr_type].get(profile_id, "") if profile_id else ""


def _refresh_custom_formats(arr_type: str) -> None:
    """Fetch and cache all custom formats defined in Sonarr/Radarr.

    Called on startup and every hour by the scheduler thread so the cache
    stays current as users add/remove custom formats in their arr instances.
    """
    url = SONARR_URL if arr_type == "sonarr" else RADARR_URL
    key = SONARR_API_KEY if arr_type == "sonarr" else RADARR_API_KEY
    if not url or not key:
        return
    try:
        r = requests.get(f"{url}/api/v3/customformat",
                         headers={"X-Api-Key": key}, timeout=10)
        r.raise_for_status()
        formats = r.json()
        with _cf_lock:
            _cf_cache[arr_type] = formats
        log.info("[%s] Custom format cache refreshed — %d formats",
                 arr_type.upper(), len(formats))
    except Exception as exc:
        log.warning("[%s] Could not refresh custom formats: %s", arr_type.upper(), exc)


def _fetch_custom_formats_for_file(arr_type: str, file_id: int) -> list[str]:
    """Return matched custom-format names for a specific episode/movie file.

    Calls /api/v3/episodefile/{id} (Sonarr) or /api/v3/moviefile/{id} (Radarr)
    which includes the resolved customFormats for that file — information not
    reliably present in the webhook payload.

    Returns [] if credentials are not configured or the API call fails.
    """
    if not file_id:
        return []
    url = SONARR_URL if arr_type == "sonarr" else RADARR_URL
    key = SONARR_API_KEY if arr_type == "sonarr" else RADARR_API_KEY
    if not url or not key:
        return []
    endpoint = "episodefile" if arr_type == "sonarr" else "moviefile"
    try:
        r = requests.get(f"{url}/api/v3/{endpoint}/{file_id}",
                         headers={"X-Api-Key": key}, timeout=5)
        r.raise_for_status()
        names = [cf['name'] for cf in r.json().get('customFormats', []) if cf.get('name')]
        log.info("[%s] API custom formats for %s/%d: %r",
                 arr_type.upper(), endpoint, file_id, names)
        return names
    except Exception as exc:
        log.warning("[%s] Could not fetch custom formats for %s/%d: %s",
                    arr_type.upper(), endpoint, file_id, exc)
        return []


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class SyncTask:
    section_id: str
    raw_path: str
    rclone_host_path: str
    age_check_path: str
    mapped_folder: str
    label: str
    episode: str = ""
    quality: str = ""
    custom_formats: str = ""   # JSON-encoded list of format name strings
    quality_profile: str = ""  # e.g. "HD-1080p" from Sonarr/Radarr quality profiles
    queued_at: float = field(default_factory=time.monotonic)

    def __eq__(self, other):
        return isinstance(other, SyncTask) and self.mapped_folder == other.mapped_folder

    def __hash__(self):
        return hash(self.mapped_folder)


class SyncHistory:
    """Thread-safe SQLite-backed sync history with configurable retention."""
    def __init__(self, db_path: str = "/data/sync_history.db", retention_days: int = 7):
        self._db_path = db_path
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    label TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    duration_s REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    episode TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON sync_history(created_at DESC)")
            # Migrate existing databases that lack newer columns
            existing = {row[1] for row in conn.execute("PRAGMA table_info(sync_history)")}
            if 'episode' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN episode TEXT")
            if 'quality' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN quality TEXT DEFAULT ''")
            if 'custom_formats' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN custom_formats TEXT DEFAULT ''")
            if 'quality_profile' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN quality_profile TEXT DEFAULT ''")
            conn.commit()

    def add(self, entry: dict):
        """Add a sync entry and prune old records."""
        with self._lock:
            cutoff = time.time() - (self._retention_days * 86400)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO sync_history
                        (ts, label, path, status, error, duration_s, created_at, episode, quality, custom_formats, quality_profile)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry['ts'],
                    entry['label'],
                    entry['path'],
                    entry['status'],
                    entry.get('error', ''),
                    entry['duration_s'],
                    time.time(),
                    entry.get('episode', ''),
                    entry.get('quality', ''),
                    entry.get('custom_formats', ''),
                    entry.get('quality_profile', ''),
                ))
                # Prune old entries
                conn.execute("DELETE FROM sync_history WHERE created_at < ?", (cutoff,))
                conn.commit()

    def get_recent(self, limit: int = 50, offset: int = 0,
                   search: str = "", status_filter: str = "",
                   quality_filter: str = "", profile_filter: str = "") -> list:
        """Get recent entries with optional path search and status filter."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                conditions, params = [], []
                if search:
                    conditions.append("path LIKE ?")
                    params.append(f"%{search}%")
                if status_filter:
                    conditions.append("status = ?")
                    params.append(status_filter)
                if quality_filter:
                    conditions.append("quality = ?")
                    params.append(quality_filter)
                if profile_filter:
                    conditions.append("quality_profile = ?")
                    params.append(profile_filter)
                query = (
                    "SELECT ts, label, path, status, error, duration_s, episode, quality, custom_formats, quality_profile"
                    " FROM sync_history"
                )
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

    def count(self, search: str = "", status_filter: str = "",
              quality_filter: str = "", profile_filter: str = "") -> int:
        """Get total number of entries matching optional search and status filter."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conditions, params = [], []
                if search:
                    conditions.append("path LIKE ?")
                    params.append(f"%{search}%")
                if status_filter:
                    conditions.append("status = ?")
                    params.append(status_filter)
                if quality_filter:
                    conditions.append("quality = ?")
                    params.append(quality_filter)
                if profile_filter:
                    conditions.append("quality_profile = ?")
                    params.append(profile_filter)
                query = "SELECT COUNT(*) FROM sync_history"
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                cursor = conn.execute(query, params)
                return cursor.fetchone()[0]

    def as_list(self) -> list:
        """For backward compatibility with old code."""
        return self.get_recent(limit=50)

    def get_stats(self) -> dict:
        """Return aggregate statistics for the current retention window."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute("""
                    SELECT
                        COUNT(*)                                              AS total,
                        SUM(CASE WHEN status = 'ok'     THEN 1 ELSE 0 END)  AS successful,
                        SUM(CASE WHEN status = 'error'  THEN 1 ELSE 0 END)  AS failed,
                        SUM(CASE WHEN label  = 'SONARR' THEN 1 ELSE 0 END)  AS sonarr,
                        SUM(CASE WHEN label  = 'RADARR' THEN 1 ELSE 0 END)  AS radarr,
                        SUM(CASE WHEN label  = 'MANUAL' THEN 1 ELSE 0 END)  AS manual,
                        ROUND(AVG(duration_s), 2)                            AS avg_duration_s
                    FROM sync_history
                """).fetchone()
                last = conn.execute("""
                    SELECT ts, status, label, path
                    FROM sync_history
                    ORDER BY created_at DESC
                    LIMIT 1
                """).fetchone()
        return {
            "total":          row[0] or 0,
            "successful":     row[1] or 0,
            "failed":         row[2] or 0,
            "sonarr":         row[3] or 0,
            "radarr":         row[4] or 0,
            "manual":         row[5] or 0,
            "avg_duration_s": row[6] or 0.0,
            "last_sync": {
                "ts":     last[0],
                "status": last[1],
                "label":  last[2],
                "path":   last[3],
            } if last else None,
        }


class InviteDB:
    """SQLite-backed invite system with per-user grant tracking and auto-expiry."""

    def __init__(self, db_path: str = "/data/invites.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT UNIQUE NOT NULL,
                    label TEXT DEFAULT '',
                    section_ids TEXT DEFAULT '[]',
                    allow_sync INTEGER DEFAULT 0,
                    allow_channels INTEGER DEFAULT 1,
                    home_user INTEGER DEFAULT 0,
                    duration_days INTEGER DEFAULT 0,
                    max_uses INTEGER DEFAULT 1,
                    uses INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    link_expires_at INTEGER,
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invite_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invite_id INTEGER NOT NULL,
                    plex_username TEXT NOT NULL,
                    accepted_at INTEGER NOT NULL,
                    access_expires_at INTEGER,
                    revoked INTEGER DEFAULT 0,
                    FOREIGN KEY (invite_id) REFERENCES invites(id)
                )
            """)
            conn.commit()

    def create(self, label: str, section_ids: list, allow_sync: bool,
               allow_channels: bool, home_user: bool, duration_days: int,
               max_uses: int, link_expires_days: int) -> str:
        token = _secrets.token_urlsafe(16)
        link_exp = int(time.time() + link_expires_days * 86400) if link_expires_days else None
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO invites
                        (token, label, section_ids, allow_sync, allow_channels, home_user,
                         duration_days, max_uses, created_at, link_expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (token, label, json.dumps(section_ids), int(allow_sync),
                      int(allow_channels), int(home_user), duration_days,
                      max_uses, int(time.time()), link_exp))
                conn.commit()
        return token

    def get(self, token: str) -> Optional[dict]:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
                return dict(row) if row else None

    def list_all(self) -> list:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM invites ORDER BY created_at DESC"
                ).fetchall()
                return [dict(r) for r in rows]

    def record_acceptance(self, invite_id: int, plex_username: str, duration_days: int):
        access_exp = int(time.time() + duration_days * 86400) if duration_days else None
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO invite_grants
                        (invite_id, plex_username, accepted_at, access_expires_at)
                    VALUES (?, ?, ?, ?)
                """, (invite_id, plex_username, int(time.time()), access_exp))
                conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (invite_id,))
                conn.commit()

    def get_grants(self, invite_id: Optional[int] = None) -> list:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                if invite_id is not None:
                    rows = conn.execute(
                        "SELECT * FROM invite_grants WHERE invite_id = ? ORDER BY accepted_at DESC",
                        (invite_id,)
                    ).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT g.*, i.label AS invite_label, i.token AS invite_token
                        FROM invite_grants g
                        JOIN invites i ON i.id = g.invite_id
                        ORDER BY g.accepted_at DESC
                    """).fetchall()
                return [dict(r) for r in rows]

    def revoke_invite(self, token: str):
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("UPDATE invites SET status = 'revoked' WHERE token = ?", (token,))
                conn.commit()

    def revoke_grant(self, grant_id: int):
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("UPDATE invite_grants SET revoked = 1 WHERE id = ?", (grant_id,))
                conn.commit()

    def get_expired_grants(self) -> list:
        now = int(time.time())
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT * FROM invite_grants
                    WHERE access_expires_at IS NOT NULL
                      AND access_expires_at < ?
                      AND revoked = 0
                """, (now,)).fetchall()
                return [dict(r) for r in rows]


history = SyncHistory(db_path="/data/sync_history.db", retention_days=HISTORY_DAYS)
invite_db = InviteDB(db_path="/data/invites.db")
sync_queue: queue.Queue = queue.Queue()
_in_flight: dict = {}           # mapped_folder -> SyncTask, currently queued or being processed
_cooldown: dict = {}            # mapped_folder -> expiry monotonic timestamp, recently completed
_in_flight_lock = threading.Lock()


def _prune_cooldown():
    """Remove expired cooldown entries. Must be called with _in_flight_lock held."""
    now = time.monotonic()
    expired = [k for k, v in _cooldown.items() if now >= v]
    for k in expired:
        del _cooldown[k]
_worker_alive = threading.Event()
_worker_alive.set()


# ---------------------------------------------------------------------------
# Plex connection (lazy, with reconnect)
# ---------------------------------------------------------------------------

_plex: Optional[PlexServer] = None
_plex_lock = threading.Lock()


def get_plex() -> Optional[PlexServer]:
    global _plex
    with _plex_lock:
        if _plex is None:
            try:
                _plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT)
                log.info("Connected to Plex: %s (timeout=%ds)", _plex.friendlyName, PLEX_TIMEOUT)
            except Exception as exc:
                log.error("Could not connect to Plex: %s", exc)
        return _plex


def invalidate_plex():
    """Force a reconnect on the next call."""
    global _plex
    with _plex_lock:
        _plex = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Rclone
# ---------------------------------------------------------------------------

def rclone_vfs_refresh(host_path: str, label: str):
    """Clear and async-refresh the rclone VFS cache for the given path.
    No-op when USE_RCLONE is false."""
    if not USE_RCLONE:
        return
    if not RCLONE_RC_URL:
        log.warning("[%s] [RCLONE] USE_RCLONE=true but RCLONE_RC_URL is not set — skipping.", label)
        return

    auth = (RCLONE_RC_USER, RCLONE_RC_PASS) if RCLONE_RC_USER else None
    full = host_path.rstrip('/')
    root = RCLONE_MOUNT_ROOT.rstrip('/')

    if root and full.startswith(root):
        target = full[len(root):].lstrip('/')
    else:
        target = full
        if root:
            log.warning("[%s] [RCLONE] Path '%s' is not under mount root '%s'", label, full, root)

    try:
        log.info("[%s] [RCLONE] Forget: '%s'", label, target)
        requests.post(f"{RCLONE_RC_URL}/vfs/forget",
                      json={"dir": target}, auth=auth, timeout=15)

        log.info("[%s] [RCLONE] Refresh (async): '%s'", label, target)
        res = requests.post(f"{RCLONE_RC_URL}/vfs/refresh",
                            json={"dir": target, "recursive": True, "_async": True},
                            auth=auth, timeout=15)
        if res.ok:
            log.info("[%s] [RCLONE] Queued job %s", label, res.json().get('jobid'))
        else:
            log.error("[%s] [RCLONE] Error %d: %s", label, res.status_code, res.text)
    except requests.RequestException as exc:
        log.error("[%s] [RCLONE] Connection error: %s", label, exc)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def sync_worker():
    log.info("Sync worker started")
    while _worker_alive.is_set():
        try:
            task: SyncTask = sync_queue.get(timeout=1)
        except queue.Empty:
            continue

        start = time.monotonic()
        status = "ok"
        error_msg = ""

        try:
            # Optional delay (e.g. wait for Sonarr to finish writing)
            if WEBHOOK_DELAY > 0:
                elapsed = time.monotonic() - task.queued_at
                remaining = WEBHOOK_DELAY - elapsed
                if remaining > 0:
                    log.info("[%s] [DELAY] Waiting %.0fs before processing...", task.label, remaining)
                    time.sleep(remaining)

            # Rclone VFS cache
            rclone_vfs_refresh(task.rclone_host_path, task.label)

            # Minimum file age check
            if MINIMUM_AGE > 0:
                check = task.age_check_path.rstrip('/')
                if os.path.exists(check):
                    age = time.time() - os.path.getmtime(check)
                    wait = MINIMUM_AGE - age
                    if wait > 0:
                        log.info("[%s] [AGE] File too young, waiting %ds...", task.label, int(wait))
                        time.sleep(wait)
                else:
                    log.warning("[%s] [AGE] Path not visible in container: %s", task.label, check)

            # Plex scan with retry on timeout
            plex_instance = get_plex()
            if plex_instance:
                for attempt in range(1, 4):
                    try:
                        library = plex_instance.library.sectionByID(task.section_id)
                        log.info("[%s] [SCAN] Attempt %d/3 → %s", task.label, attempt, task.mapped_folder)
                        library.update(path=task.mapped_folder)

                        time.sleep(20)
                        item = _find_plex_item(plex_instance, library, task)

                        if item:
                            log.info("[%s] [METADATA] Found '%s', analyzing...", task.label, item.title)
                            item.analyze()
                        else:
                            log.warning("[%s] [METADATA] Item not found in library DB.", task.label)
                        break

                    except Exception as exc:
                        if "timeout" in str(exc).lower() and attempt < 3:
                            log.warning("[%s] [PLEX] Timeout on attempt %d, retrying in 10s...", task.label, attempt)
                            # Reconnect in case the connection went stale
                            invalidate_plex()
                            plex_instance = get_plex()
                            time.sleep(10)
                        else:
                            raise

        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            log.error("[%s] [ERROR] %s", task.label, exc)
        finally:
            with _in_flight_lock:
                _in_flight.pop(task.mapped_folder, None)
                if SYNC_COOLDOWN > 0:
                    _cooldown[task.mapped_folder] = time.monotonic() + SYNC_COOLDOWN

            duration = round(time.monotonic() - start, 1)
            history.add({
                "ts": now_local().isoformat(),
                "label": task.label,
                "path": task.mapped_folder,
                "status": status,
                "error": error_msg,
                "duration_s": duration,
                "episode": task.episode,
                "quality": task.quality,
                "custom_formats": task.custom_formats,
                "quality_profile": task.quality_profile,
            })
            sync_queue.task_done()

    log.info("Sync worker stopped")


def custom_format_refresh_scheduler() -> None:
    """Background thread: refresh the custom-format cache from Sonarr/Radarr every hour.

    Runs an initial population 15 seconds after startup (giving the arrs time
    to be reachable), then repeats every hour.  Shutdown is honoured within one
    second via the shared _worker_alive event.
    """
    log.info("Custom format scheduler started")
    # Short initial delay so the arrs are likely up before we hit their API
    deadline = time.monotonic() + 15
    while _worker_alive.is_set() and time.monotonic() < deadline:
        time.sleep(1)

    while _worker_alive.is_set():
        for arr_type in ('sonarr', 'radarr'):
            _refresh_custom_formats(arr_type)
        # Wait 1 hour, checking shutdown signal each second
        deadline = time.monotonic() + 3600
        while _worker_alive.is_set() and time.monotonic() < deadline:
            time.sleep(1)

    log.info("Custom format scheduler stopped")


def invite_expiry_scheduler() -> None:
    """Background thread: auto-revoke expired Plex access grants once per hour."""
    log.info("Invite expiry scheduler started")
    # Initial delay before first check
    deadline = time.monotonic() + 60
    while _worker_alive.is_set() and time.monotonic() < deadline:
        time.sleep(1)
    while _worker_alive.is_set():
        try:
            expired = invite_db.get_expired_grants()
            for grant in expired:
                try:
                    plex_instance = get_plex()
                    if plex_instance:
                        account = plex_instance.myPlexAccount()
                        account.removeFriend(grant['plex_username'])
                        log.info("[INVITE] Auto-revoked expired access for '%s'", grant['plex_username'])
                    invite_db.revoke_grant(grant['id'])
                except Exception as exc:
                    log.warning("[INVITE] Error auto-revoking '%s': %s",
                                grant.get('plex_username'), exc)
        except Exception as exc:
            log.error("[INVITE] Expiry scheduler error: %s", exc)
        deadline = time.monotonic() + 3600
        while _worker_alive.is_set() and time.monotonic() < deadline:
            time.sleep(1)
    log.info("Invite expiry scheduler stopped")


def _find_plex_item(plex_instance, library, task: SyncTask):
    """Try to locate the newly-scanned item in Plex via path query then title search."""
    search_path = task.mapped_folder.rstrip('/')
    folder_name = os.path.basename(search_path)
    clean_title = re.sub(r'\s*[\(\{\[].*', '', folder_name).strip()

    for i in range(6):
        log.info("[%s] [METADATA] Lookup %d/6 for '%s'", task.label, i + 1, clean_title)
        try:
            encoded = urllib.parse.quote(search_path)
            xml = plex_instance.query(f"/library/sections/{task.section_id}/all?path={encoded}")
            container = MediaContainer(plex_instance, xml)
            if container.metadata:
                return container.metadata[0]
        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise

        # Fallback: title search + path match
        try:
            for res in library.search(title=clean_title):
                locs = res.locations if hasattr(res, 'locations') else []
                if not locs and hasattr(res, 'media'):
                    locs = [p.file for m in res.media for p in m.parts]
                if any(search_path in loc for loc in locs):
                    return res
        except Exception:
            pass

        time.sleep(20)

    return None


# ---------------------------------------------------------------------------
# Webhook processing
# ---------------------------------------------------------------------------

def _merge_episode_counts(existing: str, incoming: str) -> str:
    """Accumulate episode info when duplicate webhooks arrive for the same folder.

    When individual filenames are known, stores as a JSON list so the UI can
    render a hover tooltip. Falls back to a plain count string for older records
    that only carry a count.
    """
    def _to_list(ep: str) -> tuple:
        if not ep:
            return [], 0
        try:
            parsed = json.loads(ep)
            if isinstance(parsed, list):
                return parsed, len(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        m = re.match(r'^(\d+) episodes?$', ep.strip())
        if m:
            return [], int(m.group(1))
        return [ep], 1  # single filename

    def _ep_key(ep: str):
        m = re.search(r'[Ss](\d+)[Ee](\d+)', ep)
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else None

    existing_names, existing_count = _to_list(existing)
    incoming_names, incoming_count = _to_list(incoming)

    if existing_names or incoming_names:
        seen_keys = {_ep_key(ep) for ep in existing_names}
        merged = list(existing_names)
        for ep in incoming_names:
            k = _ep_key(ep)
            if k is None or k not in seen_keys:
                merged.append(ep)
                if k:
                    seen_keys.add(k)
        if len(merged) == 1:
            return merged[0]
        return json.dumps(merged)

    # Both sides are count-only — no filenames to recover
    total = existing_count + incoming_count
    return f"{total} episodes" if total != 1 else (existing or incoming)


def _parse_episode_field(ep_str: str) -> tuple:
    """Parse a stored episode field into (display_str, episode_list).

    episode_list is non-empty only when individual filenames are known,
    which enables the hover tooltip in the UI.
    """
    if not ep_str:
        return "", []
    try:
        parsed = json.loads(ep_str)
        if isinstance(parsed, list) and parsed:
            if len(parsed) == 1:
                return parsed[0], []
            return f"{len(parsed)} episodes", parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return ep_str, []


def _extract_file_meta(file_obj: dict) -> tuple:
    """Return (quality_name, custom_format_names) from an episodeFile / movieFile dict.

    Sonarr/Radarr webhooks send episodeFile.quality as a plain string (e.g. "WEBDL-1080p").
    The API object form {quality: {name: "..."}} is also handled for completeness.
    Note: customFormats are at the top-level payload, not inside the file object — callers
    must extract those separately from the raw event dict.
    """
    quality = ""
    q = file_obj.get('quality')
    if isinstance(q, str):
        quality = q                          # webhook plain-string form
    elif isinstance(q, dict):
        inner = q.get('quality', {})
        if isinstance(inner, dict):
            quality = inner.get('name', '') or ''
        elif isinstance(inner, str):
            quality = inner

    return quality, []


def _merge_custom_formats(existing: str, incoming: str) -> str:
    """Union two JSON-encoded custom-format name lists, preserving order."""
    def _to_list(s: str) -> list:
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, str)]
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    merged = list(_to_list(existing))
    seen = set(merged)
    for fmt in _to_list(incoming):
        if fmt not in seen:
            merged.append(fmt)
            seen.add(fmt)
    return json.dumps(merged) if merged else ""


def enqueue_sync(raw_path: str, label: str, episode: str = "",
                 quality: str = "", custom_formats: str = "", quality_profile: str = ""):
    """Validate, map, and enqueue a sync task. Returns (response_dict, http_status)."""
    if not raw_path:
        return {"status": "skipped", "reason": "empty path"}, 200

    mapped_folder  = apply_path_mapping(raw_path, PATH_REPLACEMENTS, label, is_dir=True)
    rclone_path    = apply_path_mapping(raw_path, RCLONE_PATH_REPLACEMENTS, label, is_dir=False) \
                     if USE_RCLONE else ""
    age_check_path = mapped_folder

    # Section mapping
    comp = mapped_folder.rstrip('/').lower()
    section_id = next(
        (SECTION_MAPPING[p] for p in sorted(SECTION_MAPPING, key=len, reverse=True) if comp.startswith(p)),
        None
    )

    if not section_id:
        log.warning("[%s] [SKIP] No section mapping for '%s'", label, mapped_folder)
        return {"status": "skipped", "reason": "no section mapping"}, 200

    task = SyncTask(
        section_id=section_id,
        raw_path=raw_path,
        rclone_host_path=rclone_path,
        age_check_path=age_check_path,
        mapped_folder=mapped_folder,
        label=label,
        episode=episode,
        quality=quality,
        custom_formats=custom_formats,
        quality_profile=quality_profile,
    )

    # Deduplication: if same folder already queued or in cooldown, skip re-queuing
    with _in_flight_lock:
        if mapped_folder in _in_flight:
            existing_task = _in_flight[mapped_folder]
            if episode:
                merged = _merge_episode_counts(existing_task.episode, episode)
                existing_task.episode = merged
                log.info("[%s] [DEDUP] Already queued: %s — merged episode info to '%s'",
                         label, mapped_folder, merged)
            else:
                log.info("[%s] [DEDUP] Already queued: %s", label, mapped_folder)
            # Quality / profile: keep first non-empty value
            if not existing_task.quality and quality:
                existing_task.quality = quality
            if not existing_task.quality_profile and quality_profile:
                existing_task.quality_profile = quality_profile
            # Custom formats: union
            if custom_formats:
                existing_task.custom_formats = _merge_custom_formats(
                    existing_task.custom_formats, custom_formats)
            return {"status": "deduplicated"}, 200

        if SYNC_COOLDOWN > 0:
            _prune_cooldown()
            expiry = _cooldown.get(mapped_folder, 0)
            if time.monotonic() < expiry:
                log.info("[%s] [COOLDOWN] Recently synced, dropping follow-up event: %s", label, mapped_folder)
                return {"status": "deduplicated"}, 200

        _in_flight[mapped_folder] = task

    sync_queue.put(task)
    log.info("[%s] [QUEUE] Added (depth=%d): %s", label, sync_queue.qsize(), mapped_folder)
    return {"status": "queued"}, 200


def process_webhook(data: dict, instance_type: str):
    if not data:
        return jsonify({"error": "No payload"}), 400

    event = data.get('eventType', '')

    if event == "Test":
        label_up = instance_type.upper()
        file_obj  = data.get('episodeFile') or data.get('movieFile') or {}
        raw_qual  = file_obj.get('quality', 'NOT PRESENT in episodeFile/movieFile')
        raw_cf    = data.get('customFormats', 'NOT PRESENT at top level')
        log.info("[%s] Test webhook received — quality=%r  customFormats=%r  payload_keys=%s",
                 label_up, raw_qual, raw_cf, sorted(data.keys()))
        return jsonify({"status": "test_success"}), 200

    # Skip events where no useful scan can be performed:
    #   Grab              — file is queued in the download client, not on disk yet
    #   EpisodeFileDelete / MovieFileDelete — the deleted file's path ends up in
    #                       `episodeFile`, which would record the OLD filename in
    #                       history; upgrades are covered by the subsequent Download event
    #   SeriesDelete / MovieDelete — entire series/movie removed; Plex scheduled scans
    #                       will eventually catch this, a targeted partial scan won't help
    _SKIP = {
        'Grab',
        'EpisodeFileDelete', 'EpisodeFileDeleted', 'SeriesDelete',   # Sonarr
        'MovieFileDelete',   'MovieFileDeleted',   'MovieDelete',     # Radarr
    }
    if event in _SKIP:
        log.info("[%s] Skipping event type '%s' (no scan needed)", instance_type.upper(), event)
        return jsonify({"status": "skipped", "reason": "event type not handled"}), 200

    label = instance_type.upper()
    log.info("[%s] Processing event type '%s'", label, event)
    raw_path = ""
    episode = ""
    quality = ""
    custom_formats_list: list = []

    if 'movie' in data:
        raw_path = data['movie'].get('folderPath', '')
        mf = data.get('movieFile', {})
        if mf:
            quality, _ = _extract_file_meta(mf)

    elif 'series' in data:
        series_path = data['series'].get('path', '')
        raw_path = series_path  # always scan the show root, never the season subfolder

        # Sonarr uses different keys depending on event type:
        #   episodeFile         — single episode download/delete
        #   episodeFiles        — batch/season-pack download
        #   renamedEpisodeFiles — rename events
        episode_files = []

        # Build the set of OLD filenames being replaced so we can discard stale episode info.
        # On upgrade events Sonarr populates `deletedFiles` with the file(s) that were replaced.
        # In some Sonarr versions the `episodeFile` field in the Download webhook can transiently
        # point to the old file before the rename completes; filtering against deletedFiles guards
        # against recording the replaced filename in sync history.
        _deleted_filenames: set = set()
        if data.get('isUpgrade'):
            for df in data.get('deletedFiles', []):
                dfn = df.get('relativePath', '').replace('\\', '/').split('/')[-1]
                if dfn:
                    _deleted_filenames.add(dfn)

        ef = data.get('episodeFile', {})
        if ef:
            rp = ef.get('relativePath', '')
            if rp:
                fn = rp.replace('\\', '/').split('/')[-1]
                if fn not in _deleted_filenames:
                    episode_files = [fn]
                    quality, _ = _extract_file_meta(ef)
                else:
                    log.info("[%s] episodeFile '%s' matches a deletedFile — discarding stale episode info",
                             label, fn)

        if not episode_files:
            efs = data.get('episodeFiles', [])
            if efs:
                episode_files = [
                    f.get('relativePath', '').replace('\\', '/').split('/')[-1]
                    for f in efs if f.get('relativePath')
                ]
                # Quality from first file (custom formats come from top-level payload)
                if efs:
                    quality, _ = _extract_file_meta(efs[0])

        if not episode_files:
            refs = data.get('renamedEpisodeFiles', [])
            if refs:
                episode_files = [
                    f.get('relativePath', '').replace('\\', '/').split('/')[-1]
                    for f in refs if f.get('relativePath')
                ]
                if refs:
                    quality, _ = _extract_file_meta(refs[0])

        if len(episode_files) == 1:
            episode = episode_files[0]
        elif len(episode_files) > 1:
            episode = json.dumps(episode_files)

    # Log raw webhook fields for operator visibility.
    _raw_file = data.get('episodeFile') or data.get('movieFile') or {}
    _raw_qual = _raw_file.get('quality', '<MISSING>')
    _raw_cf   = data.get('customFormats', '<MISSING>')
    log.info("[%s] Raw webhook fields — episodeFile/movieFile.quality=%r  top-level customFormats=%r",
             label, _raw_qual, _raw_cf)

    # Resolve the file ID so we can query the arr API for accurate custom formats.
    # Webhooks sometimes omit customFormats or only include a subset; the
    # /episodefile/{id} and /moviefile/{id} endpoints always return the full
    # evaluated list for that file.
    _file_id = 0
    if data.get('movieFile'):
        _file_id = data['movieFile'].get('id', 0)
    elif data.get('episodeFile'):
        _file_id = data['episodeFile'].get('id', 0)
    elif data.get('episodeFiles'):
        _file_id = data['episodeFiles'][0].get('id', 0)
    elif data.get('renamedEpisodeFiles'):
        _file_id = data['renamedEpisodeFiles'][0].get('id', 0)

    api_cf = _fetch_custom_formats_for_file(instance_type, _file_id)
    if api_cf:
        custom_formats_list = api_cf
    else:
        # Fall back to whatever the webhook included (may be empty or partial)
        for cf in data.get('customFormats', []):
            if isinstance(cf, dict):
                name = cf.get('name', '')
            elif isinstance(cf, str):
                name = cf
            else:
                name = ''
            if name and name not in custom_formats_list:
                custom_formats_list.append(name)

    if quality or custom_formats_list:
        log.info("[%s] Captured quality=%r custom_formats=%r", label, quality, custom_formats_list)

    # Quality profile — not present in the webhook payload; requires an API round-trip.
    item_id = 0
    if 'movie' in data:
        item_id = data['movie'].get('id', 0)
    elif 'series' in data:
        item_id = data['series'].get('id', 0)
    quality_profile = _get_quality_profile_name(instance_type, item_id)

    custom_formats = json.dumps(custom_formats_list) if custom_formats_list else ""
    result, status = enqueue_sync(raw_path, label, episode=episode,
                                  quality=quality, custom_formats=custom_formats,
                                  quality_profile=quality_profile)
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Demo mode — fake data for screenshots / public demos
# ---------------------------------------------------------------------------

_DEMO_INVITES = [
    {
        "id": 1, "token": "demoABC123", "label": "Friends & Family",
        "section_ids": '["1","2"]', "section_names": ["TV Shows", "Movies"],
        "allow_sync": 0, "allow_channels": 1, "home_user": 0,
        "duration_days": 0, "max_uses": 10, "uses": 3,
        "created_at": 1736000000, "link_expires_at": None, "status": "active",
        "link_expired": False, "max_reached": False, "is_active": True,
        "grants": [
            {"id": 1, "invite_id": 1, "plex_username": "alice", "accepted_at": 1736100000, "access_expires_at": None, "revoked": 0},
            {"id": 2, "invite_id": 1, "plex_username": "bob",   "accepted_at": 1736200000, "access_expires_at": None, "revoked": 0},
            {"id": 3, "invite_id": 1, "plex_username": "charlie","accepted_at": 1736300000,"access_expires_at": None, "revoked": 0},
        ],
    },
    {
        "id": 2, "token": "demoXYZ789", "label": "30-day trial",
        "section_ids": '["2"]', "section_names": ["Movies"],
        "allow_sync": 0, "allow_channels": 0, "home_user": 0,
        "duration_days": 30, "max_uses": 1, "uses": 1,
        "created_at": 1735000000, "link_expires_at": 1738000000, "status": "active",
        "link_expired": True, "max_reached": True, "is_active": False,
        "grants": [
            {"id": 4, "invite_id": 2, "plex_username": "diana", "accepted_at": 1735100000, "access_expires_at": 1737700000, "revoked": 0},
        ],
    },
]

_DEMO_SESSIONS = [
    {
        "user": "john", "title": "Ozymandias", "show": "Breaking Bad",
        "episode": "S05 · E14", "type": "episode", "player": "Plex for Apple TV",
        "state": "playing", "progress_pct": 42,
        "duration_str": "47:12", "position_str": "19:50",
        "quality": "1080p", "stream_type": "Direct Play", "transcode": False,
    },
    {
        "user": "sarah", "title": "Dune: Part Two", "show": None,
        "episode": None, "type": "movie", "player": "Plex Web",
        "state": "playing", "progress_pct": 68,
        "duration_str": "2:46:00", "position_str": "1:52:53",
        "quality": "4K", "stream_type": "Direct Stream", "transcode": False,
    },
    {
        "user": "mike", "title": "Napkins", "show": "The Bear",
        "episode": "S03 · E01", "type": "episode", "player": "Plex for Android",
        "state": "paused", "progress_pct": 15,
        "duration_str": "39:04", "position_str": "5:51",
        "quality": "720p", "stream_type": "Transcode", "transcode": True,
    },
]


def _demo_history() -> list:
    """Return fake sync history entries for demo/screenshot mode."""
    from datetime import timedelta
    now = now_local()
    raw = [
        {
            "ts": (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 3.2,
            "path": "/media/tv/Breaking Bad (2008)/Season 5/",
            "episode": "Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.mkv",
            "quality": "Bluray-1080p", "custom_formats": '["HDR", "DV"]',
            "quality_profile": "HD-1080p Remux",
        },
        {
            "ts": (now - timedelta(minutes=18)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 4.1,
            "path": "/media/tv/The Bear (2022)/Season 3/",
            "episode": '["The.Bear.S03E01.Napkins.1080p.WEB-DL.mkv","The.Bear.S03E02.Bolognese.1080p.WEB-DL.mkv","The.Bear.S03E03.Doors.And.Windows.1080p.WEB-DL.mkv"]',
            "quality": "WEB-DL-1080p", "custom_formats": '["HLG"]',
            "quality_profile": "WEB-1080p",
        },
        {
            "ts": (now - timedelta(hours=1, minutes=5)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "RADARR", "status": "ok", "error": "", "duration_s": 5.7,
            "path": "/media/movies/Dune Part Two (2024)/",
            "episode": "", "quality": "Bluray-2160p",
            "custom_formats": '["HDR10+", "DV", "Remux"]',
            "quality_profile": "UHD Remux",
        },
        {
            "ts": (now - timedelta(hours=2, minutes=32)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 2.8,
            "path": "/media/tv/Severance (2022)/Season 2/",
            "episode": "Severance.S02E04.Woes.Hollow.1080p.ATVP.WEB-DL.mkv",
            "quality": "WEB-DL-1080p", "custom_formats": '["ATMOS"]',
            "quality_profile": "WEB-1080p",
        },
        {
            "ts": (now - timedelta(hours=4, minutes=17)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "RADARR", "status": "error",
            "error": "ReadTimeout: Plex did not respond within 60s",
            "duration_s": 60.0,
            "path": "/media/movies/Avatar The Way of Water (2022)/",
            "episode": "", "quality": "Bluray-2160p",
            "custom_formats": '["HDR10", "DV"]', "quality_profile": "UHD Remux",
        },
        {
            "ts": (now - timedelta(hours=6, minutes=44)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 3.5,
            "path": "/media/tv/Shogun (2024)/Season 1/",
            "episode": "Shogun.S01E05.Crimson.Sky.1080p.DSNP.WEB-DL.mkv",
            "quality": "WEB-DL-1080p", "custom_formats": '[]',
            "quality_profile": "WEB-1080p",
        },
        {
            "ts": (now - timedelta(hours=9, minutes=11)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "RADARR", "status": "ok", "error": "", "duration_s": 4.3,
            "path": "/media/movies/Oppenheimer (2023)/",
            "episode": "", "quality": "Bluray-1080p",
            "custom_formats": '["ATMOS", "TrueHD"]', "quality_profile": "HD-1080p Remux",
        },
        {
            "ts": (now - timedelta(hours=11, minutes=58)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "MANUAL", "status": "ok", "error": "", "duration_s": 2.1,
            "path": "/media/tv/The Last of Us (2023)/Season 1/",
            "episode": "The.Last.of.Us.S01E01.When.Youre.Lost.in.the.Darkness.1080p.WEB-DL.mkv",
            "quality": "WEB-DL-1080p", "custom_formats": '[]', "quality_profile": "",
        },
    ]
    for item in raw:
        display, ep_list = _parse_episode_field(item.get('episode', ''))
        item['episode_display'] = display
        item['episode_list'] = ep_list
        cf_raw = item.get('custom_formats', '') or ''
        try:
            item['custom_format_list'] = json.loads(cf_raw) if cf_raw else []
        except (json.JSONDecodeError, ValueError):
            item['custom_format_list'] = []
    return raw


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/webhook/sonarr', methods=['POST'])
@csrf.exempt
def webhook_sonarr():
    return process_webhook(request.get_json(silent=True) or {}, "sonarr")


@app.route('/webhook/radarr', methods=['POST'])
@csrf.exempt
def webhook_radarr():
    return process_webhook(request.get_json(silent=True) or {}, "radarr")


@app.route('/login/demo')
def login_demo():
    if not DEMO_MODE:
        return redirect(url_for('login'))
    session.permanent = False
    session['authenticated'] = True
    session['demo'] = True
    return redirect(url_for('manual_webhook'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == MANUAL_USER and password == MANUAL_PASS:
            session.permanent = False
            session['authenticated'] = True
            return redirect(url_for('manual_webhook'))
        error = "Invalid username or password."
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/', methods=['GET', 'POST'])
@requires_auth
def manual_webhook():
    message = ""
    msg_class = "info"

    if request.method == 'POST':
        raw_path = request.form.get('path', '').strip()
        if raw_path:
            result, _ = enqueue_sync(raw_path, "MANUAL")
            status = result.get("status")
            if status == "queued":
                message = f"✓ Sync queued for: {result.get('path', raw_path)}"
                msg_class = "success"
            elif status == "deduplicated":
                message = f"⚠ Already in queue: {raw_path}"
                msg_class = "warn"
            else:
                message = f"✗ {result.get('reason', 'Unknown error')} for: {raw_path}"
                msg_class = "error"
        else:
            message = "✗ No path provided."
            msg_class = "error"

    # Filters
    search_q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    quality_filter = request.args.get('quality', '').strip()
    profile_filter = request.args.get('profile', '').strip()
    if status_filter not in ('ok', 'error', ''):
        status_filter = ''

    # Pagination
    page = max(1, int(request.args.get('page', 1)))
    per_page = 25
    offset = (page - 1) * per_page

    if session.get('demo'):
        recent = _demo_history()
        total_count = len(recent)
        total_pages = 1
    else:
        recent = history.get_recent(limit=per_page, offset=offset, search=search_q,
                                    status_filter=status_filter, quality_filter=quality_filter,
                                    profile_filter=profile_filter)
        total_count = history.count(search=search_q, status_filter=status_filter,
                                    quality_filter=quality_filter, profile_filter=profile_filter)
        total_pages = (total_count + per_page - 1) // per_page

    for item in recent:
        display, ep_list = _parse_episode_field(item.get('episode', ''))
        item['episode_display'] = display
        item['episode_list'] = ep_list
        cf_raw = item.get('custom_formats', '') or ''
        try:
            item['custom_format_list'] = json.loads(cf_raw) if cf_raw else []
        except (json.JSONDecodeError, ValueError):
            item['custom_format_list'] = []

    def _qs(**kw):
        return urllib.parse.urlencode([(k, v) for k, v in kw.items() if v])

    # search_qs: preserves q + quality + profile (used by status pills)
    search_qs    = _qs(q=search_q, quality=quality_filter, profile=profile_filter)
    # filter_qs: all active filters (used by pagination)
    filter_qs    = _qs(q=search_q, status=status_filter, quality=quality_filter, profile=profile_filter)
    # no_quality_qs: all filters except quality (used by quality tag links + clear quality pill)
    no_quality_qs = _qs(q=search_q, status=status_filter, profile=profile_filter)
    # no_profile_qs: all filters except profile (used by profile tag links + clear profile pill)
    no_profile_qs = _qs(q=search_q, status=status_filter, quality=quality_filter)

    return render_template(
        'manual_ui.html',
        message=message,
        msg_class=msg_class,
        history=recent,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        retention_days=HISTORY_DAYS,
        search_q=search_q,
        status_filter=status_filter,
        quality_filter=quality_filter,
        profile_filter=profile_filter,
        search_qs=search_qs,
        filter_qs=filter_qs,
        no_quality_qs=no_quality_qs,
        no_profile_qs=no_profile_qs,
        plex_url=PLEX_URL,
        demo=session.get('demo', False),
    )


@app.route('/now-playing')
@requires_auth
def now_playing():
    demo = session.get('demo', False)
    if demo:
        sessions = _DEMO_SESSIONS
    else:
        sessions = []
        try:
            plex_instance = get_plex()
            if plex_instance:
                for s in plex_instance.sessions():
                    players = getattr(s, 'players', [])
                    player = players[0] if players else None
                    ts_list = getattr(s, 'transcodeSessions', [])
                    ts = ts_list[0] if ts_list else None
                    media_type = getattr(s, 'type', 'unknown')
                    season_ep = None
                    if media_type == 'episode':
                        si, ei = getattr(s, 'parentIndex', None), getattr(s, 'index', None)
                        if si is not None and ei is not None:
                            season_ep = f"S{int(si):02d} · E{int(ei):02d}"
                    view_offset = getattr(s, 'viewOffset', 0) or 0
                    duration = getattr(s, 'duration', 0) or 0
                    progress_pct = round(view_offset / duration * 100, 1) if duration else 0
                    if ts:
                        vd = getattr(ts, 'videoDecision', 'directplay')
                        stream_type = {'directplay': 'Direct Play', 'copy': 'Direct Stream',
                                       'transcode': 'Transcode'}.get(vd, 'Direct Play')
                        transcode = vd == 'transcode'
                    else:
                        stream_type, transcode = 'Direct Play', False
                    quality = ''
                    if getattr(s, 'media', None):
                        res = getattr(s.media[0], 'videoResolution', '') or ''
                        quality = (res + 'p') if res.isdigit() else res

                    def _ms(ms):
                        ms = ms or 0
                        h, rem = divmod(ms // 1000, 3600)
                        m, sec = divmod(rem, 60)
                        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

                    usernames = getattr(s, 'usernames', [])
                    sessions.append({
                        "user": usernames[0] if usernames else "Unknown",
                        "title": getattr(s, 'title', ''),
                        "show": getattr(s, 'grandparentTitle', None) if media_type == 'episode' else None,
                        "episode": season_ep, "type": media_type,
                        "player": getattr(player, 'title', '') if player else '',
                        "state": getattr(player, 'state', 'playing') if player else 'playing',
                        "progress_pct": progress_pct,
                        "duration_str": _ms(duration) if duration else '--',
                        "position_str": _ms(view_offset),
                        "quality": quality, "stream_type": stream_type, "transcode": transcode,
                    })
        except Exception as exc:
            log.warning("Failed to fetch Plex sessions for /now-playing: %s", exc)
    return render_template('now_playing.html', sessions=sessions, demo=demo)


@app.route('/health', methods=['GET'])
def health():
    plex_ok = get_plex() is not None
    return jsonify({
        "status": "ok" if plex_ok else "degraded",
        "plex_connected": plex_ok,
        "rclone_enabled": USE_RCLONE,
        "queue_depth": sync_queue.qsize(),
        "worker_alive": _worker_alive.is_set(),
        "recent_history": history.as_list()[:10],
    }), 200 if plex_ok else 207


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Aggregate stats endpoint — designed for Homepage customapi widget.

    Example Homepage widget config:
        widget:
          type: customapi
          url: http://<host>:5000/api/stats
          refreshInterval: 30000
          mappings:
            - field: syncs.total    label: Total    format: number
            - field: syncs.ok       label: Success  format: number
            - field: syncs.failed   label: Failed   format: number
            - field: queue.depth    label: Queued   format: number
    """
    stats = history.get_stats()
    last  = stats["last_sync"]
    with _in_flight_lock:
        in_flight_count = len(_in_flight)
    return jsonify({
        "syncs": {
            "total":          stats["total"],
            "ok":             stats["successful"],
            "failed":         stats["failed"],
            "sonarr":         stats["sonarr"],
            "radarr":         stats["radarr"],
            "manual":         stats["manual"],
            "avg_duration_s": stats["avg_duration_s"],
        },
        "queue": {
            "depth":     sync_queue.qsize(),
            "in_flight": in_flight_count,
        },
        "worker": {
            "alive": _worker_alive.is_set(),
        },
        "last_sync": {
            "at":     last["ts"]     if last else None,
            "status": last["status"] if last else None,
            "label":  last["label"]  if last else None,
            "path":   last["path"]   if last else None,
        },
        "retention_days": HISTORY_DAYS,
    })



# ---------------------------------------------------------------------------
# Sessions / Now Playing API
# ---------------------------------------------------------------------------

@app.route('/api/sessions', methods=['GET'])
@requires_auth
def api_sessions():
    """Return current Plex sessions for the Now Playing dashboard."""
    if session.get('demo'):
        demo_result = []
        for s in _DEMO_SESSIONS:
            demo_result.append({
                'session_id': f"demo-{s['user']}",
                'rating_key': '', 'plex_item_key': '', 'type': s['type'],
                'title': s['title'], 'year': None,
                'show_title': s.get('show'), 'season_episode': s.get('episode'),
                'thumb_key': None,
                'progress_pct': s['progress_pct'],
                'view_offset_ms': 0, 'duration_ms': 0,
                'state': s['state'], 'stream_type': s['stream_type'],
                'video_resolution': s['quality'].replace('p','') if s['quality'].endswith('p') else s['quality'],
                'bitrate_kbps': None,
                'user': s['user'],
                'player_device': s['player'], 'player_platform': '', 'player_product': '',
                'player_address': '', 'player_remote_address': '',
            })
        return jsonify({'sessions': demo_result, 'machine_id': ''})
    plex_instance = get_plex()
    if not plex_instance:
        return jsonify({"error": "Plex not connected", "sessions": []}), 503
    try:
        sessions_data = plex_instance.sessions()
        result = []
        machine_id = getattr(plex_instance, 'machineIdentifier', '')
        for s in sessions_data:
            players = getattr(s, 'players', [])
            player = players[0] if players else None
            transcode_sessions = getattr(s, 'transcodeSessions', [])
            ts = transcode_sessions[0] if transcode_sessions else None

            media_type = getattr(s, 'type', 'unknown')

            season_ep = None
            if media_type == 'episode':
                season = getattr(s, 'parentIndex', None)
                episode = getattr(s, 'index', None)
                if season and episode:
                    season_ep = f"S{int(season):02d}E{int(episode):02d}"

            # Prefer show poster for episodes
            if media_type == 'episode':
                thumb_key = getattr(s, 'grandparentThumb', None) or getattr(s, 'thumb', None)
            else:
                thumb_key = getattr(s, 'thumb', None)

            view_offset = getattr(s, 'viewOffset', 0) or 0
            duration = getattr(s, 'duration', 0) or 0
            progress_pct = round((view_offset / duration * 100), 1) if duration > 0 else 0

            if ts:
                vd = getattr(ts, 'videoDecision', 'directplay')
                stream_type = {
                    'directplay': 'Direct Play',
                    'copy': 'Direct Stream',
                    'transcode': 'Transcode',
                }.get(vd, vd.title() if vd else 'Direct Play')
            else:
                stream_type = 'Direct Play'

            media_parts = getattr(s, 'media', [])
            video_resolution = None
            bitrate = None
            if media_parts:
                video_resolution = getattr(media_parts[0], 'videoResolution', None)
                bitrate = getattr(media_parts[0], 'bitrate', None)

            usernames = getattr(s, 'usernames', [])
            username = usernames[0] if usernames else getattr(s, 'username', 'Unknown')

            player_address = getattr(player, 'address', '') or '' if player else ''
            player_remote  = getattr(player, 'remotePublicAddress', '') or '' if player else ''

            result.append({
                'session_id':       str(getattr(s, 'sessionKey', id(s))),
                'rating_key':       str(getattr(s, 'ratingKey', '')),
                'plex_item_key':    getattr(s, 'key', ''),
                'type':             media_type,
                'title':            getattr(s, 'title', ''),
                'year':             getattr(s, 'year', None),
                'show_title':       getattr(s, 'grandparentTitle', None),
                'season_episode':   season_ep,
                'thumb_key':        thumb_key,
                'progress_pct':     progress_pct,
                'view_offset_ms':   view_offset,
                'duration_ms':      duration,
                'state':            getattr(player, 'state', 'unknown') if player else 'unknown',
                'stream_type':      stream_type,
                'video_resolution': video_resolution,
                'bitrate_kbps':     bitrate,
                'user':             username,
                'player_device':    getattr(player, 'title', getattr(player, 'device', '')) if player else '',
                'player_platform':  getattr(player, 'platform', '') if player else '',
                'player_product':   getattr(player, 'product', '') if player else '',
                'player_address':   player_address,
                'player_remote_address': player_remote,
            })
        return jsonify({'sessions': result, 'machine_id': machine_id})
    except Exception as exc:
        log.error("Error fetching sessions: %s", exc)
        return jsonify({'error': 'Failed to retrieve sessions.', 'sessions': []}), 500


@app.route('/api/thumb')
@requires_auth
def api_thumb():
    """Proxy Plex thumbnails so the token never appears in the browser."""
    key = request.args.get('key', '').strip()
    if not key:
        return '', 404
    if not (key.startswith('/library/') or key.startswith('/photo/')):
        return '', 403
    try:
        width  = max(1, min(int(request.args.get('w', '80')),  2000))
        height = max(1, min(int(request.args.get('h', '120')), 2000))
    except (ValueError, TypeError):
        return '', 400
    try:
        url = (
            f"{PLEX_URL}/photo/:/transcode"
            f"?url={urllib.parse.quote(key)}"
            f"&width={width}&height={height}&minSize=1&upscale=1"
            f"&X-Plex-Token={PLEX_TOKEN}"
        )
        resp = requests.get(url, timeout=10)
        if resp.ok:
            ct = resp.headers.get('Content-Type', 'image/jpeg')
            # Only forward safe image content types; never forward text/html or
            # other types that the browser would render, which could allow XSS.
            if not ct.startswith('image/'):
                ct = 'image/jpeg'
            return resp.content, 200, {
                'Content-Type': ct,
                'Cache-Control': 'public, max-age=3600',
                'X-Content-Type-Options': 'nosniff',
            }
        return '', 404
    except Exception as exc:
        log.warning("Thumb proxy error: %s", exc)
        return '', 404


@app.route('/api/geoip')
@requires_auth
def api_geoip():
    """Proxy IP geolocation via ip-api.com with server-side caching."""
    ip = request.args.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'no ip'}), 400
    if _is_private_ip(ip):
        return jsonify({'private': True, 'ip': ip})
    with _geo_cache_lock:
        cached = _geo_cache.get(ip)
        if cached and time.time() - cached.get('_ts', 0) < 86400:
            out = dict(cached)
            out.pop('_ts', None)
            return jsonify(out)
    try:
        r = requests.get(
            f'http://ip-api.com/json/{ip}'
            '?fields=status,city,country,countryCode,regionName,lat,lon,isp,org,query',
            timeout=5,
        )
        data = r.json()
        data['_ts'] = time.time()
        with _geo_cache_lock:
            _geo_cache[ip] = data
        out = dict(data)
        out.pop('_ts', None)
        return jsonify(out)
    except Exception as exc:
        log.warning("GeoIP lookup failed for ip=%r: %s", request.args.get('ip'), exc)
        return jsonify({'error': 'Geolocation lookup failed.'}), 500


# ---------------------------------------------------------------------------
# Invite management routes (admin)
# ---------------------------------------------------------------------------

def _invite_validity(invite: dict) -> Optional[str]:
    """Return an error string if the invite cannot be used, else None."""
    if not invite:
        return 'This invite link is not valid.'
    if invite.get('status') == 'revoked':
        return 'This invite link has been revoked.'
    le = invite.get('link_expires_at')
    if le and int(time.time()) > le:
        return 'This invite link has expired.'
    max_uses = invite.get('max_uses', 0)
    if max_uses > 0 and invite.get('uses', 0) >= max_uses:
        return 'This invite has reached its maximum number of uses.'
    return None


def _plex_section_names(plex_instance, section_ids: list) -> list[str]:
    try:
        all_sections = {str(s.key): s.title for s in plex_instance.library.sections()}
        if section_ids:
            return [all_sections.get(str(sid), str(sid)) for sid in section_ids]
        return list(all_sections.values())
    except Exception:
        return []


@app.route('/invites', methods=['GET'])
@requires_auth
def invites_page():
    if session.get('demo'):
        return render_template('invites.html', invites=_DEMO_INVITES,
                               libraries=[{"id": "1", "title": "TV Shows", "type": "show"},
                                          {"id": "2", "title": "Movies", "type": "movie"}],
                               plex_error=None, demo=True)

    plex_instance = get_plex()
    libraries  = []
    plex_error = None
    if plex_instance:
        try:
            libraries = [
                {'id': str(s.key), 'title': s.title, 'type': s.type}
                for s in plex_instance.library.sections()
            ]
        except Exception as exc:
            plex_error = str(exc)

    invites    = invite_db.list_all()
    all_grants = invite_db.get_grants()
    grants_by_invite: dict = {}
    for g in all_grants:
        grants_by_invite.setdefault(g['invite_id'], []).append(g)

    now = int(time.time())
    lib_map = {lib['id']: lib['title'] for lib in libraries}
    for inv in invites:
        inv['grants'] = grants_by_invite.get(inv['id'], [])
        try:
            inv['section_ids'] = json.loads(inv.get('section_ids', '[]') or '[]')
        except Exception:
            inv['section_ids'] = []
        inv['section_names'] = [lib_map.get(str(sid), str(sid)) for sid in inv['section_ids']]
        le = inv.get('link_expires_at')
        inv['link_expired']  = bool(le and now > le)
        inv['max_reached']   = inv.get('max_uses', 0) > 0 and inv.get('uses', 0) >= inv.get('max_uses', 0)
        inv['is_active']     = inv.get('status') == 'active' and not inv['link_expired'] and not inv['max_reached']

    return render_template('invites.html', invites=invites, libraries=libraries,
                           plex_error=plex_error, demo=session.get('demo', False))


@app.route('/invites/create', methods=['POST'])
@requires_auth
def create_invite():
    label             = request.form.get('label', '').strip()
    section_ids       = request.form.getlist('sections')
    allow_sync        = bool(request.form.get('allow_sync'))
    allow_channels    = bool(request.form.get('allow_channels'))
    home_user         = bool(request.form.get('home_user'))
    duration_days     = int(request.form.get('duration_days', '0') or '0')
    max_uses          = int(request.form.get('max_uses', '1') or '1')
    link_expires_days = int(request.form.get('link_expires_days', '7') or '7')
    invite_db.create(label, section_ids, allow_sync, allow_channels, home_user,
                     duration_days, max_uses, link_expires_days)
    return redirect(url_for('invites_page'))


@app.route('/invites/revoke/<token>', methods=['POST'])
@requires_auth
def revoke_invite(token):
    invite_db.revoke_invite(token)
    return redirect(url_for('invites_page'))


@app.route('/invites/revoke_grant/<int:grant_id>', methods=['POST'])
@requires_auth
def revoke_grant(grant_id):
    all_grants = invite_db.get_grants()
    matched = next((g for g in all_grants if g['id'] == grant_id), None)
    if matched:
        try:
            plex_instance = get_plex()
            if plex_instance:
                account = plex_instance.myPlexAccount()
                account.removeFriend(matched['plex_username'])
                log.info("[INVITE] Manually revoked Plex access for '%s'", matched['plex_username'])
        except Exception as exc:
            log.warning("[INVITE] Error revoking Plex friend '%s': %s", matched.get('plex_username'), exc)
        invite_db.revoke_grant(grant_id)
    return redirect(url_for('invites_page'))


# ---------------------------------------------------------------------------
# Public invite / onboarding routes
# ---------------------------------------------------------------------------

@app.route('/invite/<token>', methods=['GET'])
def invite_onboard(token):
    invite = invite_db.get(token)
    err = _invite_validity(invite)
    if err:
        return render_template('invite_onboard.html', error=err, step='error',
                               invite=invite, section_names=[], token=token,
                               plex_server_name='', plex_username='',
                               plex_url=PLEX_URL,
                               onboard_wiki_url=ONBOARD_WIKI_URL,
                               onboard_request_url=ONBOARD_REQUEST_URL)

    plex_instance   = get_plex()
    plex_server_name = getattr(plex_instance, 'friendlyName', '') if plex_instance else ''
    section_ids     = json.loads(invite.get('section_ids', '[]') or '[]')
    section_names   = _plex_section_names(plex_instance, section_ids) if plex_instance else []

    step = request.args.get('step', 'welcome')
    return render_template(
        'invite_onboard.html',
        invite=invite, section_names=section_names, step=step,
        token=token, error=None,
        plex_server_name=plex_server_name, plex_username='',
        plex_url=PLEX_URL,
        onboard_wiki_url=ONBOARD_WIKI_URL,
        onboard_request_url=ONBOARD_REQUEST_URL,
    )


@app.route('/invite/<token>/accept', methods=['POST'])
@csrf.exempt
def accept_invite(token):
    invite = invite_db.get(token)
    err = _invite_validity(invite)
    if err:
        return render_template('invite_onboard.html', error=err, step='error',
                               invite=invite, section_names=[], token=token,
                               plex_server_name='', plex_username='',
                               plex_url=PLEX_URL,
                               onboard_wiki_url=ONBOARD_WIKI_URL,
                               onboard_request_url=ONBOARD_REQUEST_URL)

    plex_username    = request.form.get('plex_username', '').strip()
    plex_instance    = get_plex()
    plex_server_name = getattr(plex_instance, 'friendlyName', '') if plex_instance else ''

    if not plex_username:
        return render_template('invite_onboard.html', invite=invite, step='accept',
                               error='Please enter your Plex username or email.',
                               token=token, section_names=[], plex_server_name=plex_server_name,
                               plex_username='',
                               plex_url=PLEX_URL,
                               onboard_wiki_url=ONBOARD_WIKI_URL,
                               onboard_request_url=ONBOARD_REQUEST_URL)

    if not plex_instance:
        return render_template('invite_onboard.html', invite=invite, step='accept',
                               error='Server error: cannot connect to Plex. Please try again later.',
                               token=token, section_names=[], plex_server_name=plex_server_name,
                               plex_username=plex_username,
                               plex_url=PLEX_URL,
                               onboard_wiki_url=ONBOARD_WIKI_URL,
                               onboard_request_url=ONBOARD_REQUEST_URL)
    try:
        account       = plex_instance.myPlexAccount()
        section_ids   = json.loads(invite.get('section_ids', '[]') or '[]')
        all_lib       = plex_instance.library.sections()
        sections      = [s for s in all_lib if str(s.key) in [str(sid) for sid in section_ids]] \
                        if section_ids else None
        section_names = _plex_section_names(plex_instance, section_ids)

        account.inviteFriend(
            user=plex_username,
            server=plex_instance,
            sections=sections,
            allowSync=bool(invite.get('allow_sync')),
            allowCameraUpload=False,
            allowChannels=bool(invite.get('allow_channels')),
        )
        invite_db.record_acceptance(invite['id'], plex_username, invite.get('duration_days', 0))
        log.info("[INVITE] '%s' accepted invite '%s'", plex_username, invite.get('label', token))

        return render_template(
            'invite_onboard.html',
            step='done', invite=invite, section_names=section_names,
            token=token, error=None,
            plex_server_name=plex_server_name, plex_username=plex_username,
            plex_url=PLEX_URL,
            onboard_wiki_url=ONBOARD_WIKI_URL,
            onboard_request_url=ONBOARD_REQUEST_URL,
        )
    except Exception as exc:
        err = str(exc)
        log.error("[INVITE] Acceptance error for '%s': %s", plex_username, exc)
        if 'already' in err.lower() or 'exist' in err.lower():
            user_err = f"'{plex_username}' may already have access, or has already been invited."
        elif 'not found' in err.lower() or 'invalid' in err.lower() or '404' in err:
            user_err = f"Plex account '{plex_username}' not found. Please check your username or email."
        else:
            user_err = "Could not process the invite. Please try again or contact the server owner."
        return render_template(
            'invite_onboard.html',
            invite=invite, step='accept', error=user_err,
            token=token, section_names=[], plex_server_name=plex_server_name,
            plex_username=plex_username,
            plex_url=PLEX_URL,
            onboard_wiki_url=ONBOARD_WIKI_URL,
            onboard_request_url=ONBOARD_REQUEST_URL,
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _handle_shutdown(signum, frame):
    log.info("Shutdown signal received, stopping worker...")
    _worker_alive.clear()
    sys.exit(0)


_REDACT_RE = re.compile(r'token|pass|secret|key|credential|auth', re.IGNORECASE)


def _log_env():
    """Log all environment variables, redacting sensitive ones."""
    log.info("=== Environment ===")
    for name, value in sorted(os.environ.items()):
        display = "***REDACTED***" if _REDACT_RE.search(name) else value
        log.info("  %-40s = %s", name, display)
    log.info("===================")


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Suppress werkzeug's "Running on ..." banner lines
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    log.info("=== Media Servarr Sync starting ===")
    log.info("Rclone integration: %s", "ENABLED" if USE_RCLONE else "DISABLED (set USE_RCLONE=true to enable)")
    log.info("Sonarr API: %s", SONARR_URL if SONARR_URL and SONARR_API_KEY else "NOT CONFIGURED (set SONARR_URL + SONARR_API_KEY for quality-profile badges)")
    log.info("Radarr API: %s", RADARR_URL if RADARR_URL and RADARR_API_KEY else "NOT CONFIGURED (set RADARR_URL + RADARR_API_KEY for quality-profile badges)")
    _log_env()

    worker_thread = threading.Thread(target=sync_worker, daemon=True, name="sync-worker")
    worker_thread.start()

    cf_thread = threading.Thread(target=custom_format_refresh_scheduler, daemon=True, name="cf-scheduler")
    cf_thread.start()

    invite_thread = threading.Thread(target=invite_expiry_scheduler, daemon=True, name="invite-expiry")
    invite_thread.start()

    log.info("Webhook receiver active on port %d", PORT)
    serve(app, host='0.0.0.0', port=PORT)
