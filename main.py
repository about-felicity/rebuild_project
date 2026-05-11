from pathlib import Path

import asyncio
import json
import mimetypes
import os
import re
import queue as sync_queue
import shutil
import tempfile
import threading
import uuid
from typing import Any
from urllib.parse import unquote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_core import agent_loop, extract_last_assistant_text
from agent_runtime.messages import strip_markdown_images
from agent_runtime.reply_policy import apply_assistant_reply_policy, extract_generation_tool_trace
from agent_runtime.chat_augment import augment_message_for_model
from runtime_ctx import (
    agent_chat_video_ref_stack,
    agent_last_storyboard_uri,
    agent_pending_storyboard_asset_mirror,
    agent_project_id_ctx,
)
from data.db import (
    DB_PATH,
    FRAMEOS_SHARED_PROJECT_ID,
    init_db,
    insert_project_asset,
    load_history_for_model,
    list_chat_messages_for_api,
    list_chat_project_ids_recent_first,
    append_chat_assistant_message,
    append_chat_user_message,
    clear_session as db_clear_session,
    delete_project_asset_owned,
    delete_project_data,
    list_project_assets,
    resolve_chat_referenced_assets,
)
from data.media_mirror import _CT_EXT, _safe_project_segment, ensure_project_media_dir
from data.product_catalog_sync import sync_product_catalog_from_repo
from agent_runtime.douyin_service import douyin_fetch_and_persist
from agent_runtime.media_thumbnail import get_or_create_thumbnail, normalize_src_to_file
from agent_runtime.storyboard_run import run_storyboard_for_project
from agent_runtime.storyboard_video_run import run_storyboard_video_for_project

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

app = FastAPI()

# credentials=True 时浏览器不允许 Allow-Origin: *，8080 静态页 → 8000 API 会被拦。
# 本 API 不依赖浏览器 Cookie，关闭 credentials 即可与 allow_origins=["*"] 并存。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", include_in_schema=False)
def api_health() -> dict[str, bool | str]:
    """进程探活（不读写数据库）；浏览器打不开 /app/ 时先访问此地址确认服务已监听。"""
    return {"ok": True, "service": "frameos"}


_MEDIA_DIR = Path(__file__).resolve().parent / "data" / "media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")

_SAMPLE_ASSETS_DIR = Path(__file__).resolve().parent / "static" / "static" / "sample-assets"
THUMB_ROUTE_ROOTS: list[tuple[str, Path]] = [("/media/", _MEDIA_DIR)]
if _SAMPLE_ASSETS_DIR.is_dir():
    app.mount(
        "/sample-assets",
        StaticFiles(directory=str(_SAMPLE_ASSETS_DIR)),
        name="sample-assets",
    )
    THUMB_ROUTE_ROOTS.append(("/sample-assets/", _SAMPLE_ASSETS_DIR))

# 前端静态页：请用 http://127.0.0.1:8000/app/ （根路径会重定向到此处）
_UI_STATIC_ROOT = Path(__file__).resolve().parent / "static" / "static"


@app.on_event("startup")
def _startup() -> None:
    # 图生视频 / 工具链解析 ``/media/...`` → 本地文件再转 data URL；与 ``agent_runtime.media_hub`` 默认一致
    _mr = Path(__file__).resolve().parent / "data" / "media"
    _mr.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MEDIA_ROOT", str(_mr.resolve()))
    init_db()
    n_cat = sync_product_catalog_from_repo()
    print(
        f"[FrameOS] 数据库: {DB_PATH.resolve()} | 生成素材(本地文件): {_MEDIA_DIR.resolve()}",
        flush=True,
    )
    print(
        f"[FrameOS] 公共产品图（产品图/）已同步至素材库: {n_cat} 条，所有项目可见",
        flush=True,
    )
    if _UI_STATIC_ROOT.is_dir():
        print(
            "[FrameOS] 前端: http://127.0.0.1:8000/app/ （根路径 / 会重定向）",
            flush=True,
        )
    print("[FrameOS] 探活: http://127.0.0.1:8000/api/health", flush=True)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1)
    project: str | None = ""
    project_id: str = Field(..., min_length=1)
    agent_mode: str = Field(
        "normal",
        description="normal | abstract：抽象模式追加互联网鬼才广告导演 persona。",
    )
    referenced_asset_ids: list[int] = Field(
        default_factory=list,
        description="用户在对话中引用的素材库 project_assets.id（图生视频作首帧）。",
    )


def _parse_server_asset_ids_from_user_message(text: str) -> list[int]:
    """
    从用户正文中解析「服务器 id：35、81」一段。
    聊天 UI 会把引用说明追加在气泡里；若 JSON 未带上 referenced_asset_ids，仍可依此文解析并注入 /media/ 块。
    """
    if not text:
        return []
    m = re.search(r"服务器\s*id\s*[：:]\s*([\d、，,\s]+)", text)
    if not m:
        return []
    parts = re.split(r"[、，,\s]+", m.group(1).strip())
    out: list[int] = []
    seen: set[int] = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            n = int(p)
        except ValueError:
            continue
        if n > 0 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _merge_referenced_asset_ids(body_ids: list[int], parsed: list[int]) -> list[int]:
    """请求体中的 id 优先，其后补齐消息里解析到的 id（去重）。"""
    seen: set[int] = set()
    out: list[int] = []
    for x in list(body_ids) + list(parsed):
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n <= 0 or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _asset_row_usable_as_video_first_frame(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind") or "").lower()
    if kind in ("video", "storyboard"):
        return False
    uri = str(row.get("uri") or "").lower().split("?", 1)[0]
    if kind == "image":
        return True
    return any(uri.endswith(suf) for suf in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))


def _infer_reference_semantic_lines(row: dict[str, Any]) -> list[str]:
    """
    根据入库 meta、library、路径等给出「产品 / 人物 / 分镜帧 / 通用」线索，供模型编排 prompt。
    无法从元数据可靠区分「动作示范图 vs 纯背景图」时明确写清，避免瞎猜。
    """
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    dest = str(meta.get("destination") or "").strip()
    library = str(row.get("library") or "").strip().lower()
    kind = str(row.get("kind") or "").strip().lower()
    uri = str(row.get("uri") or "").strip().lower()
    out: list[str] = []
    if dest == "character_library":
        out.append("系统标注：人物/角色素材（Agent 角色库 destination=character_library）")
    elif dest == "product_library":
        out.append("系统标注：产品/包装主视觉（Agent 产品库 destination=product_library）")
    elif dest == "storyboard_library":
        out.append("系统标注：分镜格画面（Agent 分镜库 destination=storyboard_library）")
    elif "/storyboard_runs/" in uri and "/storyboard_frames/" in uri:
        out.append("系统标注：分镜流水线镜头关键帧（路径含 storyboard_frames）")
    elif library == "storyboard" and kind == "storyboard":
        out.append("系统标注：分镜 JSON 条目（非栅格图；一般不作首帧）")
    else:
        out.append(
            "系统标注：通用图片（用户上传或其它来源）；**无法**仅从文件自动判定是「动作示范」"
            "还是「背景/氛围参考」——必须以用户本条消息文字为准；若用户未说明，先简短追问。"
        )
    return out


def _format_referenced_assets_block(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = [
        "【系统注入 · 用户在本条消息中引用的素材库图片】",
        "用途说明：方舟 ``tasks.create`` 的 ``content`` 为 ``text`` + 若干 ``image_url``；当前网关通常要求"
        "每张图带 ``role``（单图 ``first_frame``；多图时全部为 ``reference_image``，与首帧不可混用）。"
        "多图时 **第 1 张即首帧**（source_image），其余为参考。若个别环境可不传 role，设 ``ARK_VIDEO_IMAGE_URL_USE_ROLE=0``。"
        "source_image：若本回合已先 ``generate_storyboard_image`` 成功，则**必须**用其返回的 ``created_asset_ids[0]``，"
        "不要用下列列表「第 1 条」顶替。否则默认下列第 1 条 uri；除非用户明确「用第 N 张作首帧」。"
        "若列表中含路径 ``/_product_catalog/`` 的单品主图：该图**不得**单独作首帧；应先用 ``generate_storyboard_image`` 合成场景格，"
        "或由服务端把非产品那张优先作首帧、产品作参考（你仍应在 prompt 里写清左右手持物与包装一致）。"
        "若本条引用 **≥2 张** 可作首帧的图：**必须先** ``generate_storyboard_image`` 再 ``generate_video_clip``；"
        "否则视频工具会直接报错（禁止用上传的人物/产品单图顶替合成首帧）。分镜入库后，素材库 ``·首帧`` 镜像在**视频开始生成时**才写入。"
        "reference_image_urls 为其余 uri（见工具参数）。",
        "若同时含「产品图 + 人物图」且为两张独立图：通常 **人物作 source_image**（在提示词里写清「图片1」），"
        "**产品图进 reference_image_urls**（提示词里用「图片2」约束包装）；并在 prompt 中显式写"
        "「图片1=…」「图片2=…」以符合方舟多参考引用习惯。",
        "规则：若用户明确要求生成/做视频/图生视频/动起来，且已给出时长（秒），"
        "你必须调用 generate_video_clip；source_image 填所选一条的 **uri**（一般为 /media/...，"
        "服务端会转为 data URL 提交 Ark）；多引用时 **勿漏填 reference_image_urls**。",
        "duration 必须为正整数秒，取自用户原文；未说清时先追问，禁止猜测为 0。",
        "",
    ]
    for i, r in enumerate(rows, 1):
        uri = str(r.get("uri") or "").strip()
        name = str(r.get("name") or "").strip()
        kind = str(r.get("kind") or "").strip()
        lib = str(r.get("library") or "").strip()
        aid = r.get("id")
        lines.append(f"{i}. asset_id={aid} library={lib} kind={kind} name={name}")
        for cue in _infer_reference_semantic_lines(r):
            lines.append(f"   · {cue}")
        lines.append(f"   uri={uri}")
    return "\n".join(lines)

class ChatResponse(BaseModel):
    reply: str
    tool_trace: dict[str, Any] | None = None

class ChatMessageRow(BaseModel):
    role: str
    content: str
    seq: int = 0
    id: int = 0


class MessagesResponse(BaseModel):
    messages: list[ChatMessageRow]


class ChatProjectIdsResponse(BaseModel):
    project_ids: list[str]


class ProjectAssetRow(BaseModel):
    id: int
    project_id: str
    library: str
    kind: str
    name: str
    uri: str
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

class SessionClearedResponse(BaseModel):
    ok: bool = True


class DouyinFetchBody(BaseModel):
    project_id: str = Field(..., min_length=1)
    share_text: str = Field(..., min_length=1)
    video_title: str = ""


class DouyinFetchResponse(BaseModel):
    ok: bool = True
    uri: str


def _agent_failure_http_detail(exc: Exception) -> tuple[int, str, str]:
    """
    返回 (HTTP 状态码, 写入 chat 库的简短说明, 返回给客户端的 detail)。
    503/529/过载等与「网关 502」区分，便于前端提示用户重试。
    """
    raw = str(exc)
    low = raw.lower()
    busy = (
        "503" in raw
        or "529" in raw
        or "service_unavailable" in low
        or "too busy" in low
        or "overloaded" in low
        or "负载" in raw
        or "繁忙" in raw
    )
    if busy:
        msg = (
            "大模型服务暂时繁忙，请稍等几秒后重试；若频繁出现可更换 API 线路或错峰使用。"
        )
        return 503, msg, msg
    if "429" in raw or "rate_limit" in low or "too many requests" in low:
        msg = "请求过于频繁，请稍后再试。"
        return 429, msg, msg
    return 502, raw[:800], raw


@app.post("/api/agent/chat", response_model=ChatResponse)
async def agent_chat(body: ChatBody) -> ChatResponse:
    key = body.project_id.strip()
    if not key:
        raise HTTPException(400, detail="project_id is required")

    user_content = (body.message or "").strip()
    mode = (body.agent_mode or "normal").strip().lower()
    if mode not in ("normal", "abstract"):
        mode = "normal"

    hist: list[dict[str, Any]] = load_history_for_model(key)
    ref_ids = _merge_referenced_asset_ids(
        list(body.referenced_asset_ids or []),
        _parse_server_asset_ids_from_user_message(user_content),
    )
    ref_rows = resolve_chat_referenced_assets(key, ref_ids)
    usable_ref = [r for r in ref_rows if _asset_row_usable_as_video_first_frame(r)]
    ref_block = _format_referenced_assets_block(usable_ref)
    resolved_ids = {int(r["id"]) for r in ref_rows}
    missing_ids = [i for i in ref_ids if i not in resolved_ids]
    if ref_block and missing_ids:
        ref_block += (
            "\n\n【系统提示 · 部分引用未解析】用户声明的 asset_id="
            f"{missing_ids} 在当前项目/公共库素材列表中未找到对应行（不要在 source_image 中编造 URI）。"
        )
    user_for_model = augment_message_for_model(key, user_content)
    if ref_ids and not usable_ref:
        ids_txt = "、".join(str(i) for i in ref_ids)
        user_for_model = (
            "【系统提示】用户尝试引用素材作图生视频首帧（asset_id："
            + ids_txt
            + "），但未能得到可用的 /media/… 栅格图 URI（可能 id 不属于本项目、素材已删除，"
            "或条目为视频/分镜 JSON 等非首帧类型）。请用中文简要说明，并建议用户在素材库中点击图片并使用「引用为 Agent 首帧」。\n\n"
            "---\n\n【用户原文】\n"
            + user_for_model
        )
    elif ref_block:
        user_for_model = ref_block + "\n\n---\n\n【用户原文】\n" + user_for_model
    hist.append({"role": "user", "content": user_for_model})
    append_chat_user_message(key, user_content)

    video_ref_rows: list[dict[str, Any]] = []
    for r in usable_ref:
        u = str(r.get("uri") or "").strip()
        if not u:
            continue
        meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
        video_ref_rows.append(
            {
                "uri": u,
                "meta": dict(meta),
                "library": str(r.get("library") or ""),
                "kind": str(r.get("kind") or ""),
                "name": str(r.get("name") or ""),
            }
        )

    def _run_agent_in_worker() -> None:
        t_pid = agent_project_id_ctx.set(key)
        t_vid = agent_chat_video_ref_stack.set(video_ref_rows)
        t_sb = agent_last_storyboard_uri.set(None)
        t_mirr = agent_pending_storyboard_asset_mirror.set(None)
        try:
            agent_loop(hist, mode)
        finally:
            agent_pending_storyboard_asset_mirror.reset(t_mirr)
            agent_last_storyboard_uri.reset(t_sb)
            agent_chat_video_ref_stack.reset(t_vid)
            agent_project_id_ctx.reset(t_pid)

    try:
        await asyncio.to_thread(_run_agent_in_worker)
    except Exception as e:
        status, for_db, detail = _agent_failure_http_detail(e)
        try:
            append_chat_assistant_message(key, f"（生成失败）{for_db}")
        except Exception:
            pass
        raise HTTPException(status, detail=detail) from e

    reply = extract_last_assistant_text(hist)
    reply = strip_markdown_images(reply)
    reply = apply_assistant_reply_policy(hist, reply)
    tool_trace = extract_generation_tool_trace(hist)

    append_chat_assistant_message(key, reply)
    return ChatResponse(reply=reply, tool_trace=tool_trace or None)


@app.get(
    "/api/agent/chat-project-ids",
    response_model=ChatProjectIdsResponse,
)
async def list_chat_project_ids() -> ChatProjectIdsResponse:
    """有聊天记录的 project_id（最近活跃在前），用于浏览器 localStorage 丢失或换域名时恢复侧边栏。"""
    return ChatProjectIdsResponse(project_ids=list_chat_project_ids_recent_first())


@app.get(
    "/api/agent/sessions/{project_id}/messages",
    response_model=MessagesResponse,
)
async def get_session_messages(project_id: str) -> MessagesResponse:
    rows = list_chat_messages_for_api(project_id.strip())
    return MessagesResponse(messages=[ChatMessageRow(**r) for r in rows])


@app.delete(
    "/api/agent/sessions/{project_id}",
    response_model=SessionClearedResponse,
)
async def clear_session(project_id: str) -> SessionClearedResponse:
    db_clear_session(project_id.strip())
    return SessionClearedResponse(ok=True)


@app.delete(
    "/api/projects/{project_id}",
    response_model=SessionClearedResponse,
)
def delete_project(project_id: str) -> SessionClearedResponse:
    """删除该 project_id 的聊天记录与 project_assets 行（与前端「删除项目」对齐）。"""
    if project_id.strip() == FRAMEOS_SHARED_PROJECT_ID:
        raise HTTPException(
            status_code=403,
            detail="公共产品图库不可删除（内部项目 __frameos_shared__）。",
        )
    delete_project_data(project_id.strip())
    return SessionClearedResponse(ok=True)


def _unlink_owned_media_file(uri: str, project_id: str) -> None:
    """若 ``uri`` 指向本项目 ``data/media/<segment>/`` 下单一文件则删除（防路径穿越）。"""
    raw = (uri or "").strip()
    if not raw.startswith("/media/"):
        return
    path = unquote(raw.split("?")[0])
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or parts[0] != "media":
        return
    seg, *rest = parts[1], parts[2:]
    if not rest:
        return
    if seg != _safe_project_segment(project_id):
        return
    rel = "/".join(rest).replace("\\", "/")
    if not rel or ".." in rel.split("/"):
        return
    root = (_MEDIA_DIR / seg).resolve()
    full = (root / rel).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return
    if full.is_file():
        full.unlink(missing_ok=True)


@app.delete(
    "/api/projects/{project_id}/assets/{asset_id}",
    response_model=SessionClearedResponse,
)
def delete_project_asset(project_id: str, asset_id: int) -> SessionClearedResponse:
    """删除当前项目名下一条素材（库内行 + 若为本项目目录下 ``/media/`` 文件则一并删磁盘）。"""
    pid = project_id.strip()
    if pid == FRAMEOS_SHARED_PROJECT_ID:
        raise HTTPException(status_code=403, detail="不可通过此接口操作公共素材库项目。")
    uri = delete_project_asset_owned(pid, asset_id)
    if uri is None:
        raise HTTPException(
            status_code=404,
            detail="素材不存在或不属于该项目（公共目录合并项请在仓库侧维护）。",
        )
    _unlink_owned_media_file(uri, pid)
    return SessionClearedResponse(ok=True)


@app.post("/api/douyin/fetch", response_model=DouyinFetchResponse)
def http_douyin_fetch(body: DouyinFetchBody) -> DouyinFetchResponse:
    """解析抖音分享并保存为当前项目视频库素材。"""
    try:
        out = douyin_fetch_and_persist(
            body.project_id.strip(),
            body.share_text.strip(),
            video_title=(body.video_title or "").strip(),
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(502, detail=str(e)[:800]) from e
    return DouyinFetchResponse(ok=True, uri=str(out["uri"]))


def _storyboard_upload_dest_name(original: str, index: int, ext: str) -> str:
    """
    临时落盘文件名保留上传时的可读信息（品类/项目名），供分镜 Step0 把「引用文件名」一并交给 Vision。
    """
    raw = (original or "").strip() or f"product_{index}"
    stem = Path(Path(raw).name).stem
    stem = re.sub(r"[^\w\-. \u4e00-\u9fff]", "_", stem).strip("._") or f"product_{index}"
    stem = stem[:70]
    return f"{index:02d}_{stem}{ext}"


def _form_bool(raw: str | None, default: bool = True) -> bool:
    s = (raw or "").strip().lower()
    if s in ("0", "false", "no", "off", ""):
        return False
    if s in ("1", "true", "yes", "on"):
        return True
    return default


@app.post("/api/projects/{project_id}/storyboard/stream")
async def http_storyboard_pipeline_stream(
    project_id: str,
    description: str = Form(..., min_length=1),
    hotword: str = Form(..., min_length=1),
    images: list[UploadFile] = File(...),
    fps: int = Form(24),
    target_duration_sec: int = Form(30),
    style: str = Form("写实"),
    generate_shot_images: str = Form("true"),
    write_prompt_preview: str = Form("true"),
    product_ref_index: str | None = Form(
        default=None,
        description="可选，0-based：强制指定第几张上传图为万相参考；留空则由 Step0 识别产品主图",
    ),
):
    """
    multipart 上传 1–3 张产品图，SSE 推送流水线进度；结果写入当前项目目录并登记分镜库。
    """
    pid = project_id.strip()
    if not pid:
        raise HTTPException(400, detail="project_id 无效")
    files = list(images)
    if not (1 <= len(files) <= 3):
        raise HTTPException(400, detail="请上传 1–3 张产品图")

    tmp_root = Path(tempfile.mkdtemp(prefix="frameos_sb_"))
    saved: list[Path] = []
    try:
        for i, uf in enumerate(files):
            raw = await uf.read()
            if not raw:
                raise HTTPException(400, detail=f"第 {i + 1} 个文件为空")
            name = (uf.filename or f"product_{i}.jpg").strip()
            ext = Path(name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            dest = tmp_root / _storyboard_upload_dest_name(name, i, ext)
            dest.write_bytes(raw)
            saved.append(dest)
    except HTTPException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise

    gen_img = _form_bool(generate_shot_images, True)
    write_prev = _form_bool(write_prompt_preview, True)

    pri_override: int | None = None
    if product_ref_index is not None and str(product_ref_index).strip() != "":
        try:
            pri_override = int(str(product_ref_index).strip())
        except ValueError:
            pri_override = None

    q: sync_queue.Queue[Any] = sync_queue.Queue()

    def worker() -> None:
        try:
            run_storyboard_for_project(
                pid,
                saved,
                description,
                hotword,
                fps=fps,
                target_duration_sec=target_duration_sec,
                style=style,
                generate_shot_images=gen_img,
                write_prompt_preview=write_prev,
                product_ref_index=pri_override,
                callback=q.put,
            )
        except Exception as e:
            q.put({"type": "error", "message": str(e)[:2000]})
        finally:
            q.put(None)
            shutil.rmtree(tmp_root, ignore_errors=True)

    threading.Thread(target=worker, daemon=True).start()

    async def event_gen():
        while True:
            item: Any = await asyncio.to_thread(q.get)
            if item is None:
                break
            line = "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            yield line.encode("utf-8")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_RUN_ID_DIR_RE = re.compile(r"^[a-fA-F0-9]{12,32}$")


def _list_storyboard_runs_disk(project_id: str) -> list[dict[str, Any]]:
    """扫描 ``data/media/<seg>/storyboard_runs/<run_id>/``，供前端下拉选择后直连成片。"""
    seg, proj_dir = ensure_project_media_dir(project_id.strip())
    runs_root = proj_dir / "storyboard_runs"
    out: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return out
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        run_id = child.name
        if not _RUN_ID_DIR_RE.match(run_id):
            continue
        jp = child / "storyboard.json"
        script_md = child / "storyboard_script.md"
        script_prompts = child / "storyboard_script_prompts.md"
        has_script_md = script_md.is_file()
        if has_script_md:
            script_uri = f"/media/{seg}/storyboard_runs/{run_id}/storyboard_script.md"
        elif script_prompts.is_file():
            script_uri = f"/media/{seg}/storyboard_runs/{run_id}/storyboard_script_prompts.md"
        else:
            script_uri = None
        json_uri = f"/media/{seg}/storyboard_runs/{run_id}/storyboard.json" if jp.is_file() else None

        shot_count: int | None = None
        shots_with_image: int | None = None
        mtime = 0.0
        if jp.is_file():
            try:
                mtime = jp.stat().st_mtime
                data = json.loads(jp.read_text(encoding="utf-8"))
                shots = data.get("shots")
                if isinstance(shots, list):
                    shot_count = len(shots)
                    shots_with_image = sum(
                        1
                        for s in shots
                        if isinstance(s, dict)
                        and str((s.get("frame") or {}).get("image_url") or "").strip()
                    )
            except Exception:
                mtime = jp.stat().st_mtime if jp.is_file() else child.stat().st_mtime
        else:
            mtime = child.stat().st_mtime

        ready = bool(
            jp.is_file()
            and shot_count is not None
            and shot_count > 0
            and shots_with_image == shot_count
        )
        label_parts = [f"{run_id[:8]}…"]
        if has_script_md:
            label_parts.append("storyboard_script.md")
        if shot_count is not None:
            label_parts.append(f"{shots_with_image or 0}/{shot_count} 镜有图")
        out.append(
            {
                "run_id": run_id,
                "label": " · ".join(label_parts),
                "has_json": jp.is_file(),
                "has_script_md": has_script_md,
                "json_uri": json_uri,
                "script_uri": script_uri,
                "shot_count": shot_count,
                "shots_with_image": shots_with_image,
                "ready_for_video": ready,
                "updated_at": int(mtime),
            }
        )
    out.sort(key=lambda x: -int(x.get("updated_at") or 0))
    return out


@app.get("/api/projects/{project_id}/storyboard/runs")
def http_list_storyboard_runs(project_id: str) -> list[dict[str, Any]]:
    """列出本项目磁盘上的分镜 run（含是否已有 script / 是否可成片）。"""
    pid = project_id.strip()
    if not pid:
        raise HTTPException(400, detail="project_id 无效")
    return _list_storyboard_runs_disk(pid)


@app.post("/api/projects/{project_id}/storyboard/video/stream")
async def http_storyboard_video_pipeline_stream(
    project_id: str,
    run_id: str = Form(..., min_length=12, max_length=32),
):
    """
    分镜成片：读取已写入的 ``storyboard_runs/<run_id>/storyboard.json``，
    逐镜 Ark 图生视频、ffmpeg 拼接、登记视频库；SSE 推送进度。
    """
    pid = project_id.strip()
    if not pid:
        raise HTTPException(400, detail="project_id 无效")

    q: sync_queue.Queue[Any] = sync_queue.Queue()

    def worker() -> None:
        try:
            run_storyboard_video_for_project(
                pid,
                run_id.strip(),
                callback=q.put,
            )
        except Exception as e:
            q.put({"type": "error", "message": str(e)[:2000]})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def event_gen():
        while True:
            item: Any = await asyncio.to_thread(q.get)
            if item is None:
                break
            line = "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            yield line.encode("utf-8")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_UPLOAD_VIDEO_EXT = frozenset({".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"})
_UPLOAD_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"})
_UPLOAD_AUDIO_EXT = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"})


def _classify_upload(filename: str, content_type: str | None) -> tuple[str, str, str]:
    """返回 (library, kind, 磁盘扩展名)。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    suf = Path(filename).suffix.lower()
    if ct.startswith("video/"):
        ext = suf if suf in _UPLOAD_VIDEO_EXT else _CT_EXT.get(ct, ".mp4")
        return "video", "video", ext
    if ct.startswith("image/"):
        ext = suf if suf in _UPLOAD_IMAGE_EXT else _CT_EXT.get(ct, ".png")
        return "asset", "image", ext
    if ct.startswith("audio/"):
        ext = suf if suf in _UPLOAD_AUDIO_EXT else _CT_EXT.get(ct, ".mp3")
        return "asset", "audio", ext
    if suf in _UPLOAD_VIDEO_EXT:
        return "video", "video", suf
    if suf in _UPLOAD_IMAGE_EXT:
        return "asset", "image", suf
    if suf in _UPLOAD_AUDIO_EXT:
        return "asset", "audio", suf
    return "asset", "image", ".bin"


@app.post("/api/projects/{project_id}/assets", response_model=ProjectAssetRow)
async def http_upload_project_asset(
    project_id: str,
    file: UploadFile = File(...),
) -> ProjectAssetRow:
    """
    浏览器上传素材：落盘到 ``data/media/<project>/``，登记 ``project_assets``，
    返回 ``uri`` 为 ``/media/...``（刷新后仍可用）。
    """
    pid = project_id.strip()
    if not pid:
        raise HTTPException(status_code=400, detail="project_id 无效")
    if pid == FRAMEOS_SHARED_PROJECT_ID:
        raise HTTPException(status_code=403, detail="不可向公共素材库项目上传")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    orig = (file.filename or "upload").strip() or "upload"
    safe_name = Path(orig).name
    if len(safe_name) > 180:
        safe_name = safe_name[:180]

    lib, kind, ext = _classify_upload(safe_name, file.content_type)
    if ext == ".bin" and kind == "image":
        ext = ".png"

    seg, proj_dir = ensure_project_media_dir(pid)
    disk_name = f"{uuid.uuid4().hex}{ext}"
    dest = proj_dir / disk_name
    dest.write_bytes(raw)
    uri = f"/media/{seg}/{disk_name}"

    meta: dict[str, Any] = {"size": len(raw)}
    ct0 = (file.content_type or "").split(";")[0].strip()
    if ct0:
        meta["content_type"] = ct0

    aid = insert_project_asset(pid, lib, kind, safe_name, uri, meta=meta)
    return ProjectAssetRow(
        id=aid,
        project_id=pid,
        library=lib,
        kind=kind,
        name=safe_name,
        uri=uri,
        meta=meta,
        created_at=None,
    )


@app.get("/api/projects/{project_id}/assets", response_model=list[ProjectAssetRow])
def http_list_project_assets(project_id: str, library: str | None = None):
    rows = list_project_assets(project_id.strip(), library)
    return [
        ProjectAssetRow(
            id=r["id"],
            project_id=r["project_id"],
            library=r["library"],
            kind=r["kind"],
            name=r["name"],
            uri=r["uri"],
            meta=r.get("meta") or {},
            created_at=r.get("created_at"),
        )
        for r in rows
    ]


@app.get("/api/assets/thumbnail/")
async def http_asset_thumbnail(
    src: str = Query(..., min_length=1),
    w: int = Query(360, ge=1, le=800),
    h: int = Query(220, ge=1, le=800),
    kind: str = Query("image"),
) -> Response:
    """
    本地 ``/media/``、``/sample-assets/`` 文件的 JPEG 缩略图；磁盘缓存在 ``data/media/cache/thumbs/``。
    """
    k = (kind or "image").strip().lower()
    if k not in ("image", "video"):
        raise HTTPException(status_code=400, detail="kind must be image or video")
    path = normalize_src_to_file(src, THUMB_ROUTE_ROOTS)
    if path is None:
        raise HTTPException(status_code=404, detail="source not found")
    data = await asyncio.to_thread(get_or_create_thumbnail, _MEDIA_DIR, path, w, h, k)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/assets/file/")
def http_asset_file(src: str = Query(..., min_length=1)) -> FileResponse:
    """
    同源读取 ``/media/``、``/sample-assets/`` 原文件（供前端 fetch→Blob，避免静态页与挂载目录跨域）。
    """
    path = normalize_src_to_file(src, THUMB_ROUTE_ROOTS)
    if path is None:
        raise HTTPException(status_code=404, detail="source not found")
    ctype, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=ctype or "application/octet-stream",
        filename=path.name,
        headers={"Cache-Control": "private, max-age=120"},
    )


@app.get("/", include_in_schema=False, response_model=None)
def frameos_root() -> RedirectResponse | dict[str, str]:
    """根路径无前端时返回提示；有 static/static 则进入 /app/ 单页。"""
    if _UI_STATIC_ROOT.is_dir():
        return RedirectResponse(url="/app/", status_code=307)
    return {
        "service": "frameos",
        "docs": "/docs",
        "hint": "前端未找到：请确认存在 static/static，或使用 /docs 调试 API。",
    }


if _UI_STATIC_ROOT.is_dir():
    app.mount(
        "/app",
        StaticFiles(directory=str(_UI_STATIC_ROOT), html=True),
        name="frameos_ui",
    )