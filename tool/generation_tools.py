"""
Standalone image / video **tool orchestration** (prompt assembly + poll).

本模块**默认可独立跑通**项目当前栈：:mod:`ai`（DashScope 万相 Wan + 火山 Ark SDK）+
:class:`LocalWanArkHub`。也可只实现 :class:`MediaGenerationHub`，把任意 ``model_hub``
传入 ``run_generate_image`` / ``run_generate_video``。

---------------------------------------------------------------------------
WorkFlow Django 主工程：

- Agent 默认 ``model_hub`` 仍由 ``agentcore.model_hub.UnifiedModelHub`` 注入（见
  ``agentcore.django_media.get_default_media_backend``）。
- 无 Django / 脚本调用：``tool_generate_image`` / ``tool_generate_video`` 或自行组装
  ``LocalWanArkHub(MediaGenerationRequestClient.from_environ())``。

---------------------------------------------------------------------------
Expected task terminal ``status`` values: ``success``, ``failed`` (typical Django AgentTask).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from urllib.request import urlopen
from uuid import uuid4

from ai import (
    MediaGenerationRequestClient,
    extract_wan_image_urls_from_response,
    normalize_first_frame_url_for_ark,
)
from seedream_client import (
    SeedreamImageClient,
    collect_result_image_urls,
    coerce_seedream_size,
)

# —— destination → provider asset category (strings your hub understands) ——
DST_TO_ASSET_CATEGORY: dict[str, str] = {
    "character_library": "character",
    "product_library": "product",
    "storyboard_library": "storyboard",
}

_DEFAULT_IMAGE_BATCH_CAP = 6

_DST_ZH = {
    "character_library": "角色素材",
    "product_library": "产品素材",
    "storyboard_library": "分镜",
    "reference_library": "参考素材",
}

_TITLE_I2I_EN_BOILERPLATE_PREFIXES = (
    "replace only",
    "use named references",
    "[image-to-image edit",
    "[product three-view",
    "use the first attached",
    "use product_ref",
)

LayeredImagePromptFn = Callable[[str, str], str]
LayeredVideoPromptFn = Callable[[str, list[str] | None], str]


def _image_generation_provider() -> str:
    """``wan``=DashScope 万相；默认 ``seedream``=火山 Ark ``/images/generations``。"""
    v = os.getenv("IMAGE_GENERATION_PROVIDER", "seedream").strip().lower()
    return "wan" if v == "wan" else "seedream"


def _ark_image_model() -> str:
    return os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128").strip()


@runtime_checkable
class MediaGenerationHub(Protocol):
    """
    最小「媒体生成后端」协议：换模型 / 换 SDK 时实现本接口即可对接 ``run_generate_*``。

    实现类内部自行保存 API Key、model_id、client 等；本模块只负责拼 prompt 与轮询。
    """

    def submit_image_generation(self, **kwargs: Any) -> Any: ...

    def submit_video_generation(self, **kwargs: Any) -> Any: ...

    def refresh_generation_task(self, task: Any) -> Any: ...


@dataclass(frozen=True)
class WorkflowMediaEnvSnapshot:
    """
    与 ``backend/settings.py`` 中 ``MODEL_CONFIG`` 默认读取方式对齐的环境变量快照（仅展示 / 日志用）。

    不包含密钥值，只包含「当前进程环境里能读到的模型名与 provider 开关」。
    """

    image_generation_provider: str
    ark_image_model: str
    dashscope_image_model: str
    dashscope_base_http: str
    video_provider: str
    ark_video_model: str

    @classmethod
    def from_environ(cls) -> WorkflowMediaEnvSnapshot:
        return cls(
            image_generation_provider=_image_generation_provider(),
            ark_image_model=_ark_image_model(),
            dashscope_image_model=os.getenv("DASHSCOPE_IMAGE_MODEL", "wan2.7-image-pro").strip(),
            dashscope_base_http=os.getenv(
                "DASHSCOPE_BASE_HTTP_API_URL",
                "https://dashscope.aliyuncs.com/api/v1",
            ).strip(),
            video_provider=os.getenv("VIDEO_PROVIDER", "ark").strip().lower(),
            ark_video_model=os.getenv(
                "ARK_VIDEO_MODEL", "doubao-seedance-1-5-pro-251215"
            ).strip(),
        )


def format_workflow_media_routing_help() -> str:
    """多行说明：主工程默认走哪条链、该改哪些环境变量。"""
    snap = WorkflowMediaEnvSnapshot.from_environ()
    return (
        "WorkFlow 默认媒体路由（见 backend/settings.MODEL_CONFIG）:\n"
        f"  生图 provider: IMAGE_GENERATION_PROVIDER → {snap.image_generation_provider!r} "
        "(seedream=火山 Ark POST /images/generations；wan=DashScope 万相)\n"
        f"  (seedream) ARK_IMAGE_MODEL → {snap.ark_image_model!r} "
        "(密钥 SEEDREAM_API_KEY 或 ARK_API_KEY；基址 ARK_BASE_URL)\n"
        f"  (wan) DASHSCOPE_IMAGE_MODEL → {snap.dashscope_image_model!r} "
        f"(DashScope Wan；密钥 DASHSCOPE_API_KEY；基址 {snap.dashscope_base_http!r})\n"
        f"  视频 provider env: VIDEO_PROVIDER → {snap.video_provider!r}\n"
        f"  (ark 分支) ARK_VIDEO_MODEL → {snap.ark_video_model!r} "
        "(volcenginesdkarkruntime; 密钥 ARK_API_KEY)\n"
        "换 SDK: 实现自己的 MediaGenerationHub，或扩展 agentcore/model_hub.UnifiedModelHub。"
    )


class DelegatingMediaHub:
    """
    用「默认关键字参数」包裹已有 hub，便于在不改 Django 配置的情况下覆盖 ``size``、
    ``ratio`` 等（每调用仍可被 ``run_generate_*`` 的最终参数覆盖）。

    合并规则：``self.defaults`` 先写，单次 ``submit_*`` 传入的关键字在后，后者优先。
    """

    __slots__ = ("_inner", "_img_def", "_vid_def")

    def __init__(
        self,
        inner: Any,
        *,
        submit_image_defaults: Mapping[str, Any] | None = None,
        submit_video_defaults: Mapping[str, Any] | None = None,
    ) -> None:
        self._inner = inner
        self._img_def = dict(submit_image_defaults or {})
        self._vid_def = dict(submit_video_defaults or {})

    def submit_image_generation(self, **kwargs: Any) -> Any:
        merged = {**self._img_def, **kwargs}
        return self._inner.submit_image_generation(**merged)

    def submit_video_generation(self, **kwargs: Any) -> Any:
        merged = {**self._vid_def, **kwargs}
        return self._inner.submit_video_generation(**merged)

    def refresh_generation_task(self, task: Any) -> Any:
        return self._inner.refresh_generation_task(task)


def _hub_env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _seedream_build_image_field(
    input_images: list[str] | None,
    media_root: str | None,
) -> str | list[str] | None:
    if not input_images:
        return None
    out: list[str] = []
    for s in input_images:
        x = (s or "").strip()
        if not x:
            continue
        if x.startswith(("http://", "https://", "data:image/")):
            out.append(x)
            continue
        out.append(normalize_first_frame_url_for_ark(x, media_root=media_root))
    if not out:
        return None
    if len(out) == 1:
        return out[0]
    return out[:14]


def _dashscope_wan_size_string(model_id: str, size: str, has_images: bool) -> str:
    mid = (model_id or "").lower()
    s = (size or "").strip()
    if re.fullmatch(r"\d+\*\d+", s):
        return s
    key = s.upper().replace(" ", "")
    aliases = {
        "2K": "2K",
        "1K": "1K",
        "4K": "4K",
        "1:1": "2K",
        "16:9": "2K",
        "9:16": "2K",
        "4:3": "2K",
        "3:4": "2K",
    }
    band = aliases.get(key, "2K")
    if has_images and band == "4K":
        band = "2K"
    if "wan2.7-image" in mid and "pro" not in mid and band == "4K":
        band = "2K"
    return band


def _wan_hub_parameters(model_id: str, size: str, n: int, has_images: bool) -> dict[str, Any]:
    size_w = _dashscope_wan_size_string(model_id, size, has_images)
    sequential = _hub_env_bool("DASHSCOPE_WAN_ENABLE_SEQUENTIAL", False)
    n_cap = 12 if sequential else 4
    n_req = min(max(int(n), 1), n_cap)
    parameters: dict[str, Any] = {
        "size": size_w,
        "n": n_req,
        "watermark": _hub_env_bool("DASHSCOPE_IMAGE_WATERMARK", False),
    }
    if sequential:
        parameters["enable_sequential"] = True
    elif _hub_env_bool("DASHSCOPE_WAN_THINKING_MODE", True):
        parameters["thinking_mode"] = True
    return parameters


def _hub_fetch_url_bytes(url: str) -> tuple[bytes, str]:
    with urlopen(url, timeout=60) as r:
        data = r.read()
        mime = r.headers.get_content_type() or "image/jpeg"
    return data, mime


def _hub_to_data_url(data: bytes, mime: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_wan_user_message_content(
    prompt: str,
    input_images: list[str] | None,
    *,
    media_root: str | None = None,
) -> list[dict[str, Any]]:
    """构造 Wan ``messages[0].content``：先图后文（与官方示例一致）。"""
    parts: list[dict[str, Any]] = []
    for src in input_images or []:
        s = (src or "").strip()
        if not s:
            continue
        if s.startswith("data:image/"):
            parts.append({"image": s})
        elif s.startswith(("http://", "https://")):
            data, mime = _hub_fetch_url_bytes(s)
            parts.append({"image": _hub_to_data_url(data, mime)})
        else:
            parts.append(
                {"image": normalize_first_frame_url_for_ark(s, media_root=media_root)}
            )
    parts.append({"text": prompt})
    return parts


def _extract_urls_any(raw: Any) -> list[str]:
    urls: list[str] = []

    def walk(p: Any) -> None:
        if isinstance(p, str) and (
            p.startswith("http://") or p.startswith("https://")
        ):
            urls.append(p)
        elif isinstance(p, dict):
            for k, v in p.items():
                if k.lower() in {"url", "video_url", "file_url", "output_url"}:
                    if isinstance(v, str) and v.startswith("http"):
                        urls.append(v)
                walk(v)
        elif isinstance(p, list):
            for x in p:
                walk(x)

    walk(raw)
    return list(dict.fromkeys(urls))


@dataclass
class EphemeralMediaTask:
    """兼容 `wait_media_task` 所需的 duck 类型（无 ORM 时的任务占位）。"""

    id: str
    status: str
    input_payload: dict[str, Any] = field(default_factory=dict)
    output_payload: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""


class LocalWanArkHub:
    """
    将 :class:`MediaGenerationRequestClient` 适配为 :class:`MediaGenerationHub`：

    - 生图：默认 **Seedream**（``IMAGE_GENERATION_PROVIDER=seedream``）走 Ark ``POST /images/generations``；
      设 ``IMAGE_GENERATION_PROVIDER=wan`` 时走 DashScope 万相同步接口。
    - 生视频：Ark 异步任务，靠 ``refresh_generation_task`` 轮询。
    """

    __slots__ = ("_client", "_media_root", "_tasks")

    def __init__(
        self,
        client: MediaGenerationRequestClient,
        *,
        media_root: str | None = None,
    ) -> None:
        self._client = client
        self._media_root = media_root
        self._tasks: dict[str, EphemeralMediaTask] = {}

    def submit_image_generation(
        self,
        session: Any,
        prompt: str,
        title: str = "Generated Storyboard",
        asset_category: Any = None,
        destination: str = "storyboard_library",
        input_images: list[str] | None = None,
        n: int = 1,
        size: str = "2K",
    ) -> EphemeralMediaTask:
        if _image_generation_provider() == "seedream":
            return self._submit_seedream_image_generation(
                session=session,
                prompt=prompt,
                title=title,
                asset_category=asset_category,
                destination=destination,
                input_images=input_images or [],
                n=n,
                size=size,
            )
        if not self._client._image:
            raise RuntimeError("MediaGenerationRequestClient 未配置 image (DashScopeWanApiParams)")
        mid = self._client._image.model_id
        has_im = bool(input_images)
        content = build_wan_user_message_content(
            prompt, input_images, media_root=self._media_root
        )
        messages = [{"role": "user", "content": content}]
        parameters = _wan_hub_parameters(mid, size, n, has_im)
        body = self._client.build_wan_body(messages=messages, parameters=parameters)
        raw = self._client.post_wan_multimodal_generation(body)
        sc = raw.get("status_code") if isinstance(raw, dict) else None
        if sc is not None and int(sc) != 200:
            raise RuntimeError(
                f"DashScope Wan image failed: {json.dumps(raw, ensure_ascii=False)[:1200]}"
            )
        urls = extract_wan_image_urls_from_response(raw)
        if not urls:
            raise RuntimeError(
                f"DashScope Wan returned no image urls: {json.dumps(raw, ensure_ascii=False)[:1200]}"
            )

        tid = str(uuid4())
        task = EphemeralMediaTask(
            id=tid,
            status="success",
            input_payload={
                "kind": "image_generation",
                "provider": "dashscope_wan_sync",
                "session": session,
                "prompt": prompt,
                "title": title,
                "model_id": mid,
                "asset_category": asset_category,
                "destination": destination,
                "input_images": input_images or [],
                "size": parameters.get("size"),
                "n": parameters.get("n"),
            },
            output_payload={
                "created_asset_ids": urls,
                "image_urls": urls,
                "provider_create_response": raw,
            },
        )
        self._tasks[tid] = task
        return task

    def _submit_seedream_image_generation(
        self,
        *,
        session: Any,
        prompt: str,
        title: str,
        asset_category: Any,
        destination: str,
        input_images: list[str],
        n: int,
        size: str,
    ) -> EphemeralMediaTask:
        mid = _ark_image_model()
        imgs = _seedream_build_image_field(input_images, self._media_root)
        client = SeedreamImageClient.from_environ()
        size_s = coerce_seedream_size(size)
        n_req = min(max(int(n), 1), 15)
        wm = _hub_env_bool(
            "ARK_IMAGE_WATERMARK",
            _hub_env_bool("DASHSCOPE_IMAGE_WATERMARK", False),
        )
        out_fmt = os.getenv("SEEDREAM_OUTPUT_FORMAT", "png").strip() or None
        extra: dict[str, Any] = {}
        if out_fmt:
            extra["output_format"] = out_fmt
        try:
            timeout_sec = float(os.getenv("SEEDREAM_HTTP_TIMEOUT_SEC", "600").strip() or "600")
        except ValueError:
            timeout_sec = 600.0

        if n_req <= 1:
            raw = client.generate(
                model=mid,
                prompt=prompt,
                image=imgs,
                size=size_s,
                sequential_image_generation="disabled",
                response_format="url",
                watermark=wm,
                timeout_sec=timeout_sec,
                **extra,
            )
        else:
            raw = client.generate(
                model=mid,
                prompt=prompt,
                image=imgs,
                size=size_s,
                sequential_image_generation="auto",
                sequential_image_generation_options={"max_images": n_req},
                response_format="url",
                watermark=wm,
                timeout_sec=timeout_sec,
                **extra,
            )
        urls = collect_result_image_urls(raw)
        if not urls:
            raise RuntimeError(
                f"Seedream returned no image urls: {json.dumps(raw, ensure_ascii=False)[:1200]}"
            )

        tid = str(uuid4())
        task = EphemeralMediaTask(
            id=tid,
            status="success",
            input_payload={
                "kind": "image_generation",
                "provider": "ark_seedream_sync",
                "session": session,
                "prompt": prompt,
                "title": title,
                "model_id": mid,
                "asset_category": asset_category,
                "destination": destination,
                "input_images": input_images,
                "size": size_s,
                "n": n_req,
            },
            output_payload={
                "created_asset_ids": urls,
                "image_urls": urls,
                "provider_create_response": raw,
            },
        )
        self._tasks[tid] = task
        return task

    def submit_video_generation(
        self,
        session: Any,
        prompt: str,
        first_frame_url: str,
        duration: int = 5,
        last_frame_url: str | None = None,
        ratio: str | None = None,
        generate_audio: bool = True,
        watermark: bool | None = None,
        camera_move: str | None = None,
        title: str = "Generated Video",
    ) -> EphemeralMediaTask:
        if not self._client._video:
            raise RuntimeError("MediaGenerationRequestClient 未配置 video (ArkVideoApiParams)")
        raw = self._client.create_ark_video_task_raw(
            prompt=prompt,
            first_frame_url=first_frame_url,
            duration=duration,
            ratio=ratio,
            watermark=watermark,
            generate_audio=generate_audio,
            media_root=self._media_root,
        )
        provider_id = str(raw.get("id") or raw.get("task_id") or "").strip()
        if not provider_id:
            raise RuntimeError(
                f"Ark video submit failed: {json.dumps(raw, ensure_ascii=False)[:1200]}"
            )

        tid = str(uuid4())
        task = EphemeralMediaTask(
            id=tid,
            status="running",
            input_payload={
                "kind": "video_generation",
                "provider": "ark",
                "provider_task_id": provider_id,
                "session": session,
                "prompt": prompt,
                "first_frame_url": first_frame_url,
                "duration": duration,
                "title": title,
                "camera_move": camera_move,
            },
            output_payload={"provider_create_response": raw},
        )
        self._tasks[tid] = task
        return task

    def refresh_generation_task(self, task: EphemeralMediaTask) -> EphemeralMediaTask:
        prov = (task.input_payload or {}).get("provider")
        if prov in {"dashscope_wan_sync", "dashscope_sync", "ark_seedream_sync"}:
            return task
        if prov != "ark":
            task.status = "failed"
            task.error_message = f"unsupported provider for refresh: {prov}"
            return task

        if task.status in {"success", "failed"}:
            return task

        pid = (task.input_payload or {}).get("provider_task_id")
        if not pid:
            task.status = "failed"
            task.error_message = "provider_task_id missing"
            return task

        raw = self._client.get_ark_video_task_raw(pid)
        if not isinstance(raw, dict):
            task.status = "failed"
            task.error_message = f"unexpected poll payload type: {type(raw)}"
            return task

        task.output_payload = {**(task.output_payload or {}), "provider_poll_response": raw}
        status = str(raw.get("status") or "").lower()
        content = raw.get("content") or {}
        urls: list[str] = []
        if isinstance(content, dict):
            for key in ("video_url", "file_url"):
                u = str(content.get(key) or "").strip()
                if u:
                    urls.append(u)
        if not urls:
            urls = _extract_urls_any(raw)

        if status == "succeeded" and urls:
            task.status = "success"
            task.output_payload["created_asset_ids"] = urls
            task.output_payload["video_urls"] = urls
        elif status in {"failed", "cancelled", "canceled", "expired"}:
            task.status = "failed"
            task.error_message = json.dumps(raw, ensure_ascii=False)[:2000]
        elif status == "succeeded" and not urls:
            task.status = "failed"
            task.error_message = json.dumps(raw, ensure_ascii=False)[:2000]
        else:
            task.status = "running"
        return task


def _default_local_media_root() -> str | None:
    r = os.environ.get("MEDIA_ROOT", "").strip()
    return r or None


def tool_generate_image(
    user_query: str,
    destination: str = "storyboard_library",
    asset_name: str = "",
    style_hint: str = "",
    shot_brief: str = "",
    ref_assets: list | None = None,
    input_images: list | None = None,
    aspect_ratio: str = "16:9",
    n: int = 1,
    *,
    session: Any | None = None,
    size: str = "2K",
    layered_image_prompt: LayeredImagePromptFn | None = None,
    image_batch_cap: int | None = None,
    submit_image_kwargs: Mapping[str, Any] | None = None,
    client: MediaGenerationRequestClient | None = None,
    media_root: str | None = None,
) -> str:
    """
    便携入口：Wan 生图 + 本文件 prompt 编排；读取环境变量中的密钥与模型（见 :meth:`MediaGenerationRequestClient.from_environ`）。
    """
    hub = LocalWanArkHub(
        client or MediaGenerationRequestClient.from_environ(),
        media_root=media_root if media_root is not None else _default_local_media_root(),
    )
    return run_generate_image(
        hub,
        {} if session is None else session,
        user_query,
        destination,
        asset_name=asset_name,
        style_hint=style_hint,
        shot_brief=shot_brief,
        ref_assets=ref_assets,
        input_images=input_images,
        aspect_ratio=aspect_ratio,
        n=n,
        layered_image_prompt=layered_image_prompt,
        image_batch_cap=image_batch_cap,
        size=size,
        submit_image_kwargs=submit_image_kwargs,
    )


def tool_generate_video(
    prompt: str,
    source_image: str,
    duration: int | None = None,
    camera_move: str = "static",
    extra_negative_zh: list[str] | None = None,
    video_title: str = "",
    *,
    session: Any | None = None,
    layered_video_prompt: LayeredVideoPromptFn | None = None,
    submit_video_kwargs: Mapping[str, Any] | None = None,
    wait_timeout_seconds: int = 120,
    client: MediaGenerationRequestClient | None = None,
    media_root: str | None = None,
) -> str:
    """
    便携入口：Ark 图生视频 + 本文件 prompt 编排；环境变量同 :meth:`MediaGenerationRequestClient.from_environ`。
    """
    hub = LocalWanArkHub(
        client or MediaGenerationRequestClient.from_environ(),
        media_root=media_root if media_root is not None else _default_local_media_root(),
    )
    return run_generate_video(
        hub,
        {} if session is None else session,
        prompt,
        source_image=source_image,
        duration=duration,
        camera_move=camera_move,
        extra_negative_zh=extra_negative_zh,
        video_title=video_title,
        layered_video_prompt=layered_video_prompt,
        submit_video_kwargs=submit_video_kwargs,
        wait_timeout_seconds=wait_timeout_seconds,
    )


def _merge_hub_kwargs(defaults: Mapping[str, Any] | None, **required: Any) -> dict[str, Any]:
    """extras 先行，调度层 required 在后，确保 prompt/session 等不被误覆盖。"""
    base = dict(defaults or {})
    base.update(required)
    return base


def _is_transient_network_error(exc: Exception) -> bool:
    txt = str(exc).lower()
    tokens = (
        "connection reset",
        "connectionreseterror",
        "remotely closed",
        "remote host closed",
        "read timed out",
        "timed out",
        "ssl",
        "econnreset",
        "temporarily unavailable",
        "gateway timeout",
    )
    return any(t in txt for t in tokens)


def _coerce_tool_image_n(raw: Any, cap: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 1
    if v < 1:
        return 1
    return min(v, max(1, cap))


def _assemble_prompt_from_intent(
    query: str,
    style_hint: str = "",
    input_images: list | None = None,
    destination: str = "",
) -> str:
    has_ref = bool(input_images)
    q = (query or "").strip()
    sh = (style_hint or "").strip()
    ql = q.lower()

    if "三视图" in q or "three-view" in ql or "three view" in ql:
        if has_ref:
            if destination == "product_library":
                base = (
                    "Three-view orthographic illustration of the exact product shown in the reference image: "
                    "front view (正面), side view (侧面), rear view (背面) arranged horizontally. "
                    "Pure white background, clean technical illustration style, no perspective distortion. "
                    "Preserve the exact shape, colors, logo, and branding from the reference."
                )
            else:
                # 角色库 / 分镜等：避免「product」措辞把参考强行解释成工业产品（如手机）
                base = (
                    "Three-view orthographic illustration of the character, creature, or subject "
                    "shown in the reference image: "
                    "front view (正面), side view (侧面), rear view (背面) arranged horizontally. "
                    "Pure white background, clean technical illustration style, no perspective distortion. "
                    "Preserve silhouette, colors, and distinctive traits from the reference. "
                    "When the user's text specifies species, outfit, or style, follow it unless it clearly "
                    "contradicts the reference."
                )
            if q:
                base = f"User intent (subject & style): {q}\n{base}"
        else:
            # 无参考时原先未拼接用户主体，模型容易在「写实」锚点下画成常见工业产品（如手机）
            subj = q or "the subject described by the user"
            base = (
                f"Three-view orthographic illustration of: {subj}. "
                "Show front (正面), side (侧面), and rear (背面) views in one image, arranged horizontally. "
                "Pure white background, clean technical illustration style, no perspective distortion. "
                "Depict exactly what the user named (animal, person, creature, or object) — "
                "do not replace it with an unrelated mass-produced product."
            )
    elif any(k in q for k in ("广告镜头", "商业摄影", "产品广告", "product shot", "ad shot")):
        if has_ref:
            base = (
                "Commercial product photography based on the reference product image. "
                "Professional studio lighting, clean minimal background, "
                "high-end retouched commercial look. "
                "Preserve the exact product appearance from the reference image."
            )
        else:
            base = (
                "Commercial product photography. Professional studio lighting, "
                "clean minimal background, high-end retouched look."
            )
    elif has_ref:
        nrefs = len(input_images or [])
        if nrefs > 1:
            base = (
                f"Based on the {nrefs} reference images, {q}. "
                "Do not infer or rename the subject — preserve its exact appearance from the references."
            )
        else:
            base = (
                f"Based on the reference image, {q}. "
                "Do not infer or rename the subject — preserve its exact appearance."
            )
    else:
        base = q

    if sh:
        base = f"{base} Style: {sh}."

    return base


def _normalize_asset_title_candidate(candidate: str) -> str:
    text = (candidate or "").strip()
    if not text:
        return ""
    text = re.sub(r"^@\S+\s*", "", text).strip()
    text = re.sub(
        r"^(generated|image|storyboard|video|result)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return text


def _looks_like_internal_i2i_english_prompt(text: str) -> bool:
    if not (text or "").strip():
        return False
    first_line = (text.strip().lower().split("\n", 1)[0])[:200]
    return any(first_line.startswith(p) for p in _TITLE_I2I_EN_BOILERPLATE_PREFIXES)


def _extract_corner_bracket_title(text: str) -> str:
    if not (text or "").strip():
        return ""
    spans = re.findall(r"「([^」]{1,80})」", text)
    for s in spans:
        c = s.strip()
        if c and re.search(r"[\u4e00-\u9fff]", c):
            return c
    for s in spans:
        c = s.strip()
        if c:
            return c
    return ""


def _derive_asset_name(
    asset_name: str,
    shot_brief: str,
    user_query: str,
    destination: str,
    max_len: int = 20,
) -> str:
    for candidate in (asset_name, shot_brief, user_query):
        text = _normalize_asset_title_candidate(candidate)
        if not text:
            continue
        if _looks_like_internal_i2i_english_prompt(text):
            continue
        return text[:max_len]
    for source in (user_query, shot_brief):
        bracket = _extract_corner_bracket_title(source or "")
        if bracket:
            return bracket[:max_len]
    base = _DST_ZH.get(destination, "生成素材")
    suffix = uuid4().hex[:4]
    return f"{base}·{suffix}"[:max_len]


def _derive_video_title(video_title: str, prompt: str, max_len: int = 20) -> str:
    for candidate in (video_title, prompt):
        text = (candidate or "").strip()
        if not text:
            continue
        text = re.sub(r"\n.*", "", text).strip()
        text = re.sub(r"\s*(运镜|camera_move).*", "", text, flags=re.IGNORECASE).strip()
        if text:
            return text[:max_len]
    return "视频片段"


def _lite_layered_image_prompt(destination: str, base_prompt: str) -> str:
    anchor = (
        "Photorealistic professional photograph, physically plausible scene, natural lighting."
    )
    if destination == "product_library":
        anchor += (
            " Authentic product packaging shot, readable labels without warping, believable reflections."
        )
    neg_zh = "模糊，变形，多余手指，水印，字幕，低分辨率。"
    neg_en = "blur, distortion, extra fingers, watermark, subtitles, low resolution"
    body = (base_prompt or "").strip()
    return (
        f"[Physics anchor]\n{anchor}\n\n"
        f"[User intent]\n{body}\n\n"
        f"[Negative constraints zh]\n负面限制：{neg_zh}\n\n"
        f"[Negative cues]\nAvoid: {neg_en}"
    )


def _lite_layered_video_prompt(
    base_prompt: str, extra_negative_zh: list[str] | None
) -> str:
    anchor = "Smooth natural motion, physically plausible movement, stable subject identity."
    zh_parts = ["抖动过大，画面撕裂，手指数量错误，水印，字幕。"]
    if extra_negative_zh:
        for item in extra_negative_zh:
            s = str(item).strip()
            if s and s not in zh_parts:
                zh_parts.append(s)
    neg_zh = "".join(zh_parts)
    neg_en = "severe jitter, tearing, wrong finger count, watermark, subtitles"
    body = (base_prompt or "").strip()
    return (
        f"[Physics anchor]\n{anchor}\n\n"
        f"[User intent]\n{body}\n\n"
        f"[Negative constraints zh]\n负面限制：{neg_zh}\n\n"
        f"[Negative cues]\nAvoid: {neg_en}"
    )


def wait_media_task(
    model_hub: Any,
    task: Any,
    timeout_seconds: int = 120,
    interval_seconds: int = 4,
) -> Any:
    terminal = frozenset({"success", "failed"})
    status = getattr(task, "status", None)
    if status in terminal:
        return task
    deadline = time.time() + timeout_seconds
    current = task
    transient_failures = 0
    while time.time() < deadline:
        try:
            current = model_hub.refresh_generation_task(current)
            transient_failures = 0
        except Exception as exc:
            if _is_transient_network_error(exc):
                transient_failures += 1
                if transient_failures <= 8:
                    time.sleep(interval_seconds)
                    continue
            raise
        if getattr(current, "status", None) in terminal:
            return current
        time.sleep(interval_seconds)
    return current


def run_generate_image(
    model_hub: Any,
    session: Any,
    user_query: str,
    destination: str,
    asset_name: str = "",
    style_hint: str = "",
    shot_brief: str = "",
    ref_assets: list | None = None,
    input_images: list | None = None,
    aspect_ratio: str = "16:9",
    n: int = 1,
    *,
    layered_image_prompt: LayeredImagePromptFn | None = None,
    image_batch_cap: int | None = None,
    size: str = "2K",
    submit_image_kwargs: Mapping[str, Any] | None = None,
) -> str:
    asset_category = DST_TO_ASSET_CATEGORY.get(destination)
    if not asset_category:
        return f"Error: invalid destination '{destination}'."

    cap = image_batch_cap if image_batch_cap is not None else _DEFAULT_IMAGE_BATCH_CAP
    fmt = layered_image_prompt or _lite_layered_image_prompt

    intent_text = (shot_brief or "").strip() or user_query
    full_prompt = _assemble_prompt_from_intent(
        intent_text, style_hint, input_images, destination
    )
    if ref_assets:
        refs = ", ".join(str(x) for x in ref_assets)
        full_prompt = f"{full_prompt}\n参考素材ID: {refs}"
    full_prompt = f"{full_prompt}\n画幅: {aspect_ratio}"
    full_prompt = fmt(destination, full_prompt)

    generated_title = _derive_asset_name(
        asset_name=asset_name or "",
        shot_brief=shot_brief or "",
        user_query=user_query or "",
        destination=destination,
    )
    image_n = _coerce_tool_image_n(n, cap)

    task = model_hub.submit_image_generation(
        **_merge_hub_kwargs(
            submit_image_kwargs,
            session=session,
            prompt=full_prompt,
            title=generated_title,
            asset_category=asset_category,
            destination=destination,
            input_images=input_images or [],
            n=image_n,
            size=size,
        )
    )
    task = wait_media_task(model_hub, task)
    out = getattr(task, "output_payload", None) or {}
    created_ids = out.get("created_asset_ids", [])
    if not created_ids:
        return (
            "Error: image generation finished without persisted image assets. "
            "Please retry with valid reference image URLs."
        )
    inp = getattr(task, "input_payload", None) or {}
    return json.dumps(
        {
            "task_id": getattr(task, "id", None),
            "provider_task_id": inp.get("provider_task_id"),
            "destination": destination,
            "status": getattr(task, "status", None),
            "created_asset_ids": created_ids,
            "error_message": getattr(task, "error_message", None),
        },
        ensure_ascii=False,
    )


def run_generate_video(
    model_hub: Any,
    session: Any,
    prompt: str,
    source_image: str = "",
    duration: int | None = None,
    camera_move: str = "static",
    extra_negative_zh: list[str] | None = None,
    video_title: str = "",
    *,
    layered_video_prompt: LayeredVideoPromptFn | None = None,
    submit_video_kwargs: Mapping[str, Any] | None = None,
    wait_timeout_seconds: int = 120,
) -> str:
    try:
        duration_seconds = int(duration) if duration is not None else 0
    except (TypeError, ValueError):
        duration_seconds = 0
    if duration_seconds <= 0:
        return json.dumps(
            {
                "ok": False,
                "error": "生成视频前必须先询问并获得用户指定的视频时长（秒），不要使用默认时长。",
                "ask_user": "要生成多少秒的视频？",
            },
            ensure_ascii=False,
        )
    src = (source_image or "").strip()
    if not src:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "图生视频需要首帧：在 source_image 填入 /media/... 路径、公网 URL，"
                    "或用户 @ 引用素材对应的图片地址。若用户未指定首帧，先问清楚或用 @ 分镜/素材，不要猜。"
                ),
            },
            ensure_ascii=False,
        )

    fmt = layered_video_prompt or _lite_layered_video_prompt
    full_prompt = fmt((prompt or "").strip(), extra_negative_zh)
    if camera_move:
        full_prompt = f"{full_prompt}\n运镜: {camera_move}"
    clip_title = _derive_video_title(video_title, prompt)

    last_exc: Exception | None = None
    task = None
    for attempt in range(2):
        try:
            task = model_hub.submit_video_generation(
                **_merge_hub_kwargs(
                    submit_video_kwargs,
                    session=session,
                    prompt=full_prompt,
                    first_frame_url=src,
                    duration=duration_seconds,
                    camera_move=camera_move,
                    title=clip_title,
                )
            )
            task = wait_media_task(
                model_hub, task, timeout_seconds=int(wait_timeout_seconds)
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and _is_transient_network_error(exc):
                time.sleep(1.2)
                continue
            raise
    if task is None:
        raise RuntimeError(str(last_exc) if last_exc else "video generation failed")

    out = getattr(task, "output_payload", None) or {}
    inp = getattr(task, "input_payload", None) or {}
    return json.dumps(
        {
            "task_id": getattr(task, "id", None),
            "provider_task_id": inp.get("provider_task_id"),
            "status": getattr(task, "status", None),
            "created_asset_ids": out.get("created_asset_ids", []),
            "error_message": getattr(task, "error_message", None),
        },
        ensure_ascii=False,
    )


__all__ = [
    "DST_TO_ASSET_CATEGORY",
    "DelegatingMediaHub",
    "EphemeralMediaTask",
    "LocalWanArkHub",
    "MediaGenerationHub",
    "WorkflowMediaEnvSnapshot",
    "build_wan_user_message_content",
    "format_workflow_media_routing_help",
    "run_generate_image",
    "run_generate_video",
    "tool_generate_image",
    "tool_generate_video",
    "wait_media_task",
]
