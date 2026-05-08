"""
把「仅回复角色库/分镜库」等极短句与**历史中上一条实质性用户消息**拼在一起再送给模型，
否则本轮 user 内容过短，模型没有可用的 user_query，容易不调用工具或编造「已生成」。
入库时仍只存用户原文（见 ``main.agent_chat``）。
"""

from __future__ import annotations

import re

from data.db import load_history_for_model

_SHORT_LIBRARY_REPLY = re.compile(
    r"^\s*(角色库|人设库|分镜库|产品库|分镜|故事板)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)


def _last_substantive_user_message(project_id: str) -> str:
    """最近一条「不是纯库名」的用户内容。"""
    for turn in reversed(load_history_for_model(project_id)):
        if turn.get("role") != "user":
            continue
        c = str(turn.get("content") or "").strip()
        if not c or _SHORT_LIBRARY_REPLY.match(c) or len(c) < 4:
            continue
        return c
    return ""


def augment_message_for_model(project_id: str, raw_message: str) -> str:
    raw = (raw_message or "").strip()
    if not raw or not _SHORT_LIBRARY_REPLY.match(raw):
        return raw

    prior = _last_substantive_user_message(project_id)
    low = raw.casefold()
    if "产品" in low:
        tool_hint = "generate_asset_image，destination=product_library（产品库）"
    elif "分镜" in low or "故事" in low:
        tool_hint = "generate_storyboard_image（分镜库）"
    else:
        tool_hint = "generate_asset_image，destination=character_library（角色库）"

    tail = (
        f"\n\n[用户确认] 以上画面生成到：{raw}。\n"
        f"你必须在本轮立即调用工具：{tool_hint}；"
        "将用户对画面的完整需求写入 user_query（来自上一段用户原文，可适度概括，禁止空泛只写「角色库」）。"
        "禁止仅用文字声称已生成、禁止再追问库类型。"
    )

    if prior:
        return prior + tail
    return raw + (
        "\n\n（会话里找不到更早的完整需求；请根据上一句助手问题里的上下文推断画面并调用生图工具，"
        "user_query 写全画面描述。禁止只文字回复。）"
    )
