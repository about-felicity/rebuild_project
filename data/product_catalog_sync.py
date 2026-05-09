"""
将仓库根目录下 ``产品图/`` 中的图片同步到 ``data/media/_product_catalog/``，
并写入 ``project_assets``（project_id = ``FRAMEOS_SHARED_PROJECT_ID``，library = asset）。

任意真实项目在 ``list_project_assets`` 时会自动合并这些公共素材。
按 ``source_relpath`` 做 upsert，重启后尽量保持同一 ``id``，避免前端素材引用错位。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from data.db import FRAMEOS_SHARED_PROJECT_ID, _connect

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
CATALOG_MEDIA_SUBDIR = "_product_catalog"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _media_root() -> Path:
    return Path(__file__).resolve().parent / "media"


def sync_product_catalog_from_repo(repo_root: Path | None = None) -> int:
    """
    扫描 ``<repo>/产品图`` 下所有图片，复制到可访问目录并 upsert 共享素材表行。
    返回本次磁盘上仍存在的目录内图片数量。
    """
    root = (repo_root or _repo_root()).resolve()
    src = root / "产品图"
    if not src.is_dir():
        return 0

    dest_root = _media_root() / CATALOG_MEDIA_SUBDIR
    dest_root.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    for p in sorted(src.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
            files.append(p)

    rel_set: set[str] = set()
    for path in files:
        try:
            rel_set.add(path.relative_to(src).as_posix())
        except ValueError:
            continue

    with _connect() as conn:
        existing = conn.execute(
            "SELECT id, meta_json FROM project_assets WHERE project_id = ?",
            (FRAMEOS_SHARED_PROJECT_ID,),
        ).fetchall()
        for row in existing:
            meta = json.loads(row["meta_json"] or "{}")
            if not meta.get("frameos_catalog"):
                continue
            sr = meta.get("source_relpath")
            if isinstance(sr, str) and sr and sr not in rel_set:
                conn.execute(
                    "DELETE FROM project_assets WHERE id = ?",
                    (int(row["id"]),),
                )

        n = 0
        for path in files:
            try:
                rel = path.relative_to(src).as_posix()
            except ValueError:
                continue
            digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:24]
            ext = path.suffix.lower() or ".jpg"
            dest_name = f"{digest}{ext}"
            dest = dest_root / dest_name
            shutil.copy2(path, dest)
            uri = f"/media/{CATALOG_MEDIA_SUBDIR}/{dest_name}"
            display = rel if len(rel) <= 120 else "…" + rel[-117:]
            meta = {
                "frameos_catalog": True,
                "source_relpath": rel,
                "shared": True,
            }
            blob = json.dumps(meta, ensure_ascii=False)
            hit = conn.execute(
                """
                SELECT id FROM project_assets
                WHERE project_id = ?
                  AND json_extract(meta_json, '$.source_relpath') = ?
                """,
                (FRAMEOS_SHARED_PROJECT_ID, rel),
            ).fetchone()
            if hit:
                conn.execute(
                    """
                    UPDATE project_assets
                    SET name = ?, uri = ?, meta_json = ?, library = 'asset', kind = 'image'
                    WHERE id = ?
                    """,
                    (display, uri, blob, int(hit["id"])),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO project_assets (project_id, library, kind, name, uri, meta_json)
                    VALUES (?, 'asset', 'image', ?, ?, ?)
                    """,
                    (FRAMEOS_SHARED_PROJECT_ID, display, uri, blob),
                )
            n += 1
        conn.commit()
    return n
