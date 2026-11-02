"""Fish Audio Drama 3 配音纯工具函数模块：预设台词、台词规范化、上传参考声伴奏剥离、ComfyUI 工作流构造与进度估算。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from backend.core.paths import DATA_DIR

DRAMA3_DIR = DATA_DIR / "drama3"
DRAMA3_VOICES_DIR = DRAMA3_DIR / "voices"
DRAMA3_OUTPUTS_DIR = DRAMA3_DIR / "outputs"

for _p in (DRAMA3_DIR, DRAMA3_VOICES_DIR, DRAMA3_OUTPUTS_DIR):
    _p.mkdir(parents=True, exist_ok=True)

DEFAULT_PRESET_SCRIPTS: dict[str, dict[str, Any]] = {
    "solo_sweet": {
        "title": "邻家女友 · 撒娇耳语",
        "speakers": 1,
        "script": (
            "[sighs] 哥哥，你终于下班回来啦……人家在门口等了你好久好久呢。\n"
            "[giggles] 嘻嘻，骗你的啦，我也刚到没多久！不过看到你回来，我真的超开心的！\n"
            "[tender] 只要是你做的饭……我什么都喜欢吃呀，今晚早点休息哦，晚安。"
        ),
    },
    "solo_cheerful": {
        "title": "元气少女 · 开心问候",
        "speakers": 1,
        "script": (
            "[laughs] 早上好呀！快看窗外，今天的阳光超棒的！\n"
            "[excited] 我已经把早餐准备好啦，再不起床的话，煎蛋就要被我一个人偷吃光咯！\n"
            "[giggles] 快点起来吧，今天也会是超开心的一天！"
        ),
    },
    "solo_rain": {
        "title": "雨夜独白 · 委屈哭腔",
        "speakers": 1,
        "script": (
            "[panting] 别走！我跑了整整三条街才追上你……你真的要不告而别吗？\n"
            "[crying] 笨蛋！你以为不说就是为我好吗？\n"
            "[whispering] 我什么都不怕，我只想一直陪在你身边啊……"
        ),
    },
    "duet_sweet": {
        "title": "双人对话 · 情侣撒娇日常",
        "speakers": 2,
        "script": (
            "<|speaker:0|> [sighs] 喂，你盯着电脑整整两个小时了，连看都不看我一眼！\n"
            "<|speaker:1|> [chuckles] 抱歉抱歉，马上就忙完这一段了，手怎么这么凉？\n"
            "<|speaker:0|> [giggles] 哼，谁让你不理我，现在罚你抱我五分钟不许松手！\n"
            "<|speaker:1|> [tender] 遵命，别说五分钟，抱一晚上都行。"
        ),
    },
    "duet_drama": {
        "title": "双人对手戏 · 雨夜天台对峙",
        "speakers": 2,
        "script": (
            "<|speaker:0|> [panting] 站住！当年的真相到底是什么，你今天必须把话说清楚！\n"
            "<|speaker:1|> [sighs] 有些事，不知道对你来说才是最安全的。\n"
            "<|speaker:0|> [crying] 可我宁愿和你一起承担，也不要像傻瓜一样被蒙在鼓里！\n"
            "<|speaker:1|> [shaky breath] 对不起……这一次，我不会再推开你了。"
        ),
    },
    "trio_scene": {
        "title": "三人剧场 · 假日茶话会",
        "speakers": 3,
        "script": (
            "<|speaker:0|> [excited] 大家快尝尝看！这是我今天早上刚烤好的草莓泡芙！\n"
            "<|speaker:1|> [giggles] 哇，闻起来超香的，那我就不客气先拿一个啦！\n"
            "<|speaker:2|> [laughs] 慢点吃，给我们也留两个呀，咖啡马上就冲好了。"
        ),
    },
    "jp_sweet": {
        "title": "日漫女友 · 撒娇耳语 (日语)",
        "speakers": 1,
        "script": (
            "[sighs] お兄ちゃん、おかえりなさい……ずっと玄関で待ってたんだよ。\n"
            "[giggles] えへへ、うそだよ！でも、お兄ちゃんの顔を見たら、すごく嬉しくなっちゃった！\n"
            "[whispering] ねえ……今夜はずっと、わたしのそばにいてくれる？おやすみなさい。"
        ),
    },
    "jp_duet": {
        "title": "双人日漫 · 放学后的告白 (日语)",
        "speakers": 2,
        "script": (
            "<|speaker:0|> [panting] 待って！今日こそ言わなきゃって思って、走ってきたの。\n"
            "<|speaker:1|> [gasps] えっ……そんなに急いで、どうしたんだ？\n"
            "<|speaker:0|> [shy] ずっと前から……君のことが好きでした！"
        ),
    },
}


def _default_voice_slot(index: int = 0) -> dict[str, Any]:
    default_name = "Role 0 (lead)" if index == 0 else f"Role {index}"
    return {
        "index": index,
        "name": default_name,
        "local_path": "",
        "remote_name": "",
        "transcript": "",
        "duration": 0.0,
    }


def _default_voices() -> list[dict[str, Any]]:
    return [_default_voice_slot(0)]


def _clean_single_speaker_script(text: str) -> str:
    """规范化台词文本，保留合法的 <|speaker:0|> ~ <|speaker:15|> 多角色标签。"""
    raw = str(text or "")

    def _fix_speaker_tag(m: re.Match) -> str:
        idx = min(15, max(0, int(m.group(1))))
        return f"<|speaker:{idx}|>"

    cleaned = re.sub(r"<\|speaker:(\d+)\|>", _fix_speaker_tag, raw)
    return cleaned.strip()


def _normalize_voice_list(voices: Any, fallback_voices: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    base_list = voices if isinstance(voices, list) and voices else (fallback_voices or _default_voices())
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(base_list[:16]):
        v = dict(item) if isinstance(item, dict) else {}
        default_name = "Role 0 (lead)" if idx == 0 else f"Role {idx}"
        out.append({
            "index": idx,
            "name": str(v.get("name") or default_name).strip() or default_name,
            "local_path": str(v.get("local_path") or "").strip(),
            "remote_name": str(v.get("remote_name") or "").strip(),
            "transcript": str(v.get("transcript") or ""),
            "duration": float(v.get("duration") or 0.0),
        })
    return out if out else _default_voices()


_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".ts", ".m4v"}


def _clean_uploaded_voice_sync(raw_wav: Path, out_wav: Path) -> None:
    """调用本地 audio_analysis 环境中的 Demucs + Silero VAD 自动剥离背景音乐并裁剪纯伴奏尾音。"""
    root_dir = Path(__file__).resolve().parents[2]
    home = Path(os.environ.get("ANIME_STUDIO_ANALYSIS_HOME") or (root_dir / "audio_analysis"))
    py_bin = home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    worker = root_dir / "tools" / "clean_drama3_voice.py"
    if not py_bin.is_file() or not worker.is_file():
        shutil.copy2(raw_wav, out_wav)
        return

    env = os.environ.copy()
    env.update(
        ANIME_STUDIO_ANALYSIS_HOME=str(home),
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
    )
    proc = subprocess.run(
        [str(py_bin), "-X", "utf8", str(worker), str(raw_wav), str(out_wav)],
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if proc.returncode != 0 or not out_wav.is_file() or out_wav.stat().st_size <= 1024:
        shutil.copy2(raw_wav, out_wav)


def build_drama3_workflow(
    item: dict[str, Any],
    uploaded_audio_names: dict[int, str] | str,
    filename_prefix: str,
) -> dict[str, Any]:
    """生成 Drama 3 API 工作流：支持 1~16 个角色（voice0~voice15）零样本克隆与多人对话。"""
    voices = _normalize_voice_list(item.get("voices"))
    if isinstance(uploaded_audio_names, str):
        audio_map: dict[int, str] = {0: uploaded_audio_names} if uploaded_audio_names.strip() else {}
    else:
        audio_map = {int(k): str(v).strip() for k, v in (uploaded_audio_names or {}).items() if str(v).strip()}

    script_text = _clean_single_speaker_script(item.get("script_text") or "")

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "Drama3Loader",
            "inputs": {
                "model_name": str(item.get("model_name") or "drama3_w6a8experts_bf16.safetensors"),
                "experts": str(item.get("experts") or "in RAM"),
                "expert_cache_gb": float(item.get("expert_cache_gb") or 0.0),
                "second_gpu": "none",
                "split_layer": 0,
            },
            "_meta": {"title": "加载 Drama 3 主模型"},
        },
        "2": {
            "class_type": "Drama3CodecLoader",
            "inputs": {
                "codec_name": "drama3_codec.safetensors",
            },
            "_meta": {"title": "加载 Drama 3 声码器"},
        },
    }

    speakers_mode = str(item.get("speakers_mode") or "one take per line")
    if speakers_mode not in {"one take per line", "one take per line, hearing the line before", "one take per speaker", "together"}:
        speakers_mode = "one take per line"

    gen_inputs: dict[str, Any] = {
        "model": ["1", 0],
        "text": script_text,
        "seed": int(item.get("seed") or 42),
        "temperature": float(item.get("temperature") if item.get("temperature") is not None else 1.0),
        "top_p": float(item.get("top_p") if item.get("top_p") is not None else 0.9),
        "top_k": int(item.get("top_k") or 0),
        "max_frames": int(item.get("max_frames") or 800),
        "split_long_text": bool(item.get("split_long_text", True)),
        "speakers": speakers_mode,
    }

    asr_loader_added = False
    for idx, voice in enumerate(voices[:16]):
        audio_name = audio_map.get(idx, "")
        if not audio_name:
            continue
        manual_transcript = str(voice.get("transcript") or "").strip()
        load_id = f"10_{idx}" if idx > 0 else "10"
        asr_id = f"11_{idx}" if idx > 0 else "11"
        enc_id = f"12_{idx}" if idx > 0 else "12"
        role_label = str(voice.get("name") or f"Role {idx}")

        workflow[load_id] = {
            "class_type": "LoadAudio",
            "inputs": {
                "audio": audio_name,
            },
            "_meta": {"title": f"读取参考干声 ({role_label})"},
        }
        if not manual_transcript:
            if not asr_loader_added:
                workflow["3"] = {
                    "class_type": "Qwen3ASRLoader",
                    "inputs": {
                        "model_name": "qwen3_asr_1.7b_bf16.safetensors",
                    },
                    "_meta": {"title": "加载 Qwen3-ASR 1.7B 语音识别"},
                }
                asr_loader_added = True
            workflow[asr_id] = {
                "class_type": "Qwen3ASRTranscribe",
                "inputs": {
                    "model": ["3", 0],
                    "audio": [load_id, 0],
                    "language": str(item.get("asr_language") or "auto"),
                    "context": "",
                    "max_new_tokens": 4096,
                },
                "_meta": {"title": f"Qwen3-ASR 识别参考台词 ({role_label})"},
            }
            transcript_input: Any = [asr_id, 0]
        else:
            transcript_input = manual_transcript

        workflow[enc_id] = {
            "class_type": "Drama3EncodeVoice",
            "inputs": {
                "codec": ["2", 0],
                "audio": [load_id, 0],
                "transcript": transcript_input,
            },
            "_meta": {"title": f"提取参考音色特征 ({role_label})"},
        }
        gen_inputs[f"voices.voice{idx}"] = [enc_id, 0]

    workflow["200"] = {
        "class_type": "Drama3Generate",
        "inputs": gen_inputs,
        "_meta": {"title": "Drama 3 采样生成配音"},
    }
    workflow["201"] = {
        "class_type": "Drama3Decode",
        "inputs": {
            "codec": ["2", 0],
            "codes": ["200", 0],
            "shorten_long_pauses": bool(item.get("shorten_long_pauses", True)),
        },
        "_meta": {"title": "Drama 3 解码 44.1kHz 波形"},
    }
    workflow["202"] = {
        "class_type": "SaveAudio",
        "inputs": {
            "audio": ["201", 0],
            "filename_prefix": filename_prefix,
        },
        "_meta": {"title": "保存配音音频"},
    }
    return workflow


def _drama3_stage_progress(
    workflow: dict[str, Any],
    node: Any,
    value: float | None = None,
    maximum: float | None = None,
) -> float:
    info = workflow.get(str(node)) if isinstance(workflow, dict) else None
    class_type = str(info.get("class_type") or "") if isinstance(info, dict) else ""
    ratio = max(0.0, min(1.0, float(value) / float(maximum))) if value is not None and maximum else None
    if class_type == "Drama3Generate":
        return 30.0 + (ratio * 58.0 if ratio is not None else 5.0)
    floors = {
        "Drama3Loader": 6.0,
        "Drama3CodecLoader": 12.0,
        "Qwen3ASRLoader": 15.0,
        "LoadAudio": 18.0,
        "Qwen3ASRTranscribe": 22.0,
        "Drama3EncodeVoice": 27.0,
        "Drama3Decode": 91.0,
        "SaveAudio": 96.0,
    }
    base = floors.get(class_type, 5.0)
    return min(97.0, base + (ratio * 4.0 if ratio is not None else 0.0))
