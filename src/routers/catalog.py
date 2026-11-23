# OmniTheater - Personal Offline Movie Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from core.database import get_db, redis_client
from models.db import GlobalMedia, PlaybackHistory, User
from routers.dependencies import get_current_user
from services import media_repo
from services.blobstore import BlobError, STREAM_URL_TTL, blob_client, stream_body

log = logging.getLogger("omnitheater.catalog")

_PASSTHROUGH = ("content-length", "content-range", "accept-ranges")
_THUMB_CACHE = "public, max-age=86400"
_FINISHED_RATIO = 0.95

router = APIRouter(tags=["Catalog"])


class _BlobStreamingResponse(StreamingResponse):
    def __init__(self, upstream, **kwargs):
        super().__init__(stream_body(upstream), **kwargs)
        self._upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._upstream.close()


def _blob_failure(e: BlobError, what: str) -> Response:
    if e.status_code == 416:
        headers = {}
        if cr := e.headers.get("content-range"):
            headers["Content-Range"] = cr
        return Response(status_code=416, headers=headers)
    if e.status_code == 404:
        log.warning("%s: el holder no tiene el blob referenciado en la BD (%s)", what, e)
        raise HTTPException(status_code=404, detail=f"{what} no encontrado")
    log.error("%s: fallo del blob storage: %s", what, e)
    raise HTTPException(status_code=502, detail="Almacenamiento no disponible")


def _proxy_blob(file_id: int, request: Request, mime: str, what: str,
                extra_headers: dict | None = None) -> Response:
    try:
        upstream = blob_client.open(file_id, request.headers.get("range"))
    except BlobError as e:
        return _blob_failure(e, what)

    headers = {k: v for k in _PASSTHROUGH if (v := upstream.headers.get(k))}
    headers.setdefault("accept-ranges", "bytes")
    if upstream.status_code == 206:
        headers["cache-control"] = "no-store"
    if extra_headers:
        headers.update(extra_headers)

    return _BlobStreamingResponse(
        upstream,
        status_code=upstream.status_code,
        media_type=mime,
        headers=headers,
    )


@router.get("/thumbnail/{hash}")
def get_thumbnail(hash: str, request: Request):
    file_id = media_repo.get_thumbnail_blob_id(hash)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Thumbnail no encontrado")
    return _proxy_blob(
        file_id, request, "image/webp", "Thumbnail",
        extra_headers={"cache-control": _THUMB_CACHE},
    )


@router.get("/feed", dependencies=[Depends(get_current_user)])
def get_feed(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    feed, has_more = media_repo.feed_page(skip, limit)
    return {
        "feed": feed,
        "next_skip": skip + len(feed) if has_more else None,
    }


@router.get("/media/{hash}", dependencies=[Depends(get_current_user)])
def get_media(hash: str):
    meta = media_repo.get_metadata(hash)
    if meta is None:
        raise HTTPException(status_code=404, detail="Contenido no encontrado")
    return meta


@router.get("/search", dependencies=[Depends(get_current_user)])
def search_media(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    clean = "".join(c for c in q if c.isalnum() or c.isspace())
    words = [w for w in re.split(r"\s+", clean.strip()) if w]
    if not words:
        return {"results": []}

    query = db.query(GlobalMedia).filter(GlobalMedia.blobs.isnot(None))
    for word in words:
        like = f"%{word}%"
        query = query.filter(or_(
            GlobalMedia.title.ilike(like),
            GlobalMedia.aliases.ilike(like),
            GlobalMedia.uploader.ilike(like),
        ))
    rows = query.order_by(desc(GlobalMedia.added_at)).limit(limit).all()
    return {"results": [media_repo.to_metadata(r) for r in rows]}


@router.get("/history/continue")
def continue_watching(
    limit: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(PlaybackHistory)
        .filter(PlaybackHistory.user_id == user.id, PlaybackHistory.position_ms > 0)
        .order_by(desc(PlaybackHistory.updated_at))
        .limit(limit * 2)
        .all()
    )
    metas = media_repo.get_metadata_many([r.media_hash for r in rows])

    items = []
    for r in rows:
        meta = metas.get(r.media_hash)
        if meta is None:
            continue
        duration_ms = (meta["duration"] or 0) * 1000
        if duration_ms and r.position_ms >= duration_ms * _FINISHED_RATIO:
            continue
        items.append({**meta, "position_ms": r.position_ms})
        if len(items) >= limit:
            break
    return {"items": items}


@router.get("/video/{hash}/url", dependencies=[Depends(get_current_user)])
def get_video_url(hash: str):
    file_id = media_repo.get_media_blob_id(hash)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Video no encontrado")
    try:
        url = blob_client.stream_url(file_id)
    except BlobError as e:
        return _blob_failure(e, "Video")
    return {"url": url, "expires_in": STREAM_URL_TTL // 2}


@router.get("/video/{hash}", dependencies=[Depends(get_current_user)])
def get_video_redirect(hash: str):
    file_id = media_repo.get_media_blob_id(hash)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Video no encontrado")
    try:
        url = blob_client.stream_url(file_id)
    except BlobError as e:
        return _blob_failure(e, "Video")
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


@router.get("/videos/progress/{task_id}", dependencies=[Depends(get_current_user)])
def get_download_progress(task_id: str):
    task_data = redis_client.hgetall(f"task:{task_id}")
    if not task_data:
        raise HTTPException(status_code=404, detail="Tarea no encontrada o expirada")
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in task_data.items()
    }
