"""Clean reference voice audio for Fish Audio Drama 3 zero-shot cloning:
1. Separate human vocals from BGM/accompaniment using local Demucs (htdemucs).
2. Trim non-speech intros, outros, and long instrumental tails using Silero VAD v6 + RMS envelope tightening.
3. Output a clean 44.1kHz 16-bit mono dry vocal WAV.
"""
import argparse
import gc
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("ANIME_STUDIO_ANALYSIS_HOME") or (Path(__file__).resolve().parents[1] / "audio_analysis"))
for _k, _v in {
    "HF_HOME": BASE / "cache/huggingface",
    "HF_HUB_CACHE": BASE / "cache/huggingface/hub",
    "TORCH_HOME": BASE / "cache/torch",
    "TEMP": BASE / "tmp",
    "TMP": BASE / "tmp",
}.items():
    os.environ[_k] = str(_v)
    Path(_v).mkdir(parents=True, exist_ok=True)

if os.name == "nt":
    for _p in [
        Path(sys.prefix) / "Lib/site-packages/torch/lib",
        Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.6/bin"),
    ]:
        if _p.exists():
            os.environ["PATH"] = str(_p) + os.pathsep + os.environ["PATH"]

import numpy as np
import soundfile as sf
import torch
import torchaudio

_orig_torch_load = torch.load


def _compat_load(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _orig_torch_load(*args, **kwargs)


torch.load = _compat_load


def clean_voice(input_path: Path, output_path: Path, target_sr: int = 44100) -> dict:
    t0 = time.time()
    device = "cuda" if torch.cuda.is_available() else "cpu"

    wav, sr = sf.read(str(input_path), dtype="float32", always_2d=True)
    orig_duration = round(len(wav) / float(sr), 2)
    wav_t = torch.from_numpy(wav.T.copy())

    # Cap at 25 seconds to keep reference encoding fast and focused
    max_samples = int(25 * sr)
    if wav_t.shape[1] > max_samples:
        wav_t = wav_t[:, :max_samples]

    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    if wav_t.shape[0] == 1:
        wav_stereo = wav_t.repeat(2, 1)
    elif wav_t.shape[0] > 2:
        wav_stereo = wav_t[:2]
    else:
        wav_stereo = wav_t

    base_model = get_model("htdemucs")
    if sr != base_model.samplerate:
        wav_stereo = torchaudio.functional.resample(wav_stereo, sr, base_model.samplerate)

    ref = wav_stereo.mean(0)
    mean = ref.mean()
    std = ref.std().clamp_min(1e-8)
    norm_in = (wav_stereo - mean) / std

    try:
        model = base_model.to(device)
        model.eval()
        with torch.inference_mode():
            sources = apply_model(
                model,
                norm_in[None],
                device=device,
                split=True,
                segment=5,
                overlap=0.15,
                shifts=0,
                progress=False,
                num_workers=0,
            )[0].cpu()
    except Exception:
        device = "cpu"
        model = base_model.to("cpu")
        model.eval()
        with torch.inference_mode():
            sources = apply_model(
                model,
                norm_in[None],
                device="cpu",
                split=True,
                segment=5,
                overlap=0.15,
                shifts=0,
                progress=False,
                num_workers=0,
            )[0].cpu()

    sources = sources * std + mean
    vocal_idx = model.sources.index("vocals")
    vocals_mono = sources[vocal_idx].mean(0)
    backing_mono = (sources.sum(0) - sources[vocal_idx]).mean(0)
    work_sr = int(model.samplerate)

    del model, sources, norm_in, wav_stereo
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    v_np = vocals_mono.numpy()
    b_np = backing_mono.numpy()
    bgm_ratio = float(np.sqrt(np.mean(b_np ** 2)) / max(1e-6, np.sqrt(np.mean(v_np ** 2))))

    # Trim non-speech segments (e.g. pure BGM intro/outro) with Silero VAD v6 + RMS envelope tightening
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    v_16k = torchaudio.functional.resample(vocals_mono, work_sr, 16000).numpy()
    ts = get_speech_timestamps(
        v_16k,
        VadOptions(
            threshold=0.55,
            min_speech_duration_ms=200,
            min_silence_duration_ms=200,
            speech_pad_ms=60,
        ),
    )

    if ts:
        hop = int(0.04 * work_sr)
        all_rms = np.array([np.sqrt(np.mean(v_np[i : i + hop] ** 2)) for i in range(0, len(v_np), hop)])
        active_rms = all_rms[all_rms > 0.01]
        peak_rms = float(np.percentile(active_rms, 90)) if len(active_rms) > 0 else 0.1
        thresh = max(0.018, peak_rms * 0.35)

        pieces = []
        fade_in_len = int(0.015 * work_sr)
        fade_out_len = int(0.025 * work_sr)
        for seg in ts:
            s_idx = max(0, int((seg["start"] / 16000.0) * work_sr))
            e_idx = min(len(v_np), int((seg["end"] / 16000.0) * work_sr))
            chunk = v_np[s_idx:e_idx].copy()
            if len(chunk) > hop:
                c_rms = np.array([np.sqrt(np.mean(chunk[i : i + hop] ** 2)) for i in range(0, len(chunk), hop)])
                loud = np.where(c_rms >= thresh)[0]
                if len(loud) > 0:
                    c_start = max(0, int(loud[0] * hop - 0.06 * work_sr))
                    c_end = min(len(chunk), int((loud[-1] + 1) * hop + 0.10 * work_sr))
                    chunk = chunk[c_start:c_end].copy()
            if len(chunk) > (fade_in_len + fade_out_len):
                chunk[:fade_in_len] *= np.linspace(0.0, 1.0, fade_in_len, dtype=np.float32)
                chunk[-fade_out_len:] *= np.linspace(1.0, 0.0, fade_out_len, dtype=np.float32)
            if len(chunk) > int(0.15 * work_sr):
                pieces.append(chunk)
                if len(ts) > 1:
                    pieces.append(np.zeros(int(0.12 * work_sr), dtype=np.float32))

        if pieces:
            if len(pieces) > 1 and np.all(pieces[-1] == 0):
                pieces.pop()
            out_wav = np.concatenate(pieces)
        else:
            out_wav = v_np
    else:
        out_wav = v_np

    if work_sr != target_sr:
        out_wav = torchaudio.functional.resample(torch.from_numpy(out_wav), work_sr, target_sr).numpy()

    peak = float(np.max(np.abs(out_wav))) if len(out_wav) else 0.0
    if peak > 1e-4:
        out_wav = np.clip(out_wav * min(10.0, 0.9 / peak), -1.0, 1.0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), out_wav, target_sr, subtype="PCM_16")
    clean_duration = round(len(out_wav) / float(target_sr), 2)

    return {
        "ok": True,
        "device": device,
        "orig_duration": orig_duration,
        "clean_duration": clean_duration,
        "bgm_ratio": round(bgm_ratio, 3),
        "vad_segments": [(round(s["start"] / 16000.0, 2), round(s["end"] / 16000.0, 2)) for s in ts],
        "elapsed": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path")
    parser.add_argument("output_path")
    args = parser.parse_args()
    info = clean_voice(Path(args.input_path), Path(args.output_path))
    print("@@CLEAN_VOICE " + json.dumps(info, ensure_ascii=False), flush=True)
