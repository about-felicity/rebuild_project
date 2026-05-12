"""
当用户本轮明确要「成片 / 图生视频 / N 秒视频」时，保证最终执行 ``generate_video_clip``：
若引用 ≥2 张且尚无分镜首帧 URI，则先 ``generate_storyboard_image`` 再 ``generate_video_clip``；
否则仅补跑视频。与模型是否漏调工具无关（每轮 chat 最多注入一次，避免死循环）。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

_VIDEO_INTENT = re.compile(
    r"(图生视频|短视频|成片|做.{0,4}视频|生成.{0,30}视频|来段视频|"
    r"wan2|动起[来]|动起来|"
    r"\d+\s*秒.{0,12}视频|视频.{0,8}\d+\s*秒|"
    r"一段.{0,8}\d+\s*秒|\d+\s*秒钟)",
    re.IGNORECASE,
)


def synthetic_tool_use_blocks(
    specs: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """与 Anthropic ``tool_use`` 块等价的纯 dict（可 JSON 序列化），供 ``collect_tool_results`` 与后续 API 请求共用。"""
    out: list[dict[str, Any]] = []
    for name, input_obj in specs:
        out.append(
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:24]}",
                "name": name,
                "input": dict(input_obj),
            }
        )
    return out


def last_plain_string_user_message(hist: list[dict[str, Any]]) -> str:
    """本轮触发回复前，最近一条自然语言 user（排除 tool_result 的 list content）。"""
    for turn in reversed(hist):
        if turn.get("role") != "user":
            continue
        c = turn.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def subhist_since_last_plain_user(hist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从「最近一条纯文本 user」起截到末尾，用于判断本轮是否已出成片。"""
    for i in range(len(hist) - 1, -1, -1):
        if hist[i].get("role") != "user":
            continue
        c = hist[i].get("content")
        if isinstance(c, str) and c.strip():
            return hist[i:]
    return hist


def user_demands_video_clip(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_VIDEO_INTENT.search(t))


def _parse_duration_seconds(text: str) -> int:
    s = (text or "").strip()
    m = re.search(r"(\d+)\s*(?:秒|秒钟|s(?![a-zA-Z])|sec\b)", s, re.IGNORECASE)
    if m:
        try:
            n = int(m.group(1))
            return max(1, min(120, n))
        except ValueError:
            pass
    cn = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    m2 = re.search(r"([一二三四五六七八九十两]+)\s*秒", s)
    if m2:
        w = m2.group(1)
        if w in cn:
            return max(1, min(120, int(cn[w])))
        if w == "十" or w.startswith("十"):
            return 10
    return 5


def _short_video_title(text: str) -> str:
    one = (text or "").strip().split("\n", 1)[0].strip()
    if len(one) > 20:
        return one[:20] + "…"
    return one or "成片"


def _tool_result_payload_video_ok(payload: str) -> bool:
    if not payload or payload.startswith("Error:"):
        return False
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or data.get("ok") is False:
        return False
    if isinstance(data.get("destination"), str):
        return False
    st = str(data.get("status") or "")
    created = data.get("created_asset_ids") or []
    if not isinstance(created, list) or not created:
        return False
    return st == "success"


def history_has_successful_video_clip(hist: list[dict[str, Any]]) -> bool:
    for turn in hist:
        if turn.get("role") != "user":
            continue
        content = turn.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                raw = block.get("content")
            else:
                t = getattr(block, "type", None)
                if hasattr(t, "value"):
                    t = t.value
                raw = getattr(block, "content", None)
            if str(t) != "tool_result":
                continue
            if isinstance(raw, str) and _tool_result_payload_video_ok(raw):
                return True
    return False


def tool_results_include_successful_video(results: list[dict[str, Any]]) -> bool:
    for block in results:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        c = block.get("content")
        if isinstance(c, str) and _tool_result_payload_video_ok(c):
            return True
    return False


def build_forced_video_tool_round(
    hist: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """
    构造一轮「助手 tool_use」及其 ``collect_tool_results`` 输出。
    若当前上下文已满足成片（历史中已有成功视频），返回 None。
    """
    from runtime_ctx import agent_last_storyboard_uri

    from agent_runtime.tool_dispatch import (
        collect_tool_results,
        count_deduped_chat_video_ref_uris,
    )

    first_u = last_plain_string_user_message(hist)
    if not user_demands_video_clip(first_u):
        return None
    if history_has_successful_video_clip(subhist_since_last_plain_user(hist)):
        return None

    nrefs = count_deduped_chat_video_ref_uris()
    lsb = (agent_last_storyboard_uri.get() or "").strip()

    blocks: list[dict[str, Any]] = []
    specs: list[tuple[str, dict[str, Any]]] = []

    if nrefs >= 2 and not lsb:
        specs.append(
            (
                "generate_storyboard_image",
                {
                    "user_query": first_u,
                    "asset_name": "",
                    "style_hint": "",
                },
            )
        )

    if nrefs < 2 or specs or lsb:
        specs.append(
            (
                "generate_video_clip",
                {
                    "prompt": first_u,
                    "source_image": "",
                    "duration": _parse_duration_seconds(first_u),
                    "camera_move": "static",
                    "video_title": _short_video_title(first_u),
                },
            )
        )
    else:
        return None

    blocks = synthetic_tool_use_blocks(specs)
    guard_results = collect_tool_results(blocks)
    if not guard_results:
        return None
    return (blocks, guard_results)
