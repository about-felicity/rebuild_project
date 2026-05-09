"""
阿里云百炼 万相 2.7 分镜出图（与 docu.md / DashScope ImageGeneration 一致）。

环境变量：
  DASHSCOPE_API_KEY  必填
  DASHSCOPE_BASE_URL 可选，默认北京 https://dashscope.aliyuncs.com/api/v1
                      新加坡：https://dashscope-intl.aliyuncs.com/api/v1
  DASHSCOPE_WAN_USE_SYNC  若设为 1/true，强制走同步 ImageGeneration.call（易触发约 298s 流式超时，不推荐）

依赖：pip install dashscope>=1.25.15

说明：同步 call 在服务端排队稍久时会报「Response stream timeout (~298s)」；
默认改为 async_call + wait 轮询任务，与 docu.md 异步示例一致，可避免该限制。
"""

from __future__ import annotations

import mimetypes
import os
import re
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Optional

_MAX_PROMPT_CHARS = 5000


def configure_dashscope() -> None:
    import dashscope

    base = os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/api/v1",
    ).rstrip("/")
    dashscope.base_http_api_url = base


def encode_file_data_uri(file_path: str | Path) -> str:
    """data:{mime};base64,... 格式，与官方文档一致。"""
    p = Path(file_path)
    mime_type, _ = mimetypes.guess_type(str(p))
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    raw = p.read_bytes()
    import base64

    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def _safe_shot_filename(shot_id: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", shot_id.strip())
    return s or "shot"


def _task_status(rsp: Any) -> Optional[str]:
    out = rsp.output
    if out is None:
        return None
    if isinstance(out, dict):
        return str(out.get("task_status") or "")
    return str(getattr(out, "task_status", "") or "")


def _extract_image_url_from_response(rsp: Any) -> str:
    out = rsp.output
    choices = getattr(out, "choices", None)
    if choices is None and isinstance(out, dict):
        choices = out.get("choices")
    if not choices:
        raise RuntimeError("万相响应中无 choices")
    for choice in choices:
        msg = choice.get("message") if isinstance(choice, dict) else choice.message
        if msg is None:
            continue
        contents = msg.get("content") if isinstance(msg, dict) else msg.content
        if not contents:
            continue
        for content in contents:
            if isinstance(content, dict):
                typ = content.get("type")
                url = content.get("image")
            else:
                typ = getattr(content, "type", None)
                url = getattr(content, "image", None)
            if typ == "image" and url:
                return str(url)
    raise RuntimeError("万相响应中未找到 image URL")


def _coerce_wan_reference_paths(
    reference_paths: str | Path | Sequence[str | Path],
) -> list[Path]:
    if isinstance(reference_paths, (str, Path)):
        paths = [Path(reference_paths)]
    else:
        paths = [Path(p) for p in reference_paths]
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(f"参考图不存在: {p}")
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    if not out:
        raise ValueError("至少一张参考图")
    if len(out) > 2:
        out = out[:2]
    return out


def wan_generate_shot_frame(
    text_prompt: str,
    reference_paths: str | Path | Sequence[str | Path],
    *,
    api_key: Optional[str] = None,
    model: str = "wan2.7-image",
    size: str = "2K",
    watermark: bool = False,
) -> str:
    """
    参考图 + 文本提示生成分镜单帧。支持 1 张（仅产品）或 2 张（人物 + 产品，顺序须为图1人物、图2产品）。
    默认异步任务（async_call + wait）。
    """
    try:
        from dashscope.aigc.image_generation import ImageGeneration
        from dashscope.api_entities.dashscope_response import Message
    except ImportError as e:
        raise ImportError("请安装 dashscope>=1.25.15：pip install dashscope") from e

    key = api_key or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")

    configure_dashscope()
    paths = _coerce_wan_reference_paths(reference_paths)
    image_parts = [{"image": encode_file_data_uri(p)} for p in paths]
    if len(paths) == 1:
        suffix = (
            "参考图1中的产品外观、瓶型、标签配色与材质，生成电影分镜单帧静态图，"
            "严格符合上述构图与光影描述，画面无字幕、无画框、无水印式装饰。"
        )
    else:
        suffix = (
            "【双参考】参考图1为人物模特：须保持其面部、发型、须型、体型与衣着与图1一致，不得换脸成其他模特。"
            "参考图2为产品包装：画面中出现该商品时，瓶型、标签、LOGO 与主色须与图2完全一致，不得替换为其他品牌或臆造包装。"
            "在以上构图与光影描述基础上生成电影分镜单帧，无字幕、无画框、无水印式装饰。"
        )
    body = (f"{text_prompt.strip()}\n\n{suffix}")[:_MAX_PROMPT_CHARS]

    message = Message(
        role="user",
        content=[{"text": body}, *image_parts],
    )

    use_sync = os.getenv("DASHSCOPE_WAN_USE_SYNC", "").lower() in ("1", "true", "yes")

    if use_sync:
        rsp = ImageGeneration.call(
            model=model,
            api_key=key,
            messages=[message],
            watermark=watermark,
            n=1,
            size=size,
        )
        code = getattr(rsp, "status_code", None)
        if code != 200:
            msg = getattr(rsp, "message", "") or getattr(rsp, "code", "")
            raise RuntimeError(f"万相生图失败 status={code} {msg}")
        return _extract_image_url_from_response(rsp)

    task_rsp = ImageGeneration.async_call(
        model=model,
        api_key=key,
        messages=[message],
        watermark=watermark,
        n=1,
        size=size,
    )
    if getattr(task_rsp, "status_code", None) != 200:
        msg = getattr(task_rsp, "message", "") or getattr(task_rsp, "code", "")
        raise RuntimeError(f"万相创建异步任务失败 status={task_rsp.status_code} {msg}")

    print("    → 已提交异步生图任务，排队/生成中（不受单次 298s 流式读超时限制）…")
    rsp = ImageGeneration.wait(task=task_rsp, api_key=key)
    if getattr(rsp, "status_code", None) != 200:
        msg = getattr(rsp, "message", "") or getattr(rsp, "code", "")
        raise RuntimeError(f"万相查询任务失败 status={rsp.status_code} {msg}")

    st = _task_status(rsp)
    if st and st != "SUCCEEDED":
        msg = getattr(rsp, "message", "") or ""
        raise RuntimeError(f"万相任务未成功: task_status={st} {msg}".strip())

    return _extract_image_url_from_response(rsp)


def download_image_url(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, str(dest))


def fill_storyboard_shots_with_wan(
    shots: list[dict[str, Any]],
    product_reference_image_path: str | Path,
    frames_directory: str | Path,
    relative_url_prefix: str,
    *,
    character_reference_image_path: str | Path | None = None,
    api_key: Optional[str] = None,
    model: str = "wan2.7-image",
    size: str = "2K",
    watermark: bool = False,
    delay_sec: float = 0.35,
    on_shot_done: Optional[Callable[[int, int, str, str], None]] = None,
) -> None:
    """
    逐镜调用万相：使用每镜的 generated_prompts.image_prompt；
    将 PNG 保存到 frames_directory，并把 frame.image_url 设为相对路径
    「relative_url_prefix/safe_shot_id.png」（POSIX 斜杠）。

    若提供 character_reference_image_path 且与产品图不是同一文件，则万相输入顺序为：**图1 人物、图2 产品**。
    """
    prod = Path(product_reference_image_path)
    if not prod.is_file():
        raise FileNotFoundError(f"参考产品图不存在: {prod}")

    char: Optional[Path] = None
    if character_reference_image_path is not None:
        c = Path(character_reference_image_path)
        if c.is_file() and c.resolve() != prod.resolve():
            char = c

    ref_paths: list[Path] = [char, prod] if char else [prod]

    out_dir = Path(frames_directory)
    prefix = relative_url_prefix.strip().strip("/")

    for i, shot in enumerate(shots):
        gp = shot.get("generated_prompts") or {}
        prompt = str(gp.get("image_prompt") or "").strip()
        if not prompt:
            raise ValueError(f"镜头 {shot.get('shot_id')} 缺少 generated_prompts.image_prompt")

        sid = _safe_shot_filename(str(shot.get("shot_id") or f"shot_{i}"))
        fname = f"{sid}.png"
        print(f"  [WAN {i + 1}/{len(shots)}] {shot.get('shot_id')} 生图中...")

        url = wan_generate_shot_frame(
            prompt,
            ref_paths,
            api_key=api_key,
            model=model,
            size=size,
            watermark=watermark,
        )
        dest = out_dir / fname
        download_image_url(url, dest)

        frame = shot.setdefault("frame", {})
        frame["image_url"] = f"{prefix}/{fname}" if prefix else fname

        if on_shot_done:
            try:
                on_shot_done(i + 1, len(shots), str(shot.get("shot_id") or sid), str(frame["image_url"]))
            except Exception:
                pass

        if delay_sec > 0 and i + 1 < len(shots):
            time.sleep(delay_sec)
