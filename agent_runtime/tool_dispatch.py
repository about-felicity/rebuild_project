"""
将 Claude 的 ``tool_use`` 块转为 ``tool_result`` 列表，并驱动本地生图/生视频与 ``project_assets`` 入库。

**接受**：``response.content`` 里的一组 block（仅处理 ``type=="tool_use"``）。
**输出**：Anthropic Messages 要求的 ``[{"type":"tool_result","tool_use_id":...,"content": str}, ...]``；
         ``content`` 为 **字符串**：成功时多为 ``run_generate_*`` 返回原串（JSON 或 ``Error:`` 明文），
         异常时为 ``json.dumps({"ok":false,"error":...})``。

**依赖**：``runtime_ctx.agent_project_id_ctx`` 须在 ``main.agent_chat`` 的 ``to_thread`` 前后 set/reset，
         否则无法绑定项目入库。
"""

from __future__ import annotations

import json
from typing import Any

from tool.generation_tools import run_generate_image, run_generate_video
from runtime_ctx import agent_project_id_ctx

from agent_runtime.media_hub import get_media_hub
from agent_runtime.persist import persist_image_tool_output, persist_video_tool_output

TOOL_SESSION = None


def _image_tool_summary_label(inp: dict[str, Any]) -> str:
    an = str(inp.get("asset_name") or "").strip()
    if an:
        return an
    return str(inp.get("user_query") or "").strip()


def _video_tool_summary_label(inp: dict[str, Any]) -> str:
    vt = str(inp.get("video_title") or "").strip()
    if vt:
        return vt
    return str(inp.get("prompt") or "").strip()


def collect_tool_results(assistant_content: list) -> list[dict[str, Any]]:
    pid = agent_project_id_ctx.get()
    hub = get_media_hub()
    out: list[dict[str, Any]] = []

    for block in assistant_content:
        if getattr(block, "type", None) != "tool_use":
            continue

        tid = block.id
        name = getattr(block, "name", "") or ""
        raw = getattr(block, "input", None)
        inp: dict[str, Any] = raw if isinstance(raw, dict) else {}

        if not pid:
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": json.dumps(
                        {"ok": False, "error": "no project_id in context"},
                        ensure_ascii=False,
                    ),
                }
            )
            continue

        try:
            if name == "generate_storyboard_image":
                text = run_generate_image(
                    hub,
                    TOOL_SESSION,
                    user_query=str(inp.get("user_query", "")),
                    destination="storyboard_library",
                    asset_name=str(inp.get("asset_name", "") or ""),
                    style_hint=str(inp.get("style_hint", "") or ""),
                    aspect_ratio=str(inp.get("aspect_ratio", "16:9")),
                    n=int(inp.get("n", 1) or 1),
                )
                if not text.strip().startswith("Error:"):
                    persist_image_tool_output(
                        pid,
                        "storyboard_library",
                        text,
                        summary_base=_image_tool_summary_label(inp),
                    )
                out.append({"type": "tool_result", "tool_use_id": tid, "content": text})

            elif name == "generate_asset_image":
                dest = str(inp.get("destination", "character_library"))
                text = run_generate_image(
                    hub,
                    TOOL_SESSION,
                    user_query=str(inp.get("user_query", "")),
                    destination=dest,
                    asset_name=str(inp.get("asset_name", "") or ""),
                    style_hint=str(inp.get("style_hint", "") or ""),
                    aspect_ratio=str(inp.get("aspect_ratio", "16:9")),
                    n=int(inp.get("n", 1) or 1),
                )
                if not text.strip().startswith("Error:"):
                    persist_image_tool_output(
                        pid,
                        dest,
                        text,
                        summary_base=_image_tool_summary_label(inp),
                    )
                out.append({"type": "tool_result", "tool_use_id": tid, "content": text})

            elif name == "generate_video_clip":
                text = run_generate_video(
                    hub,
                    TOOL_SESSION,
                    prompt=str(inp.get("prompt", "")),
                    source_image=str(inp.get("source_image", "")),
                    duration=int(inp.get("duration", 0) or 0),
                    camera_move=str(inp.get("camera_move", "static") or "static"),
                    video_title=str(inp.get("video_title", "") or ""),
                )
                try:
                    data = json.loads(text)
                    if isinstance(data, dict) and data.get("ok") is not False:
                        persist_video_tool_output(
                            pid,
                            text,
                            summary_base=_video_tool_summary_label(inp),
                        )
                except json.JSONDecodeError:
                    pass
                out.append({"type": "tool_result", "tool_use_id": tid, "content": text})

            else:
                out.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": json.dumps(
                            {"ok": False, "error": f"unknown tool: {name}"},
                            ensure_ascii=False,
                        ),
                    }
                )
        except Exception as e:
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": json.dumps(
                        {"ok": False, "error": str(e)}, ensure_ascii=False
                    ),
                }
            )

    return out
