"""
将 ``run_generate_image`` / ``run_generate_video`` 的**文本结果**解析并写入 ``project_assets``。

**功能**：
- 图：成功时为 JSON 字符串，含 ``created_asset_ids``（本地 Hub 下多为图片 URL）。
- 视频：成功时为 JSON 字符串，``ok`` 非 false 且含 ``created_asset_ids``（多为视频 URL）。

**不处理**：以 ``Error:`` 开头的生图失败明文；JSON 解析失败；``ok: false`` 的视频校验拒绝。

**输入 / 输出**
- ``destination_to_app_library(dest) -> str``：``storyboard_library`` → ``storyboard``，其余合法 destination → ``asset``。
- ``persist_image_tool_output(...)``：展示名 ``{摘要}_{文件主名六位}``；分镜库首条入库时返回镜像素材库所需字段，否则 ``None``（镜像在视频开始时写入）。
- ``insert_storyboard_asset_library_mirror(...)``：视频开始生成时把分镜图写入「素材库」。
- ``persist_video_tool_output(..., summary_base=..., video_prompt=...) -> None``：无返回；视频库展示名优先取「【用户原文】」段，去掉前端追加的「（已引用…）」脚注，**不**再拼 ``_YYYYMMDD_HHMMSS``；过长截断；无可用文本时退化为 ``video_prompt`` 或 ``{摘要}_{六位}``。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from data.db import insert_project_asset
from data.media_mirror import mirror_http_url_to_local
from runtime_ctx import agent_chat_user_message_for_model


def _sanitize_summary_text(s: str, max_len: int = 36) -> str:
    t = re.sub(r"[\s\r\n\t]+", " ", (s or "").strip())
    t = re.sub(r'[<>:"/\\|?*]', "", t)
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t or "图"


def _sanitize_full_asset_name_text(s: str) -> str:
    """去掉路径非法字符与控制符；**不按长度截断**，保留发给 Agent 的全文。"""
    t = (s or "").strip()
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", t)
    t = re.sub(r'[<>:"/\\|?*]', "", t)
    t = re.sub(r"[\t\r\n]+", " ", t)
    t = re.sub(r" +", " ", t).strip()
    return t


# 前端 workflow-ui 发送 Agent 时在文末追加，便于解析 asset_id；视频库展示名应剔除。
_REF_NOTE_TAIL_RE = re.compile(r"\s*（已引用\s*\d+\s*张[^）]*）\s*$", re.DOTALL)

_TS_SUFFIX_RE = re.compile(r"_\d{8}_\d{6}\s*$")

_MAX_VIDEO_DISPLAY_NAME_CHARS = 200


def _user_plain_for_video_display_name(full_model_user: str) -> str:
    """从发往模型的整段 user 中取出「用户原文」并去掉引用脚注与误拼的时间后缀。"""
    t = (full_model_user or "").strip()
    if not t:
        return ""
    mk = "【用户原文】"
    if mk in t:
        t = t.split(mk, 1)[-1].strip()
    t = _REF_NOTE_TAIL_RE.sub("", t).strip()
    t = _TS_SUFFIX_RE.sub("", t).strip()
    return t


def _clip_video_display_name(s: str, max_len: int = _MAX_VIDEO_DISPLAY_NAME_CHARS) -> str:
    u = (s or "").strip()
    if len(u) <= max_len:
        return u
    return u[: max_len - 1] + "…"


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


def insert_storyboard_asset_library_mirror(
    project_id: str,
    *,
    storyboard_library_asset_id: int,
    stored: str,
    display_name: str,
    meta_base: dict,
) -> int:
    """分镜已入「分镜库」后，在视频开始生成时写入「素材库」同名图（``·首帧`` 后缀）。"""
    asset_meta = {
        **meta_base,
        "from_agent_storyboard": True,
        "paired_storyboard_library_asset_id": storyboard_library_asset_id,
    }
    aid2 = insert_project_asset(
        project_id,
        "asset",
        "image",
        f"{display_name} ·首帧",
        stored,
        meta=asset_meta,
    )
    print(
        f"[FrameOS] persist image (asset mirror on video start): project_id={project_id!r} "
        f"asset_id={aid2} uri={stored[:160]!r}",
        flush=True,
    )
    return aid2


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
) -> dict[str, Any] | None:
    """
    **接受**：``run_generate_image`` 返回的整段字符串（成功为 JSON，失败可能 ``Error: ...``）。
    **写出**：每个 ``created_asset_ids`` 元素一行 ``project_assets``（library/kind/uri/meta）。
    ``summary_base``：优先来自工具 ``asset_name``，否则 ``user_query``，用于展示名 ``{摘要}_{本地文件前6位}``。
    **返回**：仅当本次写入了「分镜库」首条图时，返回镜像素材库所需字段；否则 ``None``（镜像延后到视频开始时插入）。
    """
    t = result_text.strip()
    if t.startswith("Error:"):
        return None
    try:
        data = json.loads(result_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    created = data.get("created_asset_ids") or []
    if not created:
        return None
    lib = destination_to_app_library(destination)
    kind = "storyboard" if lib == "storyboard" else "image"
    summary = _sanitize_summary_text(summary_base)
    storyboard_mirror_pending: dict[str, Any] | None = None
    for item in created:
        uri = str(item).strip()
        local = mirror_http_url_to_local(project_id, uri)
        stored = local or uri
        meta: dict = {"destination": destination, "task_id": data.get("task_id")}
        if local:
            meta["remote_uri"] = uri
        stem6 = _stem6_from_stored_uri(stored)
        display_name = f"{summary}_{stem6}"
        aid = insert_project_asset(
            project_id,
            lib,
            kind,
            display_name,
            stored,
            meta=meta,
        )
        print(
            f"[FrameOS] persist image: project_id={project_id!r} library={lib!r} kind={kind!r} "
            f"asset_id={aid} uri={stored[:160]!r}",
            flush=True,
        )
        if lib == "storyboard" and storyboard_mirror_pending is None:
            storyboard_mirror_pending = {
                "storyboard_library_asset_id": aid,
                "stored": stored,
                "display_name": display_name,
                "meta_base": dict(meta),
            }
    return storyboard_mirror_pending


def persist_video_tool_output(
    project_id: str,
    result_text: str,
    *,
    summary_base: str = "",
    video_prompt: str = "",
) -> None:
    """
    **接受**：``run_generate_video`` 返回的 JSON 字符串（含业务错误或成功列表）。
    **写出**：``library=video``；展示名优先为「【用户原文】」经清洗与截断后的文本（去掉「已引用…」脚注，不附加时间戳）；
    否则退化为 ``video_prompt`` 或 ``{摘要}_{文件主名六位}``。
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

    time_s = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_ctx = (agent_chat_user_message_for_model.get() or "").strip()
    plain = _user_plain_for_video_display_name(raw_ctx)
    vp = (video_prompt or "").strip()

    if plain:
        base = _sanitize_full_asset_name_text(plain)
        fixed_display = _clip_video_display_name(base) if base else f"视频_{time_s}"
        use_fixed = True
    elif raw_ctx:
        t2 = _REF_NOTE_TAIL_RE.sub("", raw_ctx).strip()
        t2 = _TS_SUFFIX_RE.sub("", t2).strip()
        base = _sanitize_full_asset_name_text(t2)
        fixed_display = _clip_video_display_name(base) if base else f"视频_{time_s}"
        use_fixed = True
    elif vp:
        base = _sanitize_full_asset_name_text(_user_plain_for_video_display_name(vp) or vp)
        fixed_display = _clip_video_display_name(base) if base else f"视频_{time_s}"
        use_fixed = True
    else:
        use_fixed = False
        summary = _sanitize_summary_text(summary_base or "视频", max_len=32)

    for item in created:
        uri = str(item).strip()
        local = mirror_http_url_to_local(project_id, uri)
        stored = local or uri
        meta: dict = {"task_id": data.get("task_id")}
        if local:
            meta["remote_uri"] = uri
        for k in ("source_image_used", "reference_image_urls_requested"):
            if k in data and data.get(k) is not None:
                meta[k] = data[k]
        stem6 = _stem6_from_stored_uri(stored)
        display_name = fixed_display if use_fixed else f"{summary}_{stem6}"
        insert_project_asset(
            project_id,
            "video",
            "video",
            display_name,
            stored,
            meta=meta,
        )
