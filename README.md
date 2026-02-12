# Plex Servarr Webhook Sync

A lightweight Python-based webhook receiver designed to synchronize Sonarr and Radarr with Plex and Rclone. It handles the "new file" propagation delay often found in cloud-based MergerFS or Rclone setups by triggering targeted VFS refreshes and partial Plex library scans.

## Features

-   **Queue-Based Processing**: Webhooks are queued and processed sequentially to prevent race conditions and redundant scans.

-   **Path Mapping**: Translates paths between Sonarr/Radarr (Seedbox), Rclone (Host), and Plex (Container) environments.

-   **Rclone VFS Integration**: Automatically clears and refreshes the Rclone VFS cache for specific show/movie directories using the Rclone Remote Control (RC) API.

-   **Partial Plex Scans**: Triggers Plex to scan only the specific folder added/updated, rather than the entire library.

-   **Debouncing/Consolidation**: Groups identical sync requests arriving in short windows into a single operation.

-   **Minimum Age Check**: Optional wait period to ensure files are fully propagated across mounts before scanning.

-   **Manual Trigger UI**: Includes a simple authenticated web interface to manually trigger a sync for a specific path.


## Prerequisites

-   **Python 3.11+** or **Docker**.

-   **Rclone** running with the `--rc` flag enabled (and ideally `--rc-no-auth` or configured credentials).

-   **Plex Media Server** with an accessible API token.


## Setup & Installation

### Docker (Recommended)

1.  Clone the repository:

    ```
    git clone https://github.com/JohnFawkes/plex-servarr-sync.git
    cd plex-servarr-sync
    
    
    ```

2.  Create a `.env` file (see [Configuration](https://github.com/JohnFawkes/plex-servarr-sync?tab=readme-ov-file#configuration) below).

3.  Deploy using Docker Compose:

    ```
    services:
      plex-servarr-sync:
        image: 
        container_name: plex-servarr-sync
        restart: unless-stopped
        ports:
          - "5001:5000"
        env_file:
          - .env
        volumes:
          # Optional: Mount your media folders read-only if you want 
          # the script to verify if 'Season XX' folders exist before scanning.
          # The paths inside the container should match your PLEX paths 
          # or what you define in SECTION_MAPPING.
          - /mnt/nvme/MergerFS:/data/media:ro,rslave
          - ./.env:/app/.env:ro
        environment:
          - PYTHONUNBUFFERED=1
          - PLEXAPI_HEADER_IDENTIFIER=plex-servarr-sync
    
    
    ``` 
    
    _Note: Using `:rslave` or `:rshared` is crucial for MergerFS/FUSE mounts to propagate live changes into the container._

4. Run docker compose
   ```
   docker compose up -d


   ```

### Manual Installation

1.  Clone the repository:
    
    ```
    git clone [https://github.com/JohnFawkes/plex-servarr-sync.git](https://github.com/JohnFawkes/plex-servarr-sync.git)
    cd plex-servarr-sync


    ```
    
2.  Create a `.env` file (see [Configuration](https://github.com/JohnFawkes/plex-servarr-sync/README.md#Configuration) below).

3. Create a Virtual Environment and activate the Virtual Environment:

   ```
   python3 -m venv venv
   source venv/bin/activate


   ```
   
4.  Install dependencies:
    
    ```
    python -m pip install -r requirements.txt


    ```
    
2.  Run the script:
    
    ```
    python plex_servarr_webhook.py


    ```
    

## Configuration

Configuration is handled via environment variables in a `.env` file.

| **Variable**               | **Description**                                 | **Example**                                        | 
| `PLEX_URL`                 | URL of your Plex server.                        | `http://192.168.1.100:32400`                       | 
| `PLEX_TOKEN`               | Your Plex API token.                            | `yY6Vy...`                                         | 
| `RCLONE_RC_URL`            | URL of the Rclone RC server.                    | `http://192.168.1.1:5580`                          | 
| `RCLONE_MOUNT_ROOT`        | The base mount path on the Rclone host.         | `/mnt/media`                                       | 
| `PATH_REPLACEMENTS`        | JSON map of Sonarr paths to Plex paths.         | `'{"/home/user/media": "/data/media"}'`            | 
| `RCLONE_PATH_REPLACEMENTS` | JSON map of Sonarr paths to Rclone host paths.  | `'{"/home/user/media": "/mntmedia"}'`              | 
| `SECTION_MAPPING`          | JSON map of Plex paths to Library IDs.          | `'{"/data/media/tv": 2, "/data/media/movies": 1}'` | 
| `WEBHOOK_DELAY`            | Cooldown period before processing (e.g., 30s).  | `30s`                                              | 
| `MINIMUM_AGE`              | Min age of file on disk before scan (e.g., 2m). | `2m`                                               |

## Servarr Setup

1.  Go to **Settings > Connect** in Sonarr or Radarr.
    
2.  Add a new **Webhook**.
    
3.  **URL**: `http://<your-ip>:5000/webhook/sonarr` (or `/radarr`).
    
4.  **Method**: `POST`.
    
5.  **Notification Triggers**: Check `On Download` and `On Upgrade`.
    

## Manual Usage

Access `http://<your-ip>:5000/webhook/manual` in your browser. Authenticate with `MANUAL_USER` and `MANUAL_PASS` defined in your `.env` to manually trigger a path sync.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](GNU General Public License v3.0) file for details.
