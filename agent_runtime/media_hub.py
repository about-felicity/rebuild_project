"""
懒加载本地媒体 Hub（Wan 生图 + Ark 生视频）。

**功能**：为 ``generation_tools.run_generate_image/video`` 提供统一的 ``model_hub`` 实例，
        避免每次工具调用重复读取环境与构造 Client。

**输入**：无（参数来自环境变量，见 ``tool/ai.MediaGenerationRequestClient.from_environ``）。
**输出**：单例 ``LocalWanArkHub``；若尚未创建则现场构造。

环境变量（节选）：
- ``IMAGE_GENERATION_PROVIDER``：``seedream``（默认，火山 Ark 图生）或 ``wan``（DashScope 万相）。
- ``ARK_IMAGE_MODEL``：Seedream 模型 ID，默认 ``doubao-seedream-5-0-260128``。
- ``STORYBOARD_STRICT_PRODUCT_REF``：默认开启，分镜逐镜出图提示词强制锁产品参考图；``0`` 关闭。
- ``MEDIA_ROOT``：可选，首帧本地路径解析用，与 ``tool/ai`` 一致。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.generation_tools import LocalWanArkHub

_HUB: "LocalWanArkHub | None" = None


def get_media_hub() -> "LocalWanArkHub":
    """返回进程内共享的 ``LocalWanArkHub``。"""
    global _HUB
    if _HUB is None:
        from tool.generation_tools import LocalWanArkHub
        from tool.ai import MediaGenerationRequestClient

        client = MediaGenerationRequestClient.from_environ()
        media_root = os.environ.get("MEDIA_ROOT", "").strip() or None
        _HUB = LocalWanArkHub(client, media_root=media_root)
    return _HUB
