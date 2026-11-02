"""YuE2 优化版生歌：Options → Plan → RenderPlan → SaveAudio（pytraveler/YuE2-ComfyUI staged 节点）。"""
from __future__ import annotations

import re
from typing import Any

from backend.models.base import AudioAdapter, node_class
from backend.models.registry import register

SECTION_NAMES = "verse|chorus|bridge|pre-chorus|outro|intro"


def yue2_lyrics(lyrics: str) -> str:
    """Normalize numbered section labels ([Verse 1] -> [Verse]) without changing lyric words."""
    numbered = re.compile(rf"(?mi)^\s*\[({SECTION_NAMES})\s*\d+\]\s*$")
    text = numbered.sub(lambda m: f"[{m.group(1).title()}]", lyrics)
    if not re.search(rf"(?mi)^\s*\[(?:{SECTION_NAMES})\]\s*$", text):
        text = f"[Verse]\n{text}"
    return text


def yue2_style(genre: str, target_duration: int) -> str:
    # Only add a language prefix when Style explicitly names it; never infer from genre or lyric characters.
    style = genre
    language = ""
    if re.search(r"\bcantonese\b|粤语|粵語", genre, re.I):
        language = "Cantonese"
    elif re.search(r"\bmandarin\b|普通话|普通話", genre, re.I):
        language = "Mandarin Chinese"
    elif re.search(r"\benglish\b|英语|英語", genre, re.I):
        language = "English"
    if language and not re.match(rf"(?i)^\s*{re.escape(language)}\s*[,;]", genre):
        style = f"{language}, {genre}"
    # max_seconds is only a ceiling; the requested length goes into Style so planning arranges toward it.
    duration_line = (
        f"Target duration: approximately {target_duration} seconds. "
        f"Develop the arrangement across roughly {target_duration} seconds and end naturally near that point. "
        "Do not finish after one short pass through the lyrics; use instrumental intro, interludes, "
        "musical development, and outro as needed while keeping the supplied lyric words unchanged."
    )
    # Replace a previously generated duration line so a changed target never leaves stale timing.
    style = re.sub(r"(?im)^\s*Target\s+duration\s*:[^\n]*(?:\n|$)", "", style).strip()
    return f"{duration_line}\n{style}".strip()


@register("audio", "yue2")
class YuE2(AudioAdapter):
    def required_nodes(self):
        return ["YuE2Options", "YuE2Plan", "YuE2RenderPlan", "SaveAudioMP3"]

    def build(self, workflow, model, *, style, lyrics, target_duration, seed, filename_prefix):
        # Tight ceiling so 60s target doesn't over-generate 90s and bloat download size.
        ceiling = min(900.0, max(float(target_duration) + 6.0, float(target_duration) * 1.08))
        workflow["2"]["inputs"]["value"] = yue2_style(style, target_duration)
        workflow["3"]["inputs"]["value"] = yue2_lyrics(lyrics)
        workflow["4"]["inputs"]["seed"] = seed
        workflow["1"]["inputs"]["max_seconds"] = float(ceiling)
        workflow["7"]["inputs"]["filename_prefix"] = filename_prefix

    def stage_progress(self, workflow: dict[str, Any], node: Any, value=None, maximum=None) -> float:
        class_type = node_class(workflow, node)
        ratio = max(0.0, min(1.0, float(value) / float(maximum))) if value is not None and maximum else None
        bands = {
            "YuE2GenerateABC": (6.0, 18.0), "YuE2GenerateMusic": (26.0, 24.0), "KSampler": (54.0, 36.0),
            # Staged nodes report a 0..1 band per node; Plan is short, RenderPlan dominates.
            "YuE2Plan": (6.0, 24.0), "YuE2RenderPlan": (31.0, 64.0),
        }
        if class_type in bands:
            start, width = bands[class_type]
            return start + (ratio * width if ratio is not None else 0.0)
        floors = {
            "CheckpointLoaderSimple": 3.0, "YuE2Options": 3.0, "ConditioningZeroOut": 51.0,
            "EmptyYuE2LatentAudio": 53.0, "VAEDecodeAudio": 93.0, "VAEDecodeAudioTiled": 93.0,
            "SaveAudio": 97.0, "SaveAudioMP3": 97.0, "SaveAudioOpus": 97.0, "SaveAudioAdvanced": 97.0,
        }
        base = floors.get(class_type, 2.0)
        return min(97.0, base + ratio * 2.0) if ratio is not None else base
