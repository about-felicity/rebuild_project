"""
Claude Agent 入口：注入 ``tool/`` 到 ``sys.path``、构造 Messages API 客户端、运行多轮对话循环。

**对外导出**（供 ``main`` 使用）：
- ``agent_loop(hist)``：就地修改 ``hist``，直至 ``stop_reason != tool_use``。
- ``extract_last_assistant_text(messages)``：取最后一轮助手可见文本。

**输入**：环境变量 ``MODEL_ID``、``ANTHROPIC_API_KEY`` 等（与原先一致）。
**输出**：无模块级返回值；副作用为调用 Claude 与写库（由 ``tool_dispatch`` 触发）。
"""

from __future__ import annotations

import os
import random
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


def agent_loop(hist: list[dict[str, Any]]) -> None:
    """
**接受**：OpenAI 风格消息列表（role + content），最后一条为刚追加的用户消息。
**行为**：循环 ``messages.create``；若 ``tool_use`` 则追加 ``tool_result`` 用户消息继续；否则返回。
**输出**：无；直接修改 ``hist``。
"""
    create_kwargs: dict[str, Any] = {
        "model": MODEL,
        "system": SYSTEM,
        "messages": hist,
        "max_tokens": 8000,
    }
    if TOOLS:
        create_kwargs["tools"] = TOOLS

    n_round = 0
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
        response = _messages_create_with_retries(**create_kwargs)
        hist.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = collect_tool_results(response.content)
        if not results:
            return

        hist.append({"role": "user", "content": results})
