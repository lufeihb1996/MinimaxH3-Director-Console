# AI Anime Studio

[English](README.md) | [简体中文](README.zh-CN.md)

A Windows web console for producing AI anime and live-action short videos: projects, scenes,
shots, reference images, songs and voice-over, driven from one browser UI.

**It does no inference locally.** Every image, video and audio job is executed by a remote
ComfyUI instance (or a Pika API instance) that you point it at. This repository is the control
plane — the local SQLite database, the queue, the media handling and the UI.

```
browser (vanilla JS)  →  FastAPI backend (this repo)  →  ComfyUI / Pika  →  GPU
                              │
                              ├── data/studio.db          project, scene, shot, queue state
                              └── projects/project_NNN/   images, videos, audio, inputs
```

## Documentation

| Document | 中文 |
|---|---|
| [Quick start](docs/QUICKSTART.md) — first project in ~15 minutes | [快速开始](docs/QUICKSTART.zh-CN.md) |
| [Workflow](docs/WORKFLOW.md) — every stage from an empty project to a finished cut | [制作流程](docs/WORKFLOW.zh-CN.md) |
| [Project rules](AI_QUICK_CONTEXT.md) — architecture, model capabilities, API index (Chinese) | — |

## Features

- **Projects → Scenes → Shots** with ordering, freeze-on-enqueue inputs and per-shot versions
- **Reference images**: four slots per scene (Ref1/Ref2 characters, Ref3/Ref4 scenes) with a
  history gallery and scope control (per scene or shared across the project)
- **Video modes**: T2V (text), CUT (reference images) and CONTINUE (continue from the previous
  shot's last frame)
- **Two-pass video**: 0.4 MP draft first, then an Open-H3 refine pass to 1080p / 1.5K / 2K
- **Image workbench**: MOSS-Image 5B and Krea 2, queued separately from video
- **Audio**: YuE2 song generation, Fish Audio Drama 3 voice-over with per-speaker voice profiles
- **Shot planning from audio**: align the vocal track, cut shots on silence troughs, bind each
  shot to a timeline range
- **Merge**: per-scene and full-project merge, original audio muxed back, MV lyric subtitles
- Live progress over WebSocket, per-shot terminal log, GPU slot serialization

## Requirements

| | |
|---|---|
| OS | Windows 10/11 (the launcher and path handling assume Windows) |
| Python | 3.11 or newer (`start.bat` calls `py -3`) |
| FFmpeg | `ffmpeg.exe` **and** `ffprobe.exe` on `PATH` |
| Disk | ~150 MB for the app and its virtualenv; models live on the GPU host |
| Remote | A ComfyUI instance you can reach over HTTP(S) |

Python dependencies are small (6 packages, see `requirements.txt`).

## Quick start

```bat
git clone <your-repo-url> ai-anime-studio
cd ai-anime-studio
start.bat
```

`start.bat` creates `.venv`, installs the dependencies, verifies FFmpeg/FFprobe, opens
<http://127.0.0.1:18080> and starts uvicorn with `--reload`.

Then:

1. Open **Settings** (top right) and paste your ComfyUI URL into `comfy_url`.
2. Create a project, add a Scene, add a Shot.
3. Hit generate.

Until `comfy_url` is set the UI loads, but every generation reports
`{"online": false, "endpoint": ""}`.

Manual start, if you prefer:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 18080 --reload
```

## Step 2 — bring up the remote ComfyUI

`colab/` contains three Colab notebooks (plain `.py`, run as a cell) that install ComfyUI, the
custom nodes, `comfy-kitchen` and the model weights, then publish ComfyUI through a Cloudflare
tunnel. Pick one:

| Script | Includes | GPU |
|---|---|---|
| `colab/三合一_H3-MOSS-Drama3.py` | H3 b15-49 video + MOSS-Image 5B + Fish Audio Drama 3 (voice cloning + ASR) | A100 40 GB+ (80 GB recommended) |
| `colab/二合一_MOSS生图-OpenH3精修.py` | H3 b15-49 + MOSS-Image + Open-H3 1080p/2K second pass (no audio models) | A100 40 GB+ (80 GB recommended) |
| `colab/YuE2_纯歌曲.py` | YuE2 song generation only | L4 24 GB |

They download **well over 100 GB** of weights from public Hugging Face repositories
(`Kijai/MiniMax-H3_comfy`, `Comfy-Org/YuE2`, `fal/MiniMax-H3-Realism-People-LoRA`, …), so plan
for a long first run and a Colab Pro+ plan.

### Credentials the scripts need

Nothing is hard-coded. Set these as environment variables or Colab secrets before running:

| Variable | Purpose |
|---|---|
| `CF_TUNNEL_TOKEN` | Your Cloudflare Zero Trust tunnel token. Without it ComfyUI still starts on `:8188`, but no public URL is created. |
| `PUBLIC_URL` | The hostname the tunnel serves, e.g. `https://my-gpu.example.com`. Paste this into the app's Settings. |
| `EXTRA_LORA_URLS` | Optional, comma-separated direct URLs for extra LoRAs to download. Empty by default. |

The adult-oriented LoRA used during development is published on Civitai
([model 2836176](https://civitai.com/models/2836176)) and is **not** bundled or mirrored here —
download it yourself and drop the `.safetensors` into `ComfyUI/models/loras/` if you want it.

## Optional — audio analysis (songs, MV subtitles, auto shot planning)

Song and MV features need a **separate** Python 3.11 environment with PyTorch, WhisperX and
Demucs. It is intentionally isolated from the main `.venv`, and its caches are not committed.

```powershell
cd audio_analysis
.\setup.ps1
```

`setup.ps1` needs [uv](https://docs.astral.sh/uv/) (`pip install uv`) and does:

1. `uv venv .venv --python 3.11 --managed-python`
2. installs `torch==2.8.0` + `torchaudio==2.8.0` (CUDA 12.6 wheels) and `requirements.txt`
   (whisperx, demucs, transformers, librosa …)
3. runs `download_models.py`, which fetches ~2 GB of models into `audio_analysis/cache/`:
   - `Systran/faster-whisper-small` (~500 MB)
   - Demucs hybrid transformer `955717e8-8726e21a.th` (~300 MB)
   - wav2vec2 base LS960 (~1.2 GB, from download.pytorch.org)
   - NLTK `punkt_tab` English tokenizer

Total footprint with the venv is roughly **10–13 GB**.

The backend finds this environment through `ANIME_STUDIO_ANALYSIS_HOME`; `start.bat` already
points it at `%~dp0audio_analysis`. Without it the rest of the app still works, but song
generation fails with *local audio analysis environment not found*.

This step needs a CUDA GPU; it does not unload your remote ComfyUI models.

## Optional — Pika models

`config/models.json` registers `pika_h3`, which talks to a separate Pika service
(`/api/health`, `/api/uploads`, `/api/jobs`, `/media/{id}`). **That service is not part of this
repository.** If you do not have it, leave `pika_url` empty and use the `b15_49` video model.
The app reports `Pika: offline` and refuses to queue Pika jobs instead of failing silently.

## Data layout

| Path | Contents |
|---|---|
| `data/studio.db` | SQLite: projects, scenes, shots, generations, voices, settings |
| `projects/project_NNN/` | generated images, videos, audio, and `inputs/<id>/` snapshots |
| `audio_analysis/` | isolated analysis runtime and model cache (optional) |

`data/`, `projects/`, `.venv/`, `logs/` and the analysis caches are git-ignored. Nothing is
uploaded anywhere except to the ComfyUI/Pika endpoint you configure.

## Configuration

Settings live in the `settings` table and are editable from the UI:

| Key | Meaning |
|---|---|
| `comfy_url` | ComfyUI base URL (your tunnel hostname) |
| `pika_url` | Pika API base URL (optional) |
| `fps` | Timeline and output frame rate, default 24 |

Model catalog: `config/models.json`. Image, video and audio models each declare their adapter,
workflow file and parameter ranges. Add an entry there plus an adapter in
`backend/models/adapters/` and the UI picks it up from `/api/models`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FFmpeg not found` | Install FFmpeg, add its `bin` folder to `PATH`, reopen the terminal |
| Status shows `online: false`, `endpoint: ""` | `comfy_url` is empty — set it in Settings |
| Status shows the endpoint but offline | The tunnel or Colab runtime is down |
| `/api/status` returns 500 behind a corporate proxy | HTTP clients are created with `trust_env=False`, so system proxy variables are ignored; check the tunnel directly |
| Port 18080 already in use | `start.bat` reopens the existing instance instead of starting a second one |
| Song analysis says the environment is missing | Run `audio_analysis/setup.ps1` (see above) |

## Limitations

- Windows-oriented: `start.bat`, FFmpeg discovery and path handling assume Windows semantics.
- No local inference — a GPU host is mandatory for anything beyond UI and project management.
- The Pika service code is not included (see above).
- The UI is English; some backend status and error strings are still being translated.

## License

Not chosen yet. Add a `LICENSE` file before redistributing.
