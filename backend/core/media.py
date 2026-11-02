"""本地 FFmpeg/FFprobe 媒体工具：探测、拼接、规格统一、配音、时长适配。"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

CLEAN_METADATA_ARGS = [
    "-map_metadata", "-1",
    "-map_chapters", "-1",
    "-fflags", "+bitexact",
    "-flags:v", "+bitexact",
    "-flags:a", "+bitexact",
]


def strip_video_metadata(path: Path) -> Path:
    """无损秒级剥离视频容器内的 ComfyUI prompt JSON 与 Lavf 编码器标签。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.is_file():
        return path
    temp_out = path.with_name(f"{path.stem}.clean_meta{path.suffix}")
    cmd = [
        ffmpeg, "-v", "error", "-y", "-i", str(path),
        "-map", "0", "-c", "copy",
        *CLEAN_METADATA_ARGS,
        "-movflags", "+faststart", str(temp_out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 0:
            temp_out.replace(path)
    except Exception:
        pass
    finally:
        temp_out.unlink(missing_ok=True)
    return path


def dims_from_mp(megapixels: float, aspect_ratio: str) -> tuple[int, int]:
    try:
        a, b = aspect_ratio.split(":", 1)
        ratio = float(a) / float(b)
    except Exception:
        ratio = 9 / 16
    area = max(0.05, float(megapixels)) * 1024 * 1024
    height = math.sqrt(area / ratio)
    width = height * ratio
    width = max(64, int(round(width / 32) * 32))
    height = max(64, int(round(height / 32) * 32))
    return width, height


def frame_count(seconds: float, fps: int = 24) -> int:
    # MiniMax-H3 官方示例按 17k+5 对齐帧数；5.0s@24fps 会取 124 帧（约 5.17s）。
    raw = max(5, int(round(seconds * fps)))
    return raw + (5 - (raw % 17)) % 17


def extract_last_frame(video_path: Path, out_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_path.exists():
        return False
    cmd = [ffmpeg, "-y", "-sseof", "-0.15", "-i", str(video_path), "-frames:v", "1", str(out_path)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)
        return out_path.exists()
    except Exception:
        return False


def stabilize_continue_video(video_path: Path, anchor_frame_path: Path | str | None) -> Path:
    """基于 Reddit GeN13/KJNodes 静态首帧锚点色彩矩阵对齐（零 cv2 依赖，纯 NumPy+PIL+FFmpeg），消除跨镜色偏与首帧闪跳。"""
    if not anchor_frame_path or not video_path.is_file():
        return video_path
    anchor_path = Path(anchor_frame_path)
    if not anchor_path.is_file():
        return video_path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return video_path
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return video_path

    info = media_video_info(video_path)
    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    fps = _rate_to_float(info.get("avg_frame_rate")) or 24.0
    if w <= 0 or h <= 0:
        return video_path

    try:
        anchor_img = Image.open(anchor_path).convert("RGB")
        if anchor_img.size != (w, h):
            anchor_img = anchor_img.resize((w, h), Image.Resampling.LANCZOS)
        anchor = np.asarray(anchor_img, dtype=np.float32)

        raw = subprocess.check_output(
            [ffmpeg, "-v", "error", "-i", str(video_path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            timeout=120,
        )
        frame_bytes = w * h * 3
        n_frames = len(raw) // frame_bytes
        if n_frames < 4:
            return video_path
        frames = np.frombuffer(raw[: n_frames * frame_bytes], dtype=np.uint8).reshape(n_frames, h, w, 3).astype(np.float32)

        # 1. Reddit GeN13 / KJNodes Static Anchor Color Match:
        #    比较同姿态的 anchor（上一镜末帧）与 frames[1]（当前镜第 1 帧），计算静态线性色彩变换矩阵 (gain, bias)，
        #    并统一应用到全片所有帧，避免因人物后续大幅度动作改变前景占比而引发亮度呼吸闪烁。
        ref_frame = frames[1]
        mask = (anchor.mean(axis=2) > 12.0) & (ref_frame.mean(axis=2) > 12.0)
        if int(mask.sum()) < 256:
            mask = np.ones((h, w), dtype=bool)

        for c in range(3):
            a_vals = anchor[:, :, c][mask]
            f_vals = ref_frame[:, :, c][mask]
            m_a, s_a = float(a_vals.mean()), float(a_vals.std()) + 1e-5
            m_f, s_f = float(f_vals.mean()), float(f_vals.std()) + 1e-5
            gain = float(np.clip(s_a / s_f, 0.80, 1.25))
            frames[:, :, :, c] = (frames[:, :, :, c] - m_f) * gain + m_a

        # 2. 剔除被一采/二采 Guide 污染的第 0 帧，用同姿态 anchor 与对齐后的第 1、2 帧做无缝桥接，彻底消除首帧闪跳
        f1_clean = frames[1].copy()
        f2_clean = frames[2].copy()
        frames[0] = 0.70 * anchor + 0.30 * f1_clean
        frames[1] = 0.30 * anchor + 0.70 * f1_clean
        frames[2] = 0.15 * f1_clean + 0.85 * f2_clean

        out_bytes = np.clip(np.rint(frames), 0, 255).astype(np.uint8).tobytes()
        temp_out = video_path.with_name(f"{video_path.stem}.cont_stab{video_path.suffix}")
        cmd = [
            ffmpeg, "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:.6f}", "-i", "-",
            "-i", str(video_path),
            "-map", "0:v:0", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
            "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "iec61966-2-1",
            "-c:a", "copy",
            *CLEAN_METADATA_ARGS,
            "-movflags", "+faststart", str(temp_out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert proc.stdin is not None
        proc.stdin.write(out_bytes)
        proc.stdin.close()
        proc.wait(timeout=180)
        if proc.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 0:
            temp_out.replace(video_path)
    except Exception as exc:
        print(f"[stabilize_continue_video] warning: {exc}")
    finally:
        video_path.with_name(f"{video_path.stem}.cont_stab{video_path.suffix}").unlink(missing_ok=True)
    return video_path


def concat_videos(video_paths: list[Path], output_path: Path) -> Path:
    """Concatenate clips seamlessly using continuous PTS, sample-accurate audio trimming, and atomic temp-file replacement."""
    import os
    import uuid

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("\u672a\u627e\u5230 FFmpeg\uff0c\u8bf7\u5148\u5b89\u88c5 FFmpeg \u5e76\u52a0\u5165 PATH")
    if not video_paths:
        raise RuntimeError("\u6ca1\u6709\u53ef\u5408\u5e76\u7684\u89c6\u9891")

    missing = [str(p) for p in video_paths if not p.exists()]
    if missing:
        raise RuntimeError("\u4ee5\u4e0b\u89c6\u9891\u6587\u4ef6\u4e0d\u5b58\u5728: " + "; ".join(missing))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    uid = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
    temp_out = output_path.with_name(f"{output_path.stem}.tmp.{uid}{output_path.suffix}")

    clip_infos: list[tuple[int, int, float, bool, int]] = []
    for p in video_paths:
        info = media_video_info(p)
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        v_dur = float(info.get("duration") or 0.0) or media_duration_seconds(p)
        has_a = media_has_audio(p)
        fps_val = round(_rate_to_float(info.get("avg_frame_rate")) or 24)
        clip_infos.append((w, h, v_dur, has_a, fps_val if fps_val in (24, 25, 30) else 24))

    try:
        valid_dims = [(w, h) for w, h, _, _, _ in clip_infos if w > 0 and h > 0]
        if valid_dims:
            target_w, target_h = max(valid_dims, key=lambda item: item[0] * item[1])
            if target_w % 2:
                target_w += 1
            if target_h % 2:
                target_h += 1
            fps_set = {fps_v for _, _, _, _, fps_v in clip_infos}
            target_fps = next(iter(fps_set)) if len(fps_set) == 1 else 25
            any_audio = any(has_a for _, _, _, has_a, _ in clip_infos)
            inputs: list[str] = []
            filter_parts: list[str] = []
            concat_pairs: list[str] = []
            for idx, (p, (w, h, v_dur, has_a, _)) in enumerate(zip(video_paths, clip_infos)):
                inputs.extend(["-i", str(p)])
                if w == target_w and h == target_h:
                    v_chain = (
                        f"[{idx}:v:0]fps={target_fps},format=yuv420p,"
                        f"setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=iec61966-2-1,"
                        f"setpts=PTS-STARTPTS[v{idx}]"
                    )
                else:
                    v_chain = (
                        f"[{idx}:v:0]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                        f"fps={target_fps},format=yuv420p,"
                        f"setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=iec61966-2-1,"
                        f"setpts=PTS-STARTPTS[v{idx}]"
                    )
                filter_parts.append(v_chain)
                if any_audio:
                    clip_dur = max(0.04, v_dur)
                    if has_a:
                        a_chain = (
                            f"[{idx}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                            f"apad,atrim=duration={clip_dur:.6f},asetpts=PTS-STARTPTS[a{idx}]"
                        )
                    else:
                        a_chain = f"anullsrc=r=48000:cl=stereo,atrim=duration={clip_dur:.6f},asetpts=PTS-STARTPTS[a{idx}]"
                    filter_parts.append(a_chain)
                    concat_pairs.append(f"[v{idx}][a{idx}]")
                else:
                    concat_pairs.append(f"[v{idx}]")
            color_args = [
                "-color_range", "tv", "-colorspace", "bt709",
                "-color_primaries", "bt709", "-color_trc", "iec61966-2-1",
            ]
            if any_audio:
                filter_parts.append("".join(concat_pairs) + f"concat=n={len(video_paths)}:v=1:a=1[vout][aout]")
                map_args = [
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                    *color_args,
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                ]
            else:
                filter_parts.append("".join(concat_pairs) + f"concat=n={len(video_paths)}:v=1:a=0[vout]")
                map_args = [
                    "-map", "[vout]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                    *color_args,
                    "-an",
                ]
            filter_cmd = [
                ffmpeg, "-v", "error", "-y",
                *inputs,
                "-filter_complex", ";".join(filter_parts),
                *map_args,
                *CLEAN_METADATA_ARGS,
                "-movflags", "+faststart", str(temp_out),
            ]
            proc = subprocess.run(filter_cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 0 and media_video_info(temp_out).get("pix_fmt"):
                temp_out.replace(output_path)
                return output_path

        concat_list = output_path.with_name(f"{output_path.stem}.concat.{uid}.txt")
        lines = []
        for p in video_paths:
            safe = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

        encode_cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(temp_out),
        ]
        copy_cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(temp_out),
        ]
        try:
            first = subprocess.run(encode_cmd, capture_output=True, text=True, timeout=3600)
            if first.returncode != 0 or not temp_out.is_file() or temp_out.stat().st_size == 0:
                second = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=3600)
                if second.returncode != 0 or not temp_out.is_file() or temp_out.stat().st_size == 0:
                    detail = (first.stderr or second.stderr or "FFmpeg \u5408\u5e76\u5931\u8d25")[-3000:]
                    raise RuntimeError(f"FFmpeg \u5408\u5e76\u5931\u8d25: {detail}")
            temp_out.replace(output_path)
            return output_path
        finally:
            concat_list.unlink(missing_ok=True)
    finally:
        temp_out.unlink(missing_ok=True)


def media_duration_seconds(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return 0.0
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return max(0.0, float((proc.stdout or "0").strip() or 0))
    except Exception:
        return 0.0


def media_has_audio(path: Path) -> bool:
    """Return True when a media file contains at least one real audio stream."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return bool((proc.stdout or "").strip())
    except Exception:
        return False


def media_video_info(path: Path) -> dict[str, Any]:
    """读取首个视频流的分辨率、帧率、编码和时间基。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return {}
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height,pix_fmt,color_range,color_space,color_transfer,color_primaries,avg_frame_rate,time_base,duration",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams") or []
        return streams[0] if streams else {}
    except Exception:
        return {}


def _rate_to_float(value: Any) -> float:
    text = str(value or "0").strip()
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_value = float(den or 0)
            return float(num or 0) / den_value if den_value else 0.0
        return float(text or 0)
    except Exception:
        return 0.0


@lru_cache(maxsize=16)
def ffmpeg_has_encoder(name: str) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        if name.casefold() not in (proc.stdout or "").casefold():
            return False
        if name == "h264_nvenc":
            probe = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                "color=s=64x64:r=25", "-frames:v", "1", "-c:v", name, "-f", "null", "-"],
                capture_output=True, timeout=30)
            return probe.returncode == 0
        return True
    except Exception:
        return False


def normalize_scene_master_for_project(
    source: Path,
    target: Path,
    width: int,
    height: int,
    fps: int = 25,
    keep_audio: bool = False,
) -> Path:
    """整集合并前统一一个 Scene 的视频规格，避免直接流拷贝导致时间戳异常。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found. Install it and add it to PATH")
    if not source.is_file():
        raise RuntimeError(f"Scene 成片不存在: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    expected_fps = float(fps)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        info = media_video_info(target)
        target_audio_ok = (not keep_audio) or media_has_audio(target)
        if (
            int(info.get("width") or 0) == int(width)
            and int(info.get("height") or 0) == int(height)
            and abs(_rate_to_float(info.get("avg_frame_rate")) - expected_fps) <= 0.01
            and str(info.get("codec_name") or "").casefold() == "h264"
            and str(info.get("pix_fmt") or "").casefold() == "yuv420p"
            and target_audio_ok
        ):
            return target

    # Matching picture specs need only container/time-base normalization, not encoding.
    source_info = media_video_info(source)
    picture_matches = (
        int(source_info.get("width") or 0) == int(width)
        and int(source_info.get("height") or 0) == int(height)
        and abs(_rate_to_float(source_info.get("avg_frame_rate")) - expected_fps) <= .01
        and str(source_info.get("codec_name") or "").lower() == "h264"
        and str(source_info.get("pix_fmt") or "").lower() == "yuv420p"
    )
    # Original audio in ordinary videos can vary, so retain existing audio conversion there.
    if picture_matches and not keep_audio:
        copy_target = target.with_name(target.stem + "_copy_pending.mp4")
        try:
            copy = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(source),
                "-map", "0:v:0", "-c:v", "copy", "-an", "-video_track_timescale", "90000",
                *CLEAN_METADATA_ARGS,
                "-movflags", "+faststart", str(copy_target)], capture_output=True, text=True, timeout=3600)
            copied_info = media_video_info(copy_target)
            before, after = media_duration_seconds(source), media_duration_seconds(copy_target)
            if (copy.returncode == 0 and copy_target.is_file()
                and int(copied_info.get("width") or 0) == width
                and int(copied_info.get("height") or 0) == height
                and abs(_rate_to_float(copied_info.get("avg_frame_rate")) - expected_fps) <= .01
                and before > 0 and abs(before-after) <= .06):
                copy_target.replace(target)
                return target
        finally:
            copy_target.unlink(missing_ok=True)

    vf = (
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={int(fps)},format=yuv420p,setpts=PTS-STARTPTS"
    )

    source_has_audio = media_has_audio(source)

    def build_cmd(video_codec_args: list[str]) -> list[str]:
        if keep_audio and not source_has_audio:
            duration = max(0.05, media_duration_seconds(source))
            return [
                ffmpeg, "-y", "-i", str(source),
                "-f", "lavfi", "-t", f"{duration:.6f}", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0",
                "-vf", vf,
                *video_codec_args,
                "-pix_fmt", "yuv420p",
                "-video_track_timescale", "90000",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-shortest", *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(target),
            ]

        cmd = [
            ffmpeg, "-y", "-i", str(source),
            "-map", "0:v:0",
            "-vf", vf,
            *video_codec_args,
            "-pix_fmt", "yuv420p",
            "-video_track_timescale", "90000",
        ]
        if keep_audio:
            cmd += [
                "-map", "0:a:0?",
                "-af", "aresample=48000:async=1:first_pts=0",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            ]
        else:
            cmd += ["-an"]
        cmd += [*CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(target)]
        return cmd

    attempts: list[tuple[str, list[str]]] = []
    if ffmpeg_has_encoder("h264_nvenc"):
        attempts.append(
            ("NVENC", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-b:v", "0"])
        )
    attempts.append(("libx264", ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]))

    errors: list[str] = []
    for encoder_name, encoder_args in attempts:
        proc = subprocess.run(build_cmd(encoder_args), capture_output=True, text=True, timeout=3600)
        if proc.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            info = media_video_info(target)
            if (
                int(info.get("width") or 0) == int(width)
                and int(info.get("height") or 0) == int(height)
                and abs(_rate_to_float(info.get("avg_frame_rate")) - expected_fps) <= 0.01
            ):
                return target
        if proc.returncode == 0:
            errors.append(f"{encoder_name}: 输出规格校验失败，期望 {width}x{height}/{fps}fps，实际 {media_video_info(target)}；文件 {target}")
        else:
            errors.append(f"{encoder_name}: {(proc.stderr or 'unknown error')[-1200:]}")
        target.unlink(missing_ok=True)

    raise RuntimeError("Scene 合并前规格统一失败:\n" + "\n".join(errors))


def normalize_scene_masters_for_project(
    scene_masters: list[Path],
    project_dir: Path,
    keep_audio: bool = False,
) -> list[Path]:
    """把所有 Scene master 统一到同一画布、25fps、H.264、yuv420p。"""
    if not scene_masters:
        return []

    infos = [(path, media_video_info(path)) for path in scene_masters]
    valid = [
        (path, info)
        for path, info in infos
        if int(info.get("width") or 0) > 0 and int(info.get("height") or 0) > 0
    ]
    if not valid:
        raise RuntimeError("无法读取 Scene 成片的视频规格")

    _, best = max(
        valid,
        key=lambda item: int(item[1].get("width") or 0) * int(item[1].get("height") or 0),
    )
    width = int(best.get("width") or 0)
    height = int(best.get("height") or 0)
    if width % 2:
        width += 1
    if height % 2:
        height += 1
    fps = 25

    normalized_dir = project_dir / "_merge_normalized"
    normalized: list[Path] = []
    for index, source in enumerate(scene_masters, 1):
        target = normalized_dir / f"{index:03d}_{source.stem}_{width}x{height}_{fps}fps.mp4"
        normalized.append(
            normalize_scene_master_for_project(
                source,
                target,
                width=width,
                height=height,
                fps=fps,
                keep_audio=keep_audio,
            )
        )
    return normalized


def mux_mv_audio(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """以完整歌曲为主时钟：视频不足时定格末帧，视频过长时裁到歌曲长度。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found. Install it and add it to PATH")
    if not video_path.is_file():
        raise RuntimeError("MV temp video not found")
    if not audio_path.is_file():
        raise RuntimeError("MV 音频不存在，请先上传或生成歌曲")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audio_duration = media_duration_seconds(audio_path)
    video_duration = media_duration_seconds(video_path)
    if audio_duration <= 0:
        raise RuntimeError("Cannot read the full song duration")

    # 误差很小时可直接复用视频编码；否则必须整理视频长度，不能再用 -shortest 截断歌曲。
    if video_duration > 0 and abs(video_duration - audio_duration) <= 0.05:
        cmd = [
            ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{audio_duration:.6f}", *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(output_path),
        ]
    else:
        extra = max(0.0, audio_duration - max(0.0, video_duration))
        if extra > 0.03:
            vf = f"tpad=stop_mode=clone:stop_duration={extra + 0.08:.6f},trim=duration={audio_duration:.6f},setpts=PTS-STARTPTS"
        else:
            vf = f"trim=duration={audio_duration:.6f},setpts=PTS-STARTPTS"
        cmd = [
            ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{audio_duration:.6f}", *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(output_path),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("MV 音画合成失败: " + (proc.stderr or "")[-2500:])
    return output_path


def conform_video_for_timeline(
    source: Path,
    target: Path,
    target_duration: float,
    mode: str = "auto",
    fps: int = 25,
    strict_singing: bool = True,
) -> Path:
    """Prepare a Shot for final timeline merge while preserving its audio stream."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found")
    source_duration = media_duration_seconds(source)
    target_duration = float(target_duration)
    if source_duration <= 0 or target_duration <= 0:
        raise RuntimeError("Video source duration or song time slot is invalid")
    mode = str(mode or "auto").lower()
    if mode not in {"auto", "trim", "speed", "regenerate", "loop"}:
        raise RuntimeError("conform_mode must be auto / trim / speed / regenerate / loop")
    target.parent.mkdir(parents=True, exist_ok=True)

    ratio = target_duration / source_duration
    selected = mode
    if selected == "regenerate":
        raise RuntimeError(
            f"当前 Shot 源视频 {source_duration:.2f}s，目标 {target_duration:.2f}s；conform_mode=regenerate，请先重新生成合适时长的视频"
        )
    if selected == "auto":
        if source_duration + 0.03 < target_duration and ratio > 1.15:
            if strict_singing:
                raise RuntimeError(
                    f"唱歌 Shot 源视频只有 {source_duration:.2f}s，但歌曲时间槽需要 {target_duration:.2f}s，差异过大，请重新生成更长视频"
                )
            selected = "loop"
        else:
            selected = "speed" if abs(ratio - 1.0) <= 0.15 else ("trim" if source_duration > target_duration else "speed")

    has_audio = media_has_audio(source)
    common_video = [
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "iec61966-2-1",
    ]
    common_audio = ["-c:a", "aac", "-b:a", "192k"] if has_audio else []

    if selected == "loop":
        cmd = [
            ffmpeg, "-y", "-stream_loop", "-1", "-i", str(source),
            "-map", "0:v:0",
        ]
        if has_audio:
            cmd += ["-map", "0:a:0?"]
        cmd += [
            "-vf", f"fps={int(fps)}", "-t", f"{target_duration:.6f}",
            *common_video, *common_audio, *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(target),
        ]
    elif selected == "trim":
        if source_duration + 0.03 < target_duration:
            raise RuntimeError("trim cannot lengthen a short clip; use speed / loop / regenerate")
        cmd = [
            ffmpeg, "-y", "-i", str(source),
            "-map", "0:v:0",
        ]
        if has_audio:
            cmd += ["-map", "0:a:0?"]
        cmd += [
            "-vf", f"fps={int(fps)}", "-t", f"{target_duration:.6f}",
            *common_video, *common_audio, *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(target),
        ]
    else:
        cmd = [
            ffmpeg, "-y", "-i", str(source),
            "-map", "0:v:0",
        ]
        if has_audio:
            cmd += ["-map", "0:a:0?", "-filter:a", f"atempo={1.0 / ratio:.9f}"]
        cmd += [
            "-filter:v", f"setpts={ratio:.9f}*PTS,fps={int(fps)}",
            "-t", f"{target_duration:.6f}",
            *common_video, *common_audio, *CLEAN_METADATA_ARGS, "-movflags", "+faststart", str(target),
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("最终合并 Shot 时长适配失败: " + (proc.stderr or "")[-2500:])
    if has_audio and not media_has_audio(target):
        raise RuntimeError("最终合并 Shot 时长适配后音轨丢失")
    return target
