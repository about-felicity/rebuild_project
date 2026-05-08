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


def apply_assistant_reply_policy(hist: list, reply: str) -> str:
    """
    **输入**：已去掉 Markdown 外链图后的模型可见回复。
    **输出**：成功工具 → 固定短句；疑似假成功 → 警告短句；空 → 默认提示；否则原文。
    """
    minimal = compact_generation_reply(hist)
    if minimal:
        return minimal
    if reply and _HALLUCINATED_SUCCESS_RE.search(reply):
        return (
            "未检测到实际的生图/生视频成功记录（模型可能未调用工具或在编造「已生成」）。"
            "请重试，并写清库类型与画面，例如：「角色库，写实风程序员深夜加班」。"
        )
    if not reply:
        return "生成已完成，请在左侧「项目素材」中查看成片。"
    return reply
