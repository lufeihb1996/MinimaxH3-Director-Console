"""场景与全剧合并：镜头时长适配、CONTINUE 接缝平滑、场景成片缓存、全剧配原歌与字幕。"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from backend.core.db import db
from backend.core.media import concat_videos, conform_video_for_timeline, media_duration_seconds, media_has_audio, media_video_info, mux_mv_audio, normalize_scene_masters_for_project
from backend.core.ns import namespace
from backend.core.paths import PROJECTS_DIR
from backend.services.continuation import trim_for_continuity
from backend.services.mv_subtitles import burn_mv_subtitles, lyric_srt
from backend.services.native_audio import native_snapshot
from backend.services.repo import ensure_shot_dir, get_shot, previous_shot


def preferred_shot_video(
    row: sqlite3.Row | dict[str, Any],
    include_lipsync: bool = True,
) -> Path | None:
    """最终合并优先对嘴版，其次视频美化版，最后原始生成视频。"""
    row_keys = set(row.keys())
    use_lipsync = include_lipsync and (
        "lip_sync_enabled" not in row_keys or bool(int(row["lip_sync_enabled"] or 0))
    )
    keys = ("lip_sync_output_path", "face_refined_path", "output_path") if use_lipsync else ("face_refined_path", "output_path")
    for key in keys:
        raw = str(row[key] or "").strip()
        if raw:
            path = Path(raw)
            if path.is_file():
                return path
    return None


def _raw_timeline_ready_shot_video(row: sqlite3.Row | dict[str, Any]) -> Path | None:
    source = preferred_shot_video(row)
    if source is None:
        return None
    target_duration = float(row["target_duration"] or 0) if "target_duration" in row.keys() else 0.0
    snapshot = native_snapshot(dict(row))
    if snapshot:
        target_duration = float(snapshot.get("native_delivery_duration") or target_duration)
    if target_duration <= 0:
        return trim_for_continuity(row, source, namespace())
    source = trim_for_continuity(row, source, namespace())
    actual = media_duration_seconds(source)
    if actual > 0 and abs(actual - target_duration) <= 0.05:
        return source
    if "lip_sync_output_path" in row.keys() and str(row["lip_sync_output_path"] or "").strip():
        if actual > 0 and abs(actual - target_duration) <= 0.05:
            return source
    shot = get_shot(int(row["id"]))
    target = ensure_shot_dir(shot) / "timeline" / "conformed_25fps.mp4"
    source_has_audio = media_has_audio(source)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        actual = media_duration_seconds(target)
        target_audio_ok = (not source_has_audio) or media_has_audio(target)
        if actual > 0 and abs(actual - target_duration) <= 0.06 and target_audio_ok:
            return target
    return conform_video_for_timeline(
        source,
        target,
        target_duration,
        str(row["conform_mode"] or "auto") if "conform_mode" in row.keys() else "auto",
        25,
        strict_singing=bool(int(row["lip_sync_enabled"] or 0)) if "lip_sync_enabled" in row.keys() else False,
    )


def smooth_continue_shot_video(row: sqlite3.Row | dict[str, Any], ready_path: Path) -> Path:
    """Smooth CONTINUE boundary color/luminance drift and duplicate-pose stutter using vectorized NumPy float32 math + FFmpeg."""
    try:
        shot = get_shot(int(row["id"]))
        if str(shot.get("mode") or "").upper() != "CONTINUE":
            return ready_path
        prev = previous_shot(shot)
        if (
            not prev
            or int(prev.get("scene_id") or 0) != int(shot.get("scene_id") or 0)
            or str(prev.get("status") or "").upper() != "DONE"
        ):
            return ready_path
        prev_video = _raw_timeline_ready_shot_video(prev)
        if prev_video is None or not prev_video.is_file() or not ready_path.is_file():
            return ready_path

        sig_raw = (
            f"v8|{prev_video}:{prev_video.stat().st_size}:{prev_video.stat().st_mtime_ns}"
            f"|{ready_path}:{ready_path.stat().st_size}:{ready_path.stat().st_mtime_ns}"
        )
        sig = hashlib.sha1(sig_raw.encode("utf-8")).hexdigest()[:16]
        smoothed_target = ensure_shot_dir(shot) / "continuity" / f"seam_smooth_{sig}.mp4"
        if smoothed_target.is_file() and smoothed_target.stat().st_size > 0:
            return smoothed_target

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return ready_path
        info = media_video_info(ready_path)
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        fps_str = str(info.get("avg_frame_rate") or "24/1")
        if w <= 0 or h <= 0 or w % 2 or h % 2:
            return ready_path

        import numpy as np
        from PIL import Image

        frame_size = w * h * 3 // 2
        y_len = w * h
        uv_w, uv_h = w // 2, h // 2
        uv_len = uv_w * uv_h

        prev_info = media_video_info(prev_video)
        prev_w = int(prev_info.get("width") or 0)
        prev_h = int(prev_info.get("height") or 0)
        prev_cmd = [ffmpeg, "-v", "error", "-sseof", "-0.30", "-i", str(prev_video)]
        if prev_w != w or prev_h != h:
            prev_cmd += ["-vf", f"scale={w}:{h},format=yuv420p"]
        prev_cmd += ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
        prev_raw = subprocess.check_output(prev_cmd, timeout=60)
        if len(prev_raw) < frame_size:
            return ready_path
        prev_last_bytes = prev_raw[-frame_size:]

        clip_raw = subprocess.check_output(
            [
                ffmpeg, "-v", "error", "-i", str(ready_path),
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-",
            ],
            timeout=120,
        )
        n_frames = len(clip_raw) // frame_size
        if n_frames < 4:
            return ready_path

        # Vectorized float32 matrix unpacking: shape (n_frames, frame_size)
        prev_arr = np.frombuffer(prev_last_bytes, dtype=np.uint8).astype(np.float32)
        curr_arr = np.frombuffer(clip_raw[:n_frames * frame_size], dtype=np.uint8).reshape(n_frames, frame_size).astype(np.float32)

        y_mae = float(np.mean(np.abs(prev_arr[:y_len] - curr_arr[0, :y_len])))
        if y_mae > 35.0:
            return ready_path

        # Drop duplicate Latent-Token-0 guide frame(s) at the start of CONTINUE shots:
        # Frame 0 is always pinned by MiniMaxH3AddGuide(frame_idx=0); if frame 1 is also frozen (<1.5 MAE to frame 0
        # while frame 1->2 has real motion), drop both frozen head frames so motion never stalls at the splice.
        if y_mae < 8.0 and n_frames >= 8:
            step_01 = float(np.mean(np.abs(curr_arr[1, :y_len] - curr_arr[0, :y_len])))
            step_12 = float(np.mean(np.abs(curr_arr[2, :y_len] - curr_arr[1, :y_len])))
            n_drop = 2 if (step_01 < 1.4 and step_12 > 2.5 * step_01) else 1
            curr_arr = np.concatenate([curr_arr[n_drop:], np.repeat(curr_arr[-1:], n_drop, axis=0)], axis=0)

        def _low_freq_delta(p_flat: np.ndarray, c_flat: np.ndarray, pw: int, ph: int) -> np.ndarray:
            diff_2d = (p_flat - c_flat).reshape(ph, pw)
            im = Image.fromarray(diff_2d, mode="F")
            low = im.resize((32, 18), resample=Image.Resampling.BILINEAR).resize((pw, ph), resample=Image.Resampling.BILINEAR)
            return np.asarray(low, dtype=np.float32).reshape(-1)

        delta_y = _low_freq_delta(prev_arr[:y_len], curr_arr[0, :y_len], w, h)
        delta_u = _low_freq_delta(prev_arr[y_len:y_len + uv_len], curr_arr[0, y_len:y_len + uv_len], uv_w, uv_h)
        delta_v = _low_freq_delta(prev_arr[y_len + uv_len:], curr_arr[0, y_len + uv_len:], uv_w, uv_h)
        delta_full = np.concatenate([delta_y, delta_u, delta_v])

        # Match low-frequency YUV exposure/color across the clip; never alpha-blend moving hands/lips
        weights = np.linspace(1.0, 0.75, n_frames, dtype=np.float32)[:, None]
        out_arr = curr_arr + weights * delta_full[None, :]
        if y_mae < 8.0:
            static_mask = (np.abs(prev_arr - out_arr[0]) < 3.5).astype(np.float32)
            out_arr[0] = static_mask * (0.25 * prev_arr + 0.75 * out_arr[0]) + (1.0 - static_mask) * out_arr[0]

        out_bytes = np.clip(np.rint(out_arr), 0, 255).astype(np.uint8).tobytes()

        color_range = str(info.get("color_range") or "tv")
        color_space = str(info.get("color_space") or "bt709")
        color_primaries = str(info.get("color_primaries") or "bt709")
        color_trc = str(info.get("color_transfer") or "iec61966-2-1")
        color_flags = [
            "-color_range", color_range,
            "-colorspace", color_space,
            "-color_primaries", color_primaries,
            "-color_trc", color_trc,
        ]

        smoothed_target.parent.mkdir(parents=True, exist_ok=True)
        raw_head_path = smoothed_target.with_suffix(".all.yuv")
        temp_out_path = smoothed_target.with_suffix(".tmp.mp4")
        try:
            raw_head_path.write_bytes(out_bytes)
            cmd = [
                ffmpeg, "-v", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "yuv420p",
                *color_flags,
                "-s", f"{w}x{h}", "-framerate", fps_str, "-i", str(raw_head_path),
                "-i", str(ready_path),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                *color_flags,
                "-c:a", "copy",
                "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
                "-movflags", "+faststart", str(temp_out_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode == 0 and temp_out_path.is_file() and temp_out_path.stat().st_size > 0:
                temp_out_path.replace(smoothed_target)
                return smoothed_target
        finally:
            raw_head_path.unlink(missing_ok=True)
            temp_out_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"[seam-smooth] skip shot {row['id'] if 'id' in row.keys() else '?'}: {exc}")
    return ready_path


def timeline_ready_shot_video(row: sqlite3.Row | dict[str, Any]) -> Path | None:
    """Prepare shot video for timeline merge and smooth CONTINUE head frames."""
    ready = _raw_timeline_ready_shot_video(row)
    if ready is None:
        return None
    return smooth_continue_shot_video(row, ready)


SCENE_CACHE_VERSION = "v8"


def _scene_inputs_signature(paths: list[Path]) -> str | None:
    try:
        raw = SCENE_CACHE_VERSION + "|" + "|".join(
            f"{p.resolve()}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in paths
        )
    except OSError:
        return None
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_scene_master(scene_id: int, *, completed_only: bool = False, summary: dict | None = None) -> Path:
    conn = db()
    scene = conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
    if not scene:
        conn.close()
        raise RuntimeError("Scene not found")
    rows = conn.execute(
        """
        SELECT id,shot_no,status,output_path,face_refined_path,lip_sync_output_path,
               target_duration,conform_mode,lip_sync_enabled,queued_input_path,completed_input_path
        FROM shots WHERE scene_id=? ORDER BY shot_no
        """,
        (scene_id,),
    ).fetchall()
    conn.close()
    if not rows:
        raise RuntimeError("当前 Scene 还没有 Shot")

    not_ready = [str(r["shot_no"]) for r in rows if r["status"] != "DONE" or preferred_shot_video(r) is None]
    if not_ready and not completed_only:
        raise RuntimeError("以下 Shot 尚未完成，不能合并: " + ", ".join(f"#{int(n):03d}" for n in not_ready))
    total_count = len(rows)
    if completed_only:
        rows = [r for r in rows if r["status"] == "DONE" and preferred_shot_video(r) is not None]
    if not rows:
        raise RuntimeError("当前 Scene 暂无已完成且视频文件可用的 Shot")

    project_dir = PROJECTS_DIR / f"project_{int(scene['project_id']):03d}"
    scene_dir = project_dir / f"scene_{scene_id:03d}"
    output = scene_dir / f"scene_{scene_id:03d}_master.mp4"
    prepared: list[Path] = []
    for row in rows:
        path = timeline_ready_shot_video(row)
        if path is None:
            raise RuntimeError(f"Shot #{int(row['shot_no']):03d} 没有可合并视频")
        prepared.append(path)
    # 场景成片缓存：输入镜头文件（路径/大小/修改时间）都没变就直接复用上次成片。
    scene_sig = _scene_inputs_signature(prepared)
    sig_file = output.with_suffix(".mp4.sig")
    cached = (
        scene_sig is not None
        and output.is_file() and output.stat().st_size > 0
        and sig_file.is_file() and sig_file.read_text(encoding="utf-8").strip() == scene_sig
        and (globals().get("media_video_info") is None or bool(media_video_info(output).get("pix_fmt")))
    )
    if cached:
        result = output
    else:
        result = concat_videos(prepared, output)
        if scene_sig is not None and Path(result).is_file():
            sig_file.write_text(scene_sig, encoding="utf-8")
    if summary is not None:
        summary.update(merged_count=len(prepared), skipped_count=total_count - len(prepared))
    return result


def build_project_master(project_id: int, progress_cb=None) -> Path:
    report = progress_cb or (lambda pct, stage: None)
    report(1, "Preparing merge")
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        conn.close()
        raise RuntimeError("Project not found")
    scenes = conn.execute(
        "SELECT id FROM scenes WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    ).fetchall()
    audio_config = conn.execute(
        "SELECT * FROM mv_projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    conn.close()
    if not scenes:
        raise RuntimeError("当前 Project 还没有 Scene")

    subtitles = bool(audio_config and int(audio_config["native_audio_enabled"] or 0))
    if subtitles:
        if not audio_config or not int(audio_config["audio_linked"] or 0) or audio_config["analysis_status"] != "DONE":
            raise RuntimeError("歌词字幕需要关联歌曲并完成歌曲分析")
        lyric_srt(audio_config["lyrics_timeline_path"])

    scene_masters = []
    for index, scene in enumerate(scenes):
        report(3 + 27*index/len(scenes), f"合并场景 {index+1}/{len(scenes)}")
        scene_masters.append(build_scene_master(int(scene["id"])))
    report(30, "统一各场景画面规格")
    project_dir = PROJECTS_DIR / f"project_{project_id:03d}"
    output = project_dir / f"project_{project_id:03d}_master.mp4"
    final_output = output
    if subtitles:
        output = project_dir / f"project_{project_id:03d}_master_no_subtitles.mp4"
    audio_linked = bool(audio_config and int(audio_config["audio_linked"] or 0))

    # 不同视频模型可能产出不同分辨率 / FPS / time_base。
    # 先统一 Scene 规格，再做最终 stream-copy concat，避免视频流被截短。
    normalized_scene_masters = normalize_scene_masters_for_project(
        scene_masters,
        project_dir,
        keep_audio=not audio_linked,
    )

    report(60, "拼接全剧画面并配入原歌")
    if audio_linked:
        audio_path = Path(str(audio_config["audio_path"] or ""))
        if not audio_path.is_file():
            raise RuntimeError("已开启歌曲关联，但歌曲尚未生成")
        video_only = project_dir / f"project_{project_id:03d}_video_only.mp4"
        concat_videos(normalized_scene_masters, video_only)
        try:
            mux_mv_audio(video_only, audio_path, output)
        finally:
            video_only.unlink(missing_ok=True)
    else:
        concat_videos(normalized_scene_masters, output)
    if subtitles:
        report(70, "Burning in lyric subtitles")
        result = burn_mv_subtitles(output, audio_config["lyrics_timeline_path"], final_output,
            progress_cb=lambda pct: report(70 + pct*.29, "Burning in lyric subtitles"))
    else:
        final_output.with_suffix(".srt").unlink(missing_ok=True)
        result = output
    report(100, "Merge complete")
    return result
