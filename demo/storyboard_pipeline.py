"""
视频分镜脚本生成流水线 — 完整 Python 实现
四步 LLM 串联：产品图分析 → 剧本扩写 → 场景规划细化 → 分镜 JSON 生成

完整分镜脚本默认导出为 **Markdown**（`export_shots_complete_storyboard_script_markdown`）：按镜分块、易读。
仍保留 TSV 导出函数供表格工具使用。

一步出分镜图：run_storyboard_one_stop(..., generate_shot_images=True) 调用万相（见 wan_image_client.py，需 DASHSCOPE_API_KEY）。

依赖：
    pip install anthropic
    分镜出图：pip install dashscope>=1.25.15

（Pillow 可选，本模块读图仅用标准库 base64）
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import anthropic

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


# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────


def load_image_base64(path: str) -> dict[str, Any]:
    """读取本地图片，返回 Anthropic API content block"""
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
# Step 0：产品图 Vision 分析
# ──────────────────────────────────────────

STEP0_SYSTEM = """你是专业产品摄影分析师。
分析用户上传的参考图（可能多张）。其中常混有：**(A) 产品包装/瓶身/商品主图** 与 **(B) 人物肖像、场景、情绪板等非包装图**。用户会提供每张图对应的**原始文件名**：须结合文件名与画面区分 A/B。
- 后续「图生图」环节**只会选一张图作为产品外观锁定的参考**，你必须在 JSON 里用 packshot_image_index 标明哪一张是 (A) 主产品图（按上传顺序从 1 数起）。
- 若多张都是产品不同角度，选最能代表瓶身/包装的一张；若仅一张图，填 1。
若文件名与画面明显冲突，以画面为准，在 appearance 中用一句话说明不确定性。
只输出JSON，不输出任何其他内容，不加markdown代码块标记。"""

STEP0_USER_TMPL = """请分析这些参考图（含产品与可能出现的角色/场景图），输出以下JSON格式：
{{
  "product_id": "product_001",
  "packshot_image_index": 1,
  "talent_reference_image_index": null,
  "talent_appearance": null,
  "name": "产品名称或类别（可结合上方文件名与画面；若仅用画面可确定则不必强行贴合文件名）",
  "appearance": "外观描述（颜色/形状/材质/尺寸感）",
  "logo_position": "品牌Logo的位置描述，如无则填null",
  "size_estimate": "大小估计，如：手持小物/桌面中型/大型设备",
  "key_features": ["视觉特征1", "视觉特征2", "视觉特征3"],
  "brand_mood": "品牌气质关键词，如：科技感/温馨/高端/年轻",
  "color_palette": ["主色1", "主色2"],
  "usage_scenario": "推测的使用场景"
}}

说明：
- packshot_image_index：1～{n_images}，产品包装/瓶身主参考（万相「图2」或与人物同图时的唯一参考）。
- talent_reference_image_index：若仅 1 张图填 null；若有多张且其中一张为**人物/模特**（非产品主图），填其序号 1～{n_images}，且**必须**与 packshot_image_index 不同（人物图与产品图各一张时各填一个序号）。
- talent_appearance：当上一项非 null 时，用 1～2 句概括该人物的发型、脸型、体型、衣着，供分镜提示词锁脸；无人物图则 null。"""


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


def analyze_product(client: anthropic.Anthropic, image_paths: list[str]) -> dict[str, Any]:
    """Step 0：多视角产品图 → 结构化产品描述"""
    if not image_paths:
        raise ValueError("image_paths 不能为空，请提供至少一张产品图路径")

    names = ", ".join(Path(p).name for p in image_paths)
    print(f"  [Step 0] 分析产品图（引用文件：{names}）...")

    n_img = len(image_paths)
    user_text = (
        f"{_step0_filename_context(image_paths)}\n\n"
        + STEP0_USER_TMPL.format(n_images=n_img)
    )

    content: list[dict[str, Any]] = []
    for path in image_paths:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"产品图不存在: {path}")
        content.append(load_image_base64(path))
    content.append({"type": "text", "text": user_text})

    text = call_claude(client, STEP0_SYSTEM, [{"role": "user", "content": content}], max_tokens=800)
    result = parse_json_response(text)
    try:
        ix = int(result.get("packshot_image_index", 1))
    except (TypeError, ValueError):
        ix = 1
    result["packshot_image_index"] = max(1, min(n_img, ix))
    _normalize_talent_reference_index(result, n_img)
    print(
        f"  [Step 0] 完成 → 产品：{result.get('name')}，气质：{result.get('brand_mood')}，"
        f"产品主图=第{result['packshot_image_index']}张"
        + (
            f"，人物参考=第{result['talent_reference_image_index']}张"
            if result.get("talent_reference_image_index")
            else ""
        )
    )
    return result


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


def step1_duration_policy(target_duration_sec: int) -> str:
    """按目标片长约束场景数与总镜头数，避免 15s 成片被切成 9 镜。"""
    t = max(5, min(180, int(target_duration_sec)))
    if t <= 18:
        return (
            f"目标总时长约 **{t} 秒**：scene_count 只能为 **1 或 2**；"
            f"所有场景的 **shot_count 相加总和必须在 4～6 之间**（含边界）；"
            f"各场景 duration_sec 之和应约等于 {t}；单镜时长建议 **2.5～4 秒**，少用细碎切镜。"
        )
    if t <= 30:
        return (
            f"目标总时长约 **{t} 秒**：scene_count 为 **2 或 3**；"
            f"全片镜头总数（各场景 shot_count 之和）建议在 **6～9**；"
            f"各场景 duration_sec 之和约 {t}。"
        )
    if t <= 60:
        return (
            f"目标总时长约 **{t} 秒**：scene_count **3～5**；"
            f"全片镜头总数建议 **10～16**；单镜 3～6 秒为主。"
        )
    return (
        f"目标总时长约 **{t} 秒**：scene_count **4～6**；"
        f"全片镜头总数建议 **16～24**；注意节奏与叙事完整性。"
    )


# ──────────────────────────────────────────
# Step 1：剧本扩写
# ──────────────────────────────────────────

STEP1_SYSTEM = """你是一名擅长短视频营销的创意策划总监，同时熟悉网络热点和情绪营销。

你的任务：根据"一句话需求"和"热点词语"，结合产品信息，创作一个适合短视频的故事蓝图。

输出规则：
1. 只输出JSON，不输出任何其他内容，不加markdown标记
2. 故事必须自然融入热点词语，不能生硬植入
3. 产品必须在故事中发挥实际作用，不能只是背景道具
4. 叙事结构固定为：钩子（0-5s抓注意力）→ 冲突/共鸣（展开情绪）→ 转折/高潮 → 产品解决方案 → 行动号召
5. 角色情绪弧必须与热点词语的情绪内核一致
6. JSON 字符串内如需换行，必须写成转义 \\n，禁止在双引号字符串里直接按回车（否则无法解析）
7. **必须遵守「时长与镜头密度」中的 scene_count、各场景 shot_count 总和与 duration_sec**，短广告勿生成过多分镜（如 15 秒不宜 9 镜）。"""

STEP1_USER_TMPL = """## 用户输入
一句话需求：{description}
热点词语：{hotword}

## 产品信息
{product_desc}

## 时长与镜头密度（必须遵守）
{duration_policy}

## 创作要求
视频时长：{target_duration_sec}秒
风格：{style}
帧率：{fps}fps

## 输出JSON格式（数值须符合上方密度约束；下例仅为结构示意）
{{
  "video_concept": "核心创意一句话（要有冲击力）",
  "narrative_arc": {{
    "hook": "0-5秒钩子：如何抓住注意力",
    "conflict": "冲突/共鸣段：展开的情绪或问题",
    "turning_point": "转折：产品或事件如何介入",
    "resolution": "高潮解决：产品带来什么改变",
    "cta": "行动号召：最后引导用户做什么"
  }},
  "tone": "影片整体基调，如：压抑转希望/轻松幽默/热血燃情",
  "target_emotion": "希望观众最终感受到的情绪",
  "hotword_integration": "热点词语融入方式的具体说明",
  "total_duration_sec": {target_duration_sec},
  "scene_count": 2,
  "scenes": [
    {{
      "scene_id": "01",
      "scene_desc": "场景描述，格式：地点 · EXT或INT · 时间段",
      "location_type": "INT或EXT",
      "time_of_day": "清晨/白天/傍晚/深夜",
      "mood": "场景情绪关键词",
      "plot_beat": "这场戏在故事中的叙事功能",
      "duration_sec": 7,
      "shot_count": 2,
      "product_appears": true,
      "product_role": "产品在本场景中如何出现和发挥作用",
      "characters": [
        {{
          "char_id": "char_001",
          "desc": "角色简述（性别/年龄/职业/状态）",
          "emotion_arc": "本场景角色情绪变化，如：疲惫→愤怒"
        }}
      ]
    }},
    {{
      "scene_id": "02",
      "scene_desc": "第二场景示例",
      "location_type": "INT",
      "time_of_day": "白天",
      "mood": "示例",
      "plot_beat": "示例",
      "duration_sec": 8,
      "shot_count": 2,
      "product_appears": true,
      "product_role": "示例",
      "characters": [
        {{
          "char_id": "char_001",
          "desc": "与上一场同一主角时可延续 char_id",
          "emotion_arc": "示例"
        }}
      ]
    }}
  ]
}}

注意：**scenes 数组长度必须等于 scene_count**；各场景 duration_sec 之和应接近目标总时长；各场景 shot_count 之和须符合上文「镜头密度」。"""


def expand_screenplay(
    client: anthropic.Anthropic,
    pipeline_input: PipelineInput,
    product_desc: dict[str, Any],
) -> dict[str, Any]:
    """Step 1：一句话 + 热点词 + 产品 → 故事蓝图"""
    print("  [Step 1] 剧本扩写...")

    user_content = STEP1_USER_TMPL.format(
        description=pipeline_input.description,
        hotword=pipeline_input.hotword,
        product_desc=json.dumps(product_desc, ensure_ascii=False, indent=2),
        target_duration_sec=pipeline_input.target_duration_sec,
        style=pipeline_input.style,
        fps=pipeline_input.fps,
        duration_policy=step1_duration_policy(pipeline_input.target_duration_sec),
    )

    text = call_claude(client, STEP1_SYSTEM, [{"role": "user", "content": user_content}])
    result = parse_json_response(text)
    print(f"  [Step 1] 完成 → 概念：{result.get('video_concept')}")
    print(f"  [Step 1] 场景数：{len(result.get('scenes', []))}")
    return result


# ──────────────────────────────────────────
# Step 2：逐场景剧本细化
# ──────────────────────────────────────────

STEP2_SYSTEM = """你是专业编剧，负责将创意蓝图中的单个场景扩写为可拍摄的详细剧本。

输出规则：
1. 只输出JSON，不输出任何其他内容，不加markdown标记
2. beats（节拍）数量 = 该场景的 shot_count
3. 每个beat对应一个分镜，动作描述要细化到肢体层面
4. 台词要符合角色情绪弧，无台词填null
5. suggested_shot_type 必须从枚举中选：ECU/CU/MCU/MS/MLS/FS/WS/EWS"""

STEP2_USER_TMPL = """## 整体创意蓝图
{blueprint_summary}

## 当前需要细化的场景
{scene_info}

## 产品信息
{product_desc}

## 输出JSON格式
{{
  "scene_id": "{scene_id}",
  "scene_desc": "{scene_desc}",
  "beats": [
    {{
      "beat_id": 1,
      "action": "动作描述，细化到肢体，如：右手缓慢放到产品上，食指轻触表面，停顿2秒",
      "dialogue": "台词原文，无台词填null",
      "dialogue_emotion": "说台词时的情绪，无台词填null",
      "emotion": "当下角色情绪",
      "product_moment": "产品出现方式描述，无则填null",
      "suggested_shot_type": "MCU",
      "suggested_movement": "固定",
      "duration_hint_sec": 4
    }}
  ],
  "scene_sfx": ["环境音1描述", "环境音2描述"],
  "scene_bgm_mood": "BGM情绪描述，如：低沉压抑的电子氛围音"
}}"""


def expand_scene(
    client: anthropic.Anthropic,
    blueprint: dict[str, Any],
    scene: dict[str, Any],
    product_desc: dict[str, Any],
) -> dict[str, Any]:
    """Step 2：单场景细化"""
    blueprint_summary = {
        "video_concept": blueprint.get("video_concept"),
        "tone": blueprint.get("tone"),
        "target_emotion": blueprint.get("target_emotion"),
        "narrative_arc": blueprint.get("narrative_arc"),
        "hotword_integration": blueprint.get("hotword_integration"),
    }

    user_content = STEP2_USER_TMPL.format(
        blueprint_summary=json.dumps(blueprint_summary, ensure_ascii=False, indent=2),
        scene_info=json.dumps(scene, ensure_ascii=False, indent=2),
        product_desc=json.dumps(product_desc, ensure_ascii=False, indent=2),
        scene_id=scene["scene_id"],
        scene_desc=str(scene.get("scene_desc", "")),
    )

    text = call_claude(client, STEP2_SYSTEM, [{"role": "user", "content": user_content}])
    return parse_json_response(text)


# ──────────────────────────────────────────
# Step 3：分镜 JSON 生成
# ──────────────────────────────────────────

STEP3_SYSTEM = """你是专业导演助理，将详细场景剧本转化为标准分镜脚本JSON数组。

## 枚举约束（必须从以下值中选择，不可自造）
shot_type: ECU | CU | MCU | MS | MLS | FS | WS | EWS
angle: 正面 | 侧面 | 侧后方 | 背面 | 俯拍 | 仰拍 | 过肩
eye_level: 俯视 | 平视 | 微仰 | 仰视
movement.type: 固定 | 缓推 | 缓拉 | 横移 | 跟拍 | 升降 | 手持 | 环绕
lighting.key.quality: 硬光 | 柔光 | 漫射
lighting.key.temp: 冷 | 中性 | 暖
lighting.fill.intensity: 无 | 极弱 | 弱 | 中 | 强

## 相机参数规则
- ECU/CU: 85-135mm, f/1.4-f/2.8, ISO高
- MCU/MS: 35-85mm, f/2.8-f/4.0
- FS/WS/EWS: 16-35mm, f/4.0-f/8.0
- 快门速度 = fps × 2（24fps → 1/50，30fps → 1/60）
- 室内低光 ISO 800-3200，充足光线 ISO 100-400

## 时长规则
- ECU/CU: 2-4秒
- MCU/MS: 3-6秒
- FS/WS/EWS: 4-8秒
- 动作镜头按动作实际时长

## 光比规则
- 轻松场景: 2:1 ~ 3:1
- 戏剧场景: 5:1 ~ 8:1
- 压抑/惊悚: 10:1以上

## 输出规则
1. 只输出JSON数组，不输出任何其他内容，不加markdown标记
2. 所有字段必须存在，无内容填null，不可省略字段
3. image_url 统一填 null
4. generated_prompts 两字段统一填 null
5. status 统一填 "draft"
6. timecode 从参数指定的起始时间开始，连续不断"""

STEP3_USER_TMPL = """## 场景详细剧本
{scene_script}

## 产品信息
{product_desc}

## 帧率
{fps}fps

## 时间接续
本场景 timecode.in 从 "{start_timecode}" 开始（接续上一场景）

## 镜头数量
输出的 JSON 数组长度必须等于场景剧本中 beats 数组的长度（一 beat 一镜），不得多也不得少。

## 输出JSON数组格式（每个镜头一个对象）
[
  {{
    "shot_id": "S{scene_id_padded}-01",
    "scene": "{scene_id}",
    "scene_desc": "{scene_desc}",
    "timecode": {{"in": "MM:SS", "out": "MM:SS", "duration_sec": 4}},
    "status": "draft",
    "camera": {{
      "lens_mm": 50,
      "aperture": "f/2.8",
      "shutter": "1/50",
      "iso": 1600,
      "color_temp_k": 5600
    }},
    "frame": {{
      "shot_type": "MCU",
      "angle": "侧后方",
      "eye_level": "平视",
      "composition_desc": "画面构图描述",
      "image_url": null
    }},
    "movement": {{
      "type": "固定",
      "desc": null
    }},
    "performance": {{
      "action": "动作描述",
      "emotion": "情绪",
      "character_refs": ["char_001"],
      "product_refs": []
    }},
    "lighting": {{
      "key": {{"position": "左前高位", "quality": "硬光", "source": "片场灯", "temp": "冷"}},
      "fill": {{"source": "监视器冷光", "intensity": "弱"}},
      "rim": {{"desc": "硬光勾勒轮廓线"}},
      "color_temp_k": 5600,
      "ratio": "8:1",
      "shadows": "阴影描述",
      "mood": "氛围关键词"
    }},
    "audio": {{
      "dialogue": {{
        "text": "台词内容",
        "emotion": "情绪",
        "volume_db": 0,
        "character_ref": "char_001"
      }},
      "sfx": [
        {{"name": "sfx_id", "desc": "音效描述", "volume": -12}}
      ],
      "bgm": null
    }},
    "alerts": [],
    "generated_prompts": {{
      "image_prompt": null,
      "video_prompt": null
    }}
  }}
]"""


def seconds_to_timecode(seconds: int) -> str:
    """秒数转 MM:SS 格式"""
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def timecode_to_seconds(tc: str) -> int:
    """MM:SS 格式转秒数"""
    parts = tc.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def generate_shots(
    client: anthropic.Anthropic,
    scene_script: dict[str, Any],
    product_desc: dict[str, Any],
    fps: int,
    start_timecode: str = "00:00",
) -> list[dict[str, Any]]:
    """Step 3：场景剧本 → 分镜JSON数组"""
    scene_id = str(scene_script["scene_id"])
    sid = scene_id.zfill(2)

    user_content = STEP3_USER_TMPL.format(
        scene_script=json.dumps(scene_script, ensure_ascii=False, indent=2),
        product_desc=json.dumps(product_desc, ensure_ascii=False, indent=2),
        fps=fps,
        start_timecode=start_timecode,
        scene_id=scene_id,
        scene_id_padded=sid,
        scene_desc=str(scene_script.get("scene_desc", "")),
    )

    text = call_claude(
        client,
        STEP3_SYSTEM,
        [{"role": "user", "content": user_content}],
        max_tokens=6000,
    )
    shots = parse_json_response(text)
    return shots if isinstance(shots, list) else [shots]


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
    product_desc = analyze_product(client, pipeline_input.image_paths)
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
        scene_script = expand_scene(client, blueprint, scene, product_desc)
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
        shots = generate_shots(client, scene_script, product_desc, pipeline_input.fps, prev_end_time)
        all_shots.extend(shots)
        if shots:
            prev_end_time = shots[-1]["timecode"]["out"]
        emit(
            {
                "type": "step_done",
                "id": "generate_shots",
                "scene_id": sid,
                "detail": f"{len(shots)} 镜，接续至 {prev_end_time}",
            }
        )

    emit({"type": "pipeline_done", "shot_count": len(all_shots), "scene_count": len(scene_scripts)})
    return PipelineResult(
        product_desc=product_desc,
        blueprint=blueprint,
        scene_scripts=scene_scripts,
        shots=all_shots,
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
    parts: list[str] = []

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

    parts = [
        frame.get("composition_desc", ""),
        movement_map.get(movement_type, "static camera"),
        f"character: {perf.get('action')}, emotion: {perf.get('emotion')}",
        f"lighting mood: {light.get('mood')}",
        f"duration: {tc.get('duration_sec', 4)} seconds",
        "cinematic, smooth motion, film quality",
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
        gp["image_prompt"] = build_image_prompt(shot, product_desc, char_descs)
        gp["video_prompt"] = build_video_prompt(shot)
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


def run_storyboard_one_stop(
    pipeline_input: PipelineInput,
    json_out: str | Path,
    *,
    char_descs: Optional[dict[str, str]] = None,
    fill_prompts: bool = True,
    generate_shot_images: bool = True,
    wan_model: str = "wan2.7-image",
    wan_size: str = "2K",
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
        fill_storyboard_shots_with_wan(
            shots,
            prod_path,
            frames_abs,
            frames_rel,
            character_reference_image_path=char_path,
            model=wan_model,
            size=wan_size,
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

