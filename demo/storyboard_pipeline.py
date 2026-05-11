"""
视频分镜脚本生成流水线 — 完整 Python 实现
四步 LLM 串联：产品图分析 → 剧本扩写 → 场景规划细化 → 分镜 JSON 生成

完整分镜脚本默认导出为 **Markdown**（`export_shots_complete_storyboard_script_markdown`）：按镜分块、易读。
仍保留 TSV 导出函数供表格工具使用。

**片长**：用户在 ``PipelineInput.target_duration_sec`` 选的秒数 = 各镜 ``timecode.duration_sec`` 之和（见 ``normalize_shot_timeline_to_target``）；
在总长锁死的前提下，各镜秒数按模型给出的相对时长（或单镜 ``duration_weight``）**加权**分配，重要/长动作镜可更长。
分镜图数量 N 由 ``calc_shot_budget`` 按片长与 **当前 ``ARK_VIDEO_MODEL`` 单段最短时长** 共同约束（约每 3.5 秒 1 镜，且须满足 ``N × 单镜下限 ≤ 片长``；默认 2≤N≤45）。
若初稿镜数过密，``merge_shots_for_ark_duration_floor`` 会合并相邻镜，使每镜规划时长不低于方舟下限，避免成片阶段裁掉已生成内容。

一步出分镜图：run_storyboard_one_stop(..., generate_shot_images=True) 调用万相（见 wan_image_client.py，需 DASHSCOPE_API_KEY）。

依赖：
    pip install anthropic
    分镜出图：pip install dashscope>=1.25.15

Step 0 调 Anthropic Vision 时：大图会先 **缩边长 + 转 JPEG** 再 base64，减轻上游网关 ``413 Request Entity Too Large``。
环境变量：``STORYBOARD_VISION_MAX_SIDE``（默认 2048）、``STORYBOARD_VISION_JPEG_QUALITY``（默认 88）。
无 Pillow 时回退为原图直读（大文件易 413）。
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import anthropic

_TOOL_DIR = Path(__file__).resolve().parent.parent / "tool"
if _TOOL_DIR.is_dir() and str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
from ai import clamp_ark_video_duration_seconds, normalize_ark_video_model_id  # noqa: E402

# ──────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────


@dataclass
class PipelineInput:
    description: str  # 一句话描述
    hotword: str  # 热点词语
    image_paths: list[str]  # 产品三视图本地路径（1-3张）
    fps: int = 24
    target_duration_sec: int = 30
    style: str = "写实"


@dataclass
class PipelineResult:
    product_desc: dict[str, Any]
    blueprint: dict[str, Any]
    scene_scripts: list[dict[str, Any]]
    shots: list[dict[str, Any]]  # 最终分镜JSON数组
    character_visual: dict[str, Any] = field(default_factory=dict)
    timeline_merge_info: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────


def _vision_image_max_side() -> int:
    try:
        v = int(os.getenv("STORYBOARD_VISION_MAX_SIDE", "2048").strip())
        return max(512, min(8192, v))
    except ValueError:
        return 2048


def _vision_jpeg_quality() -> int:
    try:
        v = int(os.getenv("STORYBOARD_VISION_JPEG_QUALITY", "88").strip())
        return max(65, min(95, v))
    except ValueError:
        return 88


def _load_image_base64_raw(path: str) -> dict[str, Any]:
    """原图直读（仅作无 Pillow 时的回退）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    media_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = media_type_map.get(suffix, "image/jpeg")
    data = base64.standard_b64encode(p.read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def load_image_base64(path: str) -> dict[str, Any]:
    """
    读取本地图片，返回 Anthropic Messages ``image`` content block。

    有 Pillow 时：先按最长边缩至 ``STORYBOARD_VISION_MAX_SIDE``（默认 2048），再编码为 JPEG，
    避免 Step 0 多张大原图导致反代 ``413 Request Entity Too Large``。
    """
    try:
        from PIL import Image
    except ImportError:
        return _load_image_base64_raw(path)

    max_side = _vision_image_max_side()
    quality = _vision_jpeg_quality()
    p = Path(path)
    try:
        raw = p.read_bytes()
        buf = io.BytesIO()
        with Image.open(io.BytesIO(raw)) as im:
            if im.mode == "P":
                im = im.convert("RGBA")
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
            w, h = im.size
            m = max(w, h)
            if m > max_side:
                scale = max_side / float(m)
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
                im = im.resize((nw, nh), resample)
            im.save(buf, format="JPEG", quality=quality, optimize=True)
        data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
        }
    except Exception:
        return _load_image_base64_raw(path)


def _first_balanced_json_object(s: str) -> str | None:
    """从文本中取出第一个花括号平衡的 JSON 对象子串（忽略字符串内的括号）。"""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    quote = ""
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _first_balanced_json_array(s: str) -> str | None:
    """从文本中取出第一个括号平衡的 JSON 数组子串。"""
    start = s.find("[")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    quote = ""
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def parse_json_response(text: str) -> dict[str, Any] | list[Any]:
    """
    从 LLM 响应中提取 JSON，兼容带 markdown 代码块的情况。
    Claude 偶尔会输出 ```json ... ```，用正则剥掉，防止 json.loads 报错。

    模型常在字符串值里直接换行，标准 JSON 不允许未转义控制字符；
    使用 ``strict=False`` 放宽解析（Python 3.9+）。
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    else:
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    body = text.strip()
    candidates = [body]
    sub_o = _first_balanced_json_object(body)
    if sub_o and sub_o != body:
        candidates.append(sub_o)
    sub_a = _first_balanced_json_array(body)
    if sub_a and sub_a not in candidates:
        candidates.append(sub_a)

    last_err: json.JSONDecodeError | None = None
    for cand in candidates:
        try:
            return json.loads(cand, strict=False)
        except json.JSONDecodeError as e:
            last_err = e
    if last_err is not None:
        raise last_err
    raise json.JSONDecodeError("empty JSON payload", body, 0)


def make_anthropic_client(api_key: Optional[str] = None) -> anthropic.Anthropic:
    """
    与 agent / .env 对齐：支持 ANTHROPIC_API_KEY、可选 ANTHROPIC_BASE_URL、MODEL_ID。
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("请设置 ANTHROPIC_API_KEY 或传入 api_key")
    base_url = os.getenv("ANTHROPIC_BASE_URL")
    if base_url:
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    kwargs: dict[str, Any] = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def call_claude(
    client: anthropic.Anthropic,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 4000,
) -> str:
    """统一的 Claude API 调用"""
    model = os.environ.get("MODEL_ID", "claude-sonnet-4-20250514")
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    block = response.content[0]
    if block.type != "text":
        raise RuntimeError(f"Unexpected content block type: {block.type}")
    return block.text


# ──────────────────────────────────────────
# Step 0：路由 + 产品轨 / 人物轨 双解析
# ──────────────────────────────────────────

STEP0_ROUTE_SYSTEM = """你是广告素材路由员。从多张参考图中选出产品包装主图序号；若存在独立人物肖像图则给出其序号（必须与主图不同）。只输出JSON，禁止markdown。"""

STEP0_ROUTE_USER_TMPL = """{file_context}
输出JSON（字段必填，无则 null 或空字符串）：
{{
  "packshot_image_index": 1,
  "talent_reference_image_index": null,
  "one_line_product": "一句话品类/产品",
  "talent_appearance_draft": "若有人物图则1句外貌草稿，否则空字符串"
}}
规则：
- packshot_image_index: 1～{n}，最能代表瓶身/包装的一张。
- talent_reference_image_index: 若有明确人物肖像且非产品主图则 1～{n}，否则 null；不得与 packshot_image_index 相同。
文件名列表：{names_hint}
"""

STEP0_PRODUCT_SYSTEM = """你是广告产品视觉分析师。只输出JSON，禁止输出任何其他内容。
若用户另附「拍摄需求摘要」且已写明产品品类，则 **category、product_name 的品类语义必须与该摘要一致**；图像只用于描述瓶型、颜色、标签可见文字。
禁止把用户要拍的品类（如洗发）写成与需求矛盾的类目（如空气清新剂/香薰），除非摘要未提及品类且画面明确是其他品类。"""

STEP0_PRODUCT_USER = """分析这张产品图，输出以下JSON；字符串字段不得为 null（无信息用空字符串 \"\"）：
{{
  "product_name": "",
  "category": "",
  "pack_shape": "",
  "pack_color_main": "",
  "pack_color_accent": "",
  "cap_type": "",
  "surface_texture": "",
  "label_content": "",
  "size_impression": "",
  "packshot_prompt_en": ""
}}
packshot_prompt_en：英文产品描述用于图像生成，写实风格，≤30 个英文词。

{brief_addon}"""

STEP0_CHARACTER_SYSTEM = """你是影视选角导演。只输出JSON，禁止输出任何其他内容。"""

STEP0_CHARACTER_USER = """分析人物参考图，输出以下JSON；字符串字段不得为 null（无信息用 \"\"）：
{{
  "gender": "",
  "age_range": "",
  "face_shape": "",
  "skin_tone": "",
  "eye_description": "",
  "hair_style": "",
  "hair_color": "",
  "body_type": "",
  "overall_vibe": "",
  "character_prompt_en": ""
}}
character_prompt_en：英文，强调面部与发型，用于图生图锁脸，≤40 个英文词。"""


def _default_character_visual() -> dict[str, Any]:
    return {
        "gender": "未指定",
        "age_range": "青年",
        "face_shape": "",
        "skin_tone": "",
        "eye_description": "",
        "hair_style": "自然发型",
        "hair_color": "",
        "body_type": "",
        "overall_vibe": "普通素人气质",
        "character_prompt_en": (
            "consistent young adult protagonist, natural skin texture, "
            "same facial features and hairstyle across all shots, photorealistic"
        ),
    }


def _step0_filename_context(image_paths: list[str]) -> str:
    """把上传时的原始文件名提供给模型，减少「看错品类」的幻觉。"""
    lines = [
        "以下每张图与上传时的**原始文件名**一一对应（按顺序：先第1张图，再第2张……）。"
        "文件名常含品类或内部命名，请结合画面综合判断；不要忽略文件名线索。",
        "",
    ]
    for i, path in enumerate(image_paths, start=1):
        lines.append(f"- 第{i}张图 ← 文件：`{Path(path).name}`")
    return "\n".join(lines)


_HAIR_CARE_HINTS = re.compile(
    r"洗发|洗发水|洗发露|洗发乳|护发|去屑|控油|头皮|发膜|育发|秀发|防脱|\bshampoo\b",
    re.I,
)
_AIR_FRESHEN_HINTS = re.compile(
    r"air\s*fresh|air\s*freshener|\bfreshener\b|空气清新|芳香剂|空间香氛",
    re.I,
)


def _user_brief_blob(description: str, hotword: str, route_one_line: str) -> str:
    parts = [
        str(description or "").strip(),
        str(hotword or "").strip(),
        str(route_one_line or "").strip(),
    ]
    return "\n".join(p for p in parts if p)


def _user_intends_hair_care(blob: str) -> bool:
    return bool(blob and _HAIR_CARE_HINTS.search(blob))


def _vision_text_for_category_check(product_visual: dict[str, Any]) -> str:
    keys = (
        "product_name",
        "category",
        "label_content",
        "packshot_prompt_en",
        "cap_type",
    )
    return " ".join(str(product_visual.get(k) or "") for k in keys)


def _vision_suggests_air_freshener(product_visual: dict[str, Any]) -> bool:
    b = _vision_text_for_category_check(product_visual)
    if not b.strip():
        return False
    if _AIR_FRESHEN_HINTS.search(b):
        return True
    low = b.lower()
    if "air fresh" in low and not _HAIR_CARE_HINTS.search(b):
        return True
    return False


def _pick_cn_hair_product_name(blob: str) -> str:
    m = re.search(
        r"([\u4e00-\u9fffA-Za-z0-9·]{2,22}洗发(?:水|露|乳|产品)?)",
        blob,
    )
    if m:
        return m.group(1).strip()
    if "控油" in blob:
        return "控油洗发水"
    if "去屑" in blob:
        return "去屑洗发水"
    if "护发" in blob:
        return "护发洗发产品"
    return "洗发产品（包装以参考图为准）"


def _default_hair_category_phrase(blob: str) -> str:
    if "控油" in blob:
        return "控油洗发水"
    if "去屑" in blob:
        return "去屑洗发水"
    return "洗发护发"


def _reconcile_hair_brief_vs_air_freshener_vision(
    product_visual: dict[str, Any],
    route: dict[str, Any],
    description: str,
    hotword: str,
) -> None:
    """
    用户需求为洗发类，但视觉把包装读成空气清新剂时，用 brief 对齐品类与英文 packshot，
    避免 Step1/Step2 写出「洗发水剧情 + Air Fresh」撕裂。
    """
    route_line = str(route.get("one_line_product") or "").strip()
    blob = _user_brief_blob(description, hotword, route_line)
    if not _user_intends_hair_care(blob) or not _vision_suggests_air_freshener(product_visual):
        return
    cn_name = _pick_cn_hair_product_name(blob)
    route_has_hair = bool(route_line and _HAIR_CARE_HINTS.search(route_line))
    product_visual["product_name"] = route_line if route_has_hair else cn_name
    product_visual["category"] = _default_hair_category_phrase(blob)
    shape = str(product_visual.get("pack_shape") or "cylindrical bottle").strip()
    col_main = str(product_visual.get("pack_color_main") or "white").strip()
    col_accent = str(product_visual.get("pack_color_accent") or "").strip()
    cap = str(product_visual.get("cap_type") or "cap").strip()
    product_visual["label_content"] = (
        f"{product_visual['product_name']}，瓶身与标签以参考图为准"
    )[:200]
    accents = f", {col_accent} accents" if col_accent else ""
    product_visual["packshot_prompt_en"] = (
        f"Photorealistic hair-care shampoo bottle, {shape}, {col_main} body{accents}, "
        f"{cap}, label layout exactly matching reference image, studio packshot, "
        "not air freshener or room spray"
    )[:480]


def _step0_product_user_text(brief_context: str) -> str:
    ctx = str(brief_context or "").strip()
    addon = ""
    if ctx:
        addon = (
            "\n【用户拍摄需求摘要（品类与用途以前提为准；与画面营销文案冲突时以摘要品类为准）】\n"
            + ctx
        )
    return STEP0_PRODUCT_USER.format(brief_addon=addon)


def step0_route_packshot_talent(
    client: anthropic.Anthropic, image_paths: list[str]
) -> dict[str, Any]:
    n = len(image_paths)
    content: list[dict[str, Any]] = []
    for path in image_paths:
        if not Path(path).is_file():
            raise FileNotFoundError(f"产品图不存在: {path}")
        content.append(load_image_base64(path))
    names_hint = ", ".join(Path(p).name for p in image_paths)
    user_text = STEP0_ROUTE_USER_TMPL.format(
        file_context=_step0_filename_context(image_paths),
        n=n,
        names_hint=names_hint,
    )
    content.append({"type": "text", "text": user_text})
    text = call_claude(
        client, STEP0_ROUTE_SYSTEM, [{"role": "user", "content": content}], max_tokens=700
    )
    return parse_json_response(text)


def step0_analyze_product_only(
    client: anthropic.Anthropic, image_path: str, brief_context: str = ""
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        load_image_base64(image_path),
        {"type": "text", "text": _step0_product_user_text(brief_context)},
    ]
    text = call_claude(
        client, STEP0_PRODUCT_SYSTEM, [{"role": "user", "content": content}], max_tokens=1200
    )
    return parse_json_response(text)


def _backfill_product_visual_if_needed(
    product_visual: dict[str, Any],
    route: dict[str, Any],
) -> None:
    """
    Vision 偶发返回全空字符串时，用路由「一句话品类」等回填，
    避免 product_visual / packshot_prompt_en 为空导致 Step1 缺产品信息、出图缺英文产品锁。
    """
    route_line = str(route.get("one_line_product") or "").strip()
    pn = str(product_visual.get("product_name") or route_line or "").strip()
    if not pn:
        pn = "hair care shampoo bottle"
    if not str(product_visual.get("product_name") or "").strip():
        product_visual["product_name"] = pn
    if not str(product_visual.get("category") or "").strip():
        product_visual["category"] = "个人护理 / 洗发"
    if not str(product_visual.get("pack_shape") or "").strip():
        product_visual["pack_shape"] = "圆柱形瓶身（以参考图为准）"
    if not str(product_visual.get("pack_color_main") or "").strip():
        product_visual["pack_color_main"] = "以参考产品图主色为准"
    if not str(product_visual.get("cap_type") or "").strip():
        product_visual["cap_type"] = "泵头或旋盖（以参考图为准）"
    if not str(product_visual.get("surface_texture") or "").strip():
        product_visual["surface_texture"] = "以参考图瓶身材质为准"
    if not str(product_visual.get("label_content") or "").strip() and route_line:
        product_visual["label_content"] = route_line[:200]
    if not str(product_visual.get("packshot_prompt_en") or "").strip():
        safe = pn.replace('"', "").replace("\n", " ")[:72]
        product_visual["packshot_prompt_en"] = (
            f"photorealistic {safe} bottle, pump or cap as in reference image, "
            "label layout and brand colors exactly matching the product reference, "
            "clean studio packshot, no invented packaging"
        )[:480]


def step0_analyze_character_only(
    client: anthropic.Anthropic, image_path: str
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        load_image_base64(image_path),
        {"type": "text", "text": STEP0_CHARACTER_USER},
    ]
    text = call_claude(
        client, STEP0_CHARACTER_SYSTEM, [{"role": "user", "content": content}], max_tokens=1000
    )
    return parse_json_response(text)


def analyze_product(
    client: anthropic.Anthropic,
    image_paths: list[str],
    pipeline_input: Optional[PipelineInput] = None,
) -> dict[str, Any]:
    """Step 0：路由 → 产品单图 JSON + 人物单图 JSON（无人物图则用默认描述）。"""
    if not image_paths:
        raise ValueError("image_paths 不能为空，请提供至少一张产品图路径")

    names = ", ".join(Path(p).name for p in image_paths)
    print(f"  [Step 0] 双轨分析参考图（{names}）...")

    n_img = len(image_paths)
    route = step0_route_packshot_talent(client, image_paths)
    route_line = str(route.get("one_line_product") or "").strip()
    brief_ctx_parts: list[str] = []
    if pipeline_input is not None:
        brief_ctx_parts.append(f"需求：{pipeline_input.description}")
        brief_ctx_parts.append(f"热点词：{pipeline_input.hotword}")
    if route_line:
        brief_ctx_parts.append(f"路由一句话产品：{route_line}")
    brief_ctx = "\n".join(brief_ctx_parts)
    try:
        pk = int(route.get("packshot_image_index", 1))
    except (TypeError, ValueError):
        pk = 1
    pk = max(1, min(n_img, pk))
    product_path = image_paths[pk - 1]
    product_visual = step0_analyze_product_only(client, product_path, brief_context=brief_ctx)
    _backfill_product_visual_if_needed(product_visual, route)
    if pipeline_input is not None:
        _reconcile_hair_brief_vs_air_freshener_vision(
            product_visual,
            route,
            pipeline_input.description,
            pipeline_input.hotword,
        )

    raw_talent = route.get("talent_reference_image_index")
    tix: int | None
    if raw_talent is None or (isinstance(raw_talent, str) and raw_talent.lower() in ("null", "none", "")):
        tix = None
    else:
        try:
            tix = int(raw_talent)
        except (TypeError, ValueError):
            tix = None
    if tix is not None and (tix < 1 or tix > n_img or tix == pk):
        tix = None

    ta_summary = str(route.get("talent_appearance_draft") or "").strip()
    if tix is not None:
        character_visual = step0_analyze_character_only(client, image_paths[tix - 1])
        ta_bits = [
            str(character_visual.get("hair_style") or ""),
            str(character_visual.get("face_shape") or ""),
            str(character_visual.get("gender") or ""),
            str(character_visual.get("overall_vibe") or ""),
        ]
        ta_summary = " ".join(x for x in ta_bits if x).strip() or ta_summary
    else:
        character_visual = _default_character_visual()
        tix = None

    appearance = " ".join(
        x
        for x in [
            str(product_visual.get("pack_shape") or ""),
            str(product_visual.get("pack_color_main") or ""),
            str(product_visual.get("surface_texture") or ""),
            str(product_visual.get("label_content") or "")[:120],
        ]
        if x
    ).strip()

    merged: dict[str, Any] = {
        "name": product_visual.get("product_name")
        or route.get("one_line_product")
        or "产品",
        "appearance": appearance or str(product_visual.get("packshot_prompt_en", ""))[:240],
        "packshot_image_index": pk,
        "talent_reference_image_index": tix,
        "talent_appearance": ta_summary if tix is not None else None,
        "logo_position": (str(product_visual.get("label_content") or "")[:200] or None),
        "size_estimate": str(product_visual.get("size_impression") or "") or None,
        "key_features": [
            x
            for x in [
                str(product_visual.get("cap_type") or ""),
                str(product_visual.get("category") or ""),
            ]
            if x
        ],
        "brand_mood": str(product_visual.get("category") or "广告片"),
        "color_palette": [
            str(product_visual.get("pack_color_main") or ""),
            str(product_visual.get("pack_color_accent") or ""),
        ],
        "usage_scenario": "短视频广告",
        "product_visual": product_visual,
        "character_visual": character_visual,
        "packshot_prompt_en": str(product_visual.get("packshot_prompt_en") or "").strip(),
        "character_prompt_en": str(character_visual.get("character_prompt_en") or "").strip(),
    }
    print(
        f"  [Step 0] 完成 → 产品：{merged.get('name')}，主图=第{pk}张"
        + (
            f"，人物参考=第{tix}张"
            if tix is not None
            else "（无独立人物图，使用默认人物英文锁脸描述）"
        )
    )
    return merged


def _normalize_talent_reference_index(result: dict[str, Any], n_img: int) -> None:
    """多图时尽量区分人物参考与产品主图；供万相双参考输入。"""
    pk = int(result.get("packshot_image_index", 1))
    raw = result.get("talent_reference_image_index")
    talent: int | None
    if raw is None or (isinstance(raw, str) and raw.lower() in ("null", "none", "")):
        talent = None
    else:
        try:
            talent = int(raw)
        except (TypeError, ValueError):
            talent = None
    if n_img < 2:
        result["talent_reference_image_index"] = None
        result["talent_appearance"] = None
        return
    if talent is None or talent < 1 or talent > n_img or talent == pk:
        talent = next((i for i in range(1, n_img + 1) if i != pk), None)
    result["talent_reference_image_index"] = talent
    if talent is None:
        result["talent_appearance"] = None
    else:
        ta = result.get("talent_appearance")
        if ta is None or (isinstance(ta, str) and not str(ta).strip()):
            result["talent_appearance"] = None
        else:
            result["talent_appearance"] = str(ta).strip()


def _storyboard_respect_ark_shot_duration_floor() -> bool:
    """为 false 时不按方舟单段最短时长收紧镜数 / 合并镜头（仅调试用）。"""
    v = (os.getenv("STORYBOARD_RESPECT_ARK_MIN_SHOT_SEC", "true") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def storyboard_ark_video_model_id() -> str:
    return normalize_ark_video_model_id(
        (os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-1-5-pro-251215") or "").strip()
        or "doubao-seedance-1-5-pro-251215"
    )


def storyboard_per_shot_duration_bounds() -> tuple[int, int]:
    """当前 ``ARK_VIDEO_MODEL`` 下图生视频单段允许的整数秒闭区间 [lo, hi]。"""
    mid = storyboard_ark_video_model_id()
    lo = clamp_ark_video_duration_seconds(1, mid)
    hi = clamp_ark_video_duration_seconds(99, mid)
    return int(lo), int(hi)


def storyboard_timeline_min_shot_sec() -> int:
    """时间轴规划用的单镜下限（与 ``STORYBOARD_RESPECT_ARK_MIN_SHOT_SEC`` 联动）。"""
    if not _storyboard_respect_ark_shot_duration_floor():
        return 2
    lo, _hi = storyboard_per_shot_duration_bounds()
    return max(2, int(lo))


def calc_shot_budget(target_duration_sec: int, *, min_seconds_per_shot: int = 2) -> int:
    """
    全片分镜条数（= 分镜图张数 / 最终 ``shots`` 长度）。

    在 ``min_seconds_per_shot``（默认可为 2）约束下：须满足 ``N × min_seconds_per_shot ≤ 片长``，
    故镜数上限为 ``t // min_seconds_per_shot``；同时按约 **每 3.5 秒 1 镜** 估算叙事密度，
    二者取较小值。叙事上倾向至少 4 镜（三幕），但若片长不足以支撑 4 镜×下限秒，则降到可行上限。
    流水线末尾 ``normalize_shot_timeline_to_target`` 会把各镜秒数调整为**总和恰好等于目标片长**。
    """
    t = max(1, min(180, int(target_duration_sec)))
    ms = max(1, int(min_seconds_per_shot))
    density = max(1, int(round(t / 3.5)))
    cap_by_floor = max(1, t // ms)
    n = min(density, cap_by_floor, 45)
    min_n = min(4, cap_by_floor)
    return max(min_n, n)


def _merge_two_blueprint_scenes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """将两场戏合并为一场（用于镜头预算收紧时减少 scene 数）。"""
    out: dict[str, Any] = dict(a)
    da = int(a.get("duration_sec") or 0)
    db = int(b.get("duration_sec") or 0)
    out["duration_sec"] = max(1, da + db)
    sca = int(a.get("shot_count") or 0)
    scb = int(b.get("shot_count") or 0)
    out["shot_count"] = max(1, sca + scb)

    def _join(x: str, y: str) -> str:
        x, y = x.strip(), y.strip()
        if x and y:
            return f"{x} （承接）{y}"
        return x or y

    out["scene_desc"] = _join(str(a.get("scene_desc") or ""), str(b.get("scene_desc") or ""))
    out["location"] = _join(str(a.get("location") or ""), str(b.get("location") or ""))
    out["plot_beat"] = _join(str(a.get("plot_beat") or ""), str(b.get("plot_beat") or ""))
    out["product_role"] = _join(str(a.get("product_role") or ""), str(b.get("product_role") or ""))
    pa = bool(a.get("product_appears"))
    pb = bool(b.get("product_appears"))
    out["product_appears"] = pa or pb

    chars: dict[str, dict[str, Any]] = {}
    for src in (a, b):
        for c in src.get("characters") or []:
            if isinstance(c, dict):
                cid = str(c.get("char_id") or "").strip()
                if cid:
                    chars[cid] = c
    out["characters"] = list(chars.values())
    return out


def _distribute_shots_across_scenes(scenes: list[dict[str, Any]], total: int) -> None:
    """将 total 条分镜摊到各场景，每场至少 1 条；优先给时长更长的场景多分镜。"""
    m = len(scenes)
    if m == 0:
        return
    need = max(m, int(total))
    durs = [
        max(1, int(s.get("duration_sec") or s.get("shot_count") or 1)) for s in scenes
    ]
    counts = [1] * m
    leftover = need - m
    order = sorted(range(m), key=lambda i: -durs[i])
    oi = 0
    while leftover > 0:
        counts[order[oi % m]] += 1
        leftover -= 1
        oi += 1
    for s, c in zip(scenes, counts):
        s["shot_count"] = c


def normalize_blueprint_shot_budget(
    blueprint: dict[str, Any],
    *,
    target_duration_sec: int,
    min_seconds_per_shot: int = 2,
) -> int:
    """
    按片长收紧蓝图：全片分镜总数 = calc_shot_budget；
    若场景过多则合并尾部场景，再按场次时长分配各场 shot_count。
    返回归一化后的全片镜头总数。
    """
    n = calc_shot_budget(target_duration_sec, min_seconds_per_shot=min_seconds_per_shot)
    scenes = blueprint.get("scenes")
    if not isinstance(scenes, list):
        return n
    scenes = [s for s in scenes if isinstance(s, dict)]
    if not scenes:
        blueprint["scenes"] = []
        blueprint["scene_count"] = 0
        return n

    while len(scenes) > n:
        if len(scenes) < 2:
            break
        tail = scenes.pop()
        scenes[-1] = _merge_two_blueprint_scenes(scenes[-1], tail)

    m = len(scenes)
    if n < m:
        n = m
    _distribute_shots_across_scenes(scenes, n)
    blueprint["scenes"] = scenes
    blueprint["scene_count"] = m
    return n


def step1_duration_policy(
    target_duration_sec: int, *, min_seconds_per_shot: int = 2
) -> str:
    """按目标片长说明：镜数 N 由系统公式给出，成片总时长将锁为 t 秒。"""
    t = max(1, min(180, int(target_duration_sec)))
    ms = max(1, int(min_seconds_per_shot))
    n = calc_shot_budget(t, min_seconds_per_shot=ms)
    avg = t / n if n else float(t)
    lo = max(ms, int(round(avg)) - 1)
    hi = min(12, int(round(avg)) + 2)
    rec_lo = max(1, (n + 5) // 6)
    rec_hi = min(n, min(12, max(rec_lo, (n + 2) // 3)))
    n_floor = max(1, min(4, t // ms))
    ark_line = ""
    if ms >= 3 and _storyboard_respect_ark_shot_duration_floor():
        ark_line = (
            f"\n- **图生视频单镜下限**：当前模型下单段成片规划须 **≥ {ms} 秒**；"
            f"若 Step3 产出的镜数偏多，流水线会**自动合并相邻镜头**，避免云端已生成内容被裁短浪费。\n"
        )
    return (
        f"目标总时长 **{t} 秒**（成片时间轴将严格等于此值）。\n"
        f"- **全片分镜总数 N = {n}**（约每 3.5 秒 1 镜与「单镜 ≥{ms}s」共同约束；**{n_floor} ≤ N ≤ 45**；"
        f"各场景 **shot_count 之和必须恰好等于 {n}**）。\n"
        f"- **scene_count** 建议 **{rec_lo}～{rec_hi}**，且 **scene_count ≤ N**（每场至少 1 镜）。\n"
        f"- 单镜时长在脚本里会重整为总和 **{t} 秒**，当前可暂按约 **{avg:.1f} 秒/镜**构思（景别上可在 **{lo}～{hi}** 秒间浮动）。\n"
        f"- 三幕结构下 ACT2 须承担产品介入，全片最后一镜须为 packshot/品牌落版意向。"
        f"{ark_line}"
    )


def flatten_acts_to_scenes(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    """将 Step1 的 acts[].scenes 摊平为与旧版兼容的 scenes 列表。"""
    raw = blueprint.get("scenes")
    if isinstance(raw, list) and raw:
        return [s for s in raw if isinstance(s, dict)]
    out: list[dict[str, Any]] = []
    for act in blueprint.get("acts") or []:
        if not isinstance(act, dict):
            continue
        aid = act.get("act")
        aname = str(act.get("name") or "")
        for s in act.get("scenes") or []:
            if not isinstance(s, dict):
                continue
            row = dict(s)
            row["act"] = aid
            row["act_name"] = aname
            loc = str(s.get("location") or "").strip()
            ie = str(s.get("int_ext") or "").strip()
            tod = str(s.get("time_of_day") or "").strip()
            sid = str(s.get("scene_id") or "").strip()
            row.setdefault(
                "scene_desc",
                " · ".join(x for x in (loc, ie, tod) if x) or sid,
            )
            row.setdefault("location_type", ie or "INT")
            row.setdefault("plot_beat", str(s.get("scene_goal") or ""))
            row.setdefault("mood", "")
            row.setdefault("product_appears", bool(s.get("must_show")))
            row.setdefault(
                "product_role",
                "、".join(s.get("must_show") or []) if isinstance(s.get("must_show"), list) else "",
            )
            row.setdefault("characters", [])
            out.append(row)
    return out


# ──────────────────────────────────────────
# Step 1：三幕结构蓝图
# ──────────────────────────────────────────

STEP1_SYSTEM = """你是广告片导演，擅长在 15～60 秒内讲完情感完整的故事。只输出JSON，禁止markdown。

【铁律】
1. 必须输出三幕：ACT1 困境 / ACT2 转机 / ACT3 蜕变；**acts 数组长度必须为 3**，且三个元素的 **act 字段必须分别为 1、2、3**（各一幕）。
   **禁止**只输出 ACT3、禁止缺幕、禁止把三幕合并进一个 act；违反则输出无效。
2. ACT3 中**至少一个**场景的 location 字符串必须与 ACT1 中**某一个**场景的 location **完全相同**（视觉闭环）。
3. **total_shots** 必须等于用户给出的 shot_budget；所有叶子场景 **shot_count** 之和必须等于 total_shots。
4. 产品必须在 ACT2 出现：**ACT2 所有场景 shot_count 之和 ≥ 2**，且 ACT2 的 must_show 中须含与产品使用相关的条目。
5. **最后一个场景**（全片排序的最后一项）必须包含 packshot/品牌落版意向：must_show 含「产品 packshot」或「品牌落版」类描述。
6. 热点词须自然融入叙事，不得生硬口播堆砌。
7. 产品**中文品名、品牌写法**须与用户给定及「人物与产品」段落一致，**禁止**臆造未出现的英文品牌名（例如用户未写则不要用 Keebon 等自造名）。
8. 产品**功能品类**须与「需求」「热点词」一致：**禁止**在剧情里把洗发类需求写成空气清新剂等其他品类（若分析行与需求冲突，以需求为准）。"""

STEP1_USER_TMPL = """## 用户输入
需求：{description}
热点词：{hotword}

## 人物与产品（来自分析）
人物：{character_name}，气质摘要：{overall_vibe}
产品：{product_name}；包装：{pack_shape}，主色：{pack_color_main}
（叙事、台词、落版须使用上述中文品名；勿自造英文品牌。若需求/热点词写明的品类与此处不一致，**以需求品类为准**写 must_show 与台词；出图仍须贴合参考图瓶身。）

## 时长与镜头（必须遵守）
{duration_policy}
**total_shots（N）必须为：{shot_budget}**

## 参数
目标时长：{target_duration_sec} 秒
风格：{style}
帧率：{fps} fps

## 输出 JSON（acts 为三幕；每幕内含 scenes 数组）
{{
  "title": "广告片标题",
  "theme": "核心情感主题",
  "video_concept": "一句话创意",
  "tone": "整体基调",
  "target_emotion": "观众最终情绪",
  "hotword_integration": "热点词如何融入",
  "total_duration_sec": {target_duration_sec},
  "total_shots": {shot_budget},
  "acts": [
    {{
      "act": 1,
      "name": "ACT1-困境",
      "scenes": [
        {{
          "scene_id": "S01",
          "location": "场景地点（与后续闭环一致时请复用同一字符串）",
          "int_ext": "INT或EXT",
          "time_of_day": "清晨/白天/傍晚/深夜",
          "shot_count": 2,
          "scene_goal": "叙事目标",
          "emotional_arc": "起点情绪→终点情绪",
          "must_show": ["必须出现的视觉元素"]
        }}
      ]
    }},
    {{
      "act": 2,
      "name": "ACT2-转机",
      "scenes": []
    }},
    {{
      "act": 3,
      "name": "ACT3-蜕变",
      "scenes": []
    }}
  ]
}}

自检（必须在心中完成后再输出）：
□ acts.length === 3
□ ACT3 某 scene.location 与 ACT1 某 scene.location 完全相同
□ 所有 scene.shot_count 之和 === {shot_budget}
□ ACT2 场景 shot_count 之和 ≥ 2 且 must_show 体现产品使用
□ 排序上的最后一镜场景 must_show 含 packshot/品牌落版"""


def _acts_three_structure_ok(acts: Any) -> bool:
    """Step1 是否同时包含 ACT1/ACT2/ACT3 且每幕至少一场戏。"""
    if not isinstance(acts, list) or len(acts) != 3:
        return False
    seen: set[int] = set()
    for a in acts:
        if not isinstance(a, dict):
            return False
        try:
            ai = int(a.get("act"))
        except (TypeError, ValueError):
            return False
        if ai not in (1, 2, 3):
            return False
        seen.add(ai)
        scenes = a.get("scenes")
        if not isinstance(scenes, list) or len(scenes) < 1:
            return False
    return seen == {1, 2, 3}


def expand_screenplay(
    client: anthropic.Anthropic,
    pipeline_input: PipelineInput,
    product_desc: dict[str, Any],
) -> dict[str, Any]:
    """Step 1：一句话 + 热点词 + 产品 → 三幕故事蓝图"""
    print("  [Step 1] 三幕剧本扩写...")

    pv = product_desc.get("product_visual") if isinstance(product_desc.get("product_visual"), dict) else {}
    cv = (
        product_desc.get("character_visual")
        if isinstance(product_desc.get("character_visual"), dict)
        else {}
    )
    ms = storyboard_timeline_min_shot_sec()
    shot_budget = calc_shot_budget(pipeline_input.target_duration_sec, min_seconds_per_shot=ms)

    user_content = STEP1_USER_TMPL.format(
        description=pipeline_input.description,
        hotword=pipeline_input.hotword,
        character_name=str(product_desc.get("name") or "主角"),
        overall_vibe=str(cv.get("overall_vibe") or product_desc.get("talent_appearance") or ""),
        product_name=str(pv.get("product_name") or product_desc.get("name") or "产品"),
        pack_shape=str(pv.get("pack_shape") or ""),
        pack_color_main=str(pv.get("pack_color_main") or ""),
        target_duration_sec=pipeline_input.target_duration_sec,
        style=pipeline_input.style,
        fps=pipeline_input.fps,
        duration_policy=step1_duration_policy(
            pipeline_input.target_duration_sec, min_seconds_per_shot=ms
        ),
        shot_budget=shot_budget,
    )

    text = call_claude(client, STEP1_SYSTEM, [{"role": "user", "content": user_content}], max_tokens=6000)
    result = parse_json_response(text)
    if not _acts_three_structure_ok(result.get("acts")):
        print("  [Step 1] 警告：三幕校验未通过，正在重试一次（须 acts 长度=3 且 act=1,2,3 各至少一场）…")
        retry_tail = (
            "\n\n【纠错重试】你上一次 JSON 违反铁律：必须输出 **恰好 3 个** act 对象，"
            "字段 act 分别为 **1、2、3**，每幕 **scenes 至少 1 场**，"
            "**禁止**只写 ACT3 或把三幕合并成一个 act。"
            "请保留 title/theme/video_concept/total_shots 等创意意图，**重写完整 acts**。"
            f"total_shots 仍须等于 {shot_budget}，各 scene.shot_count 之和须等于 total_shots。"
        )
        text2 = call_claude(
            client,
            STEP1_SYSTEM,
            [{"role": "user", "content": user_content + retry_tail}],
            max_tokens=6000,
        )
        result = parse_json_response(text2)
    if not _acts_three_structure_ok(result.get("acts")):
        raise ValueError(
            "Step1 未产出合法三幕结构（需要 acts 含 act=1,2,3 且每幕至少一场戏）。请重试或更换 MODEL_ID。"
        )

    result["scenes"] = flatten_acts_to_scenes(result)
    result["scene_count"] = len(result["scenes"])
    result.setdefault("video_concept", result.get("title") or result.get("theme") or "")
    result.setdefault(
        "narrative_arc",
        {
            "hook": "ACT1 困境",
            "conflict": "ACT1→ACT2",
            "turning_point": "ACT2 产品介入",
            "resolution": "ACT3 蜕变",
            "cta": "落版",
        },
    )

    n_budget = normalize_blueprint_shot_budget(
        result,
        target_duration_sec=pipeline_input.target_duration_sec,
        min_seconds_per_shot=ms,
    )
    scenes = result.get("scenes") or []
    sum_shots = sum(int(s.get("shot_count") or 0) for s in scenes if isinstance(s, dict))
    print(f"  [Step 1] 完成 → {result.get('video_concept') or result.get('title')}")
    print(f"  [Step 1] 场景数：{len(scenes)}，镜头预算：{n_budget} 镜（Σshot_count={sum_shots}）")
    return result


# ──────────────────────────────────────────
# Step 2：逐场景节拍（强制对白）
# ──────────────────────────────────────────

STEP2_SYSTEM = """你是广告片编剧。只输出JSON，禁止markdown。

【对白铁律】
每个 beat 的 dialogue 字段必须为非空字符串，禁止 null。
dialogue_type 取值：spoken | internal | narration | sfx_only
- spoken：人物台词，用中文引号「」或 ""
- internal：内心独白，格式 （心里）'……'
- narration：画外旁白
- sfx_only：以文字描述具体音效（如：水滴声、门轴吱呀声）

每个场景至少 1 个 beat 为 spoken 或 internal。
若当前场为全片最后一场，则最后一个 beat 必须为 narration，内容为一句品牌 slogan（可虚构品牌语气，须积极）。

【动作铁律】
action 必须写到身体部位 + 方向 + 幅度；禁止只写「转身」「点头」等空泛词。

【景别建议】
suggested_shot_type 枚举：ECU/CU/MCU/MS/MLS/FS/WS/EWS"""

STEP2_USER_TMPL = """## 蓝图摘要
{blueprint_summary}

## 人物 / 产品
人物气质：{character_vibe}
产品：{product_line}

## 当前场景
{scene_info}

## 是否全片最后一场
{final_scene_hint}

## 输出 JSON
{{
  "scene_id": "{scene_id}",
  "scene_desc": "{scene_desc}",
  "beats": [
    {{
      "beat_id": 1,
      "duration_hint_sec": 4,
      "action": "具体动作：部位+方向+幅度+节奏",
      "dialogue_type": "spoken",
      "dialogue": "不得为空",
      "dialogue_emotion": "说或独白时的情绪",
      "emotion": "当下情绪",
      "product_moment": "产品与画面的关系，无产品则写无",
      "suggested_shot_type": "MCU",
      "suggested_movement": "固定",
      "visual_focus": "画面重心"
    }}
  ],
  "scene_sfx": ["环境音1"],
  "scene_bgm_mood": "BGM 情绪"
}}

beats 数量必须等于该场景 shot_count = {shot_count}。"""


def expand_scene(
    client: anthropic.Anthropic,
    blueprint: dict[str, Any],
    scene: dict[str, Any],
    product_desc: dict[str, Any],
    *,
    is_final_scene: bool = False,
) -> dict[str, Any]:
    """Step 2：单场景细化（强制对白）"""
    cv = (
        product_desc.get("character_visual")
        if isinstance(product_desc.get("character_visual"), dict)
        else {}
    )
    pv = product_desc.get("product_visual") if isinstance(product_desc.get("product_visual"), dict) else {}
    blueprint_summary = {
        "title": blueprint.get("title"),
        "theme": blueprint.get("theme"),
        "video_concept": blueprint.get("video_concept"),
        "tone": blueprint.get("tone"),
        "target_emotion": blueprint.get("target_emotion"),
        "hotword_integration": blueprint.get("hotword_integration"),
    }
    final_hint = (
        "是。最后一个 beat 必须为 narration，且为品牌 slogan。"
        if is_final_scene
        else "否。最后一个 beat 不要强行 narration slogan。"
    )
    product_line = " ".join(
        str(x)
        for x in (
            pv.get("product_name"),
            pv.get("pack_shape"),
            pv.get("pack_color_main"),
        )
        if x
    )
    char_vibe = str(cv.get("overall_vibe") or product_desc.get("talent_appearance") or "")

    shot_count = int(scene.get("shot_count") or 1)
    user_content = STEP2_USER_TMPL.format(
        blueprint_summary=json.dumps(blueprint_summary, ensure_ascii=False, indent=2),
        character_vibe=char_vibe,
        product_line=product_line or str(product_desc.get("name") or ""),
        scene_info=json.dumps(scene, ensure_ascii=False, indent=2),
        final_scene_hint=final_hint,
        scene_id=str(scene.get("scene_id") or "S01"),
        scene_desc=str(scene.get("scene_desc", "")),
        shot_count=shot_count,
    )

    text = call_claude(client, STEP2_SYSTEM, [{"role": "user", "content": user_content}], max_tokens=5000)
    return parse_json_response(text)


# ──────────────────────────────────────────
# Step 3：分镜 JSON 生成（连续性 + 人物/产品锁）
# ──────────────────────────────────────────

STEP3_SYSTEM_TMPL = """你是好莱坞广告分镜师。只输出 JSON 数组，禁止 markdown。

【人物外貌锁 — 每镜 image_prompt 必须包含以下英文短语（可微调连接词，不得改变五官/发型语义）】
{character_prompt_en}

【产品外貌锁 — 凡产品入镜的镜头，image_prompt 必须包含以下英文短语】
{packshot_prompt_en}
包装细节：{pack_detail}

【对白】每条分镜的 dialogue 字段为**非空字符串**；与节拍剧本一致，可略压缩。

【连续性铁律】
1. 若「上一镜 closing_state」为 null：第一镜 opening_state 写为人物静止站立、双手自然下垂、机位稳定。
2. 否则：第一镜 opening_state 必须与「上一镜 closing_state」**逐字段一致**（可复制 JSON）。
3. 从第 2 条分镜起：每条 opening_state 必须与前一条 closing_state **完全一致**。
4. 同场景内景别切换优先：远景系 → 中景系 → 近景/特写系；避免无理由跳级。
5. 人物发生明显空间位移时，closing_state 必须写出落点与朝向。

【输出长度】数组元素个数必须恰好等于输入 beats 数量。

shot_size 取值：ECU|CU|MCU|MS|MLS|LS|ELS（LS=全身/远景，ELS=大远景）

【品牌】image_prompt 为英文时，产品称呼须与剧本中的**中文品名**一致（可音译或保留中文罗马字拼写常见形式），**禁止**编造用户未提供的英文商标（如自造 Keebon 等）。"""

STEP3_USER_TMPL = """## 场景节拍（JSON）
{beats_json}

## 场景元信息
scene_id={scene_id}
location={location}
int_ext={int_ext}
time_of_day={time_of_day}
scene_desc={scene_desc}

## 上一镜 closing_state（JSON，无则 null）
{prev_closing_state_json}

## 接续时间码（本场景第一镜的 timecode_start 建议从此处开始）
{prev_end_timecode}

## 帧率
{fps} fps；建议快门约 1/{shutter_denom}

## 输出要求
输出 **仅** 一个 JSON 数组；长度 = {beat_count}。每个元素：

{{
  "shot_id": "S01-01",
  "timecode_start": "MM:SS",
  "timecode_end": "MM:SS",
  "duration_sec": 4,
  "scene_id": "{scene_id}",
  "location": "地点",
  "int_ext": "INT/EXT",
  "time_of_day": "时间段",
  "lens_mm": 50,
  "aperture": "f2.8",
  "shutter": "1/50",
  "iso": 1600,
  "color_temp_k": 5600,
  "opening_state": {{
    "character_position": "",
    "character_pose": "",
    "prop_position": "",
    "camera_position": ""
  }},
  "frame_composition": "含人物位置、景深、前后景",
  "shot_size": "MCU",
  "camera_angle": "平视/俯拍/仰拍/侧方等",
  "camera_movement": "运镜",
  "action": "动作自起至止，完整一句",
  "dialogue_type": "spoken|internal|narration|sfx_only",
  "dialogue": "不得为空",
  "emotion": "",
  "lighting": {{
    "key_light": "",
    "fill_light": "",
    "back_light": "",
    "color_temp": "",
    "ratio": ""
  }},
  "atmosphere": "",
  "closing_state": {{
    "character_position": "",
    "character_pose": "",
    "prop_position": "",
    "camera_position": ""
  }},
  "image_prompt": "英文 ≤70 词；必须含人物锁英文；产品入镜时含产品锁英文",
  "continuity_note": "与上一镜衔接说明"
}}"""


def seconds_to_timecode(seconds: int) -> str:
    """秒数转 MM:SS 格式"""
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def timecode_to_seconds(tc: str) -> int:
    """MM:SS 格式转秒数"""
    parts = tc.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _shot_timeline_weight(shot: dict[str, Any]) -> float:
    """
    用于在总时长锁死的前提下，把秒数「按需」分给各镜。

    优先级：
    1. ``shot["duration_weight"]``（正数，相对权重即可，如 2.0 表示约 2 倍于默认）
    2. 归一化前模型写在 ``timecode.duration_sec`` 里的建议秒数（Step3 常见 3～6）
    3. 兜底 1.0（均分倾向）
    """
    raw_w = shot.get("duration_weight")
    if raw_w is not None:
        try:
            return max(0.25, float(raw_w))
        except (TypeError, ValueError):
            pass
    tc = shot.get("timecode") or {}
    d = tc.get("duration_sec")
    try:
        di = int(d) if d is not None else 0
    except (TypeError, ValueError):
        di = 0
    if di > 0:
        return float(di)
    return 1.0


def _merge_two_canonical_shots(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """合并两条 Step3 产出的 canonical shot；保留左镜为首帧代表，右镜 closing 为段落收束。"""
    out: dict[str, Any] = deepcopy(left)
    lid = str(left.get("shot_id") or "").strip()
    rid = str(right.get("shot_id") or "").strip()
    out["shot_id"] = f"{lid}+{rid}" if lid and rid else (lid or rid or "merged")

    sd_l = str(left.get("scene_desc") or "").strip()
    sd_r = str(right.get("scene_desc") or "").strip()
    if sd_r and sd_r != sd_l:
        out["scene_desc"] = " / ".join(x for x in (sd_l, sd_r) if x)

    pl = left.get("performance") or {}
    pr = right.get("performance") or {}
    la = str(pl.get("action") or "").strip()
    ra = str(pr.get("action") or "").strip()
    out.setdefault("performance", {})
    out["performance"]["action"] = "；接着 ".join(x for x in (la, ra) if x) or ra or la or "."
    le = str(pl.get("emotion") or "").strip()
    re = str(pr.get("emotion") or "").strip()
    out["performance"]["emotion"] = re or le or str(out["performance"].get("emotion") or "")

    laud = left.get("audio") or {}
    raud = right.get("audio") or {}
    ldlg = laud.get("dialogue") if isinstance(laud.get("dialogue"), dict) else {}
    rdlg = raud.get("dialogue") if isinstance(raud.get("dialogue"), dict) else {}
    ltxt = str(ldlg.get("text") or "").strip()
    rtxt = str(rdlg.get("text") or "").strip()
    dlg = " ".join(x for x in (ltxt, rtxt) if x)
    out.setdefault("audio", {})
    out["audio"].setdefault("dialogue", {})
    if dlg:
        out["audio"]["dialogue"]["text"] = dlg[:500]

    cs_l = left.get("continuity_states") if isinstance(left.get("continuity_states"), dict) else {}
    cs_r = right.get("continuity_states") if isinstance(right.get("continuity_states"), dict) else {}
    out.setdefault("continuity_states", {})
    os_open = cs_l.get("opening_state")
    out["continuity_states"]["opening_state"] = os_open if isinstance(os_open, dict) else {}
    cs_close = cs_r.get("closing_state")
    out["continuity_states"]["closing_state"] = cs_close if isinstance(cs_close, dict) else {}
    cn_l = str(cs_l.get("continuity_note") or "").strip()
    cn_r = str(cs_r.get("continuity_note") or "").strip()
    out["continuity_states"]["continuity_note"] = " | ".join(x for x in (cn_l, cn_r) if x)

    mv_l = str((left.get("movement") or {}).get("type") or "固定").strip()
    mv_r = str((right.get("movement") or {}).get("type") or "固定").strip()
    dyn = ("缓推", "跟拍", "横移", "手持", "环绕", "升降", "缓拉")
    nmv = mv_r if mv_r in dyn and mv_l == "固定" else (mv_l if mv_l in dyn else mv_r)
    dl = str((left.get("movement") or {}).get("desc") or "").strip()
    dr = str((right.get("movement") or {}).get("desc") or "").strip()
    out["movement"] = {
        "type": nmv,
        "desc": " ".join(x for x in (dl, dr) if x)[:400],
    }

    dp_l = str(left.get("director_image_prompt") or "").strip()
    dp_r = str(right.get("director_image_prompt") or "").strip()
    out["director_image_prompt"] = " ".join(x for x in (dp_l, dp_r) if x)[:800]

    al: list[Any] = []
    if isinstance(left.get("alerts"), list):
        al.extend(left["alerts"])
    if isinstance(right.get("alerts"), list):
        al.extend(right["alerts"])
    al.append(
        {
            "level": "info",
            "code": "TIMELINE_MERGE",
            "message": f"与镜 {rid} 合并以满足图生视频单段最短时长",
        }
    )
    out["alerts"] = al
    out["duration_weight"] = _shot_timeline_weight(left) + _shot_timeline_weight(right)
    return out


def merge_shots_for_ark_duration_floor(
    shots: list[dict[str, Any]],
    target_duration_sec: int,
    *,
    min_per_shot: int | None = None,
) -> dict[str, Any]:
    """
    当 ``len(shots) * min_per_shot > target`` 时，反复合并**相邻**镜中权重和最小的一对，
    直至可满足「每镜时长可规划为 ≥ min_per_shot 且总和为 target」或仅剩 1 镜。
    """
    if not shots:
        return {"skipped": True, "reason": "no_shots"}
    t = max(1, min(180, int(target_duration_sec)))
    if not _storyboard_respect_ark_shot_duration_floor():
        return {"skipped": True, "reason": "respect_floor_disabled"}
    lo, hi = storyboard_per_shot_duration_bounds()
    min_ps = int(min_per_shot) if min_per_shot is not None else int(lo)
    min_ps = max(int(lo), min(int(min_ps), int(hi)))
    initial = len(shots)
    merges: list[dict[str, Any]] = []
    while len(shots) * min_ps > t and len(shots) > 1:
        best_i = 0
        best_score = float("inf")
        for i in range(len(shots) - 1):
            s = _shot_timeline_weight(shots[i]) + _shot_timeline_weight(shots[i + 1])
            if s < best_score:
                best_score = s
                best_i = i
        a = shots[best_i]
        b = shots[best_i + 1]
        id_a = str(a.get("shot_id") or "")
        id_b = str(b.get("shot_id") or "")
        merged = _merge_two_canonical_shots(a, b)
        merges.append({"from": [id_a, id_b], "into": str(merged.get("shot_id") or "")})
        shots[best_i : best_i + 2] = [merged]
    return {
        "skipped": False,
        "min_per_shot_sec": min_ps,
        "max_per_shot_sec": hi,
        "target_duration_sec": t,
        "initial_shot_count": initial,
        "final_shot_count": len(shots),
        "merges": merges,
    }


def normalize_shot_timeline_to_target(
    shots: list[dict[str, Any]],
    target_duration_sec: int,
    *,
    min_per_shot: int = 2,
    max_per_shot: int = 12,
) -> None:
    """
    将各镜 ``timecode.duration_sec`` 调整为**整数秒**，使全片时长之和等于 ``target_duration_sec``，
    并重写连续的 ``timecode.in`` / ``timecode.out``。

    分配策略（在 ``min_per_shot``～``max_per_shot`` 与总长约束下）：
    - 按 ``_shot_timeline_weight`` **加权**：模型在某镜上写了更长的 ``duration_sec``（或你设了更大的
      ``duration_weight``），该镜会分到更多秒；反之动作短的镜可以更短。
    - 若各镜权重几乎相同，则行为接近「均匀分摊」（仅整数秒下仍可能差 1s）。

    说明：Step2/3 模型原先写的各镜秒数之和常远大于目标片长；本函数在压缩到目标总长时保留**相对比例**。
    """
    if not shots:
        return
    min_ps = max(1, int(min_per_shot))
    max_ps = max(min_ps, int(max_per_shot))
    t = max(1, min(180, int(target_duration_sec)))
    n = len(shots)
    if n == 1 and t < min_ps:
        t = min_ps
    if min_ps * n > t:
        min_ps = max(1, t // n)
    lo = min_ps
    hi = max(max_ps, (t + n - 1) // n)

    weights = [_shot_timeline_weight(s) for s in shots]
    w_sum = sum(weights)
    if w_sum <= 0:
        weights = [1.0] * n
        w_sum = float(n)

    ideals = [t * weights[i] / w_sum for i in range(n)]
    durs = [max(lo, min(hi, int(round(ideals[i])))) for i in range(n)]

    diff = t - sum(durs)
    guard = 0
    while diff != 0 and guard < n * 48:
        guard += 1
        if diff > 0:
            pool = [i for i in range(n) if durs[i] < hi]
            if not pool:
                break
            idx = max(pool, key=lambda i: ideals[i] - durs[i])
            durs[idx] += 1
            diff -= 1
        else:
            pool = [i for i in range(n) if durs[i] > lo]
            if not pool:
                break
            idx = max(pool, key=lambda i: durs[i] - ideals[i])
            durs[idx] -= 1
            diff += 1

    cur = 0
    for shot, dur in zip(shots, durs):
        tc = shot.setdefault("timecode", {})
        dur_i = int(dur)
        tc["duration_sec"] = dur_i
        tc["in"] = seconds_to_timecode(cur)
        cur += dur_i
        tc["out"] = seconds_to_timecode(cur)


_ALLOWED_SHOT_TYPES = frozenset({"ECU", "CU", "MCU", "MS", "MLS", "FS", "WS", "EWS"})


def _map_shot_size_to_shot_type(raw: str) -> str:
    u = (raw or "MCU").strip().upper().replace(" ", "")
    alias = {"LS": "FS", "ELS": "EWS", "FULL": "FS", "WIDE": "WS"}
    u = alias.get(u, u)
    return u if u in _ALLOWED_SHOT_TYPES else "MCU"


def _map_angle_and_eye(camera_angle: str) -> tuple[str, str]:
    s = (camera_angle or "").strip().lower()
    if any(k in s for k in ("俯", "高机位", "俯视", "top", "high angle")):
        return "俯拍", "俯视"
    if any(k in s for k in ("仰", "低机位", "仰视", "low angle", "worm")):
        return "仰拍", "仰视"
    if "过肩" in s or "ots" in s:
        return "过肩", "平视"
    if "背" in s or "rear" in s or "behind" in s:
        return "背面", "平视"
    if "侧后" in s:
        return "侧后方", "平视"
    if "侧" in s or "profile" in s or "side" in s:
        return "侧面", "平视"
    return "正面", "平视"


def _map_movement_cn(movement: str) -> tuple[str, str | None]:
    s = (movement or "").strip().lower()
    if not s:
        return "固定", None
    if any(k in s for k in ("推", "dolly in", "push in")):
        return "缓推", movement
    if any(k in s for k in ("拉", "dolly out", "pull back")):
        return "缓拉", movement
    if any(k in s for k in ("横移", "pan", "truck", "lateral")):
        return "横移", movement
    if any(k in s for k in ("跟", "follow", "tracking")):
        return "跟拍", movement
    if any(k in s for k in ("升", "降", "crane", "jib")):
        return "升降", movement
    if any(k in s for k in ("手持", "handheld")):
        return "手持", movement
    if any(k in s for k in ("环绕", "orbit", "arc")):
        return "环绕", movement
    if "摇" in s:
        return "横移", movement
    return "固定", movement


def _director_lighting_to_canonical(lit: Any, color_temp_k: int, atmosphere: str) -> dict[str, Any]:
    if isinstance(lit, dict) and "key" in lit:
        return lit  # 已是标准结构
    d = lit if isinstance(lit, dict) else {}
    key_s = str(d.get("key_light") or "顺光主光")
    fill_s = str(d.get("fill_light") or "弱补光")
    back_s = str(d.get("back_light") or "")
    ratio = str(d.get("ratio") or "4:1")
    ct = str(d.get("color_temp") or "")
    mood = str(atmosphere or "")
    qual = "柔光"
    if any(k in key_s for k in ("硬", "hard", "轮廓")):
        qual = "硬光"
    temp_cn = "中性"
    if any(k in ct.lower() for k in ("暖", "warm", "3200", "钨")):
        temp_cn = "暖"
    elif any(k in ct.lower() for k in ("冷", "cool", "5600", "日")):
        temp_cn = "冷"
    return {
        "key": {
            "position": "场景主光位",
            "quality": qual,
            "source": "场景光",
            "temp": temp_cn,
        },
        "fill": {"source": fill_s, "intensity": "弱"},
        "rim": {"desc": back_s or "轮廓光可选"},
        "color_temp_k": int(color_temp_k) if color_temp_k else 5600,
        "ratio": ratio,
        "shadows": "",
        "mood": mood,
    }


def _dialogue_type_to_audio(
    dialogue_type: str, dialogue: str, emotion: str
) -> dict[str, Any]:
    dt = (dialogue_type or "spoken").strip().lower()
    text = (dialogue or "").strip() or "."
    emo = (emotion or "").strip() or "平静"
    cref = "char_001" if dt in ("spoken", "internal") else None
    return {
        "dialogue": {"text": text, "emotion": emo, "volume_db": 0, "character_ref": cref},
        "sfx": [],
        "bgm": None,
    }


def _shot_text_blob(shot: dict[str, Any]) -> str:
    perf = shot.get("performance") or {}
    parts = [
        str(perf.get("action") or ""),
        str((shot.get("frame") or {}).get("composition_desc") or ""),
        str(shot.get("director_image_prompt") or ""),
    ]
    if perf.get("product_refs"):
        parts.append("product packshot bottle on screen")
    return " ".join(parts).lower()


def shot_suggests_product_in_frame(shot: dict[str, Any], product_desc: dict[str, Any]) -> bool:
    """用于在 image_prompt 中追加产品英文锁。"""
    pv = product_desc.get("product_visual") if isinstance(product_desc.get("product_visual"), dict) else {}
    name = str(pv.get("product_name") or product_desc.get("name") or "")
    blob = _shot_text_blob(shot)
    keys = [
        "产品",
        "瓶",
        "包装",
        "泵头",
        "盖",
        "商品",
        "洗发水",
        "乳液",
        "面霜",
        "packshot",
        "bottle",
        "product",
        "label",
        "cap",
        "pump",
    ]
    if name:
        keys.append(name.lower())
    return any(k.lower() in blob for k in keys if k)


def _is_canonical_step3_row(row: dict[str, Any]) -> bool:
    return bool(row.get("frame")) and bool(row.get("camera")) and "timecode" in row


def _minimal_canonical_from_beat(
    beat: dict[str, Any],
    *,
    scene_id: str,
    scene_desc: str,
    beat_index: int,
    time_in: str,
    duration_sec: int,
    product_desc: dict[str, Any],
) -> dict[str, Any]:
    dur = max(1, int(duration_sec))
    t0 = timecode_to_seconds(time_in)
    t1 = t0 + dur
    action = str(beat.get("action") or "").strip() or "人物静止"
    emo = str(beat.get("emotion") or "").strip() or "平静"
    dlg = str(beat.get("dialogue") or "").strip() or "."
    d_type = str(beat.get("dialogue_type") or "spoken")
    st = str(beat.get("suggested_shot_type") or "MCU")
    sid = scene_id.zfill(2)
    shot_id = f"S{sid}-{beat_index + 1:02d}"
    product_refs: list[str] = []
    pm = str(beat.get("product_moment") or "")
    if pm and pm not in ("无", "null", "None"):
        product_refs.append("prod_001")
    shot: dict[str, Any] = {
        "shot_id": shot_id,
        "scene": scene_id,
        "scene_desc": scene_desc,
        "timecode": {"in": time_in, "out": seconds_to_timecode(t1), "duration_sec": dur},
        "status": "draft",
        "camera": {
            "lens_mm": 50,
            "aperture": "f/2.8",
            "shutter": "1/50",
            "iso": 1600,
            "color_temp_k": 5600,
        },
        "frame": {
            "shot_type": _map_shot_size_to_shot_type(st),
            "angle": "正面",
            "eye_level": "平视",
            "composition_desc": str(beat.get("visual_focus") or "") or action,
            "image_url": None,
        },
        "movement": {"type": "固定", "desc": None},
        "performance": {
            "action": action,
            "emotion": emo,
            "character_refs": ["char_001"],
            "product_refs": product_refs,
        },
        "lighting": _director_lighting_to_canonical({}, 5600, ""),
        "audio": _dialogue_type_to_audio(d_type, dlg, emo),
        "alerts": [],
        "generated_prompts": {"image_prompt": None, "video_prompt": None},
        "continuity_states": {},
    }
    if shot_suggests_product_in_frame(shot, product_desc):
        shot["performance"]["product_refs"] = list(
            dict.fromkeys((shot["performance"]["product_refs"] or []) + ["prod_001"])
        )
    return shot


def _map_director_row_to_canonical(
    row: dict[str, Any],
    *,
    beat: dict[str, Any],
    scene_id: str,
    scene_desc: str,
    location: str,
    product_desc: dict[str, Any],
    time_in: str,
    duration_sec: int,
) -> dict[str, Any]:
    dur = max(1, int(row.get("duration_sec") or duration_sec))
    t0 = timecode_to_seconds(time_in)
    t1 = t0 + dur
    ts = row.get("timecode_start")
    te = row.get("timecode_end")
    if isinstance(ts, str) and ":" in ts and isinstance(te, str) and ":" in te:
        try:
            t0 = timecode_to_seconds(ts.strip())
            t1 = timecode_to_seconds(te.strip())
            dur = max(1, t1 - t0)
        except (ValueError, IndexError):
            pass

    angle, eye = _map_angle_and_eye(str(row.get("camera_angle") or ""))
    mv_type, mv_desc = _map_movement_cn(str(row.get("camera_movement") or ""))
    shot_type = _map_shot_size_to_shot_type(str(row.get("shot_size") or ""))
    comp = str(row.get("frame_composition") or "").strip()
    action = str(row.get("action") or beat.get("action") or "").strip() or "."
    emo = str(row.get("emotion") or beat.get("emotion") or "").strip() or "平静"
    dlg_type = str(row.get("dialogue_type") or beat.get("dialogue_type") or "spoken")
    dlg = str(row.get("dialogue") or beat.get("dialogue") or "").strip() or "."

    ct_k = row.get("color_temp_k")
    try:
        ct_k_i = int(ct_k) if ct_k is not None else 5600
    except (TypeError, ValueError):
        ct_k_i = 5600
    aperture = str(row.get("aperture") or "f/2.8").strip()
    al = aperture.lower()
    if al.startswith("f/"):
        pass
    elif al.startswith("f") and len(al) > 1 and al[1] in "0123456789.":
        aperture = "f/" + aperture[1:].lstrip("/")
    elif al and not al.startswith("f"):
        aperture = f"f/{aperture.lstrip('/')}"
    shutter = str(row.get("shutter") or "1/50")
    try:
        lens = int(row.get("lens_mm") or 50)
    except (TypeError, ValueError):
        lens = 50
    try:
        iso = int(row.get("iso") or 1600)
    except (TypeError, ValueError):
        iso = 1600

    loc = str(row.get("location") or location or "").strip()
    ie = str(row.get("int_ext") or "").strip()
    tod = str(row.get("time_of_day") or "").strip()
    loc_line = " · ".join(x for x in (loc, ie, tod) if x)
    composition_desc = comp or loc_line or scene_desc

    opening = row.get("opening_state") if isinstance(row.get("opening_state"), dict) else {}
    closing = row.get("closing_state") if isinstance(row.get("closing_state"), dict) else {}
    cont_note = str(row.get("continuity_note") or "").strip()

    perf_refs = ["char_001"]
    product_refs: list[str] = []
    pm = str(beat.get("product_moment") or "")
    if pm and pm not in ("无", "null", "None"):
        product_refs.append("prod_001")
    blob_hint = f"{action} {composition_desc} {str(row.get('image_prompt') or '')}".lower()
    prod_keys = ("瓶", "产品", "包装", "pack", "bottle", "商品", "泵", "盖")
    if any(k in blob_hint for k in prod_keys):
        product_refs.append("prod_001")

    shot_id = str(row.get("shot_id") or "").strip()
    if not shot_id:
        sid = scene_id.zfill(2)
        bid = int(beat.get("beat_id") or 1)
        shot_id = f"S{sid}-{bid:02d}"

    canon: dict[str, Any] = {
        "shot_id": shot_id,
        "scene": scene_id,
        "scene_desc": scene_desc,
        "timecode": {"in": seconds_to_timecode(t0), "out": seconds_to_timecode(t1), "duration_sec": dur},
        "status": "draft",
        "camera": {
            "lens_mm": lens,
            "aperture": aperture,
            "shutter": shutter,
            "iso": iso,
            "color_temp_k": ct_k_i,
        },
        "frame": {
            "shot_type": shot_type,
            "angle": angle,
            "eye_level": eye,
            "composition_desc": composition_desc,
            "image_url": None,
        },
        "movement": {"type": mv_type, "desc": mv_desc},
        "performance": {
            "action": action,
            "emotion": emo,
            "character_refs": perf_refs,
            "product_refs": list(dict.fromkeys(product_refs)),
        },
        "lighting": _director_lighting_to_canonical(
            row.get("lighting"), ct_k_i, str(row.get("atmosphere") or "")
        ),
        "audio": _dialogue_type_to_audio(dlg_type, dlg, emo),
        "alerts": [],
        "generated_prompts": {"image_prompt": None, "video_prompt": None},
        "continuity_states": {
            "opening_state": opening,
            "closing_state": closing,
            "continuity_note": cont_note,
        },
        "director_image_prompt": str(row.get("image_prompt") or "").strip(),
    }
    if shot_suggests_product_in_frame(canon, product_desc):
        canon["performance"]["product_refs"] = list(
            dict.fromkeys((canon["performance"]["product_refs"] or []) + ["prod_001"])
        )
    return canon


def _align_step3_shots_to_beats(
    raw_rows: list[Any],
    beats: list[dict[str, Any]],
    *,
    scene_script: dict[str, Any],
    blueprint_scene: dict[str, Any] | None,
    product_desc: dict[str, Any],
    start_timecode: str,
) -> list[dict[str, Any]]:
    scene_id = str(scene_script.get("scene_id") or "S01")
    scene_desc = str(scene_script.get("scene_desc") or "")
    loc = str((blueprint_scene or {}).get("location") or "")
    ie = str((blueprint_scene or {}).get("int_ext") or "")
    tod = str((blueprint_scene or {}).get("time_of_day") or "")
    location = " · ".join(x for x in (loc, ie, tod) if x)

    rows = [r for r in raw_rows if isinstance(r, dict)]
    if len(rows) != len(beats):
        print(
            f"  [Step 3] 警告：模型返回 {len(rows)} 镜，节拍 {len(beats)}；将截断/补齐"
        )

    out: list[dict[str, Any]] = []
    cur_tc = start_timecode
    for i, beat in enumerate(beats):
        try:
            hint = int(beat.get("duration_hint_sec") or 4)
        except (TypeError, ValueError):
            hint = 4
        hint = max(1, min(12, hint))
        if i < len(rows) and _is_canonical_step3_row(rows[i]):
            canon = rows[i]
            tc = canon.get("timecode") or {}
            cur_tc = str(tc.get("in") or cur_tc)
            out.append(canon)
            cur_tc = str(tc.get("out") or cur_tc)
            continue
        if i < len(rows) and isinstance(rows[i], dict):
            row = rows[i]
            canon = _map_director_row_to_canonical(
                row,
                beat=beat,
                scene_id=scene_id,
                scene_desc=scene_desc,
                location=location,
                product_desc=product_desc,
                time_in=cur_tc,
                duration_sec=hint,
            )
            out.append(canon)
            cur_tc = canon["timecode"]["out"]
        else:
            canon = _minimal_canonical_from_beat(
                beat,
                scene_id=scene_id,
                scene_desc=scene_desc,
                beat_index=i,
                time_in=cur_tc,
                duration_sec=hint,
                product_desc=product_desc,
            )
            out.append(canon)
            cur_tc = canon["timecode"]["out"]
    return out


def generate_shots(
    client: anthropic.Anthropic,
    scene_script: dict[str, Any],
    product_desc: dict[str, Any],
    fps: int,
    start_timecode: str = "00:00",
    *,
    prev_closing_state: dict[str, Any] | None = None,
    blueprint_scene: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Step 3：场景节拍 → 标准分镜 JSON（含连续性状态，映射为下游 Wan/Seedream 结构）。"""
    scene_id = str(scene_script["scene_id"])
    beats = scene_script.get("beats") or []
    if not isinstance(beats, list):
        beats = []
    beats = [b for b in beats if isinstance(b, dict)]
    beat_count = len(beats)
    if beat_count == 0:
        return []

    cv = product_desc.get("character_visual") if isinstance(product_desc.get("character_visual"), dict) else {}
    pv = product_desc.get("product_visual") if isinstance(product_desc.get("product_visual"), dict) else {}
    char_lock = str(cv.get("character_prompt_en") or product_desc.get("character_prompt_en") or "").strip()
    if not char_lock:
        char_lock = (
            "same protagonist, consistent facial features and hairstyle, photorealistic cinematic still"
        )
    pack_lock = str(pv.get("packshot_prompt_en") or "").strip()
    pack_detail = "，".join(
        str(x)
        for x in (
            pv.get("pack_shape"),
            pv.get("pack_color_main"),
            pv.get("cap_type"),
            pv.get("surface_texture"),
        )
        if x
    )

    loc = str((blueprint_scene or {}).get("location") or "")
    ie = str((blueprint_scene or {}).get("int_ext") or "")
    tod = str((blueprint_scene or {}).get("time_of_day") or "")

    beats_payload = [dict(b) for b in beats]
    prev_json = "null" if prev_closing_state is None else json.dumps(prev_closing_state, ensure_ascii=False)
    shutter_denom = max(2, int(fps) * 2)

    system = STEP3_SYSTEM_TMPL.format(
        character_prompt_en=char_lock,
        packshot_prompt_en=pack_lock or "（无单独产品锁，按节拍中产品描述）",
        pack_detail=pack_detail or "（参考产品 JSON）",
    )
    user_content = STEP3_USER_TMPL.format(
        beats_json=json.dumps(beats_payload, ensure_ascii=False, indent=2),
        scene_id=scene_id,
        location=loc or "（见节拍）",
        int_ext=ie or "INT",
        time_of_day=tod or "（见节拍）",
        scene_desc=str(scene_script.get("scene_desc", "")),
        prev_closing_state_json=prev_json,
        prev_end_timecode=start_timecode,
        fps=int(fps),
        shutter_denom=shutter_denom,
        beat_count=beat_count,
    )

    text = call_claude(
        client,
        system,
        [{"role": "user", "content": user_content}],
        max_tokens=8000,
    )
    parsed = parse_json_response(text)
    raw_rows = parsed if isinstance(parsed, list) else [parsed]

    shots = _align_step3_shots_to_beats(
        raw_rows,
        beats,
        scene_script=scene_script,
        blueprint_scene=blueprint_scene,
        product_desc=product_desc,
        start_timecode=start_timecode,
    )
    return shots


# ──────────────────────────────────────────
# 主流水线
# ──────────────────────────────────────────


def run_pipeline_with_events(
    pipeline_input: PipelineInput,
    api_key: Optional[str] = None,
    on_event: Optional[Any] = None,
) -> PipelineResult:
    """
    与 ``run_pipeline`` 相同产物，但在各阶段调用 ``on_event(dict)`` 便于 HTTP/SSE 展示进度。
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("请设置 ANTHROPIC_API_KEY 或传入 api_key")

    def emit(payload: dict[str, Any]) -> None:
        if on_event:
            on_event(payload)

    client = make_anthropic_client(api_key=key)

    emit({"type": "step", "id": "analyze_product", "label": "分析产品图（Step 0）"})
    product_desc = analyze_product(client, pipeline_input.image_paths, pipeline_input)
    emit(
        {
            "type": "step_done",
            "id": "analyze_product",
            "detail": product_desc.get("name") or "",
            "packshot_image_index": product_desc.get("packshot_image_index"),
            "talent_reference_image_index": product_desc.get("talent_reference_image_index"),
        }
    )

    emit({"type": "step", "id": "expand_screenplay", "label": "剧本扩写（Step 1）"})
    blueprint = expand_screenplay(client, pipeline_input, product_desc)
    emit(
        {
            "type": "step_done",
            "id": "expand_screenplay",
            "detail": str(blueprint.get("video_concept") or "")[:120],
        }
    )

    scene_scripts: list[dict[str, Any]] = []
    all_shots: list[dict[str, Any]] = []
    prev_end_time = "00:00"
    prev_closing_state: dict[str, Any] | None = None

    scenes = blueprint.get("scenes") or []
    for idx, scene in enumerate(scenes):
        sid = str(scene.get("scene_id") or idx + 1)
        emit(
            {
                "type": "scene",
                "index": idx + 1,
                "total": len(scenes),
                "scene_id": sid,
                "label": str(scene.get("scene_desc") or "")[:80],
            }
        )

        emit({"type": "step", "id": "expand_scene", "scene_id": sid, "label": f"细化场景 {sid}（Step 2）"})
        scene_script = expand_scene(
            client,
            blueprint,
            scene,
            product_desc,
            is_final_scene=(idx == len(scenes) - 1),
        )
        scene_scripts.append(scene_script)
        beats = scene_script.get("beats", [])
        emit(
            {
                "type": "step_done",
                "id": "expand_scene",
                "scene_id": sid,
                "detail": f"{len(beats)} 个节拍",
            }
        )

        emit(
            {
                "type": "step",
                "id": "generate_shots",
                "scene_id": sid,
                "label": f"生成分镜 JSON · 场景 {sid}（Step 3）",
            }
        )
        shots = generate_shots(
            client,
            scene_script,
            product_desc,
            pipeline_input.fps,
            prev_end_time,
            prev_closing_state=prev_closing_state,
            blueprint_scene=scene if isinstance(scene, dict) else None,
        )
        all_shots.extend(shots)
        if shots:
            prev_end_time = shots[-1]["timecode"]["out"]
            cs = shots[-1].get("continuity_states")
            if isinstance(cs, dict):
                cl = cs.get("closing_state")
                prev_closing_state = cl if isinstance(cl, dict) else prev_closing_state
        emit(
            {
                "type": "step_done",
                "id": "generate_shots",
                "scene_id": sid,
                "detail": f"{len(shots)} 镜，接续至 {prev_end_time}",
            }
        )

    ms = storyboard_timeline_min_shot_sec()
    _mx = storyboard_per_shot_duration_bounds()[1]
    merge_info = merge_shots_for_ark_duration_floor(
        all_shots,
        pipeline_input.target_duration_sec,
        min_per_shot=ms,
    )
    if merge_info.get("merges"):
        emit(
            {
                "type": "step_done",
                "id": "timeline_shot_merge",
                "label": "已合并相邻镜头以满足图生视频单段最短时长",
                "detail": json.dumps(merge_info["merges"], ensure_ascii=False)[:2000],
                "shot_count_before": merge_info.get("initial_shot_count"),
                "shot_count_after": merge_info.get("final_shot_count"),
                "min_per_shot_sec": merge_info.get("min_per_shot_sec"),
            }
        )

    normalize_shot_timeline_to_target(
        all_shots,
        pipeline_input.target_duration_sec,
        min_per_shot=ms,
        max_per_shot=_mx,
    )

    emit({"type": "pipeline_done", "shot_count": len(all_shots), "scene_count": len(scene_scripts)})
    cv_out = product_desc.get("character_visual")
    return PipelineResult(
        product_desc=product_desc,
        blueprint=blueprint,
        scene_scripts=scene_scripts,
        shots=all_shots,
        character_visual=dict(cv_out) if isinstance(cv_out, dict) else {},
        timeline_merge_info=dict(merge_info) if isinstance(merge_info, dict) else {},
    )


def run_pipeline(
    pipeline_input: PipelineInput,
    api_key: Optional[str] = None,
) -> PipelineResult:
    """
    完整流水线入口

    Args:
        pipeline_input: 包含一句话、热点词、产品图路径
        api_key: Anthropic API Key，不传则从环境变量 ANTHROPIC_API_KEY 读取

    Returns:
        PipelineResult，包含所有中间产物和最终分镜JSON
    """
    print("\n=== 视频分镜脚本生成流水线启动 ===")
    print(f"  描述：{pipeline_input.description}")
    print(f"  热点词：{pipeline_input.hotword}")
    print(f"  产品图：{pipeline_input.image_paths}")
    result = run_pipeline_with_events(pipeline_input, api_key=api_key, on_event=None)
    print("\n=== 流水线完成 ===")
    print(f"  总镜头数：{len(result.shots)}")
    return result


# ──────────────────────────────────────────
# 工具：生成 image / video prompt
# ──────────────────────────────────────────


def build_image_prompt(
    shot: dict[str, Any],
    product_desc: dict[str, Any],
    char_descs: Optional[dict[str, str]] = None,
) -> str:
    """
    从分镜JSON自动合成图生成 prompt
    char_descs: {"char_001": "描述文字", ...}
    """
    char_descs = char_descs or {}
    hint = str(shot.get("director_image_prompt") or "").strip()

    parts: list[str] = [
        "vertical 9:16 aspect ratio, short-form video framing, portrait orientation",
    ]

    frame = shot.get("frame") or {}
    parts.append(str(frame.get("composition_desc", "") or ""))
    parts.append(
        f"{frame.get('shot_type')} shot, {frame.get('angle')}, {frame.get('eye_level')} angle"
    )

    cam = shot.get("camera") or {}
    ap = str(cam.get("aperture", "f/2.8"))
    ap_num = ap.replace("f/", "").replace("f", "", 1).strip()
    parts.append(f"{cam.get('lens_mm')}mm lens, f/{ap_num}")

    light = shot.get("lighting") or {}
    key = light.get("key") or {}
    parts.append(
        f"key light: {key.get('position')} {key.get('quality')} {key.get('temp')} temperature, "
        f"lighting ratio {light.get('ratio')}, mood: {light.get('mood')}"
    )
    parts.append(f"shadow: {light.get('shadows', '')}")

    perf = shot.get("performance") or {}
    parts.append(f"character action: {perf.get('action')}, emotion: {perf.get('emotion')}")

    for char_id in perf.get("character_refs") or []:
        if char_id in char_descs:
            parts.append(f"character: {char_descs[char_id]}")

    if perf.get("product_refs") and product_desc:
        parts.append(f"product: {product_desc.get('appearance')}")

    parts.append("cinematic still frame, film photography, high detail")

    out = ", ".join(p for p in parts if p)
    if hint:
        out = f"{hint}, {out}" if out else hint

    cv = product_desc.get("character_visual") if isinstance(product_desc.get("character_visual"), dict) else {}
    pv = product_desc.get("product_visual") if isinstance(product_desc.get("product_visual"), dict) else {}
    char_token = str(cv.get("character_prompt_en") or product_desc.get("character_prompt_en") or "").strip()
    prod_token = str(pv.get("packshot_prompt_en") or "").strip()
    inject: list[str] = []
    if char_token:
        inject.append(char_token)
    if prod_token and shot_suggests_product_in_frame(shot, product_desc):
        inject.append(prod_token)
    if inject:
        tail = ", ".join(inject)
        out = f"{out}, {tail}" if out else tail

    lock = _image_prompt_visual_lock(product_desc)
    if lock:
        out = f"{out}, {lock}" if out else lock
    return out


def _image_prompt_visual_lock(product_desc: dict[str, Any]) -> str:
    """文生图侧强制与上传参考一致（与万相多参考顺序对齐：先人物后产品）。"""
    tal_ix = product_desc.get("talent_reference_image_index")
    if tal_ix is not None:
        ta = product_desc.get("talent_appearance")
        s = (
            "【一致性】人物面部、发型、须型、体型与衣着须与参考图①（人物上传图）高度一致，不得替换为其他模特"
        )
        if ta:
            s += f"；特征摘要：{ta}"
        s += (
            "。【一致性】画面中的商品包装须与参考图②（产品主图）瓶型、标签排版与主色完全一致，不得换成其他品牌或臆造包装"
        )
        return s
    return (
        "【一致性】画面中的产品须与参考图（产品主图）的瓶型、标签与配色完全一致，不得替换为其他商品"
    )


def build_video_prompt(shot: dict[str, Any]) -> str:
    """从分镜JSON自动合成视频生成 prompt"""
    movement_map = {
        "固定": "static camera, no movement",
        "缓推": "slow dolly push in toward subject",
        "缓拉": "slow dolly pull back from subject",
        "横移": "smooth lateral tracking shot",
        "跟拍": "follow cam tracking the subject",
        "升降": "vertical camera movement, crane shot",
        "手持": "handheld camera, slight natural organic movement",
        "环绕": "slow orbital movement around subject",
    }

    perf = shot.get("performance") or {}
    frame = shot.get("frame") or {}
    light = shot.get("lighting") or {}
    tc = shot.get("timecode") or {}
    movement_type = (shot.get("movement") or {}).get("type", "固定")
    mv_desc = (shot.get("movement") or {}).get("desc")
    mv_extra = str(mv_desc).strip() if mv_desc else ""

    parts = [
        "vertical 9:16 output, portrait video, mobile short-form framing",
        frame.get("composition_desc", ""),
        movement_map.get(movement_type, "static camera"),
        mv_extra,
        f"character: {perf.get('action')}, emotion: {perf.get('emotion')}",
        f"lighting mood: {light.get('mood')}",
        f"duration: {tc.get('duration_sec', 4)} seconds",
        "cinematic, smooth motion, film quality",
        (
            "ambient audio matching the scene (environment, subtle foley, dialogue if any); "
            "avoid a completely silent video"
        ),
    ]

    dialogue = (shot.get("audio") or {}).get("dialogue")
    if dialogue and dialogue.get("text"):
        parts.append(f"character speaks with {dialogue.get('emotion')} emotion")

    return ". ".join(p for p in parts if p)


def fill_generated_prompts(
    shots: list[dict[str, Any]],
    product_desc: dict[str, Any],
    char_descs: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """
    批量填充所有镜头的 generated_prompts。
    独立步骤：人工改完分镜 JSON 后可仅调用本函数刷新 prompt，无需重跑 LLM。
    """
    for shot in shots:
        gp = shot.setdefault("generated_prompts", {})
        img = build_image_prompt(shot, product_desc, char_descs)
        gp["image_prompt"] = img
        vp = build_video_prompt(shot)
        gp["video_prompt"] = f"{vp}. Keyframe look: {img}"
    return shots


# ──────────────────────────────────────────
# 导演表导出（时间轴 / 镜号 / 相机 / 画面占位 / 光影 / 声画）
# ──────────────────────────────────────────

_SHOT_TYPE_CN: dict[str, str] = {
    "ECU": "极特写",
    "CU": "特写",
    "MCU": "中近景",
    "MS": "中景",
    "MLS": "中远景",
    "FS": "全景",
    "WS": "远景",
    "EWS": "大远景",
}


def _shot_type_cn(code: str) -> str:
    c = (code or "").strip().upper()
    return _SHOT_TYPE_CN.get(c, code or "—")


def _shot_timeline_camera_plain(shot: dict[str, Any]) -> str:
    tc = shot.get("timecode") or {}
    t_in = tc.get("in", "")
    t_out = tc.get("out", "")
    cam = shot.get("camera") or {}
    ap = str(cam.get("aperture", ""))
    lines = [
        f"{t_in}-{t_out}",
        f"镜号：{shot.get('shot_id', '')}",
        f"{cam.get('lens_mm', '')}mm",
        ap.replace("f/", "f") if ap else "",
        str(cam.get("shutter", "")),
        f"ISO{cam.get('iso', '')}",
        f"{cam.get('color_temp_k', '')}k",
    ]
    return "\n".join(x for x in lines if x)


def _shot_picture_plain(shot: dict[str, Any]) -> str:
    """
    画面列：尚未出图时用完整 image_prompt 占位；出图后 JSON 里写入 frame.image_url 则本列改为路径。
    """
    frame = shot.get("frame") or {}
    url = frame.get("image_url")
    gp = shot.get("generated_prompts") or {}
    ip = str(gp.get("image_prompt") or "").strip()
    comp = str(frame.get("composition_desc") or "").strip()

    if url:
        u = str(url).strip()
        if comp:
            return f"分镜图：{u}\n构图摘要：{comp}"
        return f"分镜图：{u}"

    block = "（分镜图待生成：下列为出图提示词，生成后将图片路径写入 JSON 的 frame.image_url 并重新导出脚本）"
    if ip:
        return f"{block}\n{ip}"
    if comp:
        return f"{block}\n构图：{comp}"
    return block


def _shot_angle_plain(shot: dict[str, Any]) -> str:
    frame = shot.get("frame") or {}
    st = _shot_type_cn(str(frame.get("shot_type") or ""))
    ang = str(frame.get("angle") or "")
    eye = str(frame.get("eye_level") or "")
    return "/".join(x for x in (st, ang, eye) if x)


def _shot_movement_plain(shot: dict[str, Any]) -> str:
    mv = shot.get("movement") or {}
    t = str(mv.get("type") or "")
    d = str(mv.get("desc") or "")
    if t and d:
        return f"{t}\n{d}"
    return t or d or "—"


def _shot_performance_plain(shot: dict[str, Any]) -> str:
    perf = shot.get("performance")
    if isinstance(perf, dict):
        a = str(perf.get("action") or "")
        e = str(perf.get("emotion") or "")
        if a and e:
            return f"{a}\n情绪：{e}"
        return a or e or "—"
    return str(perf or "—")


def _shot_lighting_plain(shot: dict[str, Any]) -> str:
    L = shot.get("lighting") or {}
    key = L.get("key") or {}
    fill = L.get("fill") or {}
    rim = L.get("rim") or {}
    lines = [
        f"主光：{key.get('position', '')}{key.get('quality', '')}{key.get('source', '')}{key.get('temp', '')}",
        f"辅光：{fill.get('source', '')}+{fill.get('intensity', '')}",
        f"逆光：{rim.get('desc', '')}",
        f"色温：{L.get('color_temp_k', '')}k",
        f"光比：{L.get('ratio', '')}",
        f"阴影/反光：{L.get('shadows', '')}",
        f"氛围：{L.get('mood', '')}",
    ]
    return "\n".join(lines)


def _shot_audio_plain(shot: dict[str, Any]) -> str:
    audio = shot.get("audio") or {}
    chunks: list[str] = []
    for s in audio.get("sfx") or []:
        if isinstance(s, dict):
            desc = str(s.get("desc") or s.get("name") or "")
            if desc:
                chunks.append(desc)
    sfx_line = "，".join(chunks) if chunks else ""

    d = audio.get("dialogue")
    dial_line = ""
    if isinstance(d, dict) and d.get("text"):
        cref = str(d.get("character_ref") or "").strip()
        em = str(d.get("emotion") or "").strip()
        who = f"{cref} " if cref else ""
        dial_line = f"{who}说：{d['text']}"
        if em:
            dial_line += f"（{em}）"

    bgm = audio.get("bgm")
    bgm_s = f"BGM：{bgm}" if bgm else ""

    parts = [p for p in (sfx_line, dial_line, bgm_s) if p]
    return "；".join(parts) if parts else "—"


def build_complete_storyboard_script_rows(
    shots: list[dict[str, Any]],
) -> tuple[list[str], list[list[str]]]:
    """表头 + 每镜一行，单元格为纯文本（可含换行）。"""
    headers = [
        "时间轴/镜号/相机参数",
        "画面",
        "景别/机位",
        "运镜",
        "表演/动作",
        "光影参数",
        "对白/音效",
    ]
    rows: list[list[str]] = []
    for shot in shots:
        rows.append(
            [
                _shot_timeline_camera_plain(shot),
                _shot_picture_plain(shot),
                _shot_angle_plain(shot),
                _shot_movement_plain(shot),
                _shot_performance_plain(shot),
                _shot_lighting_plain(shot),
                _shot_audio_plain(shot),
            ]
        )
    return headers, rows


def export_shots_complete_storyboard_script_tsv(
    shots: list[dict[str, Any]],
    out_path: str | Path,
    *,
    utf8_bom: bool = True,
) -> Path:
    """
    导出完整分镜脚本为 TSV（制表符分隔，含换行的字段自动加引号，便于 Excel / 飞书表格）。
    画面列：无分镜图时为完整出图提示词；有 frame.image_url 时为图片路径。
    """
    headers, rows = build_complete_storyboard_script_rows(shots)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    enc = "utf-8-sig" if utf8_bom else "utf-8"
    with path.open("w", encoding=enc, newline="") as f:
        w = csv.writer(f, dialect="excel-tab")
        w.writerow(headers)
        w.writerows(rows)
    return path


def render_complete_storyboard_script_tsv_string(
    shots: list[dict[str, Any]],
) -> str:
    """内存中生成 TSV 字符串（无 BOM），便于测试或 HTTP 返回。"""
    headers, rows = build_complete_storyboard_script_rows(shots)
    buf = io.StringIO()
    w = csv.writer(buf, dialect="excel-tab", lineterminator="\n")
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


def _md_fenced_block(text: str) -> str:
    """多行正文放入 fenced code block，避免破坏 Markdown 结构。"""
    t = text.strip()
    if not t:
        return "*（空）*\n\n"
    safe = t.replace("```", "``\u200b`")
    return f"```text\n{safe}\n```\n\n"


def export_shots_complete_storyboard_script_markdown(
    shots: list[dict[str, Any]],
    out_path: str | Path,
    *,
    title: str = "分镜脚本",
) -> Path:
    """
    按镜头分块的 Markdown：每镜含时间码/相机、画面、景别、运镜、表演、光影、声画，便于阅读与版本管理。
    """
    lines: list[str] = []
    lines.append(f"# {title}\n\n")
    lines.append(f"共 **{len(shots)}** 个镜头。\n\n")
    lines.append("---\n\n")

    for i, shot in enumerate(shots, 1):
        sid = str(shot.get("shot_id") or f"shot_{i}")
        tc = shot.get("timecode") or {}
        t_in = tc.get("in", "")
        t_out = tc.get("out", "")
        dur = tc.get("duration_sec", "")
        scene = str(shot.get("scene", ""))
        sdesc = str(shot.get("scene_desc", ""))

        lines.append(f"## 第 {i} 镜 · `{sid}`\n\n")
        meta = f"**时间码** {t_in} – {t_out}"
        if dur != "":
            meta += f"　**时长** {dur}s"
        if scene:
            meta += f"　**场景编号** {scene}"
        lines.append(meta + "\n\n")
        if sdesc:
            lines.append(f"*场景：{sdesc}*\n\n")

        sections: list[tuple[str, str]] = [
            ("时间轴 / 镜号 / 相机参数", _shot_timeline_camera_plain(shot)),
            ("画面", _shot_picture_plain(shot)),
            ("景别 / 机位", _shot_angle_plain(shot)),
            ("运镜", _shot_movement_plain(shot)),
            ("表演 / 动作", _shot_performance_plain(shot)),
            ("光影参数", _shot_lighting_plain(shot)),
            ("对白 / 音效", _shot_audio_plain(shot)),
        ]
        for heading, body in sections:
            lines.append(f"### {heading}\n\n")
            lines.append(_md_fenced_block(body))

        lines.append("---\n\n")

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def export_storyboard_json_to_complete_script_markdown(
    json_path: str | Path,
    out_path: str | Path,
    *,
    title: str = "分镜脚本",
) -> Path:
    """从 storyboard_output.json 导出 Markdown 分镜脚本。"""
    p = Path(json_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    shots = data.get("shots")
    if not isinstance(shots, list):
        raise ValueError("JSON 中缺少 shots 数组")
    return export_shots_complete_storyboard_script_markdown(
        shots, out_path, title=title
    )


def export_shots_storyboard_script_to_path(
    shots: list[dict[str, Any]],
    out_path: str | Path,
    *,
    md_title: str = "分镜脚本",
) -> Path:
    """
    按扩展名选择格式：.tsv → 制表符表；否则 → Markdown（无扩展名时补 .md）。
    """
    p = Path(out_path)
    if p.suffix.lower() == ".tsv":
        return export_shots_complete_storyboard_script_tsv(shots, p)
    out = p if p.suffix else p.with_suffix(".md")
    return export_shots_complete_storyboard_script_markdown(
        shots, out, title=md_title
    )


def export_storyboard_json_to_complete_script_tsv(
    json_path: str | Path,
    out_path: str | Path,
    *,
    utf8_bom: bool = True,
) -> Path:
    """从 storyboard_output.json 导出完整分镜脚本 TSV。"""
    p = Path(json_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    shots = data.get("shots")
    if not isinstance(shots, list):
        raise ValueError("JSON 中缺少 shots 数组")
    return export_shots_complete_storyboard_script_tsv(
        shots, out_path, utf8_bom=utf8_bom
    )


def default_script_tsv_path(json_path: str | Path) -> Path:
    """与 JSON 同目录：xxx.json → xxx_script.tsv（表格用，非默认）"""
    p = Path(json_path)
    return p.with_name(f"{p.stem}_script.tsv")


def default_script_md_path(json_path: str | Path) -> Path:
    """与 JSON 同目录：xxx.json → xxx_script.md（默认人类可读分镜脚本）"""
    p = Path(json_path)
    return p.with_name(f"{p.stem}_script.md")


def default_prompt_preview_tsv_path(json_path: str | Path) -> Path:
    """xxx.json → xxx_script_prompts.tsv（备用）"""
    p = Path(json_path)
    return p.with_name(f"{p.stem}_script_prompts.tsv")


def default_prompt_preview_md_path(json_path: str | Path) -> Path:
    """生图前的分镜脚本预览：xxx.json → xxx_script_prompts.md"""
    p = Path(json_path)
    return p.with_name(f"{p.stem}_script_prompts.md")


def talent_reference_index_0based(
    product_desc: dict[str, Any],
    n_paths: int,
) -> int | None:
    """Step0 人物参考图序号（0-based）；无则 None。"""
    raw = product_desc.get("talent_reference_image_index")
    if raw is None:
        return None
    try:
        ix1 = int(raw)
    except (TypeError, ValueError):
        return None
    idx0 = ix1 - 1
    if idx0 < 0 or idx0 >= n_paths:
        return None
    return idx0


def wan_reference_index_0based(
    product_desc: dict[str, Any],
    n_paths: int,
    explicit_index: int | None,
) -> int:
    """
    万相「产品」参考图序号（0-based）。人物图另见 talent_reference_index_0based。
    优先 explicit_index；否则用 Step0 的 packshot_image_index（1-based）。
    """
    if n_paths <= 0:
        raise ValueError("image_paths 为空")
    if explicit_index is not None:
        if explicit_index < 0 or explicit_index >= n_paths:
            raise IndexError(
                f"product_ref_index={explicit_index} 超出范围 0..{n_paths - 1}"
            )
        return explicit_index
    try:
        ix1 = int(product_desc.get("packshot_image_index", 1))
    except (TypeError, ValueError):
        ix1 = 1
    return max(0, min(n_paths - 1, ix1 - 1))


def _validate_frames_subdir(name: str) -> str:
    s = name.strip().replace("\\", "/").strip("/")
    if not s or ".." in s or s.startswith("/"):
        raise ValueError("frames_subdir 无效：勿使用 .. 或绝对路径")
    return s


def _default_storyboard_wan_size() -> str:
    """竖屏 9:16 分镜图；可用环境变量 ``STORYBOARD_FRAME_SIZE`` 覆盖。"""
    v = os.getenv("STORYBOARD_FRAME_SIZE", "").strip()
    return v if v else "1080*1920"


def run_storyboard_one_stop(
    pipeline_input: PipelineInput,
    json_out: str | Path,
    *,
    char_descs: Optional[dict[str, str]] = None,
    fill_prompts: bool = True,
    generate_shot_images: bool = True,
    wan_model: str = "wan2.7-image-pro",
    wan_size: str | None = None,
    wan_watermark: bool = False,
    product_ref_index: Optional[int] = None,
    frames_subdir: str = "storyboard_frames",
    write_prompt_preview_tsv: bool = True,
    script_output_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    一句话 + 产品图 → 分镜 JSON + 导演表 Markdown（*_script.md）。
    generate_shot_images=True 时：先可选写出 *_script_prompts.md，再调万相逐镜出图、
    下载到 frames_subdir，回填 frame.image_url，最后写出 JSON 与 *_script.md。
    """
    result = run_pipeline(pipeline_input)
    shots = result.shots
    if fill_prompts:
        fill_generated_prompts(shots, result.product_desc, char_descs)

    json_path = Path(json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    frames_rel = _validate_frames_subdir(frames_subdir)

    prompt_preview_path: Optional[Path] = None
    if generate_shot_images and write_prompt_preview_tsv:
        prompt_preview_path = default_prompt_preview_md_path(json_path)
        export_shots_complete_storyboard_script_markdown(
            shots, prompt_preview_path, title="分镜脚本（生图前 · 画面为提示词）"
        )

    if generate_shot_images:
        from wan_image_client import fill_storyboard_shots_with_wan

        n_ip = len(pipeline_input.image_paths)
        widx = wan_reference_index_0based(result.product_desc, n_ip, product_ref_index)
        tidx = talent_reference_index_0based(result.product_desc, n_ip)
        prod_path = pipeline_input.image_paths[widx]
        char_path = (
            pipeline_input.image_paths[tidx]
            if tidx is not None and tidx != widx
            else None
        )
        print(
            f"  [WAN] 产品参考=第 {widx + 1}/{n_ip} 张"
            + (f"；人物参考=第 {tidx + 1} 张" if char_path else "")
        )
        frames_abs = json_path.parent.joinpath(*frames_rel.split("/"))
        sz = (wan_size or "").strip() or _default_storyboard_wan_size()
        fill_storyboard_shots_with_wan(
            shots,
            prod_path,
            frames_abs,
            frames_rel,
            character_reference_image_path=char_path,
            model=wan_model,
            size=sz,
            watermark=wan_watermark,
        )

    payload: dict[str, Any] = {
        "product_desc": result.product_desc,
        "blueprint": result.blueprint,
        "scene_scripts": result.scene_scripts,
        "shots": shots,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    script_path = Path(script_output_path) if script_output_path else default_script_md_path(json_path)
    script_path = export_shots_storyboard_script_to_path(shots, script_path)
    return payload


def _shot_timeline_camera_block(shot: dict[str, Any]) -> str:
    tc = shot.get("timecode") or {}
    t_in = tc.get("in", "")
    t_out = tc.get("out", "")
    cam = shot.get("camera") or {}
    ap = str(cam.get("aperture", ""))
    lines = [
        f"{t_in}-{t_out}",
        f"镜号：{shot.get('shot_id', '')}",
        f"{cam.get('lens_mm', '')}mm",
        ap.replace("f/", "f") if ap else "",
        str(cam.get("shutter", "")),
        f"ISO{cam.get('iso', '')}",
        f"{cam.get('color_temp_k', '')}k",
    ]
    return "<br>".join(html.escape(x) for x in lines if x)


def _shot_frame_column(shot: dict[str, Any]) -> str:
    """画面列：无图时展示完整出图提示词；有 image_url 时展示预览图与路径。"""
    frame = shot.get("frame") or {}
    url = frame.get("image_url")
    comp = str(frame.get("composition_desc") or "")
    gp = shot.get("generated_prompts") or {}
    ip = str(gp.get("image_prompt") or "")
    if url:
        u = html.escape(str(url))
        c = html.escape(comp) if comp else ""
        block = (
            f'<img src="{u}" alt="分镜" style="max-width:320px"/><br>'
            f"<b>分镜图</b>：{u}"
        )
        if c:
            block += f"<br><b>构图</b>：{c}"
        return block
    tip = html.escape(
        "（分镜图待生成：下列为完整出图提示词；出图后将路径写入 JSON 的 frame.image_url 并重新导出脚本）"
    )
    comp_h = html.escape(comp) if comp else ""
    comp_block = f"<br><b>构图</b>：{comp_h}" if comp_h else ""
    if ip:
        pre = (
            f"<pre style='white-space:pre-wrap;word-break:break-word;max-width:560px;"
            f"font-size:12px;background:#f6f6f6;padding:8px'>{html.escape(ip)}</pre>"
        )
    else:
        pre = html.escape("—")
    return f"{tip}{comp_block}<br><b>出图提示词</b>：{pre}"


def _shot_angle_cell(shot: dict[str, Any]) -> str:
    frame = shot.get("frame") or {}
    parts = [
        _shot_type_cn(str(frame.get("shot_type") or "")),
        str(frame.get("angle") or ""),
        str(frame.get("eye_level") or ""),
    ]
    return html.escape("/".join(p for p in parts if p))


def _shot_movement_cell(shot: dict[str, Any]) -> str:
    mv = shot.get("movement") or {}
    t = str(mv.get("type") or "")
    d = str(mv.get("desc") or "")
    if d:
        return html.escape(f"{t}（{d}）")
    return html.escape(t or "—")


def _shot_performance_cell(shot: dict[str, Any]) -> str:
    perf = shot.get("performance") or ""
    if isinstance(perf, dict):
        a = str(perf.get("action") or "")
        e = str(perf.get("emotion") or "")
        body = f"{a}｜情绪：{e}" if e else a
    else:
        body = str(perf)
    return html.escape(body or "—")


def _shot_lighting_cell(shot: dict[str, Any]) -> str:
    L = shot.get("lighting") or {}
    key = L.get("key") or {}
    fill = L.get("fill") or {}
    rim = L.get("rim") or {}
    lines = [
        f"主光：{key.get('position', '')}{key.get('quality', '')}{key.get('source', '')}{key.get('temp', '')}",
        f"辅光：{fill.get('source', '')}+{fill.get('intensity', '')}",
        f"逆光：{rim.get('desc', '')}",
        f"色温：{L.get('color_temp_k', '')}k",
        f"光比：{L.get('ratio', '')}",
        f"阴影/反光：{L.get('shadows', '')}",
        f"氛围：{L.get('mood', '')}",
    ]
    return "<br>".join(html.escape(x) for x in lines)


def _shot_audio_cell(shot: dict[str, Any]) -> str:
    audio = shot.get("audio") or {}
    parts: list[str] = []
    d = audio.get("dialogue")
    if isinstance(d, dict) and d.get("text"):
        cref = d.get("character_ref") or ""
        parts.append(f"对白（{cref}）：{d['text']}（{d.get('emotion') or ''}）")
    for s in audio.get("sfx") or []:
        if isinstance(s, dict):
            parts.append(f"音效：{s.get('desc') or s.get('name')}（{s.get('volume')}dB）")
    bgm = audio.get("bgm")
    if bgm:
        parts.append(f"BGM：{bgm}")
    body = "<br>".join(html.escape(p) for p in parts)
    return body if body else html.escape("—")


def export_shots_director_table_html(shots: list[dict[str, Any]]) -> str:
    """
    将 shots 数组导出为 HTML 表，列与用户常用的「导演表」一致。
    「画面」列会说明：JSON 默认无分镜图文件，仅 image_prompt 可供出图。
    """
    headers = [
        "时间轴/镜号/相机参数",
        "画面",
        "景别/机位",
        "运镜",
        "表演/动作",
        "光影参数",
        "对白/音效",
    ]
    th = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    rows_html: list[str] = []
    for shot in shots:
        rows_html.append(
            "<tr>"
            f"<td>{_shot_timeline_camera_block(shot)}</td>"
            f"<td>{_shot_frame_column(shot)}</td>"
            f"<td>{_shot_angle_cell(shot)}</td>"
            f"<td>{_shot_movement_cell(shot)}</td>"
            f"<td>{_shot_performance_cell(shot)}</td>"
            f"<td>{_shot_lighting_cell(shot)}</td>"
            f"<td>{_shot_audio_cell(shot)}</td>"
            "</tr>"
        )
    table = (
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;font-size:13px;max-width:1200px'>"
        f"<thead><tr>{th}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'/>"
        "<title>分镜导演表</title></head><body>"
        f"{table}</body></html>"
    )


def export_storyboard_json_to_director_html(
    json_path: str | Path,
    out_path: str | Path | None = None,
) -> str:
    """
    读取 run_pipeline 写出的 storyboard_output.json（含 shots），生成导演表 HTML。
    """
    p = Path(json_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    shots = data.get("shots")
    if not isinstance(shots, list):
        raise ValueError("JSON 中缺少 shots 数组")
    html_doc = export_shots_director_table_html(shots)
    if out_path is not None:
        Path(out_path).write_text(html_doc, encoding="utf-8")
    return html_doc


# ──────────────────────────────────────────
# 使用示例
# ──────────────────────────────────────────

if __name__ == "__main__":
    user_input = PipelineInput(
        description="一个疲惫的打工人深夜独自加班，突然发现了改变一切的产品",
        hotword="打工人",
        image_paths=[
            "product_front.jpg",
        ],
        fps=24,
        target_duration_sec=30,
        style="写实/科技感",
    )

    result = run_pipeline(user_input)

    char_descs = {
        "char_001": "男性，30岁左右，工程师，疲惫憔悴，深色休闲外套",
    }
    shots_with_prompts = fill_generated_prompts(result.shots, result.product_desc, char_descs)

    output = {
        "product_desc": result.product_desc,
        "blueprint": result.blueprint,
        "shots": shots_with_prompts,
    }

    output_path = Path(__file__).resolve().parent / "storyboard_output.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    script_path = export_shots_complete_storyboard_script_markdown(
        shots_with_prompts,
        default_script_md_path(output_path),
    )

    print(f"\n结果已保存至：{output_path}")
    print(f"完整分镜脚本（Markdown）：{script_path}")
    print(f"总镜头数：{len(shots_with_prompts)}")

    if shots_with_prompts:
        first = shots_with_prompts[0]
        print("\n--- 第一个镜头预览 ---")
        print(f"Shot ID: {first['shot_id']}")
        print(f"时间: {first['timecode']['in']} → {first['timecode']['out']}")
        print(f"景别: {first['frame']['shot_type']} / {first['frame']['angle']}")
        ip = first["generated_prompts"]["image_prompt"] or ""
        print(f"Image Prompt: {ip[:100]}...")

