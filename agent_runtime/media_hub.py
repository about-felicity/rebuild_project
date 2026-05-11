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
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.generation_tools import LocalWanArkHub

_HUB: "LocalWanArkHub | None" = None
_HUB_MEDIA_ROOT_KEY: str | None = None


def _default_media_root_abs() -> str:
    """仓库内 ``data/media``，与 ``main`` 挂载 ``/media`` 的目录一致。"""
    root = Path(__file__).resolve().parent.parent / "data" / "media"
    root.mkdir(parents=True, exist_ok=True)
    return str(root.resolve())


def get_media_hub() -> "LocalWanArkHub":
    """
    返回进程内共享的 ``LocalWanArkHub``。

    ``MEDIA_ROOT`` 未设置时默认使用 ``<repo>/data/media``，以便 ``/media/...`` 首帧转 data URL。
    若运行中修改了 ``MEDIA_ROOT``，会重建实例以免沿用过期的 ``media_root``。
    """
    global _HUB, _HUB_MEDIA_ROOT_KEY
    from tool.generation_tools import LocalWanArkHub
    from tool.ai import MediaGenerationRequestClient

    env = os.environ.get("MEDIA_ROOT", "").strip()
    media_root = env if env else _default_media_root_abs()
    if _HUB is not None and _HUB_MEDIA_ROOT_KEY == media_root:
        return _HUB

    client = MediaGenerationRequestClient.from_environ()
    _HUB = LocalWanArkHub(client, media_root=media_root)
    _HUB_MEDIA_ROOT_KEY = media_root
    return _HUB
