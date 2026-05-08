"""从 Anthropic API 返回的 assistant 消息中提取最后一段可见文本（用于落库/回包）。"""

from __future__ import annotations

import re


def strip_markdown_images(text: str) -> str:
    """去掉 ``![alt](url)``，避免重复占位：成片已在项目素材中展示。"""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract_last_assistant_text(messages: list) -> str:
    """
**接受**：``agent_loop`` 维护的 ``hist``，元素形如 ``{"role": "...", "content": ...}``。
**输出**：自末尾起第一条 ``role=="assistant"`` 的文本；无则 ``""``。

``content`` 既可能是 ``str``，也可能是 SDK 的 block 列表（含 TextBlock）。
"""
    for turn in reversed(messages):
        if turn.get("role") != "assistant":
            continue

        content = turn.get("content")

        if isinstance(content, str):
            return content.strip()

        if not content:
            return ""

        parts: list[str] = []
        for block in content:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))

        return "\n".join(parts).strip()

    return ""
