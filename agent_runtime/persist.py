"""
将 ``run_generate_image`` / ``run_generate_video`` 的**文本结果**解析并写入 ``project_assets``。

**功能**：
- 图：成功时为 JSON 字符串，含 ``created_asset_ids``（本地 Hub 下多为图片 URL）。
- 视频：成功时为 JSON 字符串，``ok`` 非 false 且含 ``created_asset_ids``（多为视频 URL）。

**不处理**：以 ``Error:`` 开头的生图失败明文；JSON 解析失败；``ok: false`` 的视频校验拒绝。

**输入 / 输出**
- ``destination_to_app_library(dest) -> str``：``storyboard_library`` → ``storyboard``，其余合法 destination → ``asset``。
- ``persist_image_tool_output(..., summary_base=...) -> None``：无返回；展示名 ``{摘要}_{文件主名六位}``。
- ``persist_video_tool_output(..., summary_base=...) -> None``：无返回；规则同上。
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from data.db import insert_project_asset
from data.media_mirror import mirror_http_url_to_local


def _sanitize_summary_text(s: str, max_len: int = 36) -> str:
    t = re.sub(r"[\s\r\n\t]+", " ", (s or "").strip())
    t = re.sub(r'[<>:"/\\|?*]', "", t)
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t or "图"


def _stem6_from_stored_uri(stored: str) -> str:
    """本地路径 ``/media/.../uuid.png`` 或 URL 路径最后一段：取主文件名内字母数字的前 6 位（小写）。"""
    s = (stored or "").strip()
    if not s:
        return "asset"
    if s.startswith("/"):
        part = s.rstrip("/").split("/")[-1]
    elif s.startswith(("http://", "https://")):
        part = urlparse(s).path.rsplit("/", 1)[-1] or ""
    else:
        part = s.rsplit("/", 1)[-1]
    raw = part.rsplit(".", 1)[0] if part else ""
    alnum = re.sub(r"[^a-zA-Z0-9]", "", raw)
    if len(alnum) >= 6:
        return alnum[:6].lower()
    if alnum:
        return (alnum + "000000")[:6].lower()
    return "file"


def destination_to_app_library(destination: str) -> str:
    """generation_tools 的 destination → SQLite ``project_assets.library``（前端的库维度）."""
    if destination == "storyboard_library":
        return "storyboard"
    return "asset"


def persist_image_tool_output(
    project_id: str,
    destination: str,
    result_text: str,
    *,
    summary_base: str = "",
) -> None:
    """
    **接受**：``run_generate_image`` 返回的整段字符串（成功为 JSON，失败可能 ``Error: ...``）。
    **写出**：每个 ``created_asset_ids`` 元素一行 ``project_assets``（library/kind/uri/meta）。
    ``summary_base``：优先来自工具 ``asset_name``，否则 ``user_query``，用于展示名 ``{摘要}_{本地文件前6位}``。
    """
    t = result_text.strip()
    if t.startswith("Error:"):
        return
    try:
        data = json.loads(result_text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    created = data.get("created_asset_ids") or []
    if not created:
        return
    lib = destination_to_app_library(destination)
    kind = "storyboard" if lib == "storyboard" else "image"
    summary = _sanitize_summary_text(summary_base)
    for item in created:
        uri = str(item).strip()
        local = mirror_http_url_to_local(project_id, uri)
        stored = local or uri
        meta: dict = {"destination": destination, "task_id": data.get("task_id")}
        if local:
            meta["remote_uri"] = uri
        stem6 = _stem6_from_stored_uri(stored)
        display_name = f"{summary}_{stem6}"
        insert_project_asset(
            project_id,
            lib,
            kind,
            display_name,
            stored,
            meta=meta,
        )


def persist_video_tool_output(
    project_id: str,
    result_text: str,
    *,
    summary_base: str = "",
) -> None:
    """
    **接受**：``run_generate_video`` 返回的 JSON 字符串（含业务错误或成功列表）。
    **写出**：``library=video``；展示名 ``{摘要}_{文件主名六位}``。
    """
    try:
        data = json.loads(result_text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    if data.get("ok") is False:
        return
    created = data.get("created_asset_ids") or []
    if not created:
        return
    summary = _sanitize_summary_text(summary_base or "视频", max_len=32)
    for item in created:
        uri = str(item).strip()
        local = mirror_http_url_to_local(project_id, uri)
        stored = local or uri
        meta: dict = {"task_id": data.get("task_id")}
        if local:
            meta["remote_uri"] = uri
        stem6 = _stem6_from_stored_uri(stored)
        display_name = f"{summary}_{stem6}"
        insert_project_asset(
            project_id,
            "video",
            "video",
            display_name,
            stored,
            meta=meta,
        )
