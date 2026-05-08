from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Any
from pydantic import BaseModel, Field
import asyncio
from agent_core import agent_loop, extract_last_assistant_text
from agent_runtime.messages import strip_markdown_images
from agent_runtime.reply_policy import apply_assistant_reply_policy
from agent_runtime.chat_augment import augment_message_for_model
from runtime_ctx import agent_project_id_ctx
from data.db import (
    DB_PATH,
    init_db,
    load_history_for_model,
    list_chat_messages_for_api,
    list_chat_project_ids_recent_first,
    append_chat_assistant_message,
    append_chat_user_message,
    clear_session as db_clear_session,
    delete_project_data,
    list_project_assets,
)
from agent_runtime.douyin_service import douyin_fetch_and_persist

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_MEDIA_DIR = Path(__file__).resolve().parent / "data" / "media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    print(
        f"[FrameOS] 数据库: {DB_PATH.resolve()} | 生成素材(本地文件): {_MEDIA_DIR.resolve()}",
        flush=True,
    )


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1)
    project: str | None = ""
    project_id: str = Field(..., min_length=1)

class ChatResponse(BaseModel):
    reply: str

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

    token = agent_project_id_ctx.set(key)
    user_content = (body.message or "").strip()

    hist: list[dict[str, Any]] = load_history_for_model(key)
    user_for_model = augment_message_for_model(key, user_content)
    hist.append({"role": "user", "content": user_for_model})
    append_chat_user_message(key, user_content)

    try:
        await asyncio.to_thread(agent_loop, hist)
    except Exception as e:
        status, for_db, detail = _agent_failure_http_detail(e)
        try:
            append_chat_assistant_message(key, f"（生成失败）{for_db}")
        except Exception:
            pass
        raise HTTPException(status, detail=detail) from e
    finally:
        agent_project_id_ctx.reset(token)

    reply = extract_last_assistant_text(hist)
    reply = strip_markdown_images(reply)
    reply = apply_assistant_reply_policy(hist, reply)

    append_chat_assistant_message(key, reply)
    return ChatResponse(reply=reply)


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
    delete_project_data(project_id.strip())
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