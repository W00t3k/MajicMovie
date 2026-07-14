from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _whisper_available() -> tuple[str | None, Path | None]:
    binary = shutil.which(settings.whisper_bin)
    model = settings.whisper_model_path
    return binary, model if model.exists() else None


@router.get("/api/transcribe/status")
async def transcribe_status() -> dict:
    binary, model = _whisper_available()
    return {
        "available": bool(binary and model),
        "binary": binary,
        "model": str(model) if model else None,
    }


@router.post("/api/transcribe")
async def transcribe_audio(audio: UploadFile) -> dict:
    binary, model = _whisper_available()
    if not binary or not model:
        raise HTTPException(status_code=503, detail="whisper.cpp not available (binary or model missing)")

    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio upload too large")

    with tempfile.TemporaryDirectory(prefix="majic-whisper-") as tmp:
        src = Path(tmp) / (audio.filename or "input.webm")
        wav = Path(tmp) / "input.wav"
        src.write_bytes(raw)

        # Browser MediaRecorder sends webm/ogg; whisper.cpp wants 16kHz mono wav
        ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, ffmpeg_err = await ffmpeg.communicate()
        if ffmpeg.returncode != 0 or not wav.exists():
            logger.warning("ffmpeg failed: %s", (ffmpeg_err or b"").decode(errors="replace")[-300:])
            raise HTTPException(status_code=422, detail="Could not decode audio")

        proc = await asyncio.create_subprocess_exec(
            binary, "-m", str(model), "-f", str(wav), "-nt", "--no-prints",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail="Transcription timed out")

        if proc.returncode != 0:
            logger.warning("whisper failed: %s", (stderr or b"").decode(errors="replace")[-300:])
            raise HTTPException(status_code=500, detail="Transcription failed")

    text = " ".join(stdout.decode(errors="replace").split()).strip()
    return {"text": text}
