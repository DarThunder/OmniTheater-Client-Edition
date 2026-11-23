# OmniTheater - Personal Offline Movie Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import asyncio
import hashlib
import json
import logging
import uuid

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.audit import log_action
from core.database import get_db, redis_client
from models.db import User
from routers.dependencies import get_current_user
from services.fetcher import download_torrent, search_prowlarr

log = logging.getLogger("omnicast.torrents")

RESULT_TTL = 3600
TASK_TTL = 3600

router = APIRouter(prefix="/torrents", tags=["Torrents"])


class TorrentDownload(BaseModel):
    ref: str = Field(..., pattern=r"^[0-9a-f]{16}$")


def _store_result(item: dict) -> str:
    ref = hashlib.sha1(item["uri"].encode("utf-8")).hexdigest()[:16]
    try:
        redis_client.set(f"torrent:{ref}", json.dumps(item), ex=RESULT_TTL)
    except Exception:
        log.warning("No se pudo cachear el resultado de torrent", exc_info=True)
    return ref


@router.get("/search")
async def search_torrents(
    q: str = Query(..., min_length=2, max_length=100),
    category: str = Query("all", pattern="^(all|movie|tv)$"),
    limit: int = Query(25, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = q.strip()
    try:
        raw = await asyncio.to_thread(search_prowlarr, query, category, limit)
    except RuntimeError as e:
        log.error("Prowlarr sin configurar: %s", e)
        raise HTTPException(status_code=503, detail="Búsqueda de torrents no configurada")
    except httpx.HTTPError as e:
        log.error("Fallo consultando Prowlarr: %s", e)
        raise HTTPException(status_code=502, detail="Prowlarr no disponible")

    results = []
    for item in raw:
        ref = _store_result(item)
        results.append({
            "ref": ref,
            "title": item["title"],
            "indexer": item["indexer"],
            "size_bytes": item["size_bytes"],
            "seeders": item["seeders"],
            "leechers": item["leechers"],
        })

    log_action(db, user.id, "TORRENT_SEARCH", f"Query: '{query}' Category: {category}")
    return {"results": results}


@router.post("/download", status_code=202)
def start_torrent_download(
    data: TorrentDownload,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw = redis_client.get(f"torrent:{data.ref}")
    if not raw:
        raise HTTPException(status_code=404, detail="Resultado expirado, vuelve a buscar")
    item = json.loads(raw)

    task_id = str(uuid.uuid4())
    redis_client.hset(f"task:{task_id}", mapping={
        "status": "starting",
        "percent": "0%",
        "speed": "0KiB/s",
        "eta": "Unknown",
    })
    redis_client.expire(f"task:{task_id}", TASK_TTL)

    background_tasks.add_task(
        asyncio.to_thread,
        download_torrent,
        item["uri"],
        item["title"],
        item["indexer"],
        task_id,
    )

    log_action(db, user.id, "DOWNLOAD_TORRENT_START", f"Title: {item['title']}")
    return {
        "message": "Descarga iniciada en segundo plano",
        "task_id": task_id,
    }