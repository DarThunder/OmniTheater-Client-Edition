# OmniTheater - Personal Offline Movie Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from core.configs import TMDB_API_KEY, TMDB_LANGUAGE

log = logging.getLogger("omnitheater.tmdb_matcher")

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w500"
MIN_SCORE = 0.75
YEAR_MISMATCH_PENALTY = 0.40

_EXT = re.compile(r"\.(mp4|mkv|webm|avi|mov|m4v)$", re.IGNORECASE)
_YEAR_PAREN = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_YEAR_ANY = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_JUNK = re.compile(
    r"(?<![a-z0-9])(?:480p|576p|720p|1080p|1440p|2160p|4k|uhd|blu ray|bluray|brrip|"
    r"bdrip|dvdrip|webrip|web dl|webdl|hdrip|hdtv|x264|x265|h264|h265|hevc|aac\d*|"
    r"ac3|dts|extended|repack|proper|remastered|unrated|imax|yify|yts|rarbg)(?![a-z0-9])",
    re.IGNORECASE,
)

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" -:.,;([{)]}")

def parse_release(name: str) -> tuple[str, int | None, str]:
    s = _EXT.sub("", (name or "").strip())
    s = re.sub(r"[._\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    m = _JUNK.search(s)
    head = s[: m.start()] if m else s
    head = re.sub(r"[\(\[\{\s]+$", "", head)

    year: int | None = None
    title = head
    mp = _YEAR_PAREN.search(head)
    if mp:
        year = int(mp.group(1))
        title = head[: mp.start()] + " " + head[mp.end():]
    else:
        years = [y for y in _YEAR_ANY.finditer(head) if y.start() > 0]
        if years:
            y = years[-1]
            year = int(y.group(1))
            title = head[: y.start()]

    return _clean(title), year, _clean(re.sub(r"[\(\)\[\]]", " ", head))

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()

def _year_of(c: dict) -> int | None:
    rd = c.get("release_date") or ""
    return int(rd[:4]) if rd[:4].isdigit() else None

def _pick(results: list[dict], title: str, year: int | None) -> tuple[dict, float] | None:
    best, best_score = None, 0.0
    for c in results:
        if not c.get("poster_path"):
            continue
        sim = max(
            (_sim(n, title) for n in (c.get("title"), c.get("original_title")) if n),
            default=0.0,
        )
        cy = _year_of(c)
        if year and cy:
            d = abs(cy - year)
            sim += 0.10 if d == 0 else 0.03 if d == 1 else -YEAR_MISMATCH_PENALTY
        sim += min((c.get("popularity") or 0) / 10000, 0.01)
        if sim > best_score:
            best, best_score = c, sim
    if best is not None and best_score >= MIN_SCORE:
        return best, best_score
    return None

def _search(client: httpx.Client, title: str, year: int | None) -> list[dict]:
    params = {
        "api_key": TMDB_API_KEY,
        "query": title,
        "language": TMDB_LANGUAGE,
        "include_adult": "false",
    }
    if year:
        params["year"] = year
    resp = client.get(f"{API}/search/movie", params=params)
    resp.raise_for_status()
    return resp.json().get("results", [])

def match_release(name: str) -> dict | None:
    if not TMDB_API_KEY:
        return None
    title, year, head = parse_release(name)
    if not title:
        return None

    attempts: list[tuple[str, int | None]] = []
    for q, y in ((title, year), (title, None), (head, None)):
        if q and (q, y) not in attempts:
            attempts.append((q, y))

    try:
        with httpx.Client(timeout=10.0) as client:
            for q, y in attempts:
                picked = _pick(_search(client, q, y), q, year)
                if picked:
                    c, score = picked
                    return {
                        "tmdb_id": c["id"],
                        "title": c.get("title") or q,
                        "original_title": c.get("original_title") or "",
                        "year": _year_of(c),
                        "poster_path": c["poster_path"],
                        "score": score,
                    }
    except Exception:
        log.warning("Fallo la consulta a TMDB para %r", name, exc_info=True)
    return None

def download_poster(poster_path: str, dest: Path) -> bool:
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(f"{IMG}/{POSTER_SIZE}{poster_path}")
            resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest.stat().st_size > 0
    except Exception:
        log.warning("No se pudo descargar el poster %s", poster_path, exc_info=True)
        return False

def fetch_poster(poster_path: str, workdir: Path, stem: str = "poster") -> Path | None:
    jpg = workdir / f"{stem}.jpg"
    if not download_poster(poster_path, jpg):
        return None
    webp = workdir / f"{stem}.webp"
    try:
        from PIL import Image

        with Image.open(jpg) as img:
            img.convert("RGB").save(webp, format="WEBP", quality=85)
        return webp if webp.stat().st_size > 0 else None
    except Exception:
        log.warning("No se pudo convertir el poster a webp", exc_info=True)
        return None
    finally:
        jpg.unlink(missing_ok=True)

def build_aliases(match: dict, *extra: str | None) -> str:
    seen: dict[str, None] = {}
    for item in (match.get("title"), match.get("original_title"), *extra):
        for part in (item or "").split(" | "):
            part = part.strip()
            if part and _norm(part) not in {_norm(k) for k in seen}:
                seen[part] = None
    return " | ".join(seen)

def merge_aliases(base: str | None, extra: str | None) -> str:
    base = (base or "").strip()
    low = _norm(base)
    new = [p for p in (extra or "").split(" | ") if p.strip() and _norm(p) not in low]
    if not new:
        return base
    return f"{base} {' | '.join(new)}".strip()