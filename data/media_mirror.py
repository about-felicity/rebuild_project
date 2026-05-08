"""把生图/生视频返回的 http(s) URL 落到本地 ``data/media/``，供 ``GET /media/...`` 访问。"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

MEDIA_ROOT = Path(__file__).resolve().parent / "media"

_CT_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


def _safe_project_segment(project_id: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (project_id or "").strip())[:80]
    return s or "default"


def _ext_from(url: str, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    path = urlparse(url).path.lower()
    for e in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov"):
        if path.endswith(e):
            return e
    return ".bin"


def mirror_http_url_to_local(project_id: str, remote_url: str) -> str | None:
    """
    下载远程资源到 ``data/media/<safe(project_id)>/``。
    **返回**：``/media/<segment>/<uuid>.<ext>``；非 http(s) 或失败时返回 ``None``。
    """
    u = (remote_url or "").strip()
    if not u.startswith(("http://", "https://")):
        return None
    seg = _safe_project_segment(project_id)
    dest_dir = MEDIA_ROOT / seg
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        req = Request(u, headers={"User-Agent": "FrameOS/1.0"})
        with urlopen(req, timeout=180) as resp:
            ctype = resp.headers.get("Content-Type")
            ext = _ext_from(u, ctype)
            name = f"{uuid.uuid4().hex}{ext}"
            out = dest_dir / name
            with open(out, "wb") as f:
                shutil.copyfileobj(resp, f)
        return f"/media/{seg}/{name}"
    except Exception:
        return None
