#!/usr/bin/env python3
"""
独立脚本：按队列向 FrameOS Agent 发消息，每轮等服务器返回后再发下一轮。

说明（重要）：
- 当前 ``POST /api/agent/chat`` 是**同步**的：HTTP 连接会一直保持到 ``agent_loop`` 整轮结束
  （含工具调用），返回体里已有 ``reply`` 与 ``tool_trace``。因此「发完再查是否生成完」
  在现有实现里等价于：**等 POST 返回即可**；下面的 ``poll_messages`` 仅作二次校验（看库里
  是否已追加 user/assistant 消息），便于你将来若改成异步任务仍可沿用同一套轮询逻辑。

用法（先 ``serve-lan.bat`` / ``uvicorn`` 起服务）::

    set FRAMEOS_BASE_URL=http://127.0.0.1:8000
    set FRAMEOS_PROJECT_ID=你的项目id
    python test.py

或修改 ``_default_steps()`` 里的 ``product_name`` / ``referenced_asset_ids``；
也可用 ``--steps-file steps.json`` 从 JSON 数组加载多轮（见 ``--help``）。

依赖：httpx（已在 requirements.txt）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    print("请安装 httpx: pip install httpx", file=sys.stderr)
    sys.exit(1)


# ---------- 默认多轮任务：只改产品名与素材 id，其余句式相同 ----------

MESSAGE_TEMPLATE = """帮我生成一段5秒的视频，视频中的人物严格按照上传的人物和产品参考图；视频画面：人物坐在桌子前，左手拿着产品（{product_name}），右手指着产品介绍产品优势，台词为简单介绍产品（需要说出产品的名称）。需要保证手里的产品是上传的产品素材，不要改变。"""


def _default_steps() -> list[dict[str, Any]]:
    """示例：请按你库里真实 asset id 修改。"""
    return [
        {
            "product_name": "控油蓬松洗发水",
            "referenced_asset_ids": [35, 81],
        },
        {
            "product_name": "科熙本控油蓬松洗发水",
            "referenced_asset_ids": [35, 81],
        },
    ]


def build_user_message(product_name: str, asset_ids: list[int]) -> str:
    body = MESSAGE_TEMPLATE.format(product_name=product_name)
    if not asset_ids:
        return body
    ids_txt = "、".join(str(i) for i in asset_ids)
    suffix = f"\n\n（已引用 {len(asset_ids)} 张素材库图片，服务器 id：{ids_txt}；首帧默认第 1 张，除非你在上文指定）"
    return body + suffix


def post_agent_chat(
    client: httpx.Client,
    base_url: str,
    project_id: str,
    message: str,
    referenced_asset_ids: list[int],
    agent_mode: str = "normal",
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/agent/chat"
    payload = {
        "message": message,
        "project_id": project_id,
        "project": "",
        "agent_mode": agent_mode,
        "referenced_asset_ids": referenced_asset_ids,
    }
    r = client.post(url, json=payload)
    r.raise_for_status()
    return r.json()


def get_session_messages(
    client: httpx.Client, base_url: str, project_id: str
) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/agent/sessions/{project_id}/messages"
    r = client.get(url)
    r.raise_for_status()
    data = r.json()
    return list(data.get("messages") or [])


def poll_until_assistant_after_user(
    client: httpx.Client,
    base_url: str,
    project_id: str,
    user_seq_before: int,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 0.5,
) -> list[dict[str, Any]]:
    """
    在 POST 已返回的前提下，通常立刻满足；若将来 chat 变异步，可依赖此函数等到 assistant 落库。
    """
    deadline = time.monotonic() + timeout_sec
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = get_session_messages(client, base_url, project_id)
        # 是否存在比 user_seq_before 更大的 assistant
        for row in last:
            if row.get("role") != "assistant":
                continue
            try:
                seq = int(row.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > user_seq_before:
                return last
        time.sleep(interval_sec)
    return last


def summarize_tool_trace(tool_trace: dict[str, Any] | None) -> str:
    if not tool_trace:
        return "(无 tool_trace)"
    parts = []
    sb = tool_trace.get("storyboard_uris") or []
    vids = tool_trace.get("videos") or []
    imgs = tool_trace.get("asset_image_uris") or []
    if sb:
        parts.append(f"分镜/库 URI: {len(sb)} 条")
    if vids:
        parts.append(f"视频: {len(vids)} 段")
    if imgs:
        parts.append(f"角色/产品图入库 URI: {len(imgs)} 条")
    return "；".join(parts) if parts else json.dumps(tool_trace, ensure_ascii=False)[:500]


def step_success(resp: dict[str, Any], *, strict_video: bool) -> bool:
    reply = str(resp.get("reply") or "")
    if "（生成失败）" in reply[:80] or reply.strip().startswith("（生成失败）"):
        return False
    if not strict_video:
        return True
    tt = resp.get("tool_trace") or {}
    vids = tt.get("videos") or []
    return bool(vids)


def health_ok(client: httpx.Client, base_url: str) -> bool:
    try:
        r = client.get(f"{base_url.rstrip('/')}/api/health", timeout=10.0)
        return r.status_code == 200
    except OSError:
        return False


def run_queue(
    base_url: str,
    project_id: str,
    steps: list[dict[str, Any]],
    *,
    chat_timeout_sec: float,
    poll_timeout_sec: float,
    sleep_between_sec: float,
    strict_video: bool,
    dry_run: bool,
) -> int:
    headers = {}
    token = (os.getenv("FRAMEOS_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    exit_code = 0
    with httpx.Client(timeout=chat_timeout_sec, headers=headers or None) as client:
        if not health_ok(client, base_url):
            print(f"[warn] 探活失败: {base_url}/api/health （若服务未启动请先起 uvicorn）")

        for i, step in enumerate(steps, 1):
            name = str(step.get("product_name") or "").strip()
            ids = step.get("referenced_asset_ids") or []
            if not isinstance(ids, list):
                ids = []
            ids = [int(x) for x in ids if str(x).strip().isdigit()]

            msg = build_user_message(name, ids)
            print(f"\n======== 第 {i}/{len(steps)} 轮 ========")
            print(f"产品名: {name!r}")
            print(f"referenced_asset_ids: {ids}")
            if dry_run:
                print("--- dry-run 正文 ---")
                print(msg[:1200])
                print("---")
                continue

            msgs_before = get_session_messages(client, base_url, project_id)
            max_seq_before = 0
            for row in msgs_before:
                try:
                    max_seq_before = max(max_seq_before, int(row.get("seq") or 0))
                except (TypeError, ValueError):
                    pass

            t0 = time.perf_counter()
            try:
                resp = post_agent_chat(
                    client, base_url, project_id, msg, ids
                )
            except httpx.HTTPStatusError as e:
                print(f"[error] HTTP {e.response.status_code}: {e.response.text[:800]}")
                exit_code = 1
                break
            dt = time.perf_counter() - t0

            poll_until_assistant_after_user(
                client,
                base_url,
                project_id,
                max_seq_before,
                timeout_sec=poll_timeout_sec,
            )

            ok = step_success(resp, strict_video=strict_video)
            print(f"耗时 {dt:.1f}s | 判定: {'成功' if ok else '未满足严格条件/失败'}")
            print("tool_trace 摘要:", summarize_tool_trace(resp.get("tool_trace")))
            preview = str(resp.get("reply") or "")[:600]
            print("reply 预览:\n", preview + ("…" if len(str(resp.get("reply"))) > 600 else ""))

            if not ok:
                exit_code = 1
                break

            if i < len(steps) and sleep_between_sec > 0:
                time.sleep(sleep_between_sec)

    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description="队列调用 FrameOS /api/agent/chat")
    p.add_argument(
        "--base-url",
        default=os.getenv("FRAMEOS_BASE_URL", "http://127.0.0.1:8000").strip(),
        help="服务根 URL",
    )
    p.add_argument(
        "--project-id",
        default=os.getenv("FRAMEOS_PROJECT_ID", "").strip(),
        help="项目 id（与前端 localStorage / 库一致）",
    )
    p.add_argument(
        "--chat-timeout",
        type=float,
        default=float(os.getenv("FRAMEOS_CHAT_TIMEOUT_SEC", "3600")),
        help="单轮 POST 读超时（秒），生成分镜+视频可能很久",
    )
    p.add_argument(
        "--poll-timeout",
        type=float,
        default=30.0,
        help="POST 后校验会话消息的额外等待（秒）",
    )
    p.add_argument(
        "--sleep-between",
        type=float,
        default=float(os.getenv("FRAMEOS_STEP_SLEEP_SEC", "2")),
        help="两轮之间的间隔（秒）",
    )
    p.add_argument(
        "--strict-video",
        action="store_true",
        help="要求 tool_trace 中出现视频段才算成功",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印将要发送的正文，不调 API")
    p.add_argument(
        "--steps-file",
        default="",
        help="JSON 文件路径，内容为数组，每项含 product_name、referenced_asset_ids（可选）；"
        "未指定则用代码内 _default_steps()",
    )
    args = p.parse_args()

    if not args.project_id and not args.dry_run:
        print("请设置 --project-id 或环境变量 FRAMEOS_PROJECT_ID", file=sys.stderr)
        return 2

    steps_path = (args.steps_file or "").strip()
    if steps_path:
        raw = Path(steps_path).read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if not isinstance(loaded, list) or not loaded:
            print("--steps-file 须为非空 JSON 数组", file=sys.stderr)
            return 2
        steps = loaded
    else:
        steps = _default_steps()
    return run_queue(
        args.base_url,
        args.project_id,
        steps,
        chat_timeout_sec=args.chat_timeout,
        poll_timeout_sec=args.poll_timeout,
        sleep_between_sec=args.sleep_between,
        strict_video=args.strict_video,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
