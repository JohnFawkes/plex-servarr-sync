"""
plex-servarr-sync
-----------------
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

import plexapi
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
from plexapi.server import PlexServer
from plexapi.base import MediaContainer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Load configuration from .env file
load_dotenv()

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
PLEX_TIMEOUT    = int(os.getenv("PLEX_TIMEOUT", 60))
PORT            = int(os.getenv("PORT", 5000))
WEBHOOK_DELAY   = parse_duration(os.getenv("WEBHOOK_DELAY", "30"))
MINIMUM_AGE     = parse_duration(os.getenv("MINIMUM_AGE", "0"))
MANUAL_USER     = os.getenv("MANUAL_USER", "admin")
MANUAL_PASS     = os.getenv("MANUAL_PASS", "password")

# Rclone — set USE_RCLONE=false to skip all rclone calls entirely
USE_RCLONE        = os.getenv("USE_RCLONE", "false").strip().lower() in ("1", "true", "yes")
RCLONE_RC_URL     = os.getenv("RCLONE_RC_URL", "").rstrip('/')
RCLONE_RC_USER    = os.getenv("RCLONE_RC_USER", "")
RCLONE_RC_PASS    = os.getenv("RCLONE_RC_PASS", "")
RCLONE_MOUNT_ROOT = os.getenv("RCLONE_MOUNT_ROOT", "").rstrip('/')

plexapi.TIMEOUT = PLEX_TIMEOUT

# Set a stable client identifier so Plex doesn't register a new device on every container start
PLEX_IDENTIFIER           = os.getenv("PLEX_IDENTIFIER", "plex-servarr-sync")
plexapi.X_PLEX_IDENTIFIER = PLEX_IDENTIFIER
plexapi.X_PLEX_PRODUCT    = PLEX_IDENTIFIER

PATH_REPLACEMENTS        = parse_json_env("PATH_REPLACEMENTS")
RCLONE_PATH_REPLACEMENTS = parse_json_env("RCLONE_PATH_REPLACEMENTS")
SECTION_MAPPING          = parse_json_env("SECTION_MAPPING")


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
    queued_at: float = field(default_factory=time.monotonic)

    def __eq__(self, other):
        return isinstance(other, SyncTask) and self.mapped_folder == other.mapped_folder

    def __hash__(self):
        return hash(self.mapped_folder)


class SyncHistory:
    """Thread-safe ring buffer of recent sync results."""
    def __init__(self, maxlen: int = 50):
        self._lock = threading.Lock()
        self._buf: deque = deque(maxlen=maxlen)

    def add(self, entry: dict):
        with self._lock:
            self._buf.appendleft(entry)

    def as_list(self) -> list:
        with self._lock:
            return list(self._buf)


history = SyncHistory()
sync_queue: queue.Queue = queue.Queue()
_in_flight: set = set()         # mapped_folder strings currently queued or being processed
_in_flight_lock = threading.Lock()
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
        auth = request.authorization
        if not auth or auth.username != MANUAL_USER or auth.password != MANUAL_PASS:
            return jsonify({"message": "Authentication Required"}), 401, \
                   {'WWW-Authenticate': 'Basic realm="Plex Sync"'}
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
                _in_flight.discard(task.mapped_folder)

            duration = round(time.monotonic() - start, 1)
            history.add({
                "ts": datetime.now(timezone.utc).isoformat(),
                "label": task.label,
                "path": task.mapped_folder,
                "status": status,
                "error": error_msg,
                "duration_s": duration,
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

def enqueue_sync(raw_path: str, label: str):
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

    # Deduplication: skip if same folder is already queued/in-flight
    with _in_flight_lock:
        if mapped_folder in _in_flight:
            log.info("[%s] [DEDUP] Already queued: %s", label, mapped_folder)
            return {"status": "deduplicated"}, 200
        _in_flight.add(mapped_folder)

    task = SyncTask(
        section_id=section_id,
        raw_path=raw_path,
        rclone_host_path=rclone_path,
        age_check_path=age_check_path,
        mapped_folder=mapped_folder,
        label=label,
    )
    sync_queue.put(task)
    log.info("[%s] [QUEUE] Added (depth=%d): %s", label, sync_queue.qsize(), mapped_folder)
    return {"status": "queued", "path": mapped_folder}, 200


def process_webhook(data: dict, instance_type: str):
    if not data:
        return jsonify({"error": "No payload"}), 400

    event = data.get('eventType', '')
    if event == "Test":
        return jsonify({"status": "test_success"}), 200

    label = instance_type.upper()
    raw_path = ""
    if 'movie' in data:
        raw_path = data['movie'].get('folderPath', '')
    elif 'series' in data:
        raw_path = data['series'].get('path', '')

    result, status = enqueue_sync(raw_path, label)
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


@app.route('/webhook/manual', methods=['GET', 'POST'])
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

    recent = history.as_list()
    return render_template_string(MANUAL_UI_TEMPLATE, message=message, msg_class=msg_class, history=recent)


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


# ---------------------------------------------------------------------------
# Manual UI
# ---------------------------------------------------------------------------

MANUAL_UI_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>plex-servarr-sync</title>
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

  .logo-dot {
    width: 10px; height: 10px;
    background: var(--accent);
    border-radius: 50%;
    box-shadow: 0 0 12px var(--accent);
    flex-shrink: 0;
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

  .h-path   { color: var(--text); word-break: break-all; }
  .h-meta   { color: var(--muted); text-align: right; white-space: nowrap; }
  .h-label  { color: var(--accent); font-size: 10px; }
  .h-error  { color: var(--error); font-size: 10px; margin-top: 2px; }
  .h-ts     { display: block; }

  .empty    { color: var(--muted); font-family: 'IBM Plex Mono', monospace; font-size: 12px; text-align: center; padding: 16px 0; }
</style>
</head>
<body>
<div class="shell">
  <header>
    <div class="logo-dot"></div>
    <h1>PLEX-SERVARR-SYNC</h1>
    <span class="subtitle">Manual trigger</span>
  </header>

  <div class="card">
    <div class="card-label">Trigger path scan</div>
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
    <div class="card-label">Recent syncs</div>
    {% if history %}
      {% for h in history %}
      <div class="history-item">
        <div class="status-dot {{ 'dot-ok' if h.status == 'ok' else 'dot-error' }}"></div>
        <div>
          <span class="h-path">{{ h.path }}</span>
          <div class="h-label">{{ h.label }} &nbsp;·&nbsp; {{ h.duration_s }}s</div>
          {% if h.error %}<div class="h-error">{{ h.error }}</div>{% endif %}
        </div>
        <div class="h-meta">
          <span class="h-ts">{{ h.ts[11:19] }}</span>
          <span>{{ h.ts[:10] }}</span>
        </div>
      </div>
      {% endfor %}
    {% else %}
      <p class="empty">No syncs yet.</p>
    {% endif %}
  </div>
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


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    log.info("=== plex-servarr-sync starting ===")
    log.info("Rclone integration: %s", "ENABLED" if USE_RCLONE else "DISABLED (set USE_RCLONE=true to enable)")

    worker_thread = threading.Thread(target=sync_worker, daemon=True, name="sync-worker")
    worker_thread.start()

    log.info("Webhook receiver active on port %d", PORT)
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
