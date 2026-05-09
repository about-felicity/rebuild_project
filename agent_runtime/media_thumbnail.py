"""
本地 ``/media/`` 映射目录下文件的缩略图：磁盘缓存 + Pillow；视频用 ffmpeg 抽帧（可选）。
供 ``GET /api/assets/thumbnail/`` 使用。
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image, ImageDraw


def _cache_root(media_dir: Path) -> Path:
    d = (media_dir / "cache" / "thumbs").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_src_to_file(
    src: str,
    route_roots: Sequence[tuple[str, Path]],
) -> Path | None:
    """
    将查询参数 ``src``（``/media/...``、``/sample-assets/...`` 或同源完整 URL 的 path）
    解析为已存在文件路径；``route_roots`` 为 ``(URL 前缀, 磁盘根目录)`` 列表。
    """
    raw = (src or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = unquote(urlparse(raw).path or "")
    else:
        raw = unquote(raw)
    if not raw.startswith("/"):
        raw = "/" + raw
    for prefix, root in route_roots:
        if not raw.startswith(prefix):
            continue
        rel = raw[len(prefix) :].lstrip("/").replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            return None
        root_r = root.resolve()
        full = (root_r / rel).resolve()
        try:
            full.relative_to(root_r)
        except ValueError:
            return None
        if full.is_file():
            return full
    return None


def _thumb_cache_path(
    media_dir: Path, source: Path, mtime_ns: int, size: int, w: int, h: int, kind: str
) -> Path:
    payload = f"{source.resolve()}|{mtime_ns}|{size}|{w}|{h}|{kind}"
    hkey = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
    base = _cache_root(media_dir)
    out_dir = base / hkey[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{hkey}.jpg"


def _image_to_jpeg_bytes(im: Image.Image, quality: int = 82) -> bytes:
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _resize_cover(im: Image.Image, w: int, h: int) -> Image.Image:
    im = im.copy()
    im.thumbnail((max(w, 1), max(h, 1)), Image.Resampling.LANCZOS)
    cw, ch = im.size
    if cw < w or ch < h:
        ratio = max(w / cw, h / ch)
        nw, nh = int(cw * ratio) + 1, int(ch * ratio) + 1
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
        cw, ch = im.size
    left = max(0, (cw - w) // 2)
    top = max(0, (ch - h) // 2)
    return im.crop((left, top, left + w, top + h))


def thumbnail_from_image_file(source: Path, w: int, h: int) -> bytes:
    with Image.open(source) as im:
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        out = _resize_cover(bg, w, h)
    return _image_to_jpeg_bytes(out)


def _video_frame_ffmpeg(source: Path, w: int, h: int) -> bytes | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    out_p: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            out_p = Path(tmp.name)
        vf = f"scale={max(w, 1)}:{max(h, 1)}:force_original_aspect_ratio=decrease,pad={max(w, 1)}:{max(h, 1)}:(ow-iw)/2:(oh-ih)/2"
        r = subprocess.run(
            [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "0.2",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                vf,
                "-q:v",
                "3",
                str(out_p),
            ],
            capture_output=True,
            timeout=90,
        )
        if r.returncode != 0 or not out_p.is_file() or out_p.stat().st_size == 0:
            out_p.unlink(missing_ok=True)
            return None
        data = out_p.read_bytes()
        out_p.unlink(missing_ok=True)
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            out = _resize_cover(im, w, h)
        return _image_to_jpeg_bytes(out)
    except (OSError, subprocess.SubprocessError, Image.UnidentifiedImageError):
        if out_p is not None:
            try:
                out_p.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def placeholder_video_jpeg(w: int, h: int) -> bytes:
    return _placeholder_media_jpeg(w, h, label="VIDEO", play_glyph=True)


def _placeholder_media_jpeg(w: int, h: int, *, label: str, play_glyph: bool) -> bytes:
    w, h = max(min(w, 800), 1), max(min(h, 800), 1)
    im = Image.new("RGB", (w, h), (32, 32, 38))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([4, 4, w - 5, h - 5], radius=8, outline=(90, 90, 100), width=2)
    if play_glyph:
        dr.polygon(
            [(w // 2 - 18, h // 2 - 28), (w // 2 - 18, h // 2 + 28), (w // 2 + 28, h // 2)],
            fill=(200, 200, 210),
        )
    dr.text((8, h - 28), label[:12], fill=(140, 140, 150))
    return _image_to_jpeg_bytes(im)


def get_or_create_thumbnail(
    media_dir: Path,
    source: Path,
    w: int,
    h: int,
    kind: str,
) -> bytes:
    """
    返回 JPEG 字节；命中 ``media_dir/cache/thumbs`` 则直读。
    """
    w = max(1, min(int(w), 800))
    h = max(1, min(int(h), 800))
    st = source.stat()
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    size = int(st.st_size)
    cache_p = _thumb_cache_path(media_dir, source, mtime_ns, size, w, h, kind)
    if cache_p.is_file():
        return cache_p.read_bytes()

    if kind == "video":
        blob = _video_frame_ffmpeg(source, w, h)
        if not blob:
            blob = placeholder_video_jpeg(w, h)
    else:
        try:
            blob = thumbnail_from_image_file(source, w, h)
        except (OSError, Image.UnidentifiedImageError, ValueError):
            blob = _placeholder_media_jpeg(w, h, label="IMAGE", play_glyph=False)

    cache_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.replace(cache_p)
    return blob
