"""Song-locked H3 graph wiring. Uses native ComfyUI nodes, no extra model."""
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path

from backend.models.video import video_model

REQUIRED_NODES = ["LoadAudio", "VAEEncodeAudio", "SolidMask", "SetLatentNoiseMask", "LTXVConcatAVLatent"]


def native_snapshot(shot):
    if shot.get("native_song_audio"):
        return shot
    for key in ("queued_input_path", "completed_input_path"):
        path = Path(str(shot.get(key) or ""))
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8")).get("shot", {})
            return data if data.get("native_song_audio") else None
    return None


def freeze_song_audio(shot, folder, ns):
    conn = ns["db"]()
    try:
        row = conn.execute("SELECT mp.* FROM mv_projects mp JOIN scenes sc ON sc.project_id=mp.project_id WHERE sc.id=?", (shot["scene_id"],)).fetchone()
    finally:
        conn.close()
    if not row or not row["native_audio_enabled"]:
        return {}
    model = video_model(shot.get("video_model"))
    if not model.get("supports_audio_lock"):
        raise RuntimeError(f"{model['label']} 不支持 MV 原声驱动（config/models.json 的 supports_audio_lock）")
    start, end = float(shot.get("timeline_start") or 0), float(shot.get("timeline_end") or 0)
    if end <= start or row["analysis_status"] != "DONE":
        raise RuntimeError("原声驱动需要先完成歌曲分析并写入 Shot 起止时间")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found")
    fps = int(ns["get_settings"]().get("fps") or 24)
    deliv_start = round(start * 25) / 25
    deliv_end = round(end * 25) / 25
    deliv_dur = max(0.04, round(deliv_end - deliv_start, 6))
    duration = ns["frame_count"](math.ceil(deliv_dur*fps)/fps, fps)/fps
    result = {"native_song_audio": True, "native_audio_mux": "local", "native_target_duration": end-start, "native_render_duration": duration, "native_delivery_duration": deliv_dur}
    for field, source_field in (("native_vocals_path", "vocals_path"), ("native_mix_path", "audio_path")):
        source = Path(str(row[source_field] or ""))
        if not source.is_file():
            raise RuntimeError("原声驱动素材不存在："+source_field)
        target = folder / (field+".wav")
        slice_dur = max(end - start, deliv_dur)
        cmd = [ffmpeg,"-v","error","-y","-ss",f"{deliv_start:.6f}","-i",str(source),
               "-af",f"atrim=duration={slice_dur:.8f},apad,atrim=duration={duration:.8f}","-ac","2","-ar","32000","-c:a","pcm_s16le",str(target)]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode or not target.is_file():
            raise RuntimeError("歌曲片段截取失败："+proc.stderr.decode(errors="replace")[-1000:])
        hashed = folder / (field + chr(95) + hashlib.md5(target.read_bytes()).hexdigest()[:8] + chr(46) + 'wav')
        target.replace(hashed)
        result[field] = str(hashed)
    return result


def wire_native_audio(workflow, voice):
    if not all(key in workflow for key in ("125", "120", "130")):
        raise RuntimeError("原声驱动需要完整 b15–49 两轮工作流")
    # The first latent may already contain AV streams. Native Concat swaps just audio.
    original = workflow["125"]["inputs"]["latent_image"]
    workflow.update({
        "mv_voice": {"class_type":"LoadAudio","inputs":{"audio":voice}},
        "mv_encode": {"class_type":"VAEEncodeAudio","inputs":{"audio":["mv_voice",0],"vae":["120",0]}},
        "mv_zero": {"class_type":"SolidMask","inputs":{"value":0.0,"width":1,"height":1}},
        "mv_lock": {"class_type":"SetLatentNoiseMask","inputs":{"samples":["mv_encode",0],"mask":["mv_zero",0]}},
        "mv_initial": {"class_type":"LTXVConcatAVLatent","inputs":{"video_latent":original,"audio_latent":["mv_lock",0]}},
    })
    workflow["125"]["inputs"]["latent_image"] = ["mv_initial",0]
    # Reinject the same protected source after spatial upscale; never sample a new song.
    if "b15_concat" in workflow:
        workflow["b15_concat"]["inputs"]["audio_latent"] = ["mv_lock",0]
    # Cloud renders picture only; the frozen full mix never leaves Windows.
    workflow["130"]["inputs"].pop("audio", None)
    workflow.pop("121",None)


def finish_native_video(source: Path, snapshot: dict) -> Path:
    """Trim picture and attach frozen original song locally, never layering cloud audio."""
    mix = Path(str(snapshot.get("native_mix_path") or ""))
    if not mix.is_file():
        raise RuntimeError("本地原歌片段缺失，无法完成视频配音；原始下载视频已保留")
    duration = float(snapshot.get("native_delivery_duration") or snapshot["native_target_duration"])
    if duration <= 0:
        raise RuntimeError("本地配音目标时长无效")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("本地配音需要 FFmpeg")
    target = source.with_name(source.stem + "_localmix.mp4")
    cmd = [ffmpeg,"-v","error","-y","-i",str(source),"-i",str(mix),
           "-map","0:v:0","-map","1:a:0",
           "-vf",f"fps=25,trim=duration={duration:.8f},setpts=PTS-STARTPTS",
           "-af",f"apad,atrim=duration={duration:.8f},asetpts=PTS-STARTPTS",
           "-t",f"{duration:.8f}","-c:v","libx264","-preset","fast","-crf","18",
           "-pix_fmt","yuv420p","-color_range","tv","-colorspace","bt709","-color_primaries","bt709","-color_trc","iec61966-2-1",
           "-c:a","aac","-b:a","192k","-ar","48000","-ac","2",
           "-map_metadata","-1","-map_chapters","-1","-fflags","+bitexact","-flags:v","+bitexact","-flags:a","+bitexact",
           "-movflags","+faststart",str(target)]
    proc = subprocess.run(cmd,capture_output=True,timeout=300)
    if proc.returncode or not target.is_file() or not target.stat().st_size:
        raise RuntimeError("本地原歌配音失败，已保留下载视频："+proc.stderr.decode(errors="replace")[-1500:])
    target.replace(source)
    return source
