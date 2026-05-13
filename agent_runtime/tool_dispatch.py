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
import os
from typing import Any

from tool.generation_tools import run_generate_image, run_generate_video
from data.media_mirror import mirror_http_url_to_local
from runtime_ctx import (
    agent_chat_video_ref_stack,
    agent_last_storyboard_uri,
    agent_pending_storyboard_asset_mirror,
    agent_project_id_ctx,
)

from agent_runtime.media_hub import get_media_hub
from agent_runtime.persist import (
    insert_storyboard_asset_library_mirror,
    persist_image_tool_output,
    persist_video_tool_output,
)
from agent_runtime.video_guard import video_tool_json_indicates_success

TOOL_SESSION = None


def _agent_ark_video_wait_timeout_sec() -> int:
    """Agent ``generate_video_clip`` 轮询方舟任务的最长等待；默认 600s，与前端 AGENT_CHAT_TIMEOUT_MS 留余量。"""
    raw = (os.environ.get("ARK_VIDEO_WAIT_TIMEOUT_SEC") or "").strip()
    if raw:
        try:
            v = int(raw)
            return max(60, min(v, 3600))
        except ValueError:
            pass
    return 600


def _row_destination(row: dict[str, Any]) -> str:
    m = row.get("meta")
    if isinstance(m, dict):
        d = str(m.get("destination") or "").strip().lower()
        if d in ("character_library", "product_library"):
            return d
    # 浏览器直传素材常无 destination，仅靠文件名 / 路径粗分人物 vs 产品
    name = str(row.get("name") or "").lower()
    uri = str(row.get("uri") or "").lower()
    uri_base = uri.split("?", 1)[0]
    # 仓库同步的产品主图落在 data/media/_product_catalog/，常无 meta.destination
    if "/_product_catalog/" in uri_base:
        return "product_library"
    blob = f"{name} {uri}"
    char_kw = ("人物", "角色", "模特", "人像", "character", "avatar", "人设", "模特图")
    prod_kw = (
        "产品",
        "包装",
        "瓶身",
        "商品",
        "product",
        "sku",
        "洗发水",
        "化妆品",
        "控油",
        "蓬松",
        "产品图",
    )
    # 先判人物，避免「人物产品介绍」被「产品」误伤
    if any(k in blob for k in char_kw):
        return "character_library"
    if any(k in blob for k in prod_kw):
        return "product_library"
    return ""


def _deduped_chat_ref_uris() -> list[str]:
    """与 ``_coalesce_video_references`` 相同的栈内去重 URI 顺序列表。"""
    stack = agent_chat_video_ref_stack.get()
    if not stack:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for row in stack:
        if not isinstance(row, dict):
            continue
        u = str(row.get("uri") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def count_deduped_chat_video_ref_uris() -> int:
    """本轮可作首帧的引用图数量（去重 URI）；供 ``video_guard`` 判断是否必须先分镜。"""
    return len(_deduped_chat_ref_uris())


def _dedupe_str_list(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        s = (x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _stack_uris_ordered_for_image_input() -> list[str]:
    """
    分镜 / 生图：把本轮引用栈里的图按「人物 → 产品 → 其余」排序后作为 input_images，
    便于 Seedream/Wan 多参考锁定外观（与视频侧人物优先逻辑一致）。
    """
    stack = agent_chat_video_ref_stack.get()
    if not stack:
        return []
    rows_by_uri: dict[str, dict[str, Any]] = {}
    uris: list[str] = []
    for row in stack:
        if not isinstance(row, dict):
            continue
        u = str(row.get("uri") or "").strip()
        if not u or u in rows_by_uri:
            continue
        rows_by_uri[u] = row
        uris.append(u)
    char = [u for u in uris if _row_destination(rows_by_uri[u]) == "character_library"]
    prod = [u for u in uris if _row_destination(rows_by_uri[u]) == "product_library"]
    cs, ps = set(char), set(prod)
    rest = [u for u in uris if u not in cs and u not in ps]
    return _dedupe_str_list(char + prod + rest)[:8]


def _coalesce_video_references(
    source_image: str,
    model_refs: list[str],
) -> tuple[str, list[str]]:
    """
    用本轮 chat 解析到的引用栈补全 / 纠正 generate_video_clip 的首帧与附加参考图，
    避免模型漏传 reference_image_urls；人物+产品分图时优先人物作首帧、产品进参考。
    另：路径含 ``/_product_catalog/`` 的入库单品图一律视为产品库；多图时不得将其作首帧（除非栈内仅有产品图）。
    """
    stack = agent_chat_video_ref_stack.get()
    if not stack:
        return (source_image or "").strip(), _dedupe_str_list(list(model_refs))

    rows_by_uri: dict[str, dict[str, Any]] = {}
    uris: list[str] = []
    for row in stack:
        if not isinstance(row, dict):
            continue
        u = str(row.get("uri") or "").strip()
        if not u or u in rows_by_uri:
            continue
        rows_by_uri[u] = row
        uris.append(u)

    src = (source_image or "").strip()
    mrefs = _dedupe_str_list(list(model_refs))

    # 首帧不在引用栈内（例如上一步分镜工具返回的 /media/...）：必须保留，避免被栈逻辑改回单张人物图。
    if src and src not in rows_by_uri:
        refs = [u for u in uris if u != src]
        refs = _dedupe_str_list(refs + [r for r in mrefs if r != src])
        return src, refs[:8]

    char = [u for u in uris if _row_destination(rows_by_uri[u]) == "character_library"]
    prod = [u for u in uris if _row_destination(rows_by_uri[u]) == "product_library"]

    if len(uris) >= 2 and char and prod:
        if src in char:
            new_src = src
        elif src in prod:
            new_src = char[0]
        elif src in uris:
            new_src = src
        else:
            new_src = char[0]
        refs = [u for u in uris if u != new_src]
        refs = _dedupe_str_list(refs + [r for r in mrefs if r != new_src])
        return new_src, refs[:8]

    prod_set = set(prod)
    non_prod = [u for u in uris if u not in prod_set]
    # 能识别出产品库图、但另一张未被标成「人物」时，仍禁止把白底/单品图当首帧（否则整段像幻灯片推产品）
    if len(uris) >= 2 and prod and non_prod:
        if (not src) or (src in prod) or (src not in uris):
            new_src = non_prod[0]
        elif src in non_prod:
            new_src = src
        else:
            new_src = non_prod[0]
        refs = [u for u in uris if u != new_src]
        refs = _dedupe_str_list(refs + [r for r in mrefs if r != new_src])
        return new_src, refs[:8]

    if len(uris) >= 2:
        if src in uris:
            new_src = src
        else:
            new_src = uris[0]
        refs = [u for u in uris if u != new_src]
        refs = _dedupe_str_list(refs + [r for r in mrefs if r != new_src])
        return new_src, refs[:8]

    refs = [r for r in mrefs if r != src]
    for u in uris:
        if u != src and u not in refs:
            refs.append(u)
    return src, _dedupe_str_list(refs)[:8]


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
    video_written_in_batch = False

    for block in assistant_content:
        if isinstance(block, dict):
            if block.get("type") != "tool_use":
                continue
            tid = str(block.get("id") or block.get("tool_use_id") or "")
            name = str(block.get("name") or "")
            raw = block.get("input")
        else:
            if getattr(block, "type", None) != "tool_use":
                continue
            tid = str(getattr(block, "id", "") or "")
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
                sb_input_images = _stack_uris_ordered_for_image_input()
                # 分镜首帧：服务端固定 9:16、单张（n=1），忽略模型传入的 aspect_ratio / n，保证与竖屏视频链路一致。
                text = run_generate_image(
                    hub,
                    TOOL_SESSION,
                    user_query=str(inp.get("user_query", "")),
                    destination="storyboard_library",
                    asset_name=str(inp.get("asset_name", "") or ""),
                    style_hint=str(inp.get("style_hint", "") or ""),
                    aspect_ratio="9:16",
                    n=1,
                    input_images=sb_input_images or None,
                )
                if not text.strip().startswith("Error:"):
                    sb_pend = persist_image_tool_output(
                        pid,
                        "storyboard_library",
                        text,
                        summary_base=_image_tool_summary_label(inp),
                    )
                    uri_ok = False
                    try:
                        dj = json.loads(text)
                        for raw_u in dj.get("created_asset_ids") or []:
                            u = str(raw_u).strip()
                            if not u:
                                continue
                            local = mirror_http_url_to_local(pid, u)
                            agent_last_storyboard_uri.set((local or u).strip())
                            uri_ok = True
                            break
                    except Exception:
                        agent_last_storyboard_uri.set(None)
                    if uri_ok and isinstance(sb_pend, dict) and sb_pend.get("stored"):
                        agent_pending_storyboard_asset_mirror.set(sb_pend)
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
                if video_written_in_batch:
                    out.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "error": (
                                        "同一轮 assistant 中已成功生成并入库一条成片；"
                                        "已跳过重复的 generate_video_clip，避免视频库出现两条相同请求。"
                                    ),
                                    "skipped_duplicate_video": True,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    continue
                stack_n = len(_deduped_chat_ref_uris())
                lsb_gate = (agent_last_storyboard_uri.get() or "").strip()
                if stack_n >= 2 and not lsb_gate:
                    out.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "error": (
                                        "本条消息引用了 2 张及以上可作首帧的素材，不能直接用人物/产品参考图当视频首帧。"
                                        "请在本轮内先调用 generate_storyboard_image 合成场景首帧，再调用 generate_video_clip，"
                                        "且 source_image 使用分镜返回的 /media/... URI（服务端会把分镜首帧交给视频模型后再写入素材库镜像）。"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    continue
                raw_refs = inp.get("reference_image_urls")
                ref_list: list[str] = []
                if isinstance(raw_refs, list):
                    ref_list = [
                        str(x).strip()
                        for x in raw_refs
                        if isinstance(x, (str, int)) and str(x).strip()
                    ]
                src_model = str(inp.get("source_image", "") or "").strip()
                lsb = (agent_last_storyboard_uri.get() or "").strip()
                if lsb:
                    stack = agent_chat_video_ref_stack.get() or []
                    stack_uris = {
                        str(r.get("uri") or "").strip()
                        for r in stack
                        if isinstance(r, dict) and str(r.get("uri") or "").strip()
                    }
                    # 同轮已出分镜，但模型仍用「引用栈里的人物/产品」当首帧 → 强制改为最新分镜 URI
                    if (not src_model) or (src_model in stack_uris and src_model != lsb):
                        inp = dict(inp)
                        inp["source_image"] = lsb
                src_in, refs_in = _coalesce_video_references(
                    str(inp.get("source_image", "")),
                    ref_list,
                )
                pend = agent_pending_storyboard_asset_mirror.get()
                if (
                    pend
                    and isinstance(pend, dict)
                    and lsb
                    and src_in.strip() == lsb
                ):
                    try:
                        insert_storyboard_asset_library_mirror(
                            pid,
                            storyboard_library_asset_id=int(
                                pend["storyboard_library_asset_id"]
                            ),
                            stored=str(pend["stored"]),
                            display_name=str(pend["display_name"]),
                            meta_base=dict(pend["meta_base"])
                            if isinstance(pend.get("meta_base"), dict)
                            else {},
                        )
                    except (KeyError, TypeError, ValueError):
                        pass
                    else:
                        agent_pending_storyboard_asset_mirror.set(None)
                text = run_generate_video(
                    hub,
                    TOOL_SESSION,
                    prompt=str(inp.get("prompt", "")),
                    source_image=src_in,
                    duration=int(inp.get("duration", 0) or 0),
                    camera_move=str(inp.get("camera_move", "static") or "static"),
                    video_title=str(inp.get("video_title", "") or ""),
                    reference_image_urls=refs_in or None,
                    wait_timeout_seconds=_agent_ark_video_wait_timeout_sec(),
                )
                try:
                    data = json.loads(text)
                    if isinstance(data, dict) and data.get("ok") is not False:
                        persist_video_tool_output(
                            pid,
                            text,
                            summary_base=_video_tool_summary_label(inp),
                            video_prompt=str(inp.get("prompt", "") or ""),
                        )
                        if video_tool_json_indicates_success(data):
                            video_written_in_batch = True
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
