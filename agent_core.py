"""
Claude Agent 入口：注入 ``tool/`` 到 ``sys.path``、构造 Messages API 客户端、运行多轮对话循环。

**对外导出**（供 ``main`` 使用）：
- ``agent_loop(hist, agent_mode="normal")``：就地修改 ``hist``，直至 ``stop_reason != tool_use``；``abstract`` 时追加抽象广告导演 persona。
- ``extract_last_assistant_text(messages)``：取最后一轮助手可见文本。

**输入**：环境变量 ``MODEL_ID``、``ANTHROPIC_API_KEY`` 等（与原先一致）。
**输出**：无模块级返回值；副作用为调用 Claude 与写库（由 ``tool_dispatch`` 触发）。
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

# 须在任何 ``import generation_tools`` / ``import ai`` 之前执行（二者使用 ``from ai import ...``）
_TOOL_DIR = Path(__file__).resolve().parent / "tool"
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

import httpx
from anthropic import Anthropic
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from dotenv import load_dotenv

from agent_runtime.abstract_agent_mode import (
    ABSTRACT_MODE_SESSION_LOCK,
    ABSTRACT_MODE_SYSTEM_APPEND,
)
from agent_runtime.messages import extract_last_assistant_text
from agent_runtime.tool_dispatch import collect_tool_results
from agent_runtime.tools_spec import SYSTEM, TOOLS

load_dotenv(override=True)

if os.getenv("ANTHROPIC_API_KEY"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

_client_kw: dict[str, Any] = {}
if os.getenv("ANTHROPIC_BASE_URL"):
    _client_kw["base_url"] = os.getenv("ANTHROPIC_BASE_URL")

try:
    _read_timeout = float(os.getenv("ANTHROPIC_HTTP_TIMEOUT_SEC", "120"))
    _connect_timeout = float(os.getenv("ANTHROPIC_HTTP_CONNECT_TIMEOUT_SEC", "15"))
except (TypeError, ValueError):
    _read_timeout, _connect_timeout = 120.0, 15.0
# SDK 默认读超时 10min，单 worker 下会长时间占住 /api/agent/chat，后续对话全部排队表现为「一直正在输入」
_client_kw["timeout"] = httpx.Timeout(_read_timeout, connect=_connect_timeout)

client = Anthropic(**_client_kw)
MODEL = os.environ["MODEL_ID"]
try:
    _MAX_TOOL_ROUNDS = max(1, int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "32")))
except (TypeError, ValueError):
    _MAX_TOOL_ROUNDS = 32

try:
    _API_RETRY_MAX = max(1, int(os.getenv("AGENT_API_RETRY_MAX", "4")))
except (TypeError, ValueError):
    _API_RETRY_MAX = 4

# 无画面/无生成意图的寒暄或元问题：本回合不传 tools，避免误调生图/视频工具
_GENERATION_INTENT = re.compile(
    r"(生[成图动]|画一张|画个|出图|分镜|角色库|产品库|三视图|做多视图|storyboard|"
    r"做.{0,6}[图画]|视频\s*片段|图生视频|短视频|做成视频|做成短片|动起[来]|动起来|"
    r"来段视频|生成视频|做个视频|wan2|生成素材|重新生成|帮我画|给.{0,6}图)",
    re.IGNORECASE,
)
_PURE_CHITCHAT = re.compile(
    r"^\s*(你好|您好|在吗|哈喽|嗨|hi|hello|hey|早上好|中午好|下午好|晚上好|午安|"
    r"谢谢|多谢|感谢|辛苦了|拜拜|再见|好啦|嗯嗯|嗯|好的|收到|明白|了解|知道了|OK|ok|行)\b"
    r"[\s!！.。…~～,，?？…]*\s*$",
    re.IGNORECASE,
)
_META_ABOUT_AGENT = re.compile(
    r"(你|您).{0,14}(刚刚|刚才|之前).{0,10}(什么|干嘛|干了|做了|干啥|怎么)|"
    r"^(解释一下|为什么|啥情况|什么意思|怎么回事)",
    re.IGNORECASE,
)


def _last_user_content_text(hist: list[dict[str, Any]]) -> str:
    if not hist or hist[-1].get("role") != "user":
        return ""
    c = hist[-1].get("content")
    return c.strip() if isinstance(c, str) else ""


def _suppress_tools_for_user_message(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _GENERATION_INTENT.search(t):
        return False
    if _PURE_CHITCHAT.match(t):
        return True
    if _META_ABOUT_AGENT.search(t):
        return True
    return False


def _retryable_llm_error(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, InternalServerError):
        return True
    if isinstance(exc, APIStatusError):
        if exc.status_code == 429 or exc.status_code >= 500:
            return True
    low = str(exc).lower()
    if ("503" in str(exc) or "529" in str(exc)) and (
        "service_unavailable" in low
        or "too busy" in low
        or "overloaded" in low
        or "unavailable" in low
    ):
        return True
    return False


def _messages_create_with_retries(**kwargs: Any) -> Any:
    """对限流、503、网关过载等做有限次指数退避重试。"""
    delay = 0.9
    last_exc: BaseException | None = None
    for attempt in range(_API_RETRY_MAX):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            last_exc = e
            if attempt >= _API_RETRY_MAX - 1 or not _retryable_llm_error(e):
                raise
            sleep_s = min(30.0, delay + random.random() * 0.4)
            time.sleep(sleep_s)
            delay = min(delay * 2.0, 18.0)
    assert last_exc is not None
    raise last_exc


def _system_for_agent_mode(agent_mode: str) -> str:
    m = (agent_mode or "normal").strip().lower()
    if m != "abstract":
        return SYSTEM
    return (
        SYSTEM
        + "\n\n---\n【当前会话：抽象广告导演模式】\n"
        "工具调用、project_id、禁止伪造 id 等安全规则仍遵守上文。"
        "除此以外，人设与口吻以「鬼才广告导演」为准；若与上文「简短客服式寒暄」冲突，以本段及下方 persona 为准。\n\n"
        + ABSTRACT_MODE_SESSION_LOCK
        + "\n\n"
        + ABSTRACT_MODE_SYSTEM_APPEND
        + "\n\n【收束】已处于抽象模式：禁止客服腔与 1/2/3 功能说明书；禁止让用户选择模式。\n"
    )


def agent_loop(hist: list[dict[str, Any]], agent_mode: str = "normal") -> None:
    """
**接受**：OpenAI 风格消息列表（role + content），最后一条为刚追加的用户消息。
**行为**：循环 ``messages.create``；若 ``tool_use`` 则追加 ``tool_result`` 用户消息继续；否则返回。
**输出**：无；直接修改 ``hist``。

``agent_mode``：``normal`` 默认；``abstract`` 时追加互联网抽象广告导演 persona。
"""
    n_round = 0
    system_text = _system_for_agent_mode(agent_mode)
    while True:
        n_round += 1
        if n_round > _MAX_TOOL_ROUNDS:
            hist.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "已达到连续工具调用次数上限，请缩短需求或拆成多步再试。",
                        }
                    ],
                }
            )
            return
        create_kwargs: dict[str, Any] = {
            "model": MODEL,
            "system": system_text,
            "messages": hist,
            "max_tokens": 8000,
        }
        last_user_text = _last_user_content_text(hist)
        if TOOLS and not (
            last_user_text and _suppress_tools_for_user_message(last_user_text)
        ):
            create_kwargs["tools"] = TOOLS
        response = _messages_create_with_retries(**create_kwargs)
        hist.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = collect_tool_results(response.content)
        if not results:
            return

        hist.append({"role": "user", "content": results})
