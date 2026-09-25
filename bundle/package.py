"""manifest.json + bundle.zip (plan D12, D18).

The manifest lists every deliverable with size, sha256, duration and status
(ok / failed / skipped + reason), so n8n (or a person) can verify the zip and
see what is missing and why. mp4 files are STORED (video doesn't compress; a
deflate pass would only burn CPU); text, JSON and the PDF are DEFLATED.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

from core.models import ArtifactInfo

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def fill_file_info(job_dir: Path, artifacts: dict[str, ArtifactInfo], hashes: bool = True) -> None:
    """bytes (+ sha256) for every ok artifact; an ok artifact whose file is
    missing is marked failed (it would otherwise be promised but absent)."""
    for info in artifacts.values():
        if info.status != "ok":
            continue
        path = job_dir / info.path
        if not path.is_file():
            info.status, info.reason = "failed", "file missing after rendering"
            continue
        info.bytes = path.stat().st_size
        if hashes:
            info.sha256 = sha256_file(path)


def write_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_bundle(job_dir: Path, artifacts: dict[str, ArtifactInfo], dest: Path, extra: list[str] = ()) -> Path:
    """Zip every ok artifact (at its relative path) plus `extra` relative
    paths (manifest.json). Written to a temp name and renamed, so a
    half-written zip is never served."""
    tmp = dest.with_suffix(".zip.tmp")
    names = [info.path for info in artifacts.values() if info.status == "ok"] + list(extra)
    try:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
            for rel in names:
                src = job_dir / rel
                if not src.is_file():
                    continue
                method = (zipfile.ZIP_STORED if src.suffix.lower() in (".mp4", ".m4a", ".zip")
                          else zipfile.ZIP_DEFLATED)
                zf.write(src, arcname=rel.replace("\\", "/"), compress_type=method)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)  # a half-written zip can be GBs
    return dest
