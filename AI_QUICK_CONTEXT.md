# AI Anime Studio：架构与接口速查

本文件维护当前实现事实。操作边界见 [AGENTS.md](AGENTS.md)，制作方法从 [README.md](README.md) 选择对应 Skill。

## 运行环境

- 根目录：`D:/ai/AI_Anime_Studio`。FastAPI 入口 `backend.app:app`，使用根目录 `.venv`，默认地址 `http://127.0.0.1:18081`；`start.bat` 启动时开启 `--reload`。
- SQLite：`data/studio.db`。图片、音频、输入快照、各版本视频和成片在 `projects/project_NNN/`；数据库保存 Windows 本地路径。
- ComfyUI 地址保存在 `comfy_url`；Pika 地址保存在独立的 `pika_url`。两项分别指向实际推理服务，不能因使用同一隧道域名就视为兼容接口。
- ComfyUI 输出使用工作流中的 `anime_studio/{images,videos,music}` 前缀；原 Colab 环境映射至 Drive 的 `MyDrive/Singularity/`，再下载到本地。Pika 使用其独立服务的输出目录和下载接口。

## 代码位置

| 路径 | 职责 |
|---|---|
| `backend/app.py` | 应用、静态前端、路由与队列 worker |
| `backend/core/` | 路径、数据库、schemas、运行状态、GPU 锁、媒体工具、ComfyUI/Pika 客户端 |
| `backend/models/` | 模型注册、参数校验、适配器与工作流修改 |
| `backend/services/video.py` · `video_backends.py` | 视频队列、ComfyUI 执行、外部后端与恢复 |
| `backend/services/continuation.py` · `native_audio.py` | 接续帧、冻结输入、MV 人声驱动与原轨合回 |
| `backend/services/images.py` · `songs.py` | 四槽参考图、歌曲与本地分析调度 |
| `backend/services/shot_planner.py` | 真实音频时间轴切镜与边界检查 |
| `backend/services/merge.py` | 场景接缝处理、全剧合并与缓存 |
| `backend/routers/` | system、projects、images、audio、voices、shots、merge API |
| `config/models.json` · `workflows/` | 模型能力与实际工作流/请求模板 |
| `frontend/` | 原生 JS 页面与生图区 Vue |
| `audio_analysis/` | 独立本地音频模型环境与缓存 |
| `tools/` | 批量建项、分析、备份和隔离测试 |
| `colab/` | Colab 端 ComfyUI 一键启动脚本，清单与缺口见 [colab/README.md](colab/README.md) |

依赖方向与改代码规则只在 [AGENTS.md](AGENTS.md#代码规则) 维护。

## 模型能力

模型清单、参数和模板路径以 [config/models.json](config/models.json) 为准；适配器自动发现，前端从 `/api/models` 和 `/api/video-models` 获取目录。

| 类型 / ID | 执行方式与边界 |
|---|---|
| image / `moss_image_5b`（默认） | ComfyUI；MOSS SFT INT8、Qwen3.5-9B、FLUX.2 VAE。当前配置 35 步、CFG 2.8；SLG 由图像工作流设置。真实输出尺寸读取配置，不能按比例名称猜尺寸。 |
| image / `krea2` | ComfyUI；保留 Krea 2 工作流与适配器。MOSS 专用参数和写实风格约定不自动适用于它。 |
| video / `b15_49`（默认） | ComfyUI；支持 T2V、CUT、CONTINUE，以及 MV 外部原声驱动。一采通常为 0.4MP/10 步；二采首轮仍为 0.4MP/10 步，放大至 1MP 后再做 5 步、denoise 0.35。关闭二采时使用镜头选择的一采分辨率。 |
| video / `pika_h3` | 独立 Pika API；CUT→ref2va、CONTINUE→i2va、T2V→t2va。5–15 整数秒、480p/768p、固定六步；不支持 B15 二采、额外 LoRA 或 MV 外部原声锁口型。 |
| audio / `yue2` | ComfyUI Plan→RenderPlan；歌曲与声部控制边界见 [YuE2 对唱参考](.agents/skills/mv/references/yue2-duet.md)。 |

新增常规模型：登记模型目录，放入工作流，复用或新增对应适配器。新执行协议通过 `execution_backend` 和通用后端注册接入；无需修改前端模型列表。

### Pika 实现与服务边界

- `backend/core/pika.py` 调用 `/api/health`、`/api/uploads`、`/api/jobs` 和 `/media/{id}`；健康检查不提交生成。
- `backend/services/video_backends.py` 负责执行、恢复和按远端 ID 取消。Pika 当前按独立 Colab GPU 部署；同卡同时驻留 ComfyUI 与 Pika 不在当前支持范围。
- UI 用 `megapixels=0.4/1.0` 编码 480p/768p 档位，实际宽高由官方比例桶决定。
- 输入快照保存 `request_template.json`、冻结的 `pika_endpoint`；提交前保存 `execution.json`。恢复读取原地址，不跟随后来修改的设置。
- 每次生成分配唯一远端 ID、远端 version=1，本地由 `generations` 管理版本。POST 回复丢失时核对原 ID，不盲目重交。
- 状态接口保留 ComfyUI 字段并增加 `pika` 子对象；普通视频保留 Pika 原生音频。MV 项目选择 Pika 时入队前报错，不静默关闭项目开关。
- 独立 Pika 仓库的 `colab_one.py` 当前只启动 Pika；没有 MOSS 下载或 ComfyUI 生图服务。

## 参考图、队列与产物

- 四槽：`ref1/ref2` 为人物，仅在显式 `ref_scope=project` 时跨 Scene 共享来源与队列；`ref3/ref4` 为场景，固定 `scene`。
- 生图或上传成功会应用到作用域内非运行中的 CUT，并设为未来默认图；CONTINUE 和已排队的冻结输入不被改写。
- CUT 按实际非空槽位顺序传参考图；CONTINUE 只使用 `first_frame_path`，不传四槽图；T2V 无图。B15 可将同一首帧用于 Guide/ReferenceLatent，但这不代表传入额外 Ref。
- CONTINUE 默认等待同 Scene 上一镜完成，并提取其最新成片末帧。后续参数修改不影响已冻结、排队的任务。
- 入队通过 `freeze_inputs` 保存参数、模板、素材与所需音频到 `inputs/<id>/`。批量入队先检查所选镜头的输入快照，避免半批提交。
- 开启 B15 场景二采后，批量渲染会重跑全部非运行镜头，包括 DONE；不支持二采的模型跳过该切换。
- 重新生成沿用保存的参数和素材，保留旧成功版本；本次失败记录本次错误，不清空旧成片。
- ComfyUI 只有 `/history`、`/queue` 连续三次都确认无任务才判丢失；Pika 也仅在 HTTP 查询成功且连续三次缺失时判丢失，网络失败不算确认缺失。

### GPU 调度

远端任务使用共享 `gpu_slot` 串行。ComfyUI 仅在 `gpu_key` 切换时 `/free`，同一模型连续执行不反复卸载；ComfyUI OOM 释放后重试一次。本地歌曲分析不卸载远端模型。Pika 使用 `gpu_slot(None)`，只串行，不向其发送 ComfyUI `/free`。

## 音频与自动分镜

- 本地分析已接入工作台，由 [backend/services/audio_analysis.py](backend/services/audio_analysis.py) 调起 [tools/local_audio_analysis.py](tools/local_audio_analysis.py)，使用 `audio_analysis/.venv` 的独立 Python 3.11 环境，不在 API 进程导入音频模型。
- 环境/缓存根可通过 `ANIME_STUDIO_ANALYSIS_HOME` 指定；恢复工具位于 `audio_analysis/`，不混入主 `.venv`。
- 普通歌曲使用 Demucs 分离人声/伴奏，再做识别或已有歌词对齐与节拍分析；配音干声可走 `dry_vocals` 分支跳过分离。产物包含 `vocals.wav`、`no_vocals.wav`、`timeline.json`、`lyrics.srt`；原音频保留。
- MV 使用分离后的纯人声驱动画面，下载后单镜合回对应完整原轨，全剧再合回整首原轨；不能把混有 BGM 的音轨复制成 `vocals.wav`。切片文件名带内容哈希以避免远端缓存旧音频。
- 上传或替换项目音频会使旧分析和镜头时间绑定失效。先确定音频、完成分析，再保存最终镜头时间。
- 配音任务保存在 `voice_jobs`，角色通过 `voice_job_speakers` 绑定全局 `voice_profiles`。项目音频来源由 `mv_projects.audio_source/source_voice_job_id` 记录。
- 配音完成仅在项目没有音频或当前音频来自配音台时自动同步；已有歌曲或外部音频时，通过“设为本项目音频”明确替换。旧 `/api/drama3/*` 路由已迁移到音色库和配音任务接口。
- 自动分镜读取真实对齐结果，所有行要求 `aligned_ratio == 1.0`。长句拆分、短句合并均由 `shot_planner.py` 处理；镜头数不保证等于台词行数。
- 当前默认目标 5–6 秒，软上限 7 秒、硬上限 9.6 秒、最短 3 秒、每 4 镜一个段首 CUT；可通过预览参数调整。说话人切换和双人构图需在 `initial_shots` 中显式规划，不假设默认算法会识别角色。
- B15 音频镜头按 25fps、0.04 秒网格寻找静音谷底，过滤标点后检查切点；非静音切点会给出告警，不代表可以忽略吞字风险。不要把此网格套到 Pika 的整数秒参数。
- `from-timeline` 先 `dry_run=true`，再用同一组规划参数和完整 `initial_shots` 保存。数量不符返回 `409 PLAN_CHANGED`，必须重新预览；替换已有镜头需 `replace=true`，且不能有排队或运行镜头。

## 合并与字幕

- “播放本场景”合并当前 Scene 的完成镜头并缓存；CONTINUE 接缝会处理重复静止帧、低频明暗/色度与静止背景微融合，运动区域避免整帧半透明叠加。实现细节以 `merge.py` 为准，不在各 Skill 重复维护常量。
- “合并全剧”统一画布和输出帧率后拼接各 Scene；按音频设置合回原轨，MV 可压制全曲歌词字幕。
- 产物为 `project_NNN_master_no_subtitles.mp4`、`project_NNN_master.srt`、`project_NNN_master.mp4`，场景成片为 `scene_NNN_master.mp4`。文件未变化时复用有效缓存。

## 主要数据表

| 表 | 内容 |
|---|---|
| `projects` · `scenes` | 项目、场景与顺序 |
| `mv_projects` | 项目音频、来源、MV 开关、分析路径和状态 |
| `scene_image_configs` · `scene_image_jobs` · `scene_images` | 四槽配置、队列和历史 |
| `scene_default_refs` | 后续 CUT 的默认参考图 |
| `shots` · `generations` | 分镜、冻结输入、远端任务、本地版本和产物 |
| `voice_profiles` · `voice_jobs` · `voice_job_speakers` | 全局音色、项目配音任务与角色绑定 |

成功状态使用后端维护的 DONE。`shots` 中旧 `face_refined_*`、`enhance_mode`、`review_*`、`attempt_id` 等列为兼容历史数据保留，不据此推断现有功能。

## 常用接口

请求体和完整接口以运行中 <http://127.0.0.1:18081/docs> 为准，下面只维护定位索引；接口存在不等于已获得执行授权。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/status` · `GET/PUT /api/settings` | 状态、`comfy_url`、`pika_url`、fps |
| `GET /api/models` · `GET /api/video-models` | 模型目录 |
| `GET/POST /api/projects` · `GET/POST /api/projects/{id}/scenes` | 项目与场景 |
| `GET/PUT /api/projects/{id}/mv` | 项目音频、MV 开关等配置 |
| `POST /api/projects/{id}/mv/song/generate` · `POST .../mv/audio` | 生成歌曲、上传音频 |
| `POST /api/projects/{id}/audio/analyze` · `GET .../audio/timeline` | 分析音频、查看时间轴 |
| `GET /api/voices` · `POST /api/voices` | 查询音色、上传参考音频/视频入库 |
| `GET/POST /api/projects/{id}/voice-jobs` · `GET/PATCH /api/voice-jobs/{id}` | 配音任务查询、保存 |
| `PUT /api/voice-jobs/{id}/speakers/{index}` | 绑定说话人与音色 |
| `POST /api/voice-jobs/{id}/generate` · `POST .../use-as-project-audio` | 生成配音、设为项目音频 |
| `GET /api/scenes/{id}/image-configs` · `PUT .../image-configs/{role}` | 四槽配置，GET 返回数组 |
| `POST /api/scenes/{id}/images/queue` · `POST /api/projects/{id}/images/queue` | 生图入队 |
| `POST /api/scenes/{id}/images/upload` · `POST /api/scene-images/{id}/apply` | 上传参考图、应用历史图 |
| `GET/POST /api/scenes/{id}/shots` · `GET/PATCH /api/shots/{id}` | 分镜查询与保存 |
| `POST /api/scenes/{id}/shots/from-timeline` | 时间轴分镜预览与带词建镜 |
| `POST /api/shots/{id}/generate` · `POST /api/scenes/{id}/generate` · `POST /api/projects/{id}/generate` | 单镜、场景、项目生成 |
| `POST /api/scenes/{id}/second-pass` | 对支持的模型切换场景二采 |
| `POST /api/scenes/{id}/merge` · `POST /api/projects/{id}/merge` | 场景与全剧合并 |
| `/api/shots/{id}/continuation/*` | 接续帧候选与自动接续 |

## 验证位置

- 隔离测试在 `tools/tests/`；按变更运行相关脚本，不提交 GPU 作业。
- 模型/视频：`test_model_registry.py`、`test_b15_resolution.py`、`test_native_audio.py`。
- Pika：`test_pika_video.py`、`test_pika_api_isolated.py`、`pika-model-ui.cjs`；使用模拟远端，不代表实际画质或口型测试。
- 前端语法：`node --check frontend/app.js`；Node 测试的依赖按具体脚本检查。
