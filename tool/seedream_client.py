"""
火山方舟 Seedream 系列图片生成。

- **推荐**：``volcenginesdkarkruntime.Ark`` → ``client.images.generate(...)``（与官方示例一致）。
- **流式**（``stream=True``）：仍走 HTTP SSE，依赖 ``httpx``（见 :meth:`SeedreamImageClient.generate_stream`）。

环境变量：

- ``SEEDREAM_API_KEY``：优先；未设置则用 ``ARK_API_KEY``
- ``ARK_BASE_URL``：默认 ``https://ark.cn-beijing.volces.com/api/v3``
- ``STORYBOARD_STRICT_PRODUCT_REF``：默认 ``1``，分镜流水线逐镜出图时强制产品与上传参考图一致；``0``/``false`` 关闭

依赖：``pip install 'volcengine-python-sdk[ark]'``（与 ``tool/ai`` 中 Ark 生视频相同栈）。

``SeedreamImageClient`` 可单独使用；Agent 生图与分镜流水线通过 ``generation_tools.LocalWanArkHub`` /
``fill_storyboard_shots_with_seedream`` 调用（见 ``IMAGE_GENERATION_PROVIDER``）。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx

from ai import _to_plain, _without_proxy_env

# 与 generation_tools._ark_image_model 默认一致；可用环境变量 ARK_IMAGE_MODEL 覆盖
DEFAULT_ARK_SEEDREAM_MODEL = "doubao-seedream-5-0-260128"

# 分镜流水线：强制产品与上传参考图一致（默认开启；设 STORYBOARD_STRICT_PRODUCT_REF=0 可关闭）
def _strict_product_lock_enabled() -> bool:
    v = os.getenv("STORYBOARD_STRICT_PRODUCT_REF", "1").strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _strict_product_lock_preamble(*, dual_ref: bool) -> str:
    if dual_ref:
        return (
            "【产品锁死·强制】参考图1=人物模特，参考图2=用户上传的产品实拍。"
            "画面中出现的产品必须与参考图2为同一包装：外轮廓、比例、泵头/盖结构、标签版式、LOGO 形状、印刷文字、主辅色须与图2一致，"
            "禁止替换为其它品牌、禁止臆造瓶型或「通用电商洗发水瓶」、禁止改动标签文案与图形。"
            "若下方镜头描述中的 product/瓶型/圆柱/磨砂等词与参考图2视觉不一致，一律以参考图2为准并忽略冲突描述。"
            "图1仅用于锁定人物面部、发型、体型与衣着，不得换脸。"
        )
    return (
        "【产品锁死·强制】参考图1=用户上传的产品实拍。"
        "画面中出现的产品必须与参考图1为同一包装：外轮廓、比例、泵头/盖结构、标签版式、LOGO、文字、配色须与图1一致，"
        "禁止替换品牌、禁止臆造包装、禁止改成其它常见瓶型。"
        "若下方镜头描述中的 product/瓶型/材质等与参考图1不一致，一律以参考图1为准并忽略冲突描述。"
    )


_REF_SUFFIX_SINGLE = (
    "在参考图1产品外观完全不变的前提下，生成电影分镜单帧静态图；"
    "遵循上方镜头构图、机位与光影；画面无字幕、无画框、无水印式装饰。"
)
_REF_SUFFIX_DUAL = (
    "【双参考执行】人物以参考图1为准；产品以参考图2为准，手持物必须是图2所示实物，不得变形走样。"
    "在镜头描述基础上生成单帧静态图；无字幕、无画框、无水印式装饰。"
)
_MAX_PROMPT_CHARS = 5000


class SeedreamGenerationError(RuntimeError):
    """SDK 异常或 HTTP 非 2xx（仅流式路径）时抛出。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def image_file_to_data_url(path: str | Path) -> str:
    """本地图片 → ``data:image/<小写格式>;base64,...``（符合 Seedream 文档）。"""
    p = Path(path)
    raw = p.read_bytes()
    mime, _ = mimetypes.guess_type(str(p))
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    mime = mime.lower()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def coerce_seedream_size(size: str) -> str:
    """将工具侧 ``2K`` / ``WxH`` 规范为 Seedream ``size`` 字段。"""
    s = (size or "").strip().replace("×", "x")
    if re.fullmatch(r"(?i)\d+x\d+", s):
        return s.lower()
    u = s.upper().replace(" ", "")
    if u == "1K":
        return "2K"
    if u in ("2K", "3K", "4K"):
        return u
    return "2K"


def _safe_shot_filename(shot_id: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", shot_id.strip())
    return s or "shot"


def download_image_url(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, str(dest))


def collect_result_image_urls(response_json: dict[str, Any]) -> list[str]:
    """
    从非流式响应的 ``data`` 数组中提取成功项的 ``url``（``b64_json`` 模式无 url，需自行解析）。
    """
    out: list[str] = []
    for item in response_json.get("data") or []:
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            continue
        u = item.get("url")
        if isinstance(u, str) and u.strip():
            out.append(u.strip())
    return out


def _ensure_ark_sdk():
    try:
        from volcenginesdkarkruntime import Ark
    except ImportError as e:
        raise SeedreamGenerationError(
            "缺少 Ark SDK：请执行 pip install 'volcengine-python-sdk[ark]'",
            payload=str(e),
        ) from e
    return Ark


class SeedreamImageClient:
    """
    Seedream 图片生成：非流式走 ``Ark.images.generate``；流式仍用 HTTP。
    """

    DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

    __slots__ = ("_api_key", "_base_url")

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            key = (
                os.getenv("SEEDREAM_API_KEY", "").strip()
                or os.getenv("ARK_API_KEY", "").strip()
            )
        if not key:
            raise ValueError(
                "缺少 API Key：请设置环境变量 SEEDREAM_API_KEY 或 ARK_API_KEY"
            )
        base = (base_url or os.getenv("ARK_BASE_URL", "").strip() or self.DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self._api_key = key
        self._base_url = base

    @classmethod
    def from_environ(cls) -> SeedreamImageClient:
        return cls()

    def _endpoint(self) -> str:
        return f"{self._base_url}/images/generations"

    @staticmethod
    def _prune_body(d: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in d.items() if v is not None}

    def build_request_body(
        self,
        *,
        model: str,
        prompt: str,
        image: str | list[str] | None = None,
        size: str | None = None,
        seed: int | None = None,
        sequential_image_generation: str | None = None,
        sequential_image_generation_options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        guidance_scale: float | None = None,
        output_format: str | None = None,
        response_format: str | None = None,
        watermark: bool | None = None,
        optimize_prompt_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造请求体（``None`` 字段会省略，由服务端使用默认值）。"""
        return self._prune_body(
            {
                "model": model,
                "prompt": prompt,
                "image": image,
                "size": size,
                "seed": seed,
                "sequential_image_generation": sequential_image_generation,
                "sequential_image_generation_options": sequential_image_generation_options,
                "tools": tools,
                "stream": stream,
                "guidance_scale": guidance_scale,
                "output_format": output_format,
                "response_format": response_format,
                "watermark": watermark,
                "optimize_prompt_options": optimize_prompt_options,
            }
        )

    def _build_ark_client(self, *, timeout_sec: float) -> Any:
        Ark = _ensure_ark_sdk()
        kw: dict[str, Any] = {
            "base_url": self._base_url,
            "api_key": self._api_key,
        }
        try:
            return Ark(**kw, timeout=timeout_sec)
        except TypeError:
            return Ark(**kw)

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        image: str | list[str] | None = None,
        size: str | None = None,
        seed: int | None = None,
        sequential_image_generation: str | None = None,
        sequential_image_generation_options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        guidance_scale: float | None = None,
        output_format: str | None = None,
        response_format: str | None = None,
        watermark: bool | None = None,
        optimize_prompt_options: dict[str, Any] | None = None,
        timeout_sec: float = 300.0,
    ) -> dict[str, Any]:
        """
        非流式：``Ark.images.generate``（与官方示例一致）。

        ``stream=True`` 时请使用 :meth:`generate_stream`。
        """
        if stream:
            raise ValueError(
                "stream=True 时请使用 generate_stream()；generate() 仅支持非流式响应"
            )
        body = self.build_request_body(
            model=model,
            prompt=prompt,
            image=image,
            size=size,
            seed=seed,
            sequential_image_generation=sequential_image_generation,
            sequential_image_generation_options=sequential_image_generation_options,
            tools=tools,
            stream=False,
            guidance_scale=guidance_scale,
            output_format=output_format,
            response_format=response_format,
            watermark=watermark,
            optimize_prompt_options=optimize_prompt_options,
        )
        call_kwargs = {k: v for k, v in body.items() if k != "stream"}

        client = self._build_ark_client(timeout_sec=timeout_sec)
        with _without_proxy_env():
            try:
                resp = client.images.generate(**call_kwargs)
            except Exception as e:
                msg = str(e).strip() or type(e).__name__
                payload: Any = None
                for attr in ("body", "response", "message"):
                    if hasattr(e, attr):
                        payload = getattr(e, attr)
                        break
                raise SeedreamGenerationError(
                    f"Seedream SDK 调用失败: {msg}",
                    payload=payload,
                ) from e

        plain = _to_plain(resp)
        if not isinstance(plain, dict):
            raise SeedreamGenerationError(
                "Seedream 响应无法解析为 dict",
                payload=plain,
            )

        if plain.get("error"):
            err = plain["error"]
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or err)
            else:
                msg = str(err)
            raise SeedreamGenerationError(f"Seedream 错误: {msg}", payload=plain)

        return plain

    def generate_stream(
        self,
        *,
        model: str,
        prompt: str,
        image: str | list[str] | None = None,
        size: str | None = None,
        seed: int | None = None,
        sequential_image_generation: str | None = None,
        sequential_image_generation_options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        guidance_scale: float | None = None,
        output_format: str | None = None,
        response_format: str | None = None,
        watermark: bool | None = None,
        optimize_prompt_options: dict[str, Any] | None = None,
        timeout_sec: float = 300.0,
    ) -> Iterator[dict[str, Any]]:
        """
        流式：HTTP SSE（``httpx``）。若后续 Ark SDK 暴露等价流式接口，可再切换。
        """
        body = self.build_request_body(
            model=model,
            prompt=prompt,
            image=image,
            size=size,
            seed=seed,
            sequential_image_generation=sequential_image_generation,
            sequential_image_generation_options=sequential_image_generation_options,
            tools=tools,
            stream=True,
            guidance_scale=guidance_scale,
            output_format=output_format,
            response_format=response_format,
            watermark=watermark,
            optimize_prompt_options=optimize_prompt_options,
        )
        yield from self._post_stream(body, timeout_sec=timeout_sec)

    def _post_stream(
        self, body: dict[str, Any], *, timeout_sec: float
    ) -> Iterator[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = self._endpoint()
        with httpx.Client(timeout=timeout_sec) as client:
            with client.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    text = r.read().decode("utf-8", errors="replace")
                    raise SeedreamGenerationError(
                        f"Seedream 流式请求失败 HTTP {r.status_code}: {text[:800]}",
                        status_code=r.status_code,
                        payload=text,
                    )
                for line in r.iter_lines():
                    if not line:
                        continue
                    s = line.strip()
                    if s.startswith("data:"):
                        s = s[5:].strip()
                    if s == "[DONE]":
                        break
                    try:
                        obj = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        yield obj


def fill_storyboard_shots_with_seedream(
    shots: list[dict[str, Any]],
    product_reference_image_path: str | Path,
    frames_directory: str | Path,
    relative_url_prefix: str,
    *,
    character_reference_image_path: str | Path | None = None,
    model: str | None = None,
    size: str = "2K",
    watermark: bool = False,
    delay_sec: float = 0.35,
    on_shot_done: Optional[Callable[[int, int, str, str], None]] = None,
) -> None:
    """
    逐镜调用 Seedream：使用每镜 ``generated_prompts.image_prompt``；
    落盘 PNG/JPEG（取决于 ``SEEDREAM_OUTPUT_FORMAT``），并写回 ``frame.image_url`` 相对路径。
    双参考时图片顺序为 **人物 → 产品**（与万相一致）。
    """
    prod = Path(product_reference_image_path)
    if not prod.is_file():
        raise FileNotFoundError(f"参考产品图不存在: {prod}")

    char: Optional[Path] = None
    if character_reference_image_path is not None:
        c = Path(character_reference_image_path)
        if c.is_file() and c.resolve() != prod.resolve():
            char = c

    if char:
        image_field: str | list[str] = [
            image_file_to_data_url(char),
            image_file_to_data_url(prod),
        ]
        suffix = _REF_SUFFIX_DUAL
    else:
        image_field = image_file_to_data_url(prod)
        suffix = _REF_SUFFIX_SINGLE

    mid = (model or "").strip() or os.getenv("ARK_IMAGE_MODEL", DEFAULT_ARK_SEEDREAM_MODEL).strip()
    size_s = coerce_seedream_size(size)
    out_fmt = os.getenv("SEEDREAM_OUTPUT_FORMAT", "png").strip() or None
    extra: dict[str, Any] = {}
    if out_fmt:
        extra["output_format"] = out_fmt
    timeout_sec = float(os.getenv("SEEDREAM_HTTP_TIMEOUT_SEC", "600").strip() or "600")

    client = SeedreamImageClient.from_environ()
    out_dir = Path(frames_directory)
    prefix = relative_url_prefix.strip().strip("/")

    for i, shot in enumerate(shots):
        gp = shot.get("generated_prompts") or {}
        prompt = str(gp.get("image_prompt") or "").strip()
        if not prompt:
            raise ValueError(f"镜头 {shot.get('shot_id')} 缺少 generated_prompts.image_prompt")

        sid = _safe_shot_filename(str(shot.get("shot_id") or f"shot_{i}"))
        ext = "png" if (out_fmt or "").lower() == "png" else "jpg"
        fname = f"{sid}.{ext}"
        print(f"  [Seedream {i + 1}/{len(shots)}] {shot.get('shot_id')} 生图中...")

        pre = (
            _strict_product_lock_preamble(dual_ref=bool(char)) + "\n\n"
            if _strict_product_lock_enabled()
            else ""
        )
        body_prompt = f"{pre}{prompt}\n\n{suffix}"[:_MAX_PROMPT_CHARS]
        raw = client.generate(
            model=mid,
            prompt=body_prompt,
            image=image_field,
            size=size_s,
            sequential_image_generation="disabled",
            response_format="url",
            watermark=watermark,
            timeout_sec=timeout_sec,
            **extra,
        )
        urls = collect_result_image_urls(raw)
        if not urls:
            raise RuntimeError(f"Seedream 未返回图片 URL: {json.dumps(raw, ensure_ascii=False)[:800]}")

        dest = out_dir / fname
        download_image_url(urls[0], dest)

        frame = shot.setdefault("frame", {})
        frame["image_url"] = f"{prefix}/{fname}" if prefix else fname

        if on_shot_done:
            try:
                on_shot_done(i + 1, len(shots), str(shot.get("shot_id") or sid), str(frame["image_url"]))
            except Exception:
                pass

        if delay_sec > 0 and i + 1 < len(shots):
            time.sleep(delay_sec)
