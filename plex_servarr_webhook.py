"""
Plex Partial Sync for Sonarr & Radarr (PlexAPI Version)
------------------------------------
Logic:
1. Webhooks are immediately placed in a Queue.
2. A background worker processes the queue.
3. Rclone VFS cache is cleared/refreshed for the specific show path relative to mount root.
4. Plex is triggered to perform a partial scan.
"""

import os
import time
import json
import sys
import re
import threading
import requests
import urllib.parse
import queue
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
from plexapi.server import PlexServer
from plexapi.base import MediaContainer

# Load configuration from .env file
load_dotenv()

app = Flask(__name__)

def parse_duration(duration_str):
    if not duration_str:
        return 0
    duration_str = str(duration_str).strip().lower()
    if duration_str.isdigit():
        return int(duration_str)
    match = re.match(r'^(\d+)([smhd])$', duration_str)
    if not match:
        return 0
    value, unit = match.groups()
    value = int(value)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return value * multipliers[unit]

# --- CONFIGURATION ---
PLEX_URL = os.getenv("PLEX_URL", "http://127.0.0.1:32400").rstrip('/')
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
PORT = int(os.getenv("PORT", 5000))
RCLONE_RC_URL = os.getenv("RCLONE_RC_URL", "").rstrip('/')
RCLONE_RC_USER = os.getenv("RCLONE_RC_USER", "")
RCLONE_RC_PASS = os.getenv("RCLONE_RC_PASS", "")
RCLONE_MOUNT_ROOT = os.getenv("RCLONE_MOUNT_ROOT", "").rstrip('/')
WEBHOOK_DELAY = parse_duration(os.getenv("WEBHOOK_DELAY", 30))
MINIMUM_AGE = parse_duration(os.getenv("MINIMUM_AGE", 0))

# Manual Webhook Auth
MANUAL_USER = os.getenv("MANUAL_USER", "admin")
MANUAL_PASS = os.getenv("MANUAL_PASS", "password")

# --- GLOBALS ---
sync_queue = queue.Queue()
try:
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    print(f"--- Connected to Plex: {plex.friendlyName} ---", flush=True)
except Exception as e:
    print(f"!!! ERROR: Could not connect to Plex: {e} !!!", flush=True)
    plex = None

# --- AUTH DECORATOR ---
def check_auth(username, password):
    return username == MANUAL_USER and password == MANUAL_PASS

def authenticate():
    return jsonify({"message": "Authentication Required"}), 401, {'WWW-Authenticate': 'Basic realm="Login Required"'}

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- UTILS ---
def normalize_path(path, is_dir=True):
    if not path or not isinstance(path, str): 
        return ""
    clean = path.strip().replace('\\', '/').rstrip('/')
    return clean + '/' if is_dir else clean

def parse_json_env(env_name):
    raw_val = os.getenv(env_name, "{}").strip()
    if raw_val.startswith("'") and raw_val.endswith("'"): 
        raw_val = raw_val[1:-1]
    try: 
        data = json.loads(raw_val)
        return {normalize_path(k, is_dir=False).lower(): v for k, v in data.items()}
    except Exception as e: 
        print(f"!!! ERROR: Parsing {env_name}: {e}", flush=True)
        return {}

# Load Mappings
PATH_REPLACEMENTS = parse_json_env("PATH_REPLACEMENTS")
RCLONE_PATH_REPLACEMENTS = parse_json_env("RCLONE_PATH_REPLACEMENTS")
SECTION_MAPPING = parse_json_env("SECTION_MAPPING")

def apply_path_mapping(path, mapping_dict, label, is_dir=True):
    orig = normalize_path(path, is_dir=False)
    lower_orig = orig.lower()
    for old_prefix in sorted(mapping_dict.keys(), key=len, reverse=True):
        if lower_orig.startswith(old_prefix):
            new_prefix = mapping_dict[old_prefix]
            mapped = str(new_prefix) + orig[len(old_prefix):]
            result = normalize_path(mapped, is_dir=is_dir)
            print(f"[{label}] [DEBUG] Map: '{orig}' -> '{result}'", flush=True)
            return result
    return normalize_path(orig, is_dir=is_dir)

def rclone_vfs_refresh(rclone_host_path, label):
    """
    Triggers rclone vfs/forget and vfs/refresh.
    Converts full host path to a relative path based on RCLONE_MOUNT_ROOT.
    """
    if not RCLONE_RC_URL: return
    auth = (RCLONE_RC_USER, RCLONE_RC_PASS) if RCLONE_RC_USER else None
    
    full_path = rclone_host_path.rstrip('/')
    mount_root = RCLONE_MOUNT_ROOT.rstrip('/')
    
    if full_path.startswith(mount_root):
        target_dir = full_path[len(mount_root):].lstrip('/')
    else:
        target_dir = full_path
        print(f"[{label}] [RCLONE] Warning: Path '{full_path}' is not under root '{mount_root}'", flush=True)

    try:
        print(f"[{label}] [RCLONE] Forget: '{target_dir}'", flush=True)
        requests.post(f"{RCLONE_RC_URL}/vfs/forget", json={"dir": target_dir}, auth=auth, timeout=15)
        
        print(f"[{label}] [RCLONE] Refresh (Async): '{target_dir}'", flush=True)
        res = requests.post(f"{RCLONE_RC_URL}/vfs/refresh", 
                            json={"dir": target_dir, "recursive": True, "_async": True}, 
                            auth=auth, timeout=15)
        if res.status_code == 200:
            print(f"[{label}] [RCLONE] Success: JobID {res.json().get('jobid')}", flush=True)
        else:
            print(f"[{label}] [RCLONE] Error {res.status_code}: {res.text}", flush=True)
    except Exception as e:
        print(f"[{label}] [RCLONE] RC Connection Error: {e}", flush=True)

# --- BACKGROUND WORKER ---
def sync_worker():
    print("--- Sync Worker Started ---", flush=True)
    while True:
        task = sync_queue.get()
        if task is None: break
        
        section_id, raw_path, rclone_host_path, age_check_path, mapped_folder, label = task
        
        # Consolidation
        pending_items = []
        try:
            while True:
                extra = sync_queue.get_nowait()
                if extra[4] == mapped_folder: continue
                pending_items.append(extra)
        except queue.Empty: pass
        for item in pending_items: sync_queue.put(item)

        try:
            # 1. Rclone Refresh (Targeted relative folder)
            rclone_vfs_refresh(rclone_host_path, label)

            # 2. Minimum Age Check
            if MINIMUM_AGE > 0:
                check_path = age_check_path.rstrip('/')
                if os.path.exists(check_path):
                    mtime = os.path.getmtime(check_path)
                    age = time.time() - mtime
                    if age < MINIMUM_AGE:
                        wait_rem = MINIMUM_AGE - age
                        print(f"[{label}] [AGE] Too young. Waiting {int(wait_rem)}s at {check_path}...", flush=True)
                        time.sleep(wait_rem)
                else:
                    print(f"[{label}] [AGE] Warning: Folder not visible inside container: {check_path}", flush=True)

            if plex:
                library = plex.library.sectionByID(section_id)
                print(f"[{label}] [SCAN] Triggering Plex scan for: {mapped_folder}", flush=True)
                library.update(path=mapped_folder)
                
                # 3. Buffer and Metadata
                time.sleep(20)
                search_path = mapped_folder.rstrip('/')
                folder_name = os.path.basename(search_path)
                clean_title = re.sub(r'\s*[\(\{\[].*', '', folder_name).strip()
                
                item = None
                for i in range(6):
                    print(f"[{label}] [METADATA] Lookup {i+1}/6 for '{clean_title}'", flush=True)
                    try:
                        encoded = urllib.parse.quote(search_path)
                        xml = plex.query(f"/library/sections/{section_id}/all?path={encoded}")
                        container = MediaContainer(plex, xml)
                        if container.metadata:
                            item = container.metadata[0]
                    except: pass
                    
                    if not item:
                        results = library.search(title=clean_title)
                        for res in results:
                            locs = res.locations if hasattr(res, 'locations') else []
                            if not locs and hasattr(res, 'media'):
                                for m in res.media:
                                    for p in m.parts: locs.append(p.file)
                            if any(search_path in l for l in locs):
                                item = res
                                break
                    if item: break
                    time.sleep(20)

                if item:
                    print(f"[{label}] [METADATA] Success: {item.title}. Analyzing...", flush=True)
                    item.analyze()
        except Exception as e:
            print(f"[{label}] [ERROR] Worker failed: {e}", flush=True)
        
        sync_queue.task_done()
        time.sleep(5)

threading.Thread(target=sync_worker, daemon=True).start()

# --- ROUTES ---
def process_webhook(data, instance_type):
    if not data: return jsonify({"error": "No payload"}), 400
    if data.get('eventType') == "Test": return jsonify({"status": "test_success"}), 200

    label = instance_type.upper()
    raw_path = ""
    if 'movie' in data: raw_path = data['movie'].get('folderPath', '')
    elif 'series' in data: raw_path = data['series'].get('path', '')

    if not raw_path: return jsonify({"status": "skipped"}), 200

    mapped_folder = apply_path_mapping(raw_path, PATH_REPLACEMENTS, label, is_dir=True)
    rclone_host_path = apply_path_mapping(raw_path, RCLONE_PATH_REPLACEMENTS, label, is_dir=False)
    age_check_path = mapped_folder

    section_id = None
    comp_path = mapped_folder.rstrip('/').lower()
    for root_path in sorted(SECTION_MAPPING.keys(), key=len, reverse=True):
        if comp_path.startswith(root_path):
            section_id = SECTION_MAPPING[root_path]
            break

    if not section_id:
        print(f"[{label}] [SKIP] No mapping for '{mapped_folder}'", flush=True)
        return jsonify({"status": "skipped"}), 200

    print(f"[{label}] [QUEUE] Added: {mapped_folder}", flush=True)
    sync_queue.put((section_id, raw_path, rclone_host_path, age_check_path, mapped_folder, label))
    return jsonify({"status": "queued"}), 200

@app.route('/webhook/sonarr', methods=['POST'])
def webhook_sonarr(): return process_webhook(request.json, "sonarr")

@app.route('/webhook/radarr', methods=['POST'])
def webhook_radarr(): return process_webhook(request.json, "radarr")

@app.route('/webhook/manual', methods=['GET', 'POST'])
@requires_auth
def manual_webhook():
    message = ""
    if request.method == 'POST':
        raw_path = request.form.get('path', '').strip()
        if raw_path:
            label = "MANUAL"
            mapped_folder = apply_path_mapping(raw_path, PATH_REPLACEMENTS, label, is_dir=True)
            rclone_host_path = apply_path_mapping(raw_path, RCLONE_PATH_REPLACEMENTS, label, is_dir=False)
            age_check_path = mapped_folder

            section_id = None
            comp_path = mapped_folder.rstrip('/').lower()
            for root_path in sorted(SECTION_MAPPING.keys(), key=len, reverse=True):
                if comp_path.startswith(root_path):
                    section_id = SECTION_MAPPING[root_path]
                    break
            
            if section_id:
                sync_queue.put((section_id, raw_path, rclone_host_path, age_check_path, mapped_folder, label))
                message = f"Sync queued for: {mapped_folder}"
            else:
                message = f"Error: No library section mapping found for {mapped_folder}"
    
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head><title>Manual Plex Sync</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; border: 1px solid #ccc; border-radius: 8px;">
            <h2>Manual Plex Sync</h2>
            {% if message %}<p style="color: blue;">{{ message }}</p>{% endif %}
            <p>Enter the path as provided by Sonarr/Radarr (e.g. /home/johnfawkes/MergerFS/tvshows/ShowName):</p>
            <form method="post">
                <input type="text" name="path" placeholder="/home/johnfawkes/MergerFS/..." style="width:100%; padding: 8px; margin-bottom: 10px;">
                <button type="submit" style="padding: 10px 20px; cursor: pointer;">Trigger Sync</button>
            </form>
        </body>
        </html>
    ''', message=message)

if __name__ == '__main__':
    print(f"--- Servarr Sync Receiver Active on Port {PORT} ---", flush=True)
    app.run(host='0.0.0.0', port=PORT)
