# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed
- Sonarr upgrade events (and standalone file-delete events) no longer record the **old** episode filename in sync history. Sonarr's `EpisodeFileDeleted` webhook payload carries an `episodeFile` key that points to the *deleted* file; if that event was processed first it would write the old filename to history and potentially lock the folder in the cooldown window before the `Download` event arrived with the new file. `EpisodeFileDeleted`, `SeriesDelete`, `MovieFileDeleted`, `MovieDelete`, and `Grab` events are now silently skipped — the `Download` event that follows an upgrade is sufficient to trigger the scan with the correct new filename.

---

## [v0.2.0] - 2026-03-02

### Added
- **Stats API** — new unauthenticated `GET /api/stats` endpoint returning aggregate sync counts (total, ok, failed, sonarr, radarr, manual), average duration, queue depth, in-flight count, worker status, and last sync info. Designed for the [Homepage](https://gethomepage.dev) `customapi` widget; widget YAML snippet included in the endpoint docstring
- **Episode hover tooltip** — when multiple episodes are merged into a single sync record the episode-count badge (e.g. `5 episodes`) now shows a dropdown tooltip on hover listing every individual filename. Tooltip only appears when filenames are known (new-format records); old count-only records display the badge without a tooltip
- **History search bar** — text field above the sync history list that filters by path using a server-side SQL `LIKE` query so results span all pages, not just the currently visible page. Debounced auto-submit fires 400 ms after the user stops typing
- **History status filter** — **All / OK / Failed** pill buttons filter the history list by status. Active pill is highlighted in the matching colour (amber / green / red). Search text and status filter are preserved across pagination and when switching between filter tabs

### Changed
- Episode data for batch Sonarr imports is now stored as a JSON list of filenames (e.g. `["S01E01.mkv","S01E02.mkv"]`) instead of a plain count string. Enables the hover tooltip. Single-episode records continue to store a plain filename string. Old count-only records (`"N episodes"`) are handled transparently
- `_merge_episode_counts` updated to merge JSON filename lists and dedup by normalised SxxExx key; falls back to additive count for legacy records that carry no filenames

---

## [v0.1.2]

### Added
- `SYNC_COOLDOWN` documented in README env var table and `.env.example`

---

## [v0.1.1] - 2026-02-28

### Added
- Post-sync cooldown window (`SYNC_COOLDOWN`, default `5m`) — after a path finishes processing it is held in a cooldown set for the configured duration; any Sonarr follow-up events that arrive within that window (e.g. the `Rename` webhook that fires after a `Download`) are silently dropped, preventing duplicate history entries for the same show

### Fixed
- Multi-episode deduplication: when Sonarr fires 3+ rapid webhooks for separate episodes that all map to the same show folder, the dedup logic now merges the episode counts instead of silently discarding duplicates — the history record correctly shows e.g. `3 episodes` rather than only the first episode's filename
- Duplicate history entries caused by Sonarr's trailing `Rename` webhook arriving after a `Download` task had already finished processing

---

## [v0.1.0] - 2026-02-27

### Added
- Logo SVG displayed in the WebUI dashboard header and login card, replacing the plain text placeholder
- CSS `drop-shadow` glow applied to the logo icon for visual consistency with the accent colour
- `logo.svg` added to the repository and displayed in the README header

---

## [v0.0.16] - 2026-02-27

### Added
- Episode count badge rendered in the sync history UI for season pack / multi-episode imports, distinguishing them from single-file downloads at a glance
- SVG favicon embedded as an inline data-URI in both the dashboard and login templates — no external file dependency

---

## [v0.0.15] - 2026-02-27

### Changed
- Removed mergerfs options from Docker Compose example
- Updated README screenshots

---

## [v0.0.14] - 2026-02-26

### Added
- `RCLONE_PATH_REPLACEMENTS` — decouples the path sent to rclone VFS refresh from the Plex library path, supporting setups where the rclone remote host path differs from the container mount target

---

## [v0.0.13] - 2026-02-26

### Added
- Handle all three Sonarr episode payload variants: `episodeFile` (single download), `episodeFiles` (season pack / batch), and `renamedEpisodeFiles` (rename events)

---

## [v0.0.12] - 2026-02-25

### Fixed
- Sonarr webhooks now always scan the show root folder rather than the season subfolder, ensuring Plex correctly associates new episodes with the existing show entry

---

## [v0.0.11] - 2026-02-25

### Added
- Episode filename shown in the recent syncs history list for Sonarr events (single episode shows filename, batch shows count)

---

## [v0.0.10] - 2026-02-25

### Changed
- Web UI moved from `/webhook/manual` to the root path `/` — the dashboard is now accessible directly at the container URL
- Sonarr scan path changed to season subfolder (later corrected in [v0.0.12](#v0012---2026-02-25))

---

## [v0.0.9] - 2026-02-20

### Changed
- Upgraded Flask to 3.1.3

---

## [v0.0.7] - 2026-02-15

### Changed
- Upgraded base Docker image to Python 3.14 slim
- Fixed typos from rebrand

---

## [v0.0.6] - 2026-02-15

### Fixed
- Dockerfile typo

---

## [v0.0.5] - 2026-02-15

### Changed
- Project rebranded to **Media Servarr Sync**

---

## [v0.0.4] - 2026-02-15

### Fixed
- Removed stale path info from script output

---

## [v0.0.3] - 2026-02-15

### Fixed
- Dockerfile script name correction post-rebrand

---

## [v0.0.1] - 2026-02-15

### Added
- Initial project push with core webhook receiver for Sonarr and Radarr
- Plex library scan triggered on webhook receipt via PlexAPI
- Plex timeout handling with up to 3 retries and 10 s back-off; connection invalidated and re-established between attempts
- Metadata analysis (`item.analyze()`) after successful scan to update Plex match data
- Thread-safe task queue with a background worker daemon thread
- In-flight deduplication — prevents the same folder being queued more than once simultaneously
- SQLite-backed sync history with per-page pagination in the UI
- Sync history retention configurable via `HISTORY_DAYS` environment variable
- Optional rclone VFS cache integration (`USE_RCLONE`, `RCLONE_RC_URL`) to issue VFS forget + async refresh before scanning
- Configurable webhook delay (`WEBHOOK_DELAY`) to allow Sonarr time to finish writing before scanning
- Minimum file age check (`MINIMUM_AGE`) to ensure files have settled on disk before scanning
- Path replacement mappings for Plex paths, rclone paths, and library section IDs via environment variables
- Login / logout authentication protecting the manual trigger UI; credentials configured via `MANUAL_USER` and `MANUAL_PASS`
- Timezone-aware timestamps throughout the sync history UI
- `PLEXAPI_HEADER_IDENTIFIER` environment variable to override the Plex client identifier sent with API requests
- Proper health check endpoint (`/health`) returning JSON with Plex connectivity, rclone state, queue depth, and worker thread status; responds HTTP 207 when degraded
- GitHub Actions CI/CD workflows for Docker image publishing
- Docker Compose configuration with JSON log rotation (10 MB max, 3 files) and persistent data volume
- Docker health check hitting `/health` at 30 s intervals
- LICENSE file (MIT)
- Renovate bot configuration for automated dependency updates
