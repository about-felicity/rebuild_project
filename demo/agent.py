from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

from storyboard_pipeline import (
    PipelineInput,
    default_prompt_preview_md_path,
    default_script_md_path,
    export_shots_storyboard_script_to_path,
    fill_generated_prompts,
    run_storyboard_one_stop,
)

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

ROOT = Path(os.getcwd()).resolve()

SYSTEM = f"""你是「视频分镜」助手，工作目录：{ROOT}。
你只能通过工具调用项目里的 storyboard_pipeline（无 Shell、不能任意读写磁盘）。

默认行为（用户一句话 + 产品图要分镜时）：只调用一次 run_storyboard_pipeline 即完成全流程——
LLM 分镜 + 万相逐镜出图 + 回填图片路径 + 写出 JSON 与 Markdown 分镜脚本（*_script.md）。工具默认已开启生图，你不要再传 generate_shot_images=false，除非用户明确要求「只要文案不要出图」。
回复里总结产出路径与镜头数即可；禁止在结尾追问「要不要生图」「要不要下一步」等。
若万相失败（如未配置 DASHSCOPE_API_KEY），简洁说明原因与需配置的环境变量，不要套话邀请确认。

- fill_storyboard_prompts：仅在用户要改已有 JSON 的 prompt 时用（不跑万相）。

向用户说明能力时：只描述分镜流水线；不要声称能执行终端命令。
产品图路径用相对工作区；若用户贴了绝对路径，转成相对路径再调用工具。"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_storyboard_pipeline",
        "description": (
            "一步完成：产品图分析 → 剧本 → 分镜 JSON →（默认）万相逐镜出图 → Markdown 分镜脚本（*_script.md）。"
            "image_paths 为相对工作区根目录的 1–3 张产品图路径；默认 generate_shot_images=true。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "一句话视频创意/需求",
                },
                "hotword": {"type": "string", "description": "要融入的热点词语"},
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "产品图路径（相对工作区）",
                },
                "fps": {"type": "integer", "description": "帧率，默认 24"},
                "target_duration_sec": {
                    "type": "integer",
                    "description": "目标片长（秒），默认 30",
                },
                "style": {"type": "string", "description": "影像风格，默认「写实」"},
                "output_path": {
                    "type": "string",
                    "description": "写出完整 JSON 的相对路径，默认 storyboard_output.json",
                },
                "script_output_path": {
                    "type": "string",
                    "description": "完整分镜脚本路径；默认「JSON 主名_script.md」。若路径以 .tsv 结尾则导出制表格式",
                },
                "fill_prompts": {
                    "type": "boolean",
                    "description": "是否在 shots 上填充 image_prompt / video_prompt，默认 true",
                },
                "char_descs": {
                    "type": "object",
                    "description": "可选：角色 id -> 外观描述，供 fill_generated_prompts 使用",
                    "additionalProperties": {"type": "string"},
                },
                "generate_shot_images": {
                    "type": "boolean",
                    "description": "是否用万相逐镜出图（需 DASHSCOPE_API_KEY）。默认 true；仅当用户明确不要出图时设为 false",
                },
                "write_prompt_preview_tsv": {
                    "type": "boolean",
                    "description": "在生图前是否额外写出 *_script_prompts.md（画面为提示词），默认 true；仅当 generate_shot_images 为 true 时有效",
                },
                "wan_model": {
                    "type": "string",
                    "description": "万相模型，默认 wan2.7-image-pro；可改 wan2.7-image",
                },
                "wan_size": {
                    "type": "string",
                    "description": "分镜出图分辨率：竖屏 9:16 默认 1080*1920；可写 2K/4K 或 WxH。未填则用环境变量 STORYBOARD_FRAME_SIZE",
                },
                "product_ref_image_index": {
                    "type": "integer",
                    "description": "参考哪一张产品图（image_paths 下标），默认 0",
                },
                "shot_images_dir": {
                    "type": "string",
                    "description": "分镜 PNG 保存子目录（相对 JSON 所在目录），默认 storyboard_frames",
                },
            },
            "required": ["description", "hotword", "image_paths"],
        },
    },
    {
        "name": "fill_storyboard_prompts",
        "description": (
            "读取已有分镜结果 JSON（需含 product_desc 与 shots），"
            "调用 fill_generated_prompts 后写出；不重新跑 LLM 流水线。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string", "description": "输入 JSON，相对工作区"},
                "output_path": {
                    "type": "string",
                    "description": "输出路径；默认覆盖 input_path",
                },
                "char_descs": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["input_path"],
        },
    },
]


def _safe_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if ROOT not in p.parents and p != ROOT:
        raise ValueError("Path escapes workspace")
    return p


def tool_run_storyboard_pipeline(inp: dict[str, Any]) -> str:
    rel_images: list[str] = inp["image_paths"]
    abs_images: list[str] = []
    for rel in rel_images:
        try:
            p = _safe_path(rel)
        except ValueError as e:
            return f"Error: {e}"
        if not p.is_file():
            return f"Error: 找不到图片文件: {rel}"
        abs_images.append(str(p))

    out_rel = inp.get("output_path") or "storyboard_output.json"
    try:
        out_path = _safe_path(out_rel)
    except ValueError as e:
        return f"Error: {e}"

    pi = PipelineInput(
        description=inp["description"],
        hotword=inp["hotword"],
        image_paths=abs_images,
        fps=int(inp.get("fps") or 24),
        target_duration_sec=int(inp.get("target_duration_sec") or 30),
        style=str(inp.get("style") or "写实"),
    )
    fill_prompts = inp.get("fill_prompts", True)
    char_descs = inp.get("char_descs")
    if char_descs is not None and not isinstance(char_descs, dict):
        char_descs = None

    if "generate_shot_images" not in inp:
        gen_img = True
    else:
        v = inp["generate_shot_images"]
        gen_img = True if v is None else bool(v)
    script_arg = inp.get("script_output_path")
    try:
        script_path_opt = _safe_path(script_arg) if script_arg else None
    except ValueError as e:
        return f"Error: script_output_path: {e}"

    try:
        _widx = inp.get("product_ref_image_index")
        wan_ref_override = int(_widx) if _widx is not None and str(_widx).strip() != "" else None
        payload = run_storyboard_one_stop(
            pi,
            out_path,
            char_descs=char_descs,
            fill_prompts=fill_prompts,
            generate_shot_images=gen_img,
            wan_model=str(inp.get("wan_model") or "wan2.7-image-pro"),
            wan_size=str(
                inp.get("wan_size")
                or os.environ.get("STORYBOARD_FRAME_SIZE", "").strip()
                or "1080*1920"
            ),
            product_ref_index=wan_ref_override,
            frames_subdir=str(inp.get("shot_images_dir") or "storyboard_frames"),
            write_prompt_preview_tsv=bool(inp.get("write_prompt_preview_tsv", True)),
            script_output_path=script_path_opt,
        )
    except Exception as e:
        return f"Error: {e}"

    shots = payload["shots"]
    script_path = script_path_opt or default_script_md_path(out_path)
    try:
        script_rel = script_path.resolve().relative_to(ROOT)
    except ValueError:
        script_rel = script_path

    summary: dict[str, Any] = {
        "ok": True,
        "output_path": out_rel,
        "script_path": str(script_rel).replace("\\", "/"),
        "shot_count": len(shots),
        "scene_count": len(payload["scene_scripts"]),
        "product_name": payload["product_desc"].get("name"),
        "generate_shot_images": gen_img,
    }
    if gen_img and inp.get("write_prompt_preview_tsv", True):
        try:
            pprev = default_prompt_preview_md_path(out_path).resolve().relative_to(ROOT)
            summary["prompt_preview_script_path"] = str(pprev).replace("\\", "/")
        except ValueError:
            summary["prompt_preview_script_path"] = str(
                default_prompt_preview_md_path(out_path)
            )
        summary["shot_images_dir"] = str(inp.get("shot_images_dir") or "storyboard_frames")
    return json.dumps(summary, ensure_ascii=False, indent=2)


def tool_fill_storyboard_prompts(inp: dict[str, Any]) -> str:
    input_rel = inp["input_path"]
    output_rel = inp.get("output_path") or input_rel
    char_descs = inp.get("char_descs")
    if char_descs is not None and not isinstance(char_descs, dict):
        char_descs = None

    try:
        p_in = _safe_path(input_rel)
        p_out = _safe_path(output_rel)
    except ValueError as e:
        return f"Error: {e}"
    if not p_in.is_file():
        return f"Error: 找不到文件: {input_rel}"

    try:
        data = json.loads(p_in.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"Error: JSON 无效: {e}"

    product_desc = data.get("product_desc")
    shots = data.get("shots")
    if not isinstance(product_desc, dict) or not isinstance(shots, list):
        return "Error: JSON 需包含 product_desc（对象）与 shots（数组）"

    fill_generated_prompts(shots, product_desc, char_descs)
    data["shots"] = shots
    p_out.parent.mkdir(parents=True, exist_ok=True)
    p_out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    script_path = default_script_md_path(p_out)
    export_shots_storyboard_script_to_path(shots, script_path)
    try:
        script_rel = script_path.resolve().relative_to(ROOT)
    except ValueError:
        script_rel = script_path
    return json.dumps(
        {
            "ok": True,
            "wrote": output_rel,
            "script_path": str(script_rel).replace("\\", "/"),
            "shot_count": len(shots),
        },
        ensure_ascii=False,
        indent=2,
    )


def dispatch_tool(name: str, inp: dict[str, Any]) -> str:
    if name == "run_storyboard_pipeline":
        return tool_run_storyboard_pipeline(inp)
    if name == "fill_storyboard_prompts":
        return tool_fill_storyboard_prompts(inp)
    return f"Error: Unknown tool {name}"


def _tool_preview(name: str, inp: dict[str, Any]) -> str:
    if name == "run_storyboard_pipeline":
        return (
            f"run_storyboard_pipeline hotword={inp.get('hotword')!r} "
            f"images={inp.get('image_paths')}"
        )
    if name == "fill_storyboard_prompts":
        return f"fill_storyboard_prompts {inp.get('input_path')}"
    return name


def agent_loop(messages: list) -> None:
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                preview = _tool_preview(block.name, block.input)
                print(f"\033[33m{preview}\033[0m")
                output = dispatch_tool(block.name, block.input)
                print(output[:200] + ("..." if len(output) > 200 else ""))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
