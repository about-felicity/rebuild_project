"""
将 ``demo/storyboard_pipeline`` 接入 FrameOS：按项目写入 ``data/media/<project>/storyboard_runs/``，
并登记 ``project_assets``（library=storyboard）。通过 ``callback`` 推送进度事件供 SSE 使用。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parent.parent
_DEMO_DIR = _ROOT / "demo"
_TOOL_DIR = _ROOT / "tool"
if str(_DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(_DEMO_DIR))
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

from storyboard_pipeline import (  # noqa: E402
    PipelineInput,
    default_prompt_preview_md_path,
    default_script_md_path,
    export_shots_complete_storyboard_script_markdown,
    export_shots_storyboard_script_to_path,
    fill_generated_prompts,
    run_pipeline_with_events,
    talent_reference_index_0based,
    wan_reference_index_0based,
)
from wan_image_client import fill_storyboard_shots_with_wan  # noqa: E402

from seedream_client import (  # noqa: E402
    DEFAULT_ARK_SEEDREAM_MODEL,
    fill_storyboard_shots_with_seedream,
)

from data.db import insert_project_asset
from data.media_mirror import ensure_project_media_dir


def _storyboard_image_provider() -> str:
    return (
        "wan"
        if os.getenv("IMAGE_GENERATION_PROVIDER", "seedream").strip().lower() == "wan"
        else "seedream"
    )


def normalize_shot_frame_urls(
    shots: list[dict[str, Any]],
    media_seg: str,
    run_id: str,
) -> None:
    """将 ``frame.image_url`` 转为 ``/media/...`` 可访问路径。"""
    base = f"/media/{media_seg}/storyboard_runs/{run_id}/"
    for shot in shots:
        fr = shot.get("frame") or {}
        u = fr.get("image_url")
        if not u or not isinstance(u, str):
            continue
        u = u.strip()
        if u.startswith(("http://", "https://", "/media/")):
            continue
        fr["image_url"] = base + u.replace("\\", "/").lstrip("/")


def run_storyboard_for_project(
    project_id: str,
    abs_image_paths: list[Path],
    description: str,
    hotword: str,
    *,
    fps: int = 24,
    target_duration_sec: int = 30,
    style: str = "写实",
    generate_shot_images: bool = True,
    write_prompt_preview: bool = True,
    wan_model: str | None = None,
    wan_size: str = "2K",
    product_ref_index: Optional[int] = None,
    callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def emit(d: dict[str, Any]) -> None:
        if callback:
            callback(d)

    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("project_id 不能为空")
    if not abs_image_paths:
        raise ValueError("请至少上传 1 张产品图")
    for p in abs_image_paths:
        rp = Path(p)
        if not rp.is_file():
            raise FileNotFoundError(f"产品图不存在: {p}")

    run_id = uuid.uuid4().hex[:16]
    media_seg, proj_dir = ensure_project_media_dir(pid)
    run_dir = proj_dir / "storyboard_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    emit({"type": "run_start", "run_id": run_id, "project_id": pid})

    pi = PipelineInput(
        description=description.strip(),
        hotword=hotword.strip(),
        image_paths=[str(Path(p).resolve()) for p in abs_image_paths],
        fps=int(fps),
        target_duration_sec=int(target_duration_sec),
        style=(style or "").strip() or "写实",
    )

    result = run_pipeline_with_events(pi, on_event=emit)
    shots = result.shots
    cv = result.product_desc.get("character_visual")
    cv = cv if isinstance(cv, dict) else {}
    ce = str(cv.get("character_prompt_en") or result.product_desc.get("character_prompt_en") or "").strip()
    char_descs = {"char_001": ce} if ce else None
    fill_generated_prompts(shots, result.product_desc, char_descs)

    json_path = run_dir / "storyboard.json"
    frames_rel_dir = "storyboard_frames"
    frames_dir = run_dir / frames_rel_dir

    if generate_shot_images and write_prompt_preview:
        pprev = default_prompt_preview_md_path(json_path)
        export_shots_complete_storyboard_script_markdown(
            shots,
            pprev,
            title="分镜脚本（生图前 · 画面为提示词）",
        )
        emit(
            {
                "type": "artifact",
                "kind": "markdown_preview",
                "uri": f"/media/{media_seg}/storyboard_runs/{run_id}/{pprev.name}",
            }
        )

    if generate_shot_images:
        n_paths = len(abs_image_paths)
        widx = wan_reference_index_0based(result.product_desc, n_paths, product_ref_index)
        tidx = talent_reference_index_0based(result.product_desc, n_paths)
        prod_ref = str(Path(abs_image_paths[widx]).resolve())
        char_ref: str | None = None
        if tidx is not None and tidx != widx:
            char_ref = str(Path(abs_image_paths[tidx]).resolve())
        prov = _storyboard_image_provider()
        if prov == "wan":
            batch_lbl = (
                "万相逐镜出图（图1人物 + 图2产品）"
                if char_ref
                else "万相逐镜出图（产品参考 第 " + str(widx + 1) + "/" + str(n_paths) + " 张）"
            )
        else:
            batch_lbl = (
                "Seedream 逐镜出图（图1人物 + 图2产品）"
                if char_ref
                else "Seedream 逐镜出图（产品参考 第 "
                + str(widx + 1)
                + "/"
                + str(n_paths)
                + " 张）"
            )
        emit(
            {
                "type": "step",
                "id": "wan_batch",
                "label": batch_lbl,
                "wan_ref_index": widx,
                "wan_talent_index": tidx,
                "image_provider": prov,
            }
        )

        def on_shot_done(_i: int, _total: int, shot_id: str, _rel_url: str) -> None:
            normalize_shot_frame_urls(shots, media_seg, run_id)
            emit(
                {
                    "type": "wan_shot_done",
                    "index": _i,
                    "total": _total,
                    "shot_id": shot_id,
                }
            )

        if prov == "wan":
            wm = (wan_model or "").strip() or "wan2.7-image-pro"
            fill_storyboard_shots_with_wan(
                shots,
                prod_ref,
                frames_dir,
                frames_rel_dir,
                character_reference_image_path=char_ref,
                model=wm,
                size=wan_size,
                watermark=False,
                on_shot_done=on_shot_done,
            )
        else:
            sm = (wan_model or "").strip() or os.getenv(
                "ARK_IMAGE_MODEL", DEFAULT_ARK_SEEDREAM_MODEL
            ).strip()
            fill_storyboard_shots_with_seedream(
                shots,
                prod_ref,
                frames_dir,
                frames_rel_dir,
                character_reference_image_path=char_ref,
                model=sm,
                size=wan_size,
                watermark=False,
                on_shot_done=on_shot_done,
            )
        normalize_shot_frame_urls(shots, media_seg, run_id)
        emit({"type": "step_done", "id": "wan_batch", "detail": f"{len(shots)} 镜"})

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

    script_path = export_shots_storyboard_script_to_path(
        shots, default_script_md_path(json_path)
    )

    json_uri = f"/media/{media_seg}/storyboard_runs/{run_id}/storyboard.json"
    script_uri = f"/media/{media_seg}/storyboard_runs/{run_id}/{script_path.name}"

    meta_extra: dict[str, Any] = {
        "run_id": run_id,
        "shot_count": len(shots),
        "scene_count": len(result.scene_scripts),
        "script_uri": script_uri,
        "hotword": hotword.strip(),
    }
    pn = result.product_desc.get("name")
    if pn:
        meta_extra["product_name"] = str(pn)

    title = f"分镜 · {(hotword.strip() or '未命名')[:32]}"
    asset_id = insert_project_asset(
        pid,
        "storyboard",
        "storyboard",
        title,
        uri=json_uri,
        meta=meta_extra,
    )

    # 万相逐镜 PNG 仅写在 JSON 的 frame.image_url 里时，分镜库网格不会出现「单镜图」；逐条登记便于浏览与复用。
    if generate_shot_images:
        for idx, shot in enumerate(shots):
            fr = shot.get("frame") or {}
            img_u = (fr.get("image_url") or "").strip()
            if not img_u.startswith("/media/"):
                continue
            shot_id = str(shot.get("shot_id") or f"shot_{idx + 1}")
            nm = (f"分镜图 · {shot_id}")[:120]
            insert_project_asset(
                pid,
                "storyboard",
                "image",
                nm,
                uri=img_u,
                meta={
                    "run_id": run_id,
                    "shot_id": shot_id,
                    "storyboard_bundle_asset_id": asset_id,
                    "storyboard_json_uri": json_uri,
                    "shot_index": idx + 1,
                    "shot_total": len(shots),
                    "source": "storyboard_pipeline_wan",
                },
            )

    shot_frames: list[dict[str, Any]] = []
    if generate_shot_images:
        for idx, shot in enumerate(shots):
            fr = shot.get("frame") or {}
            img_u = (fr.get("image_url") or "").strip()
            if not img_u:
                continue
            shot_id = str(shot.get("shot_id") or f"shot_{idx + 1}")
            shot_frames.append(
                {"shot_id": shot_id, "image_url": img_u, "index": idx + 1}
            )

    out = {
        "run_id": run_id,
        "asset_id": asset_id,
        "json_uri": json_uri,
        "script_uri": script_uri,
        "shot_count": len(shots),
        "project_id": pid,
        "shot_frames": shot_frames,
    }
    emit({"type": "done", **out})
    return out
