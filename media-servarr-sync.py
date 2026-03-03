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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import plexapi
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
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
            # Migrate existing databases that lack the episode column
            existing = {row[1] for row in conn.execute("PRAGMA table_info(sync_history)")}
            if 'episode' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN episode TEXT")
            conn.commit()

    def add(self, entry: dict):
        """Add a sync entry and prune old records."""
        with self._lock:
            cutoff = time.time() - (self._retention_days * 86400)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO sync_history (ts, label, path, status, error, duration_s, created_at, episode)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry['ts'],
                    entry['label'],
                    entry['path'],
                    entry['status'],
                    entry.get('error', ''),
                    entry['duration_s'],
                    time.time(),
                    entry.get('episode', ''),
                ))
                # Prune old entries
                conn.execute("DELETE FROM sync_history WHERE created_at < ?", (cutoff,))
                conn.commit()

    def get_recent(self, limit: int = 50, offset: int = 0,
                   search: str = "", status_filter: str = "") -> list:
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
                where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                params.extend([limit, offset])
                cursor = conn.execute(f"""
                    SELECT ts, label, path, status, error, duration_s, episode
                    FROM sync_history
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                """, params)
                return [dict(row) for row in cursor.fetchall()]

    def count(self, search: str = "", status_filter: str = "") -> int:
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
                where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                cursor = conn.execute(f"SELECT COUNT(*) FROM sync_history {where}", params)
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


history = SyncHistory(db_path="/data/sync_history.db", retention_days=HISTORY_DAYS)
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
            })
            sync_queue.task_done()

    log.info("Sync worker stopped")


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


def enqueue_sync(raw_path: str, label: str, episode: str = ""):
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
    return {"status": "queued", "path": mapped_folder}, 200


def process_webhook(data: dict, instance_type: str):
    if not data:
        return jsonify({"error": "No payload"}), 400

    event = data.get('eventType', '')

    if event == "Test":
        return jsonify({"status": "test_success"}), 200

    # Skip events where no useful scan can be performed:
    #   Grab              — file is queued in the download client, not on disk yet
    #   EpisodeFileDeleted / MovieFileDeleted — the deleted file's path ends up in
    #                       `episodeFile`, which would record the OLD filename in
    #                       history; upgrades are covered by the subsequent Download event
    #   SeriesDelete / MovieDelete — entire series/movie removed; Plex scheduled scans
    #                       will eventually catch this, a targeted partial scan won't help
    _SKIP = {
        'Grab',
        'EpisodeFileDeleted', 'SeriesDelete',   # Sonarr
        'MovieFileDeleted',   'MovieDelete',     # Radarr
    }
    if event in _SKIP:
        log.debug("[%s] Skipping event type '%s'", instance_type.upper(), event)
        return jsonify({"status": "skipped", "reason": f"event type '{event}' not handled"}), 200

    label = instance_type.upper()
    raw_path = ""
    episode = ""
    if 'movie' in data:
        raw_path = data['movie'].get('folderPath', '')
    elif 'series' in data:
        series_path = data['series'].get('path', '')
        raw_path = series_path  # always scan the show root, never the season subfolder

        # Sonarr uses different keys depending on event type:
        #   episodeFile        — single episode download/delete
        #   episodeFiles       — batch/season-pack download
        #   renamedEpisodeFiles — rename events
        episode_files = []

        ef = data.get('episodeFile', {})
        if ef:
            rp = ef.get('relativePath', '')
            if rp:
                episode_files = [rp.replace('\\', '/').split('/')[-1]]

        if not episode_files:
            efs = data.get('episodeFiles', [])
            if efs:
                episode_files = [
                    f.get('relativePath', '').replace('\\', '/').split('/')[-1]
                    for f in efs if f.get('relativePath')
                ]

        if not episode_files:
            refs = data.get('renamedEpisodeFiles', [])
            if refs:
                episode_files = [
                    f.get('relativePath', '').replace('\\', '/').split('/')[-1]
                    for f in refs if f.get('relativePath')
                ]

        if len(episode_files) == 1:
            episode = episode_files[0]
        elif len(episode_files) > 1:
            episode = json.dumps(episode_files)

    result, status = enqueue_sync(raw_path, label, episode=episode)
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/webhook/sonarr', methods=['POST'])
def webhook_sonarr():
    return process_webhook(request.get_json(silent=True) or {}, "sonarr")


@app.route('/webhook/radarr', methods=['POST'])
def webhook_radarr():
    return process_webhook(request.get_json(silent=True) or {}, "radarr")


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == MANUAL_USER and password == MANUAL_PASS:
            session.permanent = False
            session['authenticated'] = True
            next_url = request.args.get('next', url_for('manual_webhook'))
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template_string(LOGIN_TEMPLATE, error=error)


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
    if status_filter not in ('ok', 'error', ''):
        status_filter = ''

    # Pagination
    page = max(1, int(request.args.get('page', 1)))
    per_page = 25
    offset = (page - 1) * per_page

    recent = history.get_recent(limit=per_page, offset=offset, search=search_q, status_filter=status_filter)
    total_count = history.count(search=search_q, status_filter=status_filter)
    total_pages = (total_count + per_page - 1) // per_page

    for item in recent:
        display, ep_list = _parse_episode_field(item.get('episode', ''))
        item['episode_display'] = display
        item['episode_list'] = ep_list

    search_qs = urllib.parse.urlencode([('q', search_q)])
    filter_qs  = urllib.parse.urlencode([('q', search_q), ('status', status_filter)])

    return render_template_string(
        MANUAL_UI_TEMPLATE,
        message=message,
        msg_class=msg_class,
        history=recent,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        retention_days=HISTORY_DAYS,
        search_q=search_q,
        status_filter=status_filter,
        search_qs=search_qs,
        filter_qs=filter_qs,
    )


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
# Manual UI
# ---------------------------------------------------------------------------

MANUAL_UI_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Media Servarr Sync</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'><rect width='24' height='24' rx='4' fill='%230d0d0f'/><path stroke='%23e5a00d' stroke-width='2' stroke-linecap='round' d='M20 12a8 8 0 1 1-1.6-4.8'/><polyline stroke='%23e5a00d' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' points='20,4 20,8 16,8'/></svg>">
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap');

  :root {
    --bg: #0d0d0f;
    --surface: #161618;
    --border: #2a2a2e;
    --accent: #e5a00d;
    --accent-dim: rgba(229,160,13,0.12);
    --text: #e8e8e8;
    --muted: #666;
    --success: #4ade80;
    --warn: #fb923c;
    --error: #f87171;
    --info: #60a5fa;
    --radius: 4px;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 48px 20px;
  }

  .shell {
    width: 100%;
    max-width: 680px;
  }

  header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 36px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 20px;
  }

  .logo-icon {
    flex-shrink: 0;
    filter: drop-shadow(0 0 6px rgba(229,160,13,0.5));
  }

  h1 {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--accent);
  }

  .subtitle {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-left: auto;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 16px;
  }

  .card-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }

  .input-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }

  input[type="text"] {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    padding: 10px 14px;
    outline: none;
    transition: border-color 0.15s;
  }

  input[type="text"]:focus {
    border-color: var(--accent);
  }

  input[type="text"]::placeholder { color: var(--muted); }

  button[type="submit"] {
    background: var(--accent);
    border: none;
    border-radius: var(--radius);
    color: #0d0d0f;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    padding: 10px 18px;
    cursor: pointer;
    text-transform: uppercase;
    transition: opacity 0.15s;
    white-space: nowrap;
  }

  button[type="submit"]:hover { opacity: 0.85; }

  .msg {
    margin-top: 14px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    padding: 10px 14px;
    border-radius: var(--radius);
    border-left: 3px solid;
  }

  .msg.success { border-color: var(--success); color: var(--success); background: rgba(74,222,128,0.07); }
  .msg.warn    { border-color: var(--warn);    color: var(--warn);    background: rgba(251,146,60,0.07); }
  .msg.error   { border-color: var(--error);   color: var(--error);   background: rgba(248,113,113,0.07); }
  .msg.info    { border-color: var(--info);    color: var(--info);    background: rgba(96,165,250,0.07); }

  .history-item {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 10px;
    align-items: start;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
  }

  .history-item:last-child { border-bottom: none; }

  .status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    margin-top: 3px;
    flex-shrink: 0;
  }
  .dot-ok    { background: var(--success); }
  .dot-error { background: var(--error); }

  .h-path    { color: var(--text); word-break: break-all; }
  .h-episode { color: var(--muted); font-size: 11px; margin-top: 2px; word-break: break-all; }
  .episode-count-badge { display: inline-block; margin-top: 3px; padding: 1px 7px; background: var(--accent); color: var(--bg); border-radius: 3px; font-size: 10px; font-weight: 700; letter-spacing: 0.02em; cursor: default; }

  .ep-wrap { position: relative; display: inline-block; }
  .ep-tooltip {
    display: none;
    position: absolute;
    left: 0; top: calc(100% + 4px);
    background: var(--surface);
    border: 1px solid var(--accent);
    border-radius: var(--radius);
    padding: 6px 10px;
    min-width: 220px; max-width: 460px;
    z-index: 50;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }
  .ep-wrap:hover .ep-tooltip { display: block; }
  .ep-tip-row {
    font-size: 10px; color: var(--text);
    padding: 3px 0; border-bottom: 1px solid var(--border);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ep-tip-row:last-child { border-bottom: none; }

  .filter-bar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
  .filter-search {
    flex: 1; min-width: 0;
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    color: var(--text); font-family: 'IBM Plex Mono', monospace; font-size: 12px;
    padding: 7px 12px; outline: none; transition: border-color 0.15s;
  }
  .filter-search:focus { border-color: var(--accent); }
  .filter-search::placeholder { color: var(--muted); }
  .status-pills { display: flex; gap: 4px; flex-shrink: 0; }
  .pill {
    padding: 5px 10px; font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase;
    border: 1px solid var(--border); border-radius: var(--radius);
    color: var(--muted); text-decoration: none; transition: all 0.15s; white-space: nowrap;
  }
  .pill:hover { border-color: var(--text); color: var(--text); }
  .pill.active          { border-color: var(--accent);  color: var(--accent);  background: var(--accent-dim); }
  .pill.pill-ok.active  { border-color: var(--success); color: var(--success); background: rgba(74,222,128,0.08); }
  .pill.pill-err.active { border-color: var(--error);   color: var(--error);   background: rgba(248,113,113,0.08); }
  .h-meta    { color: var(--muted); text-align: right; white-space: nowrap; }
  .h-label   { color: var(--accent); font-size: 10px; }
  .h-error   { color: var(--error); font-size: 10px; margin-top: 2px; }
  .h-ts     { display: block; }

  .empty    { color: var(--muted); font-family: 'IBM Plex Mono', monospace; font-size: 12px; text-align: center; padding: 16px 0; }

  .pagination {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
  }

  .page-link {
    color: var(--accent);
    text-decoration: none;
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    transition: background 0.15s, border-color 0.15s;
  }

  .page-link:hover:not(.disabled) {
    background: var(--accent-dim);
    border-color: var(--accent);
  }

  .page-link.disabled {
    color: var(--muted);
    cursor: not-allowed;
    opacity: 0.5;
  }

  .page-info {
    color: var(--muted);
  }

  a.logout {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    text-decoration: none;
    margin-left: auto;
    padding: 4px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    transition: color 0.15s, border-color 0.15s;
  }
  a.logout:hover { color: var(--error); border-color: var(--error); }
</style>
</head>
<body>
<div class="shell">
  <header>
    <svg class="logo-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" width="28" height="28">
      <rect width="24" height="24" rx="4" fill="#0d0d0f"/>
      <path stroke="#e5a00d" stroke-width="2" stroke-linecap="round" d="M20 12a8 8 0 1 1-1.6-4.8"/>
      <polyline stroke="#e5a00d" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="20,4 20,8 16,8"/>
    </svg>
    <h1>MEDIA SERVARR SYNC</h1>
    <span class="subtitle">Manual trigger</span>
    <a class="logout" href="/logout">Logout</a>
  </header>

  <div class="card">
    <div class="card-label">Trigger path scan - (Path needs to be the same as your root path in sonarr or radarr)</div>
    <form method="post">
      <div class="input-row">
        <input type="text" name="path" placeholder="/mnt/media/tv/ShowName" autocomplete="off" spellcheck="false">
        <button type="submit">QUEUE</button>
      </div>
      {% if message %}
      <div class="msg {{ msg_class }}">{{ message }}</div>
      {% endif %}
    </form>
  </div>

  <div class="card">
    <div class="card-label">
      Recent syncs
      <span style="color: var(--muted); font-size: 9px; margin-left: 8px;">
        ({{ total_count }} total, {{ retention_days }} day{{ 's' if retention_days != 1 else '' }} retention)
      </span>
    </div>
    <div class="filter-bar">
      <form method="get" style="display:contents">
        <input type="hidden" name="status" value="{{ status_filter }}">
        <input class="filter-search" type="text" name="q" id="search-input"
               value="{{ search_q }}" placeholder="Search by path…"
               autocomplete="off" spellcheck="false">
      </form>
      <div class="status-pills">
        <a href="?{{ search_qs | safe }}&status=&page=1"
           class="pill{{ ' active' if not status_filter else '' }}">All</a>
        <a href="?{{ search_qs | safe }}&status=ok&page=1"
           class="pill pill-ok{{ ' active' if status_filter == 'ok' else '' }}">OK</a>
        <a href="?{{ search_qs | safe }}&status=error&page=1"
           class="pill pill-err{{ ' active' if status_filter == 'error' else '' }}">Failed</a>
      </div>
    </div>
    {% if history %}
      {% for h in history %}
      <div class="history-item">
        <div class="status-dot {{ 'dot-ok' if h.status == 'ok' else 'dot-error' }}"></div>
        <div>
          <span class="h-path">{{ h.path }}</span>
          {% if h.episode_display %}
            {% if h.episode_list %}
            <div>
              <div class="ep-wrap">
                <span class="episode-count-badge">{{ h.episode_display }}</span>
                <div class="ep-tooltip">
                  {% for ep in h.episode_list %}<div class="ep-tip-row">{{ ep }}</div>{% endfor %}
                </div>
              </div>
            </div>
            {% else %}
              {% set ep_words = h.episode_display.split() %}
              {% if ep_words|length == 2 and ep_words[1] in ['episode', 'episodes'] %}
              <div><span class="episode-count-badge">{{ h.episode_display }}</span></div>
              {% else %}
              <div class="h-episode">{{ h.episode_display }}</div>
              {% endif %}
            {% endif %}
          {% endif %}
          <div class="h-label">{{ h.label }} &nbsp;·&nbsp; {{ h.duration_s }}s</div>
          {% if h.error %}<div class="h-error">{{ h.error }}</div>{% endif %}
        </div>
        <div class="h-meta">
          <span class="h-ts">{{ h.ts[11:19] }}</span>
          <span>{{ h.ts[:10] }}</span>
        </div>
      </div>
      {% endfor %}

      {% if total_pages > 1 %}
      <div class="pagination">
        {% if page > 1 %}
        <a href="?{{ filter_qs | safe }}&page={{ page - 1 }}" class="page-link">← Prev</a>
        {% else %}
        <span class="page-link disabled">← Prev</span>
        {% endif %}

        <span class="page-info">Page {{ page }} of {{ total_pages }}</span>

        {% if page < total_pages %}
        <a href="?{{ filter_qs | safe }}&page={{ page + 1 }}" class="page-link">Next →</a>
        {% else %}
        <span class="page-link disabled">Next →</span>
        {% endif %}
      </div>
      {% endif %}
    {% else %}
      <p class="empty">No syncs yet{% if search_q or status_filter %} matching filters{% endif %}.</p>
    {% endif %}
  </div>
</div>
<script>
(function(){
  var si = document.getElementById('search-input');
  if (!si) return;
  var t;
  si.addEventListener('input', function(){
    clearTimeout(t);
    t = setTimeout(function(){ si.form.submit(); }, 400);
  });
})();
</script>
</body>
</html>'''

LOGIN_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Media Servarr Sync · Login</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'><rect width='24' height='24' rx='4' fill='%230d0d0f'/><path stroke='%23e5a00d' stroke-width='2' stroke-linecap='round' d='M20 12a8 8 0 1 1-1.6-4.8'/><polyline stroke='%23e5a00d' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' points='20,4 20,8 16,8'/></svg>">
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap');

  :root {
    --bg: #0d0d0f;
    --surface: #161618;
    --border: #2a2a2e;
    --accent: #e5a00d;
    --text: #e8e8e8;
    --muted: #666;
    --error: #f87171;
    --radius: 4px;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 36px 32px;
    width: 100%;
    max-width: 360px;
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 28px;
  }

  .logo-icon {
    flex-shrink: 0;
    filter: drop-shadow(0 0 6px rgba(229,160,13,0.5));
  }

  h1 {
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.06em;
    color: var(--accent);
    text-transform: uppercase;
  }

  label {
    display: block;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 6px;
  }

  input {
    display: block;
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    padding: 10px 12px;
    margin-bottom: 16px;
    outline: none;
    transition: border-color 0.15s;
  }

  input:focus { border-color: var(--accent); }

  button {
    width: 100%;
    background: var(--accent);
    border: none;
    border-radius: var(--radius);
    color: #0d0d0f;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.08em;
    padding: 11px;
    cursor: pointer;
    text-transform: uppercase;
    margin-top: 4px;
    transition: opacity 0.15s;
  }

  button:hover { opacity: 0.85; }

  .error {
    font-size: 11px;
    color: var(--error);
    background: rgba(248,113,113,0.07);
    border: 1px solid var(--error);
    border-radius: var(--radius);
    padding: 8px 12px;
    margin-bottom: 16px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <svg class="logo-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" width="28" height="28">
      <rect width="24" height="24" rx="4" fill="#0d0d0f"/>
      <path stroke="#e5a00d" stroke-width="2" stroke-linecap="round" d="M20 12a8 8 0 1 1-1.6-4.8"/>
      <polyline stroke="#e5a00d" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="20,4 20,8 16,8"/>
    </svg>
    <h1>Media Servarr Sync</h1>
  </div>
  {% if error %}
  <div class="error">{{ error }}</div>
  {% endif %}
  <form method="post">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" autocomplete="username" autofocus>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>'''


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
    _log_env()

    worker_thread = threading.Thread(target=sync_worker, daemon=True, name="sync-worker")
    worker_thread.start()

    log.info("Webhook receiver active on port %d", PORT)
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
