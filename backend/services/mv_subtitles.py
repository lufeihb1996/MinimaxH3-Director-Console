"""Burn existing MV lyric timings; no new transcription or model loading."""
import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def lyric_srt(timeline_path):
    path = Path(timeline_path)
    if not path.is_file():
        raise RuntimeError("歌词时间轴不存在，请先分析歌曲")
    data = json.loads(path.read_text(encoding="utf-8"))
    cues = []
    duration = float(data.get("duration") or 0)
    def stamp(seconds):
        ms = round(seconds * 1000)
        return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"
    for line in data.get("lines", []):
        text = str(line.get("text") or "").strip()
        start, end = float(line.get("start", 0)), float(line.get("end", 0))
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start, end = max(0, start), min(end, duration) if duration > 0 else end
        if not text or round(end*1000) <= round(start*1000):
            continue
        # Preserve words and punctuation; prevent ASS override markup in source text.
        text = text.replace("{", "（").replace("}", "）")
        text = "\n".join(x.strip() for x in text.splitlines() if x.strip())
        cues.append((start, end, text))
    cues.sort(key=lambda x: x[0])
    if not cues:
        raise RuntimeError("歌词时间轴没有有效歌词，无法添加字幕")
    return "\n\n".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}" for i,(a,b,text) in enumerate(cues,1)) + "\n"


def burn_mv_subtitles(source, timeline_path, output, progress_cb=None):
    source, output = Path(source), Path(output)
    text = lyric_srt(timeline_path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到FFmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Relative ASCII subtitle filename avoids Windows drive/quote filter escaping.
    with tempfile.TemporaryDirectory(prefix="mv_subs_") as tmp:
        folder = Path(tmp)
        (folder / "lyrics.srt").write_text(text, encoding="utf-8")
        candidate = output.with_name(output.stem + "_subtitles_pending.mp4")
        style = "FontName=Microsoft YaHei,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=24"
        try:
            command = [ffmpeg,"-v","error","-y","-i",str(source.resolve()),
                "-map","0:v:0","-map","0:a:0?","-vf",f"subtitles=filename=lyrics.srt:force_style='{style}'",
                "-c:v","libx264","-preset","fast","-crf","18","-pix_fmt","yuv420p",
                "-color_range","tv","-colorspace","bt709","-color_primaries","bt709","-color_trc","iec61966-2-1",
                "-c:a","copy","-map_metadata","-1","-map_chapters","-1","-fflags","+bitexact","-flags:v","+bitexact","-flags:a","+bitexact",
                "-movflags","+faststart","-progress",str(folder/"progress.txt"),str(candidate.resolve())]
            probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(source.resolve())], capture_output=True, timeout=30)
            duration = max(.001, float(probe.stdout.strip() or 0))
            with (folder/"error.log").open("wb") as errors:
                proc = subprocess.Popen(command, cwd=folder, stdout=subprocess.DEVNULL, stderr=errors)
                started = time.monotonic()
                try:
                    while proc.poll() is None:
                        if time.monotonic()-started > 1800:
                            raise RuntimeError("歌词字幕压制超时")
                        progress_file = folder/"progress.txt"
                        if progress_cb and progress_file.exists():
                            values = [line.split("=",1)[1] for line in progress_file.read_text(errors="ignore").splitlines() if line.startswith("out_time_us=")]
                            if values:
                                try: progress_cb(min(99, max(0, int(values[-1])/1000000/duration*100)))
                                except ValueError: pass
                        time.sleep(.3)
                finally:
                    if proc.poll() is None: proc.kill()
                    proc.wait()
            if proc.returncode or not candidate.is_file():
                raise RuntimeError("歌词字幕压制失败：" + (folder/"error.log").read_text(errors="replace")[-1600:])
            output.with_suffix(".srt").write_text(text,encoding="utf-8")
            candidate.replace(output)
        finally:
            candidate.unlink(missing_ok=True)
    return output
