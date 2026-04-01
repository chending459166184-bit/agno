from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from app.execution.schemas import ExecutionArtifact


@dataclass(slots=True)
class FileSnapshot:
    relative_path: str
    size_bytes: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_files(root: Path) -> dict[str, FileSnapshot]:
    if not root.exists():
        return {}
    snapshots: dict[str, FileSnapshot] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        snapshots[rel] = FileSnapshot(
            relative_path=rel,
            size_bytes=path.stat().st_size,
            sha256=_sha256(path),
        )
    return snapshots


def detect_artifacts(
    before: dict[str, FileSnapshot],
    after_root: Path,
) -> list[ExecutionArtifact]:
    after = snapshot_files(after_root)
    artifacts: list[ExecutionArtifact] = []
    for rel, item in after.items():
        existing = before.get(rel)
        if existing and existing.sha256 == item.sha256:
            continue
        artifacts.append(
            ExecutionArtifact(
                relative_path=rel,
                size_bytes=item.size_bytes,
                mime_type=mimetypes.guess_type(rel)[0] or "application/octet-stream",
            )
        )
    artifacts.sort(key=lambda item: item.relative_path)
    return artifacts


def read_artifact_payload(root: Path, rel_path: str) -> dict:
    target = (root / rel_path).resolve()
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel_path)
    if root.resolve() not in target.parents:
        raise ValueError("artifact path 越界")
    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/xml",
    }:
        return {
            "relative_path": rel_path,
            "mime_type": mime_type,
            "encoding": "utf-8",
            "content": target.read_text(encoding="utf-8"),
        }
    return {
        "relative_path": rel_path,
        "mime_type": mime_type,
        "encoding": "binary",
        "size_bytes": target.stat().st_size,
    }
