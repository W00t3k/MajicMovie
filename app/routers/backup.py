"""Backup and Restore endpoints for Majic Movie Selector.

GET  /api/backup/download  — download a zip of the full system state
POST /api/restore          — upload a backup zip to restore
GET  /api/backup/status    — last backup info
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

project_root = Path(__file__).resolve().parent.parent.parent
_last_backup: dict | None = None


def _backup_manifest() -> dict:
    return {
        "version": "1.0",
        "app": "Majic Movie Selector",
        "created_at": datetime.now(UTC).isoformat(),
        "files": [],
    }


def _build_backup_zip() -> tuple[bytes, str]:
    """Build a zip archive of all critical system files and return (bytes, filename)."""
    buf = io.BytesIO()
    manifest = _backup_manifest()
    included: list[str] = []

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. SQLite database (memory store)
        db_path = project_root / settings.memory_db_path
        if db_path.exists():
            zf.write(db_path, "data/memory.sqlite")
            included.append("data/memory.sqlite")

        # 2. All data/*.json agent datasets
        data_dir = project_root / "data"
        if data_dir.exists():
            for json_file in sorted(data_dir.glob("*.json")):
                arc_name = f"data/{json_file.name}"
                zf.write(json_file, arc_name)
                included.append(arc_name)

        # 3. .env file (config + API keys)
        env_path = project_root / ".env"
        if env_path.exists():
            zf.write(env_path, ".env")
            included.append(".env")

        # 4. .env.example
        env_example = project_root / ".env.example"
        if env_example.exists():
            zf.write(env_example, ".env.example")
            included.append(".env.example")

        # 5. Manifest
        manifest["files"] = included
        zf.writestr("backup_manifest.json", json.dumps(manifest, indent=2))

    filename = f"majic-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return buf.getvalue(), filename


@router.get("/api/backup/download")
async def download_backup():
    """Download a full system backup as a zip file."""
    global _last_backup
    try:
        zip_bytes, filename = _build_backup_zip()
        _last_backup = {
            "filename": filename,
            "size_bytes": len(zip_bytes),
            "created_at": datetime.now(UTC).isoformat(),
        }
        logger.info("Backup created: %s (%d bytes)", filename, len(zip_bytes))

        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}") from exc


@router.get("/api/backup/status")
async def backup_status():
    """Return info about the last backup and current data sizes."""
    db_path = project_root / settings.memory_db_path
    data_dir = project_root / "data"

    db_size = db_path.stat().st_size if db_path.exists() else 0
    json_count = len(list(data_dir.glob("*.json"))) if data_dir.exists() else 0
    env_exists = (project_root / ".env").exists()

    return {
        "ok": True,
        "last_backup": _last_backup,
        "current": {
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / 1024 / 1024, 2),
            "json_datasets": json_count,
            "env_present": env_exists,
        },
    }


@router.post("/api/restore")
async def restore_backup(file: UploadFile):
    """Restore from a backup zip file. Validates manifest before applying."""
    global _last_backup
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Must upload a .zip backup file")

    try:
        content = await file.read()
        buf = io.BytesIO(content)

        if not zipfile.is_zipfile(buf):
            raise HTTPException(status_code=400, detail="Invalid zip file")

        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()

            # Validate manifest
            if "backup_manifest.json" not in names:
                raise HTTPException(status_code=400, detail="Missing backup_manifest.json — not a valid Majic backup")

            manifest = json.loads(zf.read("backup_manifest.json"))
            if manifest.get("app") != "Majic Movie Selector":
                raise HTTPException(status_code=400, detail="Not a Majic Movie Selector backup")

            # Restore SQLite DB
            if "data/memory.sqlite" in names:
                db_path = project_root / settings.memory_db_path
                db_path.parent.mkdir(parents=True, exist_ok=True)
                # Backup existing DB first
                if db_path.exists():
                    shutil.copy2(db_path, db_path.with_suffix(".sqlite.pre-restore"))
                db_path.write_bytes(zf.read("data/memory.sqlite"))
                logger.info("Restored memory.sqlite")

            # Restore data/*.json
            for name in names:
                if name.startswith("data/") and name.endswith(".json"):
                    dest = project_root / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
            logger.info("Restored %d json datasets", sum(1 for n in names if n.startswith("data/") and n.endswith(".json")))

            # Restore .env
            if ".env" in names:
                env_path = project_root / ".env"
                if env_path.exists():
                    shutil.copy2(env_path, env_path.with_suffix(".env.pre-restore"))
                env_path.write_bytes(zf.read(".env"))
                logger.info("Restored .env")

        return {
            "ok": True,
            "message": "Restore successful. Restart the app to apply all changes.",
            "manifest": manifest,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Restore failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc
