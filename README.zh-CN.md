# AI Anime Studio

[English](README.md) | [简体中文](README.zh-CN.md)

Windows 上的 AI 动漫 / 真人短片制作工作台：项目、场景、分镜、参考图、歌曲、配音，全在一个浏览器界面里完成。

**本地不做推理。** 生图、视频、音频全部由你指定的远端 ComfyUI（或 Pika API）执行；本仓库是控制面——本地 SQLite 数据库、队列、素材处理和界面。

```
浏览器（原生 JS）  →  FastAPI 后端（本仓库）  →  ComfyUI / Pika  →  GPU
                          │
                          ├── data/studio.db          项目、场景、分镜、队列状态
                          └── projects/project_NNN/   图片、视频、音频、输入快照
```

## 文档

| 文档 | English |
|---|---|
| [快速开始](docs/QUICKSTART.zh-CN.md) — 约 15 分钟做出第一个项目 | [Quick start](docs/QUICKSTART.md) |
| [制作流程](docs/WORKFLOW.zh-CN.md) — 从空项目到成片的每一步 | [Workflow](docs/WORKFLOW.md) |
| [架构与接口速查](AI_QUICK_CONTEXT.md) — 模型能力、数据表、API 索引 | — |

## 功能

- **项目 → 场景 → 分镜**：排序、入队冻结输入、每个分镜多版本
- **参考图**：每个场景四槽（Ref1/Ref2 人物，Ref3/Ref4 场景），带历史图和作用域控制（场景内 / 跨项目共享）
- **视频模式**：T2V（纯文本）、CUT（参考图）、CONTINUE（接上一镜末帧）
- **二采**：先出 0.4MP 草稿，再用 Open-H3 精修到 1080p / 1.5K / 2K
- **生图工作台**：MOSS-Image 5B 与 Krea 2，与视频队列独立排队
- **音频**：YuE2 歌曲生成；Fish Audio Drama 3 配音，支持多说话人与全局音色库
- **按音频分镜**：对齐人声、在人声谷底切镜、把每个分镜绑到时间轴区间
- **合并**：场景合并与全剧合并，原音轨合回，MV 自动压歌词字幕
- WebSocket 实时进度、单镜终端日志、GPU 槽位串行调度

## 环境要求

| | |
|---|---|
| 系统 | Windows 10/11（启动脚本和路径处理按 Windows 语义写） |
| Python | 3.11 以上（`start.bat` 调用 `py -3`） |
| FFmpeg | `ffmpeg.exe` **和** `ffprobe.exe` 在 `PATH` 里 |
| 磁盘 | 应用本体加虚拟环境约 150 MB；模型在 GPU 那边 |
| 远端 | 一台能通过 HTTP(S) 访问的 ComfyUI |

Python 依赖只有 6 个包，见 `requirements.txt`。

## 快速开始

```bat
git clone <你的仓库地址> ai-anime-studio
cd ai-anime-studio
start.bat
```

`start.bat` 会自动建 `.venv`、装依赖、校验 FFmpeg/FFprobe、打开 <http://127.0.0.1:18080> 并带 `--reload` 启动 uvicorn。

然后：

1. 打开右上角**设置**，把 `comfy_url` 填成你的 ComfyUI 地址。
2. 新建项目 → 新建场景 → 新建分镜。
3. 点生成。

`comfy_url` 没填时界面照常打开，但所有生成都会返回 `{"online": false, "endpoint": ""}`。

手动启动：

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 18080 --reload
```

## 第二步 — 起远端 ComfyUI

`colab/` 里有三个 Colab 脚本（纯 `.py`，整段当一格跑），负责装 ComfyUI、自定义节点、comfy-kitchen 和模型权重，然后通过 Cloudflare 隧道把 ComfyUI 暴露出来。三选一：

| 脚本 | 包含 | 显卡 |
|---|---|---|
| `colab/三合一_H3-MOSS-Drama3.py` | H3 b15-49 视频 + MOSS-Image 5B + Fish Audio Drama 3（声音克隆 + ASR） | A100 40GB 起（建议 80GB） |
| `colab/二合一_MOSS生图-OpenH3精修.py` | H3 b15-49 + MOSS-Image + Open-H3 1080p/2K 二采（不含音频模型） | A100 40GB 起（建议 80GB） |
| `colab/YuE2_纯歌曲.py` | 只跑 YuE2 歌曲 | L4 24GB |

权重从公共 Hugging Face 仓库下载（`Kijai/MiniMax-H3_comfy`、`Comfy-Org/YuE2`、`fal/MiniMax-H3-Realism-People-LoRA` 等），**总量 100 GB 以上**，首次运行要留足时间，建议 Colab Pro+。

### 脚本需要的凭据

代码里没有任何硬编码，全部走环境变量或 Colab Secrets：

| 变量 | 用途 |
|---|---|
| `CF_TUNNEL_TOKEN` | 你自己的 Cloudflare Zero Trust 隧道 token。不填的话 ComfyUI 照样在 `:8188` 启动，但没有公网地址。 |
| `PUBLIC_URL` | 隧道对外域名，例如 `https://my-gpu.example.com`，填进设置页的就是它。 |
| `EXTRA_LORA_URLS` | 可选，逗号分隔的 LoRA 直链。默认不下载任何额外 LoRA。 |

开发时用到的成人向 LoRA 发布在 Civitai（[model 2836176](https://civitai.com/models/2836176)），本仓库**不打包也不镜像**它——需要就自行下载，把 `.safetensors` 放进 `ComfyUI/models/loras/`。

## 可选 — 音频分析（歌曲、MV 字幕、按音频分镜）

歌曲和 MV 相关功能需要一套**独立**的 Python 3.11 环境（PyTorch + WhisperX + Demucs），与主 `.venv` 隔离，缓存不入库。

```powershell
cd audio_analysis
.\setup.ps1
```

`setup.ps1` 需要 [uv](https://docs.astral.sh/uv/)（`pip install uv`），它会：

1. `uv venv .venv --python 3.11 --managed-python`
2. 装 `torch==2.8.0` + `torchaudio==2.8.0`（CUDA 12.6 轮子）和 `requirements.txt`（whisperx、demucs、transformers、librosa 等）
3. 跑 `download_models.py`，把约 2 GB 模型下到 `audio_analysis/cache/`：
   - `Systran/faster-whisper-small`（约 500 MB）
   - Demucs hybrid transformer `955717e8-8726e21a.th`（约 300 MB）
   - wav2vec2 base LS960（约 1.2 GB，来自 download.pytorch.org）
   - NLTK `punkt_tab` 英文分词器

含虚拟环境总共约 **10–13 GB**。

后端通过 `ANIME_STUDIO_ANALYSIS_HOME` 找到这套环境，`start.bat` 已经指向 `%~dp0audio_analysis`。没装时其余功能正常，但歌曲生成会报 *local audio analysis environment not found*。

这一步需要 CUDA 显卡；它不会卸载远端 ComfyUI 的模型。

## 可选 — Pika 模型

`config/models.json` 里登记了 `pika_h3`，它对接一套独立的 Pika 服务（`/api/health`、`/api/uploads`、`/api/jobs`、`/media/{id}`）。**这套服务不在本仓库。** 没有就把 `pika_url` 留空，用 `b15_49` 视频模型。应用会显示 `Pika: offline` 并拒绝入队，而不是静默失败。

## 数据布局

| 路径 | 内容 |
|---|---|
| `data/studio.db` | SQLite：项目、场景、分镜、生成记录、音色、设置 |
| `projects/project_NNN/` | 生成的图片、视频、音频，以及 `inputs/<id>/` 输入快照 |
| `audio_analysis/` | 独立分析运行时与模型缓存（可选） |

`data/`、`projects/`、`.venv/`、`logs/` 和分析缓存都已 git 忽略。除了你配置的 ComfyUI/Pika 地址，没有任何数据会被上传。

## 配置项

设置存在 `settings` 表，界面可直接改：

| 键 | 含义 |
|---|---|
| `comfy_url` | ComfyUI 地址（你的隧道域名） |
| `pika_url` | Pika API 地址（可选） |
| `fps` | 时间轴与输出帧率，默认 24 |

模型目录在 `config/models.json`：每个模型声明适配器、工作流文件和参数范围。加一个模型 = 目录里加一项 + `backend/models/adapters/` 里加适配器，界面会从 `/api/models` 自动读到。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `FFmpeg not found` | 装 FFmpeg，把 `bin` 加进 `PATH`，重开终端 |
| 状态是 `online: false`、`endpoint: ""` | `comfy_url` 为空，去设置里填 |
| 有地址但显示离线 | 隧道或 Colab 运行时挂了 |
| 在公司代理后面 `/api/status` 返回 500 | HTTP 客户端用 `trust_env=False` 创建，不吃系统代理变量；直接测隧道地址 |
| 18080 端口被占 | `start.bat` 会直接打开已有实例，不再起第二个 |
| 歌曲分析提示环境不存在 | 跑 `audio_analysis/setup.ps1`（见上） |

## 限制

- 面向 Windows：`start.bat`、FFmpeg 查找和路径处理都是 Windows 语义。
- 没有本地推理——想要出片必须有 GPU 主机。
- Pika 服务端代码不在仓库里（见上）。
- 界面已英文化；后端部分状态和报错文案仍在翻译中。

## 许可证

尚未选定。分发前请先补一个 `LICENSE` 文件。
