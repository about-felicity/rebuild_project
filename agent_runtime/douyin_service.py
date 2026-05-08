"""抖音分享解析下载并写入当前项目视频库（HTTP 接口与工具层共用逻辑）。"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from data.media_mirror import ensure_project_media_dir
from tool.douyin_core import download_from_share
from agent_runtime.persist import persist_video_tool_output


def douyin_fetch_and_persist(
    project_id: str,
    share_text: str,
    *,
    video_title: str = "",
) -> dict[str, Any]:
    """
    下载无水印 MP4 到 ``data/media``，写入 ``project_assets``（视频库）。
    返回 ``{"uri": "/media/...", "ok": True}``；失败抛 ``ValueError`` / 底层异常。
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("project_id 不能为空")
    share = (share_text or "").strip()
    if not share:
        raise ValueError("请粘贴抖音分享链接或含 http(s) 的分享全文")

    # 必须在 TemporaryDirectory 仍存活时完成复制，否则退出 with 后临时文件会被删掉。
    seg, dest_dir = ensure_project_media_dir(pid)
    final_name = f"{uuid.uuid4().hex}.mp4"
    final_path = dest_dir / final_name
    with tempfile.TemporaryDirectory() as td:
        src = Path(download_from_share(share, td))
        shutil.copy2(src, final_path)
    uri = f"/media/{seg}/{final_name}"
    summary = (video_title or "").strip() or "抖音"
    payload = json.dumps(
        {"ok": True, "created_asset_ids": [uri], "task_id": None},
        ensure_ascii=False,
    )
    persist_video_tool_output(pid, payload, summary_base=summary)
    return {"ok": True, "uri": uri}
