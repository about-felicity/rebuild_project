"""
根据本轮 ``hist`` 里 tool_result 是否表明生图/生视频成功，决定用户可见的最终回复；
兼容 Anthropic SDK 的 block 对象；并拦截「未真调用工具却声称生成成功」的幻觉话术。
"""

from __future__ import annotations

import json
import re
from typing import Any

_DESTINATION_LABEL: dict[str, str] = {
    "storyboard_library": "分镜库",
    "character_library": "角色库",
    "product_library": "产品库",
}

# 典型「营销体成功宣称」，且 history 里没有成功 tool 时视为幻觉
_HALLUCINATED_SUCCESS_RE = re.compile(
    r"(##\s*✅|生成成功\s*[！!🎉]|已生成并存入\s*\*\*|已生成并存入[^。\n]*角色库|已生成并存入[^。\n]*分镜)",
    re.IGNORECASE | re.MULTILINE,
)

# 工具已失败时，模型仍编造「联系管理员调像素」等（与当前服务端行为不符）
_MISLEADING_OPS_ADVICE = (
    "联系管理员",
    "管理员调整",
    "系统侧的限制",
    "我无法调整",
    "服务端固定参数",
)


def _norm_block_type(block: Any) -> str:
    if isinstance(block, dict):
        t = block.get("type")
    else:
        t = getattr(block, "type", None)
    if t is None:
        return ""
    if hasattr(t, "value"):
        t = t.value
    return str(t)


def _coerce_tool_result_payload_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.startswith("```"):
            lines = s.split("\n")
            if len(lines) >= 2 and lines[-1].strip() == "```":
                s = "\n".join(lines[1:-1]).strip()
        return s
    if isinstance(raw, list):
        parts: list[str] = []
        for x in raw:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(str(x.get("text") or ""))
            elif _norm_block_type(x) == "text":
                parts.append(str(getattr(x, "text", "") or ""))
        return "\n".join(parts).strip() if parts else None
    return str(raw).strip() or None


def _tool_result_json_string(block: Any) -> str | None:
    if _norm_block_type(block) != "tool_result":
        return None
    if isinstance(block, dict):
        c = block.get("content")
    else:
        c = getattr(block, "content", None)
    return _coerce_tool_result_payload_str(c)


def compact_generation_reply(hist: list[dict[str, Any]]) -> str | None:
    """
    自最近一轮往前找**最后一条**成功的生图/生视频 tool_result，返回一句「已保存到…。」
    """
    for turn in reversed(hist):
        if turn.get("role") != "user":
            continue
        content = turn.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            payload = _tool_result_json_string(block)
            if not payload:
                continue
            if payload.startswith("Error:"):
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("ok") is False:
                continue
            created = data.get("created_asset_ids") or []
            if not created:
                continue
            dest = data.get("destination")
            if isinstance(dest, str) and dest in _DESTINATION_LABEL:
                return f"已保存到{_DESTINATION_LABEL[dest]}。"
            return "已保存到视频库。"
    return None


def extract_generation_tool_trace(hist: list[dict[str, Any]]) -> dict[str, Any]:
    """
    从本轮 ``hist`` 里解析所有生图/生视频 tool_result JSON，供 ``/api/agent/chat`` 返回 ``tool_trace``，
    便于前端提示「分镜是否入库」「视频首帧 URI」等（不依赖模型自然语言）。
    """
    storyboard_uris: list[str] = []
    asset_image_uris: list[str] = []
    videos: list[dict[str, Any]] = []

    for msg in hist:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            payload = _tool_result_json_string(block)
            if not payload or payload.startswith("Error:"):
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("ok") is False:
                continue
            created = data.get("created_asset_ids") or []
            if not created:
                continue
            dest = data.get("destination")
            if isinstance(dest, str) and dest == "storyboard_library":
                for x in created:
                    storyboard_uris.append(str(x).strip())
                continue
            if isinstance(dest, str) and dest in ("character_library", "product_library"):
                for x in created:
                    asset_image_uris.append(str(x).strip())
                continue
            # 视频：带追溯字段，或根据输出 URL 推断
            if data.get("source_image_used") is not None or data.get(
                "reference_image_urls_requested"
            ) is not None:
                videos.append(
                    {
                        "first_frame_url": str(data.get("source_image_used") or ""),
                        "reference_image_urls_requested": list(
                            data.get("reference_image_urls_requested") or []
                        ),
                        "output_uris": [str(x).strip() for x in created if x],
                    }
                )
                continue
            first = str(created[0]).strip().lower()
            if ".mp4" in first or "video" in first or "/video" in first:
                videos.append(
                    {
                        "first_frame_url": "",
                        "reference_image_urls_requested": [],
                        "output_uris": [str(x).strip() for x in created if x],
                    }
                )

    return {
        "storyboard_uris": storyboard_uris,
        "asset_image_uris": asset_image_uris,
        "videos": videos,
    }


def _failure_line_from_penultimate_user_tool_batch(hist: list) -> str | None:
    """
    取「最后一条助手回复」前一条 user 消息里的 tool_result 失败摘要（通常为刚结束的一轮工具）。
    用于在模型胡编「联系管理员」时用 Ark/工具原文覆盖。
    """
    if len(hist) < 2:
        return None
    turn = hist[-2]
    if turn.get("role") != "user":
        return None
    content = turn.get("content")
    if not isinstance(content, list):
        return None
    errs: list[str] = []
    for block in content:
        payload = _tool_result_json_string(block)
        if not payload:
            continue
        s = payload.strip()
        if s.startswith("Error:"):
            errs.append(s[:900])
            continue
        try:
            j = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(j, dict) and j.get("ok") is False:
            msg = str(j.get("error") or "").strip()
            errs.append((msg or json.dumps(j, ensure_ascii=False))[:900])
    if not errs:
        return None
    return errs[-1]


def apply_assistant_reply_policy(hist: list, reply: str) -> str:
    """
    **输入**：已去掉 Markdown 外链图后的模型可见回复。
    **输出**：成功工具 → 固定短句；疑似假成功 → 警告短句；空 → 默认提示；否则原文。
    """
    minimal = compact_generation_reply(hist)
    if minimal:
        return minimal

    tool_fail = _failure_line_from_penultimate_user_tool_batch(hist)
    if tool_fail and (
        not (reply or "").strip()
        or any(p in (reply or "") for p in _MISLEADING_OPS_ADVICE)
        or ("像素" in (reply or "") and "下限" in (reply or ""))
    ):
        return f"未成功：{tool_fail}"

    if reply and _HALLUCINATED_SUCCESS_RE.search(reply):
        return (
            "未检测到实际的生图/生视频成功记录（模型可能未调用工具或在编造「已生成」）。"
            "请重试，并写清库类型与画面，例如：「角色库，写实风程序员深夜加班」。"
        )
    if not reply:
        return "生成已完成，请在左侧「项目素材」中查看成片。"
    return reply
