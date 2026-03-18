# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [v0.10.0] - 2026-03-18

### Added
- **Custom formats fetched from arr API** — custom formats are now retrieved directly from the Sonarr/Radarr `/episodefile/{id}` or `/moviefile/{id}` endpoint instead of relying on the webhook payload, which can be incomplete or stale.

### Fixed
- **Waitress WSGI server** — replaced the Flask development server with [waitress](https://docs.pylonsproject.org/projects/waitress/) for production use.
- **`EpisodeFileDelete` / `MovieFileDelete` skipping** — the event names in `_SKIP` were incorrect (`EpisodeFileDelete` instead of `EpisodeFileDeleted`); events are now skipped as intended.
- **Custom format tag tooltip** — hovering a custom format (purple) tag now shows a `"Format"` tooltip label, consistent with Quality and Profile tags.

### Changed
- Raw `quality` and `customFormats` fields from every incoming Sonarr/Radarr webhook are now logged at `INFO` level, making it easier to diagnose mismatches between what the arr sends and what gets stored.
- CI: `templates/**` added to the Docker workflow path filter so image builds are triggered when template files change.

### Security
- **Reflected XSS (CodeQL #2/#3)** — webhook responses no longer echo user-supplied data (`path`, `eventType`) back in the JSON body, eliminating the taint flows flagged by CodeQL.
- **Open redirect (login)** — the post-login `next` redirect is now replaced entirely with a static `redirect(url_for('manual_webhook'))`, removing all user-controlled data from the call to `redirect()`.
- **CSRF protection** — replaced the invalid Django-style `{% csrf_token %}` tag with proper Flask-WTF CSRF token injection (`{{ csrf_token() }}`) in both the login and manual UI forms.
- **SSTI prevention** — inline template strings moved to files under `templates/` and rendered via `render_template()`, eliminating server-side template injection risk.
- **SQL injection prevention** — removed f-string interpolation in raw SQL queries; all dynamic values now use parameterised query placeholders.
- **Pip CVE-2026-1703** — base Docker image pins `pip >= 26.0` to address the path traversal vulnerability.

---

## [v0.9.0] - 2026-03-14

### Added
- **Tag colour legend** — a fixed panel on the left side of the web UI lists the three tag colours and what they mean: green = Quality Profile, blue = Quality, purple = Custom Format. The legend is hidden automatically on narrow viewports where it would overlap the main content.

### Security
- **Open redirect hardening** — the post-login `next` redirect now additionally rejects URLs with an explicit scheme (e.g. `javascript:`, `data:`, `http:`) and protocol-relative URLs (e.g. `//evil.com`). Only strict relative paths beginning with `/` are accepted; everything else falls back to the default page.

---

## [v0.8.0] - 2026-03-07

### Added
- **Hoverable quality and profile tags** — hovering a quality or profile tag shows a small tooltip label (`"Quality"` / `"Profile"`) so the purpose of each tag is immediately clear without needing to know the colour convention.
- **Filterable quality and profile tags** — clicking any quality or profile tag filters the sync history to entries matching that exact value. The active tag is highlighted with a colour-matched glow ring. Active filters appear as dismissible `×` pills in the filter bar alongside the status pills. All active filters (search, status, quality, profile) are preserved across pagination and search-form submits.

### Fixed
- Tag tooltips now use a dark background (`#1a1a2e`) with light text for legibility against all tag colours.

---

## [v0.7.0] - 2026-03-07

### Added
- **Event type in webhook logs** — the `eventType` field from each Sonarr/Radarr webhook is included in the processing log line for easier debugging.

### Fixed
- Sonarr and Radarr API credentials are validated at startup and their configured/missing state is logged, so misconfigured quality-profile lookups are immediately visible.
- Removed a spurious double border line above the pagination buttons in the sync history UI.

---

## [v0.6.0] - 2026-03-07

### Added
- **Quality profile badges** — the quality profile name (e.g. `HD Quality`, `Any`) is fetched from the Sonarr/Radarr API using the profile ID from the webhook and displayed as a green tag alongside quality and custom-format tags. Profile ID→name mapping is cached in memory to avoid redundant API calls.

---

## [v0.5.0] - 2026-03-05

### Added
- **Auto-refresh with countdown** — the sync history panel refreshes automatically every 30 seconds. A live countdown timer shows seconds until the next refresh and a manual `↻` button triggers an immediate update.

---

## [v0.4.0] - 2026-03-05

### Added
- **Quality and custom-format tags** — each sync history entry now captures the file quality (e.g. `WEBDL-1080p`) and custom format names (e.g. `Uncensored`, `JA+EN Audio`) from the Sonarr/Radarr webhook payload and displays them as colour-coded inline tags beneath the episode info. Quality tags are shown in blue; custom-format tags in purple. Works for single downloads, batch/season-pack imports, rename events, and Radarr movies.

### Fixed
- Quality and custom-format values are now correctly extracted from the nested `quality.quality.name` and `customFormatScore` fields in Sonarr/Radarr webhook payloads.

### Changed
- Renovate updated `docker/setup-buildx-action` to v4 and `docker/login-action` to v4.

---

## [v0.3.0] - 2026-03-03

### Added
- All environment variables are logged at startup with secret values redacted, making misconfiguration easier to spot.
- Flask development-server startup banner is suppressed for cleaner container logs.

### Fixed
- Sonarr upgrade events (and standalone file-delete events) no longer record the **old** episode filename in sync history. `EpisodeFileDeleted`, `SeriesDelete`, `MovieFileDeleted`, `MovieDelete`, and `Grab` events are now silently skipped — the `Download` event that follows an upgrade is sufficient to trigger the scan with the correct new filename.
- Additional guard for Sonarr `Download` events with `isUpgrade: true`: if the `episodeFile` field transiently references one of the files listed in `deletedFiles` (the files being replaced), that stale filename is discarded rather than written to history.
- Multi-episode deduplication: when Sonarr fires both an `Import` and a trailing `Rename` webhook for the same episodes, the rename no longer creates a duplicate history entry with a doubled episode count.

### Changed
- Added Trivy container image vulnerability scanning to the Docker CI workflow.
- Added `workflow_dispatch` trigger to allow manual runs of the Docker publish workflow.

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
