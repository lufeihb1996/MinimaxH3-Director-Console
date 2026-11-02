"""Run local audio models in the isolated D-drive interpreter, never in the API process."""
import asyncio
import json
import os
import subprocess
from pathlib import Path


async def run_local_analysis(audio: Path, lyrics: str, output: Path, progress_cb, *, dry_vocals: bool = False):
    home = Path((os.environ.get("ANIME_STUDIO_ANALYSIS_HOME") or "").strip() or (Path(__file__).resolve().parents[2] / "audio_analysis"))
    python = home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    worker = Path(__file__).resolve().parents[2] / "tools/local_audio_analysis.py"
    if not python.is_file():
        raise RuntimeError(f"本地歌曲分析环境不存在：{python}")
    output.mkdir(parents=True, exist_ok=False)
    lyrics_file = output / "source_lyrics.txt"
    lyrics_file.write_text(lyrics, encoding="utf-8")
    env = os.environ.copy()
    env.update(ANIME_STUDIO_ANALYSIS_HOME=str(home), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    cmd = [str(python), str(worker), str(audio), "--output", str(output), "--lyrics-file", str(lyrics_file)]
    if dry_vocals:
        cmd.append("--dry-vocals")
    proc = await asyncio.to_thread(
        subprocess.Popen,
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    tail = []
    try:
        async with asyncio.timeout(3600):
            while True:
                raw = await asyncio.to_thread(proc.stdout.readline)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("@@PROGRESS "):
                    item = json.loads(line[len("@@PROGRESS "):])
                    await progress_cb(float(item["progress"]), str(item["message"]))
                else:
                    tail = (tail + [line[-1000:]])[-12:]
            code = await asyncio.to_thread(proc.wait)
        if code:
            raise RuntimeError("本地歌曲分析失败：" + "\n".join(tail)[-3000:])
        timeline = json.loads((output / "timeline.json").read_text(encoding="utf-8"))
        if not timeline.get("lines") or not timeline.get("beats"):
            raise RuntimeError("本地分析没有返回有效歌词或节拍时间轴")
        for name in ("vocals.wav", "no_vocals.wav", "lyrics.srt"):
            if not (output / name).is_file() or not (output / name).stat().st_size:
                raise RuntimeError("本地分析产物缺失：" + name)
        return timeline
    finally:
        if proc.poll() is None:
            proc.kill()
            await asyncio.to_thread(proc.wait)
