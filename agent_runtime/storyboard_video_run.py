"""
分镜成片：逐镜 Ark 图生视频 → 本地落盘 → **按分镜时长裁切/垫齐** → ffmpeg 拼接 → 登记视频库。

``storyboard.json`` 里各镜 ``timecode.duration_sec`` 为**时间轴目标**。主流水线已按 ``ARK_VIDEO_MODEL``
单段最短时长合并镜头并分配时长（见 ``demo/storyboard_pipeline`` 的 ``merge_shots_for_ark_duration_floor``），
故时间轴与方舟 ``duration`` 通常一致；若人工把某镜改短于下限，仍用 ``ark_duration`` 请求方舟并用
``_ffmpeg_normalize_clip_to_duration`` 裁回分镜秒数兜底。
需本机 ``ffmpeg`` / ``ffprobe`` 可用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
_DEMO_DIR = _ROOT / "demo"
_TOOL_DIR = _ROOT / "tool"
if str(_DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(_DEMO_DIR))
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

from storyboard_pipeline import build_video_prompt, fill_generated_prompts  # noqa: E402

from data.db import insert_project_asset  # noqa: E402
from data.media_mirror import ensure_project_media_dir, mirror_http_url_to_local  # noqa: E402
from tool.generation_tools import LocalWanArkHub, run_generate_video  # noqa: E402
from tool.ai import (  # noqa: E402
    MediaGenerationRequestClient,
    clamp_ark_video_duration_seconds,
    normalize_ark_video_model_id,
)

_MEDIA_ROOT = _ROOT / "data" / "media"

_MOVEMENT_CAMERA_EN: dict[str, str] = {
    "固定": "static camera, no movement",
    "缓推": "slow dolly push in",
    "缓拉": "slow dolly pull back",
    "横移": "smooth lateral tracking",
    "跟拍": "follow cam tracking subject",
    "升降": "crane up or down",
    "手持": "subtle handheld movement",
    "环绕": "slow orbital move around subject",
}


def _safe_run_id(raw: str) -> str:
    s = (raw or "").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{12,32}", s):
        raise ValueError("run_id 无效（应为分镜任务返回的十六进制 id）")
    return s.lower()[:16]


def _coerce_clip_duration_sec(raw: Any) -> int:
    """
    与分镜 ``timecode.duration_sec`` 对齐，并按 ``ARK_VIDEO_MODEL`` 钳到方舟允许的整数秒。
    Seedance 1.5 pro 图生视频要求 duration ∈ [4,12]（见官方文档），小于 4 会 400 InvalidParameter。
    """
    try:
        d = int(round(float(raw)))
    except (TypeError, ValueError):
        d = 5
    mid = normalize_ark_video_model_id(
        os.environ.get("ARK_VIDEO_MODEL", "").strip() or "doubao-seedance-1-5-pro-251215"
    )
    return clamp_ark_video_duration_seconds(d, mid)


def _timeline_clip_duration_sec(raw: Any) -> int:
    """分镜时间轴上的目标时长（秒），用于 ffmpeg 裁切/垫片，不向方舟抬最小值。"""
    try:
        return max(1, int(round(float(raw))))
    except (TypeError, ValueError):
        return 5


def _ffprobe_duration_sec(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe or not path.is_file():
        return None
    cmd = [
        exe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        return None
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return None


def _ffmpeg_normalize_clip_to_duration(src: Path, dst: Path, duration_sec: int) -> None:
    """
    将单段成片裁/垫为**精确** ``duration_sec`` 秒，便于多段拼接后总长 = 各镜之和
    （与 ``normalize_shot_timeline_to_target`` 在 JSON 里锁定的总时长一致）。

    云端图生视频常返回比请求更长的片段；若不处理，直接 concat 会得到「选 20s 却 30s+」的成片。
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        shutil.copy2(src, dst)
        return
    D = float(max(1, int(duration_sec)))
    act = _ffprobe_duration_sec(src)
    if act is None or act <= 0:
        act = D
    pad = max(0.0, D - act)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)

    def _ok() -> bool:
        return dst.is_file() and dst.stat().st_size > 32

    def _run(cmd: list[str]) -> bool:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return r.returncode == 0 and _ok()

    base = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]

    attempts: list[list[str]] = []
    if pad > 0.05:
        attempts.append(
            base
            + [
                "-vf",
                f"tpad=stop_mode=clone:stop_duration={pad:.4f}",
                "-af",
                f"apad=pad_dur={pad:.4f}",
                "-t",
                f"{D:.4f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(dst),
            ]
        )
        # 无音轨或 apad 失败时：画面 tpad + 静音 AAC，避免与后续带音频片段 concat 不兼容
        attempts.append(
            [
                exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-f",
                "lavfi",
                "-t",
                f"{D:.4f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-filter_complex",
                f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.4f}[v]",
                "-map",
                "[v]",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(dst),
            ]
        )

    attempts.append(
        base
        + [
            "-t",
            f"{D:.4f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dst),
        ]
    )
    attempts.append(base + ["-t", f"{D:.4f}", "-c:v", "copy", "-c:a", "copy", str(dst)])
    # 源无音轨等：只取画面并配静音，保证每段都有 v+a 便于 concat
    attempts.append(
        [
            exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-t",
            f"{D:.4f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-t",
            f"{D:.4f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dst),
        ]
    )

    for cmd in attempts:
        if _run(cmd):
            return
    shutil.copy2(src, dst)


def _media_url_to_disk_path(uri: str) -> Path | None:
    u = (uri or "").strip()
    if not u.startswith("/media/"):
        return None
    rel = u[len("/media/") :].lstrip("/\\")
    p = _MEDIA_ROOT / rel
    return p if p.is_file() else None


def _download_video_to_run_dir(
    project_id: str, remote_url: str, dest_file: Path
) -> None:
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    local_uri = mirror_http_url_to_local(project_id, remote_url)
    if local_uri:
        src = _media_url_to_disk_path(local_uri)
        if src and src.is_file():
            shutil.copy2(src, dest_file)
            return
    raise RuntimeError(f"无法下载视频片段: {remote_url[:120]!r}")


def _ffmpeg_concat(clip_paths: list[Path], out_mp4: Path) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("未找到 ffmpeg：请安装并加入 PATH 后再试拼接。")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    list_path = out_mp4.with_suffix(".concat.txt")
    try:
        lines: list[str] = []
        for p in clip_paths:
            ap = p.resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{ap}'")
        list_path.write_text("\n".join(lines), encoding="utf-8")

        def _run(extra: list[str]) -> subprocess.CompletedProcess[str]:
            cmd = [
                exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                *extra,
                str(out_mp4),
            ]
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        r = _run(["-c", "copy"])
        if r.returncode == 0 and out_mp4.is_file():
            return
        err1 = (r.stderr or "")[:800]
        r2 = _run(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
            ]
        )
        if r2.returncode != 0 or not out_mp4.is_file():
            err2 = (r2.stderr or "")[:800]
            raise RuntimeError(
                f"ffmpeg 拼接失败（-c copy: {err1!r}；重编码: {err2!r}）"
            )
    finally:
        try:
            list_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_storyboard_video_for_project(
    project_id: str,
    run_id: str,
    *,
    callback: Callable[[dict[str, Any]], None] | None = None,
    poll_timeout_sec: int | None = None,
) -> dict[str, Any]:
    """
    读取 ``storyboard_runs/<run_id>/storyboard.json``，对每镜用分镜图 + ``video_prompt`` 调 Ark 生视频，
    拼接为 ``storyboard_assembled.mp4``，写入视频库。
    """
    def emit(d: dict[str, Any]) -> None:
        if callback:
            callback(d)

    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("project_id 不能为空")
    rid = _safe_run_id(run_id)

    media_seg, proj_dir = ensure_project_media_dir(pid)
    run_dir = proj_dir / "storyboard_runs" / rid
    json_path = run_dir / "storyboard.json"
    if not json_path.is_file():
        raise FileNotFoundError(f"未找到分镜数据: {json_path}")

    raw = json.loads(json_path.read_text(encoding="utf-8"))
    shots: list[dict[str, Any]] = raw.get("shots") or []
    if not isinstance(shots, list) or not shots:
        raise ValueError("storyboard.json 中缺少 shots")

    product_desc = raw.get("product_desc") if isinstance(raw.get("product_desc"), dict) else {}
    cv = product_desc.get("character_visual")
    cv = cv if isinstance(cv, dict) else {}
    ce = str(cv.get("character_prompt_en") or product_desc.get("character_prompt_en") or "").strip()
    char_descs = {"char_001": ce} if ce else None
    fill_generated_prompts(shots, product_desc, char_descs)

    emit({"type": "video_run_start", "run_id": rid, "project_id": pid, "shot_total": len(shots)})

    mr = str(_MEDIA_ROOT.resolve())
    os.environ.setdefault("MEDIA_ROOT", mr)

    hub = LocalWanArkHub(MediaGenerationRequestClient.from_environ(), media_root=mr)
    session: dict[str, Any] = {}
    timeout = int(
        poll_timeout_sec
        if poll_timeout_sec is not None
        else int(os.getenv("STORYBOARD_VIDEO_POLL_TIMEOUT_SEC", "900") or "900")
    )

    clips_dir = run_dir / "video_clips"
    clip_paths: list[Path] = []

    for i, shot in enumerate(shots):
        sid = str(shot.get("shot_id") or f"shot_{i + 1}")
        fr = shot.get("frame") or {}
        img_u = (fr.get("image_url") or "").strip()
        if not img_u:
            raise ValueError(f"镜头 {sid} 缺少 frame.image_url，请先完成分镜出图")

        gp = shot.get("generated_prompts") or {}
        vprompt = (gp.get("video_prompt") or "").strip() or build_video_prompt(shot)
        tc = shot.get("timecode") or {}
        dur_raw = tc.get("duration_sec", 5)
        timeline_sec = _timeline_clip_duration_sec(dur_raw)
        ark_duration_sec = _coerce_clip_duration_sec(dur_raw)

        mv = (shot.get("movement") or {}).get("type", "固定")
        mv_s = str(mv or "固定").strip()
        camera_en = _MOVEMENT_CAMERA_EN.get(mv_s, _MOVEMENT_CAMERA_EN["固定"])

        emit(
            {
                "type": "step",
                "id": "shot_video",
                "label": (
                    f"图生视频 {i + 1}/{len(shots)} · {sid}"
                    f"（分镜 {timeline_sec}s / 方舟 {ark_duration_sec}s）"
                ),
                "shot_id": sid,
                "index": i + 1,
                "total": len(shots),
            }
        )

        payload = run_generate_video(
            hub,
            session,
            vprompt,
            source_image=img_u,
            duration=ark_duration_sec,
            camera_move=camera_en,
            video_title=f"sb_{rid}_{sid}",
            wait_timeout_seconds=timeout,
            submit_video_kwargs={"ratio": "9:16"},
        )
        out = json.loads(payload)
        st = str(out.get("status") or "")
        if st != "success":
            raise RuntimeError(
                out.get("error_message") or f"镜头 {sid} 视频生成失败: {out!r}"
            )
        urls = out.get("created_asset_ids") or []
        if not urls or not isinstance(urls, list):
            raise RuntimeError(f"镜头 {sid} 未返回视频 URL")
        remote = str(urls[0]).strip()
        if not remote.startswith("http"):
            raise RuntimeError(f"镜头 {sid} 视频地址异常: {remote[:200]!r}")

        safe_sid = re.sub(r"[^A-Za-z0-9._-]+", "_", sid) or "shot"
        clip_file = clips_dir / f"{i + 1:02d}_{safe_sid}.mp4"
        _download_video_to_run_dir(pid, remote, clip_file)
        if not clip_file.is_file() or clip_file.stat().st_size < 32:
            raise RuntimeError(f"镜头 {sid} 视频落盘失败")

        norm_file = clip_file.with_name(f"{clip_file.stem}_norm.mp4")
        _ffmpeg_normalize_clip_to_duration(clip_file, norm_file, timeline_sec)
        clip_paths.append(norm_file)

        rel = f"/media/{media_seg}/storyboard_runs/{rid}/video_clips/{norm_file.name}"
        emit(
            {
                "type": "shot_video_done",
                "index": i + 1,
                "total": len(shots),
                "shot_id": sid,
                "clip_uri": rel,
            }
        )

    emit({"type": "step", "id": "concat", "label": "ffmpeg 拼接成片"})

    final_name = "storyboard_assembled.mp4"
    out_mp4 = run_dir / final_name
    _ffmpeg_concat(clip_paths, out_mp4)
    if not out_mp4.is_file():
        raise RuntimeError("拼接输出文件缺失")

    final_uri = f"/media/{media_seg}/storyboard_runs/{rid}/{final_name}"

    expected_total = sum(
        _timeline_clip_duration_sec((s.get("timecode") or {}).get("duration_sec", 5))
        for s in shots
    )
    ark_requested_total = sum(
        _coerce_clip_duration_sec((s.get("timecode") or {}).get("duration_sec", 5))
        for s in shots
    )
    assembled_probe = _ffprobe_duration_sec(out_mp4)
    meta_sidecar = {
        "run_id": rid,
        "project_id": pid,
        "clip_uris": [
            f"/media/{media_seg}/storyboard_runs/{rid}/video_clips/{p.name}"
            for p in clip_paths
        ],
        "assembled_uri": final_uri,
        "shot_count": len(shots),
        "expected_total_duration_sec": expected_total,
        "ark_duration_sum_sec": ark_requested_total,
        "assembled_duration_sec": round(assembled_probe, 2)
        if assembled_probe is not None
        else None,
    }
    (run_dir / "video_assembly.json").write_text(
        json.dumps(meta_sidecar, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    title = (f"分镜成片 · {rid[:8]}…")[:120]
    asset_id = insert_project_asset(
        pid,
        "video",
        "video",
        title,
        uri=final_uri,
        meta={
            "source": "storyboard_video_pipeline",
            "run_id": rid,
            "storyboard_json": f"/media/{media_seg}/storyboard_runs/{rid}/storyboard.json",
            **meta_sidecar,
        },
    )

    emit({"type": "step_done", "id": "concat", "detail": final_uri})
    done = {
        "type": "done",
        "run_id": rid,
        "final_video_uri": final_uri,
        "asset_id": asset_id,
        "shot_count": len(shots),
        "project_id": pid,
    }
    emit(done)
    return done


__all__ = ["run_storyboard_video_for_project", "_coerce_clip_duration_sec", "_safe_run_id"]
