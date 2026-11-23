# OmniTheater

**Personal offline movie server.** A small self-hosted backend (FastAPI + SQLite) that keeps a private movie library, streams it to an Android client, and can fetch new titles through Prowlarr and aria2.

> **Status: work in progress.** Built for personal, non-commercial use. APIs may change without notice.

This repository is the **server edition**. The Android client lives in a separate project.

---

## Features

- Movie catalog with feed, search, and per-user _continue watching_.
- Streaming through short-lived URLs issued by the blob store (HTTP Range supported).
- Favorites, playlists (with custom covers), profile pictures, and playback history per user.
- Torrent search (Prowlarr) and background downloads (aria2): the largest video is remuxed to MP4, a poster is attached, and everything is uploaded to the blob store.
- Titles and posters matched automatically against [TMDB](https://www.themoviedb.org/).
- Authentication delegated to an external identity service (Osiris), with automatic user provisioning.
- Audit log of user actions.

## Architecture

```
 Android app
     │  HTTPS + Bearer JWT
     ▼
┌──────────────────────────── OmniTheater (FastAPI, :8084) ────────────────────────────┐
│  routers: catalog · torrents · users          services: media_repo · fetcher · TMDB   │
└──────┬───────────┬──────────────┬───────────────┬───────────────┬───────────────┬────┘
       │           │              │               │               │               │
    SQLite       Redis          Holder          Osiris         Prowlarr          aria2
 (metadata,   (task progress,  (blob store:   (JWT issuer,    (torrent        (downloads,
  FTS5)        locks, cache)    chunked,       JWKS)           search)         JSON-RPC)
                                deduplicated)
```

| Component        | Role                                                                                        | Default address                        |
| ---------------- | ------------------------------------------------------------------------------------------- | -------------------------------------- |
| SQLite           | Metadata, users, playlists, history (WAL mode, FTS5 index)                                  | `~/.config/omnitheater/omnitheater.db` |
| Redis (db 1)     | Download task state, duplicate-download locks, cached stream URLs, cached search results    | `localhost:6379`                       |
| Holder           | Blob storage for videos, thumbnails, and metadata files (chunked upload with deduplication) | `http://127.0.0.1:8080`                |
| Osiris           | Identity provider; the server verifies its JWTs through JWKS                                | `http://127.0.0.1:8090`                |
| Prowlarr         | Torrent indexer aggregation                                                                 | `http://127.0.0.1:9696`                |
| aria2            | Torrent downloads over JSON-RPC                                                             | `http://127.0.0.1:6800/jsonrpc`        |
| ffmpeg / ffprobe | Remuxing to MP4, frame thumbnails, duration probing                                         | on `PATH`                              |

## Configuration

Settings are read from `~/.config/omnitheater/.env`. The server refuses to start if a required variable is missing.

| Variable            | Required | Description                                                                                     |
| ------------------- | :------: | ----------------------------------------------------------------------------------------------- |
| `OSIRIS_ISSUER`     |   yes    | Expected `iss` claim of incoming JWTs                                                           |
| `REDIS_PASSWORD`    |   yes    | Redis password                                                                                  |
| `HOLDER_PUBLIC_URL` |   yes    | Public base URL of Holder; stream URLs handed to clients use this host                          |
| `PROWLARR_API_KEY`  |   yes    | Prowlarr API key                                                                                |
| `ARIA2_SECRET`      |   yes    | aria2 RPC secret                                                                                |
| `TMDB_API_KEY`      |    no    | TMDB API key (v3). Without it, posters fall back to a video frame and titles are not cleaned up |
| `TMDB_LANGUAGE`     |    no    | Language for TMDB titles and posters (default `es-MX`)                                          |

Other paths and ports are constants in `core/configs.py`: server port `8084`, profile pictures in `~/omnitheater/profiles`.

**Deployment notes**

- aria2 downloads go to `/var/lib/aria2/downloads/task-<id>/`. The user running OmniTheater must be able to write there (group-writable, setgid directories work well), because the server renames, remuxes, and deletes files in that tree.
- Place a `default.webp` in the profiles directory; it is served to users without a profile picture.

## Authentication

Every endpoint except `GET /thumbnail/{hash}` requires:

```
Authorization: Bearer <JWT issued by Osiris>
```

The token is verified against the issuer, the audience (`omnitheater`), and the Osiris JWKS endpoint. The first time a valid subject is seen, a local user named `osiris-<sub>` is created automatically and the event is written to the audit log.

## API reference

All identifiers named `hash` are the 64-character hex id of a media item. Durations are in **seconds**; playback positions are in **milliseconds**.

### Catalog

| Method | Path                         | Description                                                                                                                                |
| ------ | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `GET`  | `/feed?skip=0&limit=50`      | Newest items first. `limit` 1–200. Returns `{ "feed": [...], "next_skip": int \| null }`                                                   |
| `GET`  | `/media/{hash}`              | Metadata of one item                                                                                                                       |
| `GET`  | `/search?q=...&limit=30`     | Case-insensitive search over title, aliases, and uploader. Every word in `q` must match. `q` 1–100 chars, `limit` 1–100                    |
| `GET`  | `/history/continue?limit=20` | Items the user started and has not finished (items watched past 95% are hidden), each with `position_ms`                                   |
| `GET`  | `/video/{hash}/url`          | Returns `{ "url": "...", "expires_in": seconds }`, a short-lived direct stream URL                                                         |
| `GET`  | `/video/{hash}`              | Same, as a `307` redirect to the stream URL                                                                                                |
| `GET`  | `/thumbnail/{hash}`          | Cover image (`image/webp`). **No authentication**, so image loaders can fetch it; cached for 24 h (`Cache-Control: public, max-age=86400`) |
| `GET`  | `/videos/progress/{task_id}` | State of a background download (see [Download pipeline](#download-pipeline))                                                               |

Media metadata object:

```json
{
  "id": "…64 hex…",
  "original_id": "…",
  "extractor": "bittorrent",
  "title": "The Hunger Games",
  "uploader": "indexer or source name",
  "duration": 8520,
  "upload_date": "20261014",
  "url": "magnet:?xt=urn:btih:…",
  "downloaded_at": "2026-10-14T17:28:52+00:00"
}
```

### Torrents

| Method | Path                                           | Description                                                                                                                                                                                                       |
| ------ | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`  | `/torrents/search?q=...&category=all&limit=25` | Searches Prowlarr. `category`: `all`, `movie`, or `tv`. `q` 2–100 chars, `limit` 1–50. Results with no seeders are dropped and the rest are sorted by seeders. Each result has an opaque `ref` valid for one hour |
| `POST` | `/torrents/download`                           | Body: `{ "ref": "<16 hex chars>" }`. Starts a background download and returns `202` with `{ "message", "task_id" }`. `404` if the `ref` expired                                                                   |

Search result:

```json
{
  "ref": "a1b2c3d4e5f60718",
  "title": "…",
  "indexer": "…",
  "size_bytes": 0,
  "seeders": 0,
  "leechers": 0
}
```

### Users (`/users/me`, authenticated)

**Profile**

| Method  | Path                | Description                                                   |
| ------- | ------------------- | ------------------------------------------------------------- |
| `GET`   | `/users/me/profile` | `{ "username", "bio" }`                                       |
| `PATCH` | `/users/me/profile` | Body: `{ "bio": "..." }`                                      |
| `POST`  | `/users/me/pfp`     | Multipart `file` (any `image/*`, max 5 MB). Converted to WebP |
| `GET`   | `/users/me/pfp`     | Own picture, or `default.webp` if none                        |

**Favorites**

| Method   | Path                         | Description                        |
| -------- | ---------------------------- | ---------------------------------- |
| `GET`    | `/users/me/favorites`        | `{ "favorites": [metadata, ...] }` |
| `POST`   | `/users/me/favorites/{hash}` | Adds a favorite (idempotent)       |
| `DELETE` | `/users/me/favorites/{hash}` | Removes it                         |

**Playlists**

| Method   | Path                                    | Description                                                      |
| -------- | --------------------------------------- | ---------------------------------------------------------------- |
| `GET`    | `/users/me/playlists`                   | List with `id`, `name`, `description`, `cover_url`, `item_count` |
| `POST`   | `/users/me/playlists`                   | Body: `{ "name", "description"? }`. Names are unique per user    |
| `GET`    | `/users/me/playlists/{id}`              | Playlist with its ordered items                                  |
| `PATCH`  | `/users/me/playlists/{id}`              | Body: `{ "name"?, "description"? }`                              |
| `DELETE` | `/users/me/playlists/{id}`              | Deletes the playlist and its custom cover                        |
| `POST`   | `/users/me/playlists/{id}/items/{hash}` | Appends an item (idempotent)                                     |
| `DELETE` | `/users/me/playlists/{id}/items/{hash}` | Removes an item                                                  |
| `POST`   | `/users/me/playlists/{id}/cover`        | Multipart `file` (image, max 5 MB)                               |
| `GET`    | `/users/me/playlists/{id}/cover`        | Custom cover                                                     |
| `DELETE` | `/users/me/playlists/{id}/cover`        | Removes the custom cover                                         |

`cover_url` is the custom cover if one exists, otherwise the thumbnail of the first item, otherwise `null`.

**Playback history**

| Method  | Path                       | Description                                           |
| ------- | -------------------------- | ----------------------------------------------------- |
| `PATCH` | `/users/me/history/{hash}` | Body: `{ "position_ms": int }`                        |
| `GET`   | `/users/me/history/{hash}` | `{ "media_hash", "position_ms" }` (0 if never played) |

### Common errors

| Code  | Meaning                                        |
| ----- | ---------------------------------------------- |
| `401` | Missing or invalid token                       |
| `404` | Unknown hash, playlist, task, or expired `ref` |
| `413` | Uploaded image larger than 5 MB                |
| `416` | Invalid byte range                             |
| `502` | Holder or Prowlarr unavailable                 |
| `503` | Prowlarr not configured                        |

## Download pipeline

1. **Search**: Prowlarr, categories Movies (2000) and TV (5000).
2. **Download**: the magnet is handed to aria2 inside its own work directory. The server polls progress and publishes it to Redis.
3. **Duplicate protection**: if the torrent is already in Holder it is skipped; a Redis lock prevents two simultaneous downloads of the same torrent.
4. **Processing**: the largest video file is picked. MP4 files are only renamed; other containers are remuxed (video copied, audio to AAC 192 kbps, `faststart`).
5. **Metadata**: the release name is matched against TMDB (title, year, similarity score). On a confident match the title is cleaned up, alternate titles are stored as search aliases, and the TMDB poster becomes the cover. Otherwise a frame of the video is used.
6. **Upload**: video, cover, and a `metadata.json` go to Holder; the item is registered in SQLite.
7. **Cleanup**: aria2 entries and the work directory are removed.

`GET /videos/progress/{task_id}` returns the Redis hash for the task:

| Field     | Values                                                                     |
| --------- | -------------------------------------------------------------------------- |
| `status`  | `starting`, `downloading`, `processing`, `uploading`, `completed`, `error` |
| `percent` | e.g. `42.5%` (`-1%` on error)                                              |
| `speed`   | e.g. `3.20MiB/s`                                                           |
| `eta`     | e.g. `12:05` or `Unknown`                                                  |
| `detail`  | Error message (only on `error`)                                            |

Task state expires one hour after the last update, or five minutes after it reaches `completed` or `error`.

## Data model

| Table                         | Purpose                                                                                                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `users`                       | Local user, linked to Osiris through `osiris_sub`                                                                                                              |
| `global_media`                | One row per movie: `hash`, `title`, `uploader`, `blobs`, `duration`, `upload_date`, `extractor`, `original_id`, `source_url`, `added_at`, `aliases`, `tmdb_id` |
| `favorites`                   | User ↔ media, unique per pair                                                                                                                                  |
| `playlists`, `playlist_items` | Per-user playlists with ordered items                                                                                                                          |
| `playback_history`            | Last position per user and media                                                                                                                               |
| `audit_logs`                  | Action log                                                                                                                                                     |
| `download_tasks`              | Download task records                                                                                                                                          |
| `global_media_fts`            | FTS5 index over title, uploader, and aliases (kept in sync by triggers)                                                                                        |

`global_media.blobs` maps roles to Holder file ids: `{"media": id, "metadata": id, "thumb": id}`. Every value is a Holder file id and is deleted from Holder when the media is removed, so nothing else should be stored there.

## Project layout

```
core/       configuration, database, audit log
models/     SQLAlchemy models and Pydantic schemas
routers/    catalog, torrents, users, auth dependencies
services/   blobstore client, media repository, torrent fetcher, TMDB matcher, …
scripts/    maintenance scripts (diagnostics, poster backfill)
```

## Maintenance scripts

- `scripts/backfill_posters.py`: replaces the video-frame cover of existing items with the TMDB poster. Dry run by default; pass `--apply` to write. Use `--only "title"` to try a single movie first.
- `scripts/diagnose_aria2_perms.py`: reports file ownership and permissions of an aria2 work directory and tests the exact rename/delete operations the fetcher performs.

## License

OmniTheater is free software, licensed under the **GNU Affero General Public License v3.0 or later** (AGPL-3.0-or-later). See [`LICENSE`](LICENSE).

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Disclaimer

OmniTheater is a tool for managing a personal media library. It does not host, index, or distribute any content. You are solely responsible for what you download, store, or stream with it, and for complying with the laws and copyright rules that apply to you.
