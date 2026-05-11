"""
媒体生成 **厂商 SDK 层**（DashScope 万相 Wan HTTP + 火山 Ark `volcenginesdkarkruntime`）。

- **不依赖** Django、agentcore、业务模型；拷贝本文件 + 安装 Ark SDK 即可单独使用。
- **编排 / prompt / 轮询** 在 :mod:`generation_tools`（含 :class:`generation_tools.LocalWanArkHub`）。
- Django 注入后端见 :mod:`agentcore.django_media`。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@dataclass
class DashScopeWanApiParams:
    """Wan multimodal-generation：密钥、HTTP 基址、模型名（写入 JSON ``model``）。"""

    api_key: str
    api_base: str
    model_id: str


@dataclass
class ArkVideoApiParams:
    """Ark ``tasks.create``：密钥、Base URL、模型名（写入关键字参数 ``model``）。"""

    api_key: str
    api_base: str
    model_id: str
    resolution: str = ""
    ratio_default: str = "adaptive"
    default_generate_audio: bool = True
    default_watermark: bool = False


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def _without_proxy_env():
    keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ]
    backup = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            if k in {"NO_PROXY", "no_proxy"}:
                os.environ[k] = "*"
            else:
                os.environ.pop(k, None)
        yield
    finally:
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_with_retries(fn, attempts: int = 3, base_sleep_seconds: float = 1.2):
    last_exc = None
    for idx in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if idx >= attempts - 1:
                break
            time.sleep(base_sleep_seconds * (2**idx))
    raise last_exc


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {
            k: _to_plain(v)
            for k, v in value.__dict__.items()
            if not k.startswith("_")
        }
    return str(value)


def normalize_first_frame_url_for_ark(
    raw: str,
    *,
    media_root: str | os.PathLike[str] | None = None,
) -> str:
    """
    Ark 在云端执行，不能直接访问本机 localhost；首帧须为公网 URL 或 data:image。

    解析规则：

    * 已是 ``data:image/...``：原样返回。
    * ``https://`` / ``http://`` 且 host 非 localhost：原样返回。
    * ``http://localhost/...``：报错（请改 data URL 或公网图床）。
    * 本机文件路径：读入并转为 data URL。
    * 形如 ``/media/...``：若设置了环境变量 ``MEDIA_ROOT`` 或参数 ``media_root``，
      则拼接后读文件转 data URL。
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty first_frame_url")
    if s.startswith("data:image/"):
        return s
    if s.startswith(("http://", "https://")):
        host = (urlparse(s).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError(
                "Ark 无法拉取本机 host 上的 URL，请改用公网 https、或先把图片转成 data:image/...;base64,..."
            )
        return s

    root = media_root or os.environ.get("MEDIA_ROOT", "").strip()
    path_str = s
    if s.startswith("/media/") and root:
        rel = s[len("/media/") :].lstrip("/\\")
        path_str = str(Path(root) / rel)
    p = Path(path_str)
    if not p.is_file():
        raise RuntimeError(
            f"无法解析为首帧：既非公网 URL / data URL，也不是可读本地文件。传入: {s[:120]!r} "
            f"(若使用 /media/... 请设置环境变量 MEDIA_ROOT 或传入 media_root=)"
        )
    raw_bytes = p.read_bytes()
    mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _post_json_urllib(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float = 300.0) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {**headers, "Content-Type": "application/json"}
    req = Request(url, data=data, headers=hdrs, method="POST")

    try:
        with urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8")
    except HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                detail = json.dumps(parsed, ensure_ascii=False)[:2400]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e

    out = json.loads(txt) if txt else {}
    if not isinstance(out, dict):
        raise RuntimeError(f"unexpected JSON type: {type(out)}")
    return out


def _extract_urls_recursive(payload: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(payload, str):
        if payload.startswith("http://") or payload.startswith("https://"):
            urls.append(payload)
        return urls
    if isinstance(payload, list):
        for item in payload:
            urls.extend(_extract_urls_recursive(item))
        return urls
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in {"url", "image", "video", "video_url", "output_url"}:
                urls.extend(_extract_urls_recursive(value))
            else:
                urls.extend(_extract_urls_recursive(value))
    return urls


def extract_wan_image_urls_from_response(raw: Any) -> list[str]:
    """从 DashScope Wan multimodal-generation 同步响应中解析出图片 URL。"""
    urls = _extract_urls_recursive(raw)
    if urls:
        return list(dict.fromkeys(urls))
    if not isinstance(raw, dict):
        return []
    output = raw.get("output", {})
    choices = output.get("choices", []) if isinstance(output, dict) else []
    extracted: list[str] = []
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                image_url = part.get("image")
                if isinstance(image_url, str) and image_url:
                    extracted.append(image_url)
    return list(dict.fromkeys(extracted))


def normalize_ark_video_model_id(model_id: str) -> str:
    """
    控制台/文档里常见简写 ``doubao-seedance-1-5-pro`` 与可开通图生视频的完整 endpoint ID 不一致，
    易导致路由成仅文生或报 ``r2v`` 相关错误。统一映射到官方示例中的完整模型 ID。
    """
    m = (model_id or "").strip()
    while m.startswith("\ufeff"):
        m = m[1:].strip()
    if not m:
        return m
    low = m.lower()
    # 仅「无版本后缀」的简写映射到控制台常用可 I2V 的 endpoint id
    if low == "doubao-seedance-1-5-pro":
        return "doubao-seedance-1-5-pro-251215"
    return m


def ark_seedance_15_pro_multi_ref_r2v_risk(model_id: str) -> bool:
    """
    方舟在多张 ``image_url``（均为 reference_image）时会走 ``task_type=r2v``；
    Seedance 1.5 Pro 常见报错为 ``r2v does not support model doubao-seedance-1-5-pro``。
    单首帧 + ``first_frame`` 一般走单图 I2V，稳定得多。
    """
    ml = (model_id or "").lower()
    return "seedance-1-5" in ml and "pro" in ml


def clamp_ark_video_duration_seconds(duration: int, model_id: str) -> int:
    """
    火山方舟 CreateContentsGenerationsTasks 对 ``duration`` 强校验（整数秒）。
    文档摘要：1.0 系列 [2,12]；1.5 pro [4,12] 或 -1；2.0 系列 [4,15] 或 -1。
    本函数不返回 -1，仅钳位到各模型允许的闭区间。
    """
    try:
        d = int(round(float(duration)))
    except (TypeError, ValueError):
        d = 5
    mid = (model_id or "").lower()
    if "seedance-2" in mid:
        return max(4, min(15, d))
    if "seedance-1-5" in mid:
        return max(4, min(12, d))
    if "seedance-1-0" in mid:
        return max(2, min(12, d))
    if "seedance" in mid:
        return max(4, min(12, d))
    return max(2, min(12, d))


class MediaGenerationRequestClient:
    """
    两类调用（模型 / 密钥均来自构造时传入的 Params）：

    1. **Wan 生图**：``POST {api_base}/services/aigc/multimodal-generation/generation``，
       头 ``Authorization: Bearer {image.api_key}``；
       JSON 字段 ``model`` ← ``image.model_id``（若 ``post_wan_multimodal_generation`` 的 body 未写 model）。
    2. **Ark 生视频**：``Ark(api_key=video.api_key, base_url=video.api_base)``，
       ``tasks.create(model=video.model_id, content=[...], ...)``。
    """

    __slots__ = ("_image", "_video")

    def __init__(
        self,
        *,
        image: DashScopeWanApiParams | None = None,
        video: ArkVideoApiParams | None = None,
    ) -> None:
        self._image = image
        self._video = video

    @classmethod
    def from_environ(cls) -> MediaGenerationRequestClient:
        """从环境变量组装（命名与常见 WorkFlow .env 一致；无 Django）。"""
        ark_key_name = os.getenv("ARK_API_KEY_ENV", "ARK_API_KEY").strip() or "ARK_API_KEY"
        return cls(
            image=DashScopeWanApiParams(
                api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
                api_base=os.getenv(
                    "DASHSCOPE_BASE_HTTP_API_URL",
                    "https://dashscope.aliyuncs.com/api/v1",
                ).strip(),
                model_id=os.getenv("DASHSCOPE_IMAGE_MODEL", "wan2.7-image-pro").strip(),
            ),
            video=ArkVideoApiParams(
                api_key=os.getenv(ark_key_name, "").strip(),
                api_base=os.getenv(
                    "ARK_BASE_URL",
                    "https://ark.cn-beijing.volces.com/api/v3",
                ).strip(),
                model_id=normalize_ark_video_model_id(
                    os.getenv(
                        "ARK_VIDEO_MODEL",
                        "doubao-seedance-1-5-pro-251215",
                    ).strip()
                ),
                resolution=os.getenv("ARK_VIDEO_RESOLUTION", "").strip(),
                ratio_default=os.getenv("ARK_VIDEO_RATIO", "9:16").strip(),
                default_generate_audio=_env_bool("ARK_VIDEO_GENERATE_AUDIO", True),
                default_watermark=_env_bool("ARK_VIDEO_WATERMARK", False),
            ),
        )

    def post_wan_multimodal_generation(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self._image:
            raise RuntimeError("未配置 DashScopeWanApiParams（image=...）")
        if not self._image.api_key:
            raise RuntimeError("DashScope api_key 为空")
        payload = dict(body)
        payload["model"] = str(payload.get("model") or self._image.model_id).strip()
        if not payload["model"]:
            raise RuntimeError("Wan model_id 为空")

        base = self._image.api_base.rstrip("/")
        url = f"{base}/services/aigc/multimodal-generation/generation"
        headers = {"Authorization": f"Bearer {self._image.api_key}"}

        def _call() -> dict[str, Any]:
            return _post_json_urllib(url, headers, payload)

        with _without_proxy_env():
            return _run_with_retries(_call, attempts=3)

    def build_wan_body(
        self,
        *,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any],
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._image:
            raise RuntimeError("未配置 DashScopeWanApiParams（image=...）")
        mid = (model_id or self._image.model_id or "").strip()
        if not mid:
            raise RuntimeError("Wan model_id 为空")
        return {
            "model": mid,
            "input": {"messages": messages},
            "parameters": dict(parameters),
        }

    def create_ark_video_task_raw(
        self,
        *,
        prompt: str,
        first_frame_url: str,
        duration: int,
        ratio: str | None = None,
        watermark: bool | None = None,
        generate_audio: bool | None = None,
        media_root: str | os.PathLike[str] | None = None,
        reference_image_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self._video:
            raise RuntimeError("未配置 ArkVideoApiParams（video=...）")
        if not self._video.api_key:
            raise RuntimeError("Ark api_key 为空")

        try:
            from volcenginesdkarkruntime import Ark
        except Exception as exc:
            raise RuntimeError(
                "需要安装: pip install 'volcengine-python-sdk[ark]'"
            ) from exc

        src = normalize_first_frame_url_for_ark(first_frame_url, media_root=media_root)
        mid = normalize_ark_video_model_id(str(self._video.model_id or "").strip())
        if not mid:
            raise RuntimeError("Ark model_id 为空")

        # 当前多数方舟网关返回：InvalidParameter — role must be specified for image contents。
        # 故默认**始终**为每张图附加 role；仅当文档/地域明确可不传时设 ARK_VIDEO_IMAGE_URL_USE_ROLE=0。
        use_image_role = _env_bool("ARK_VIDEO_IMAGE_URL_USE_ROLE", True)
        role_first = (os.getenv("ARK_VIDEO_ROLE_FIRST_FRAME", "first_frame") or "first_frame").strip()
        role_ref = (
            os.getenv("ARK_VIDEO_ROLE_REFERENCE_IMAGE", "reference_image") or "reference_image"
        ).strip()

        request_ratio = ratio if ratio is not None else self._video.ratio_default
        request_watermark = (
            self._video.default_watermark if watermark is None else bool(watermark)
        )
        request_audio = (
            self._video.default_generate_audio
            if generate_audio is None
            else bool(generate_audio)
        )

        append_refs = os.getenv("ARK_VIDEO_APPEND_REFERENCE_IMAGES", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        extra_norms: list[str] = []
        if append_refs and reference_image_urls:
            seen_urls: set[str] = {src}
            cap = int(os.getenv("ARK_VIDEO_MAX_REFERENCE_IMAGES", "3") or "3")
            cap = max(0, min(8, cap))
            for raw_ref in reference_image_urls:
                if len(extra_norms) >= cap:
                    break
                ru = (raw_ref or "").strip()
                if not ru or ru in seen_urls:
                    continue
                try:
                    norm = normalize_first_frame_url_for_ark(ru, media_root=media_root)
                except Exception:
                    continue
                if norm in seen_urls:
                    continue
                seen_urls.add(norm)
                extra_norms.append(norm)

        # 多参考 → 网关常选 r2v；1.5 Pro 在该 task_type 上易 400。默认只发单首帧（prompt 仍可描述多图语义）。
        if extra_norms and ark_seedance_15_pro_multi_ref_r2v_risk(mid):
            if not _env_bool("ARK_VIDEO_SEEDANCE15_ALLOW_MULTI_REF", False):
                n_drop = len(extra_norms)
                extra_norms = []
                print(
                    "[FrameOS] Ark video: Seedance 1.5 Pro — skipping extra reference image(s) in API "
                    f"({n_drop} omitted) to avoid task_type=r2v. "
                    "Set ARK_VIDEO_SEEDANCE15_ALLOW_MULTI_REF=1 to force multi-image (may 400).",
                    flush=True,
                )

        def _image_part(url: str, role: str) -> dict[str, Any]:
            part: dict[str, Any] = {
                "type": "image_url",
                "image_url": {"url": url},
            }
            if use_image_role and role:
                part["role"] = role
            return part

        if extra_norms:
            # 多张：全部为 reference_image（不可与 first_frame 混用，否则 400）
            content = [{"type": "text", "text": prompt}]
            for u in [src, *extra_norms]:
                content.append(_image_part(u, role_ref))
        else:
            content = [
                {"type": "text", "text": prompt},
                _image_part(src, role_first),
            ]
        dur_req = int(duration or 5)
        dur_use = clamp_ark_video_duration_seconds(dur_req, mid)
        create_kwargs: dict[str, Any] = {
            "model": mid,
            "content": content,
            "ratio": request_ratio,
            "duration": dur_use,
            "watermark": request_watermark,
        }
        # 与 clamp_ark_video_duration_seconds 一致：2.x 模型 ID 常为 seedance-2-pro…（不含字面量 seedance-2-0），
        # 若此处过窄则不会传 generate_audio，成片无对白/环境声。
        if "seedance-1-5" in mid or "seedance-2" in mid:
            create_kwargs["generate_audio"] = request_audio
        res = str(self._video.resolution or "").strip()
        if res:
            create_kwargs["resolution"] = res

        client = Ark(base_url=self._video.api_base, api_key=self._video.api_key)

        ref_fallback = os.getenv("ARK_VIDEO_REF_FALLBACK", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        def _create() -> Any:
            return _to_plain(client.content_generation.tasks.create(**create_kwargs))

        with _without_proxy_env():
            try:
                raw = _run_with_retries(
                    _create,
                    attempts=4,
                    base_sleep_seconds=1.4,
                )
            except Exception as first_exc:
                c_list = create_kwargs.get("content")
                if (
                    not ref_fallback
                    or not isinstance(c_list, list)
                    or len(c_list) <= 2
                ):
                    raise
                # 勿用 c_list[:2]：多图时首图曾标为 reference_image；单图 I2V 须 first_frame。
                # 网关要求 image 必带 role，回退路径始终写 role（不跟 ARK_VIDEO_IMAGE_URL_USE_ROLE 关闭）。
                create_kwargs["content"] = [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": src},
                        "role": role_first,
                    },
                ]
                raw = _run_with_retries(
                    _create,
                    attempts=4,
                    base_sleep_seconds=1.4,
                )
                print(
                    "[FrameOS] Ark video: multi-image create failed, retried with first frame only. "
                    f"First error: {first_exc!r}"[:2000],
                    flush=True,
                )
        if not isinstance(raw, dict):
            raise RuntimeError(f"Ark create returned non-dict: {type(raw)}")
        return raw

    def get_ark_video_task_raw(self, provider_task_id: str) -> Any:
        if not self._video:
            raise RuntimeError("未配置 ArkVideoApiParams（video=...）")
        tid = (provider_task_id or "").strip()
        if not tid:
            raise RuntimeError("provider_task_id 为空")
        try:
            from volcenginesdkarkruntime import Ark
        except Exception as exc:
            raise RuntimeError("volcengine Ark SDK missing.") from exc

        client = Ark(base_url=self._video.api_base, api_key=self._video.api_key)

        with _without_proxy_env():
            return _run_with_retries(
                lambda: _to_plain(client.content_generation.tasks.get(task_id=str(tid))),
                attempts=4,
                base_sleep_seconds=1.4,
            )


__all__ = [
    "ArkVideoApiParams",
    "ark_seedance_15_pro_multi_ref_r2v_risk",
    "clamp_ark_video_duration_seconds",
    "normalize_ark_video_model_id",
    "DashScopeWanApiParams",
    "MediaGenerationRequestClient",
    "extract_wan_image_urls_from_response",
    "normalize_first_frame_url_for_ark",
]
