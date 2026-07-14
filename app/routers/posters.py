from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import FileResponse

from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

_POSTERS_DIR = Path("data/posters")
# One generation at a time - FLUX is memory-hungry
_generate_lock = asyncio.Lock()
# slug -> "generating" | "done" | "error: ..."
_jobs: dict[str, str] = {}


def _slugify(title: str, year: int | None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return f"{base}-{year}" if year else base


def _mflux_available() -> bool:
    return Path(settings.mflux_bin).exists()


class PosterRequest(BaseModel):
    title: str
    year: int | None = None
    genres: list[str] = []


@router.get("/api/posters/status")
async def posters_status() -> dict:
    return {"available": _mflux_available(), "jobs": dict(_jobs)}


@router.get("/api/posters/job/{slug}")
async def poster_job(slug: str) -> dict:
    state = _jobs.get(slug)
    out = _POSTERS_DIR / f"{slug}.png"
    if out.exists():
        return {"status": "done", "poster_url": f"/api/posters/file/{slug}.png"}
    return {"status": state or "unknown"}


@router.get("/api/posters/file/{filename}")
async def poster_file(filename: str) -> FileResponse:
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Bad filename")
    path = _POSTERS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Poster not found")
    return FileResponse(path, media_type="image/png")


async def _run_generation(slug: str, prompt: str) -> None:
    out = _POSTERS_DIR / f"{slug}.png"
    async with _generate_lock:
        if out.exists():
            _jobs[slug] = "done"
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.mflux_bin,
                "--model", settings.mflux_model,
                "--prompt", prompt,
                "--steps", str(settings.mflux_steps),
                "-q", "8",
                "--width", "512",
                "--height", "768",
                "--output", str(out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            # Generous timeout: first run downloads model weights (~34GB)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=7200)
            if proc.returncode != 0 or not out.exists():
                msg = (stderr or b"").decode(errors="replace")[-300:]
                logger.warning("mflux failed for %s: %s", slug, msg)
                _jobs[slug] = "error: generation failed"
                return
            _jobs[slug] = "done"
        except asyncio.TimeoutError:
            _jobs[slug] = "error: timed out"
        except Exception as exc:  # noqa: BLE001
            logger.warning("mflux error for %s: %s", slug, exc)
            _jobs[slug] = f"error: {exc}"


@router.post("/api/posters/generate")
async def generate_poster(payload: PosterRequest) -> dict:
    if not _mflux_available():
        raise HTTPException(status_code=503, detail="mflux not installed")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title required")

    slug = _slugify(title, payload.year)
    out = _POSTERS_DIR / f"{slug}.png"
    if out.exists():
        return {"status": "done", "poster_url": f"/api/posters/file/{slug}.png"}
    if _jobs.get(slug) == "generating":
        return {"status": "generating", "slug": slug}

    _POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    genre_text = ", ".join(payload.genres[:3]) if payload.genres else "drama"
    year_text = f", {payload.year}s era" if payload.year else ""
    prompt = (
        f"Cinematic movie poster for \"{title}\"{year_text}, {genre_text} film, "
        f"dramatic lighting, painted illustration style, bold title typography, no text artifacts"
    )
    _jobs[slug] = "generating"
    asyncio.get_running_loop().create_task(_run_generation(slug, prompt))
    return {"status": "generating", "slug": slug}
