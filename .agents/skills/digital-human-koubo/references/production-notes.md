# 口播制作与交付参考

由[口播 Skill](../SKILL.md)按需引用。这里只保留切片预检、成片检查和可选包装，建项与访谈排镜规则回到 Skill 正文。

## B15 首镜音频预检

带词建镜并读回检查后，将 `first_shot_id` 替换成当前项目首镜 ID，使用主环境执行以下检查。仅在临时目录生成音频切片，不提交远端 GPU 任务，也不写业务数据库。

```python
from pathlib import Path
import tempfile
from backend.services.repo import get_shot
from backend.services.native_audio import freeze_song_audio
from backend.core.db import db, get_settings
from backend.core.media import frame_count

with tempfile.TemporaryDirectory() as tmp:
    snapshot = freeze_song_audio(
        get_shot(first_shot_id), Path(tmp),
        {"db": db, "get_settings": get_settings, "frame_count": frame_count},
    )
    assert snapshot.get("native_song_audio") is True
```

确认项目已选定正确音轨并开启原声驱动，所需原轨、人声和时间区间有效。失败时修配置或分析产物，不将混音改名绕过检查；成功也只代表切片可准备，不能证明最终口型质量。

生成前用远端 `/object_info` 核对当前工作流所需节点与模型。原 ComfyUI 启动脚本位于桌面 `三合一.py`；按实际部署检查，不从旧文档复制另一套模型名、LoRA 开关或尺寸。

## 抽帧与接缝检查

| 位置 | 核对 |
|---|---|
| 开场与段首 CUT | 人脸、服装、参考图实际房间方位，段首景别变化是否自然 |
| 双人镜 | 当前说话者嘴型、听者是否闭嘴、两人空间与视线是否一致 |
| CONTINUE 接缝 | 静止帧卡顿、手部重影、脸和亮度漂移 |
| 音频边界 | 是否吞字、补静音或截断气口，动作是否配到纯人声区间 |

切点、25fps 网格、接缝处理和缓存的实现细节集中在[技术速查](../../../../AI_QUICK_CONTEXT.md)，修复时查看实际代码，不在 Skill 固定缓存版本号与内部阈值。

## 可选 Remotion 包装

工程为 [D:/ai/Remotion_Koubo_Studio](D:/ai/Remotion_Koubo_Studio)。先确认本地工程、脚本和模板存在；本说明不自动安装、切换或调用其他服务。

- `Tutorial169`：16:9 横屏圆角人物窗与产品卡片，是既有产品介绍/解读的首选样式；人物窗口大小直接读取当前 `Tutorial169.tsx`，不照抄旧坐标。
- `Huazi`：9:16 竖屏花字，控制装饰数量，避免遮挡脸。
- `Tutorial169` 第 0 帧有居中 3:4 封面区域（当前 x=555、宽 810、高 1080），需要正脸母图 `public/cover_host.png`。每次改布局重新检查实际手机裁切。
- 沿用项目对外保密约定：成片不暴露真实底层模型、内部参数、seed、脚本名及内部制作步骤；内部调试说明不放进公开视频。

### 编排约定

- 人声讲完整句子，卡片给证据与数字；默认每屏文字不超过约 15 字、单节点 2–5 个词，不堆段落和列表。
- 元素绑定 `tl.json` 的触发词逐个入场，可在触发前约 0.25 秒开始；关键数字滚动至对应语音落点，音效克制。
- 按内容选动态表现：划掉旧判断再显示证据（strike-flow）、真图聚焦加少量数字（spotlight-shot）、共用零基线做尺度对比（scale-compare）、结尾用 A→B 演进（kinetic-flow）。
- 主数字/主词可用 80–150px，辅助标签约 22–26px；中性底与深色字，每屏突出 1–2 个强调色重点。
- 改完包装先出约 4 张关键帧确认视觉方向，再渲染整片；已有明确确认时继续执行，不重复确认同一方案。

### 素材与时间轴

保留原素片，将本次合并视频复制为工程 `public/main.mp4`，正脸母图复制为 `public/cover_host.png`。完整配音与脚本生成逐字时间轴：

```powershell
python D:/ai/Remotion_Koubo_Studio/scripts/studio_bridge.py align D:/ai/Remotion_Koubo_Studio/script.json D:/ai/AI_Anime_Studio/projects/project_XXX/mv/audio/voice_N.wav --out D:/ai/Remotion_Koubo_Studio/src/tl.json
```

替换项目和任务占位后执行。需要抠像时查看工程内 `scripts/cutout_video.py` 的实际参数；默认交付不强制抠像。

### 渲染

在该工程目录执行。Windows 使用有限并发与 256MB 帧缓存，临时文件放工程 `.tmp`：

```powershell
Set-Location D:/ai/Remotion_Koubo_Studio
New-Item -ItemType Directory -Force -Path .tmp | Out-Null
$env:TEMP = "D:\ai\Remotion_Koubo_Studio\.tmp"
$env:TMP = "D:\ai\Remotion_Koubo_Studio\.tmp"
python scripts/check_edit.py src
node node_modules/@remotion/cli/remotion-cli.js still src/index.ts Tutorial169 cover_frame0.png --frame=0 --browser-executable="C:\Program Files\Google\Chrome\Application\chrome.exe"
node node_modules/@remotion/cli/remotion-cli.js render src/index.ts Tutorial169 out_169.mp4 --concurrency=2 --offthreadvideo-cache-size-in-bytes=268435456 --browser-executable="C:\Program Files\Google\Chrome\Application\chrome.exe"
```

竖屏选择 `Huazi` composition，输出另一个文件。命令中的 Chrome 路径按本机安装调整。

只更新封面时可先导出新封面，再输出新视频以保留原片；既有替换头两帧的命令如下：

```powershell
ffmpeg -y -i out_169.mp4 -i cover_frame0.png -filter_complex "[0:v][1:v]overlay=enable='lte(n,1)'[v]" -map "[v]" -map 0:a -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a copy out_169_new.mp4
```

核对新文件时长、音轨、首帧封面和人物遮挡后再交付，不覆盖唯一的成功素片。
