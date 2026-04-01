from __future__ import annotations

import mimetypes
import re
from pathlib import Path


SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def ensure_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def normalize_rel_path(rel_path: str) -> str:
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if not rel_path:
        raise ValueError("path 不能为空")
    if rel_path.startswith("/"):
        raise ValueError("path 必须是相对路径")
    parts = [part for part in rel_path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("path 非法")
    for part in parts:
        if not SAFE_SEGMENT.fullmatch(part):
            raise ValueError(f"非法路径片段: {part}")
    return "/".join(parts)


def resolve_path(root: Path, rel_path: str) -> tuple[Path, str]:
    safe_root = ensure_workspace(root)
    normalized = normalize_rel_path(rel_path)
    target = (safe_root / normalized).resolve()
    if safe_root not in target.parents and target != safe_root:
        raise ValueError("越权路径访问")
    return target, normalized


def file_meta(path: Path, root: Path) -> dict:
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "name": path.name,
        "size": path.stat().st_size,
        "modified_at": int(path.stat().st_mtime),
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def list_files(root: Path, prefix: str = "", limit: int = 50) -> list[dict]:
    safe_root = ensure_workspace(root)
    start_dir = safe_root
    if prefix:
        target, _ = resolve_path(safe_root, prefix)
        if not target.exists():
            return []
        start_dir = target
    candidates = [start_dir] if start_dir.is_file() else sorted(start_dir.rglob("*"))
    results: list[dict] = []
    for path in candidates:
        if path.is_file():
            results.append(file_meta(path, safe_root))
        if len(results) >= limit:
            break
    return results


def read_text_file(root: Path, rel_path: str, max_chars: int = 6000) -> dict:
    target, normalized = resolve_path(root, rel_path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"文件不存在: {normalized}")
    text = target.read_text(encoding="utf-8")
    return {
        "path": normalized,
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def save_text_file(root: Path, rel_path: str, content: str, overwrite: bool = True) -> dict:
    target, normalized = resolve_path(root, rel_path)
    if target.exists() and not overwrite:
        raise ValueError("目标文件已存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": normalized, "size": len(content.encode('utf-8'))}
