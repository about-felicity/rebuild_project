from contextvars import ContextVar
from typing import Any

agent_project_id_ctx: ContextVar[str | None] = ContextVar("agent_project_id", default=None)

# 本轮 /api/agent/chat 中解析到的、可作图生视频的引用素材（与 agent_loop 同一线程内读写）。
agent_chat_video_ref_stack: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "agent_chat_video_ref_stack",
    default=None,
)

# 本轮 /api/agent/chat 内最近一次成功的 Agent 分镜首帧 URI（/media/…），供同轮 generate_video_clip 纠偏首帧。
agent_last_storyboard_uri: ContextVar[str | None] = ContextVar(
    "agent_last_storyboard_uri",
    default=None,
)

# 分镜已写入「分镜库」后、待「视频开始生成」时再镜像到「素材库」的元数据（同一线程内消费一次）。
agent_pending_storyboard_asset_mirror: ContextVar[dict[str, Any] | None] = (
    ContextVar("agent_pending_storyboard_asset_mirror", default=None)
)