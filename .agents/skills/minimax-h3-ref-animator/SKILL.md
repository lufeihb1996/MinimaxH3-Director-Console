---
name: minimax-h3-ref-animator
description: 为 AI Anime Studio 的 H3 视频编写 CUT、CONTINUE、T2V 提示词，规划镜内剪辑、运镜、角色空间连续和声音。口播流程及固定机位要求由 digital-human-koubo 维护。
---

# H3 视频提示词与镜头设计

本项目采用 MiniMax H3 的 Ref2VA 六段式、I2VA/T2VA 三段式和 `[Shot N]` 时间语法。模型能力见 [技术速查](../../../AI_QUICK_CONTEXT.md#模型能力)；实际时长、画幅与后端参数按当前镜头填写。

## 先确定题材与输入

- 动作、漫改、快节奏剧情默认每个约 5 秒的生成段安排 2–3 个小镜头，参考 `CUT → CONTINUE` 交替；用户要求长镜头或慢节奏时按其意图调整。
- 真人固定机位口播、访谈按 [digital-human-koubo](../digital-human-koubo/SKILL.md) 执行，每次生成仅 `[Shot 1]`，不套动作片内切规则。
- MOSS 母图构图和写实风格由 [moss-image-realism](../moss-image-realism/SKILL.md) 维护。

| 模式 | 提示词结构与引用 |
|---|---|
| CUT | 六段式 Ref2VA；按实际非空 Ref1→Ref4 顺序编号 `<Picture N>`，抽离为 `<Subject N>` |
| CONTINUE | 三段式 I2VA；仅 `<Picture 1>` 表示当前首帧，不引入其他 Ref |
| T2V | 三段式 T2VA；不写图片引用或首帧对齐行 |

例如只有 Ref1 和 Ref3 有图时，它们依次是 `<Picture 1>`、`<Picture 2>`。若 Ref2 也上传了，即使本镜不描写它，Ref3 仍是 `<Picture 3>`。

## 剪辑与空间连续

- 一个小镜头写一个景别、一个主动作和一个主运镜。
- 内切格式为 `[Shot 2] At 00:01.800, the camera cuts to ...`，时间递增且小于总时长；动作片每段通常至少 1.2 秒，切点放在动作节点或台词停顿处。
- 快剪时相邻小镜头换景别或角度；说话段尽量给单人中近景和足够时长，不在半句中切走。
- CUT 的 `subject_definitions` 从参考图抽离人物/场景；正文用 `<Subject N>`。远景缩放或多人同框在 `retention_analysis` 说明 `partially_preserved` 与保留的身份特征，近景可用 `fully_preserved`。
- 固定左右与前后关系，反打保持 180° 轴线。攻击写清“起点→方向→命中点”；正脸主体的远程攻击可用前景越肩与纵深构图。
- 特效和烟雾写明相对位置，保留需要辨认的脸、眼睛和命中目标。
- CONTINUE 从上一镜末帧构图开始，只延续首帧已有主体；需要新人物或全新构图时用 CUT。是否立即换景别取决于题材，口播可保持机位。
- 动作片可用约 40% 同框交代空间、60% 单人近景表现反应作为起点，不把比例当成所有题材的限制。

## 运镜表达

常用句式：`The camera [动词] with [small/large] amplitude at [slow/fast] speed.` 固定机位用 `The camera holds a static shot.`

| 动词 | 用途 |
|---|---|
| pushes in / pulls out | 前推、后拉 |
| zooms in / zooms out | 变焦 |
| pans left/right | 水平摇 |
| trucks left/right | 水平平移 |
| tilts up/down | 上下摇 |
| pedestals up/down | 整机升降 |
| moves in an arc | 环绕 |
| tracking shot | 跟拍 |
| holds a static shot | 固定机位 |
| shakes slightly/strongly | 可选震动后缀 |
| POV | 主观视角 |
| rolls clockwise/counterclockwise | 绕镜轴翻滚 |

动作/冲击段通常用 `large amplitude at fast speed`；说话、凝视用小幅慢速或固定。避免在同一小镜头堆多个相互冲突的运动。

## 人声与口型

- 说话镜保持脸清晰，写 `clearly synchronized lip movement`、自然眨眼与稳定面部特征；按需要留约 3 秒完整说话段。
- MV 外部原声驱动时不再写另一套 `<d>` 台词，人物动作对应实际纯人声窗口。
- 模型原生发声时，将台词写入 `prompt` 正文，例如 `(S1) says: <d>[Japanese] ...</d>`；语言和内容改成本次要求，环境音写进 `overall_soundscape`。不要只填写界面的 `dialogue` 字段。
- 无后期字幕需求时写 `No subtitles, no captions, no on-screen text.`。

## CUT 模板

保留六个标题，按实际非空参考图定义主体；以下三镜只示范动作片节奏：

```text
subject_definitions:
<Subject 1> is the [人物身份、脸、发型与服装] in <Picture 1>.
<Subject 2> is the [场景与光源] in <Picture 2>.

summary:
[reference generation] A [题材与镜头数] sequence: [主体动作与结果].

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2], [Shot 3]): partially_preserved - rescaled to [景别] while [身份与服装] are retained.
<Subject 2> (appears in [Shot 1], [Shot 3]): fully_preserved - [保留的环境特征].

detailed_description:
The target video is a [实际画幅与风格] video. No subtitles, no captions, no on-screen text.
[Shot 1] A [景别与角度] shot of <Subject 1> in <Subject 2>. [一个主动作]. The camera [动词] with large amplitude at fast speed.
[Shot 2] At 00:01.800, the camera cuts to [不同景别与反应]. The camera holds a static shot.
[Shot 3] At 00:03.300, the camera cuts to [新景别、方向和结果]. The camera [动词] with large amplitude at fast speed.

overall_soundscape:
[按顺序描述环境声与动作音效].

non_diegetic_music:
N/A
```

## CONTINUE / T2V 模板

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description:
[Shot 1] The shot begins exactly from <Picture 1>: [首帧真实构图、主体动作与运镜].
[Shot 2] At 00:02.500, the camera cuts to [已有主体的新景别与动作]. No subtitles, no captions, no on-screen text.

overall_soundscape:
[环境声、音效或台词].

non_diegetic_music:
N/A
```

T2V 删除首帧对齐行与图片引用。口播保留三段式标题，只写 `[Shot 1]` 和固定机位，完整操作见口播 Skill。

## 保存前核对

模式与结构匹配；参考编号正确；切点适合实际时长；动作和运镜不冲突；人物服装、方位与上一镜连续；说话内容/外部人声窗口正确；没有未替换的模板占位。
