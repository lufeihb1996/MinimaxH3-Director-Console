# Colab 端启动脚本

远端只跑推理，项目资料仍在本地 `projects/`。本目录存放 Colab 端 ComfyUI 一键启动脚本，供随时重新拉起远端服务；脚本内已包含模型下载、自定义节点、comfy-kitchen 预检和隧道启动。

本地工作台与远端是两步：先运行这里的脚本，把打出的隧道地址填进设置页的 `comfy_url`；再双击根目录 `start.bat` 启动本地后端。

## 现有脚本

| 文件 | 覆盖模型 | 对应项目能力 | 来源日期 |
|---|---|---|---|
| [三合一_H3-MOSS-Drama3.py](三合一_H3-MOSS-Drama3.py) | H3 b15-49 两轮采样 + MOSS-Image 5B SFT INT8 + Fish Audio Drama3 35B MoE + Qwen3-ASR / ForcedAligner | 生图 `moss_image_5b`、视频 `b15_49`、配音工作台（Drama3 音色克隆） | 2026-10-29 |
| [二合一_MOSS生图-OpenH3精修.py](二合一_MOSS生图-OpenH3精修.py) | MOSS-Image 5B SFT INT8 + H3 b15-49 一采 + NVIDIA Open-H3 Regenerate 二采（1080p 2.07MP / 2K 3.69MP） | 生图 `moss_image_5b`、视频 `b15_49`（含 720p/1k/1.5k/2k 二采档位）；无音频模型 | 2026-11-1 |
| [YuE2_纯歌曲.py](YuE2_纯歌曲.py) | ComfyUI + YuE2-ComfyUI staged nodes（Plan → Render Plan → Save Audio），YuE2 3B INT8 ConvRot | 音频 `yue2`（歌曲、MV） | 2026-9-25 |

三者互不重叠：要配音就用三合一，只做生图和视频用二合一（省约 36GB 音频权重），单独出歌用 YuE2。生图与视频在同一实例内切换时由后端 `/free` 调度显存，不用重启 Colab。

二采参数以代码为准：3 步 Euler、`start_sigma` 0.78 锁动作；`config/models.json` 里的 `refine_steps` / `refine_denoise` 两个键当前没有代码读取。

## 输出位置

脚本把输出放在 `/content/ComfyUI/output_local`，工作流前缀仍是 `anime_studio/{images,videos,music}`；本地后端通过 ComfyUI 的 `/view` 接口下载成片，不依赖 Drive 挂载。

## 尚缺的启动脚本

| 缺口 | 影响 | 现有替代 |
|---|---|---|
| Pika API 服务 | 视频 `pika_h3` 需要 `/api/health`、`/api/uploads`、`/api/jobs`、`/media/{id}` 这组接口，由独立 Pika 仓库的 `colab_one.py` 启动 | 桌面 `Pika_H3_Flex_Colab测试\00_Pika_H3_Flex_一键启动.py` 起的是 ComfyUI 版，接口协议不同，不能替代 |
| Krea2 生图 | `config/models.json` 仍登记 `krea2`，但上面三个脚本都不含 Krea2 权重 | 桌面 `生图 视频一体\Krea2_生图_Singularity_Ref2VA_VSA_4步8步一体版\00_一键启动.py` |

`OpenH3_2K精修_Colab.py`（把已有草稿视频单独精修到 2K）目前没有对应的项目接口，暂不需要放进项目。
