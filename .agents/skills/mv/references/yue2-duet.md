# YuE2男女对唱：已知能力与准备规则

来源记录：2026-10-18；本地代码路径复核：2026-11-01。仅针对当前工程的 YuE2 与 pytraveler 节点，不把 YuE1 或其他音乐服务的标签语法直接套进来。入口见 [MV 流程](../SKILL.md)。

## 来源与证据等级

- [官方示例原始数据](https://raw.githubusercontent.com/map-yue2/map-yue2.github.io/main/data/cases.js)：Yacht Rock 示例在style请求男女对唱，在lyrics使用独立的 `(male vocal)`、`(female vocal)`；Honky Tonk 使用带 Him / Her / Both 的段落标题。两例的mode均为direct。说明官方展示了歌词内歌手标记的用法，不能宣称YuE2不支持男女轮唱；但本工程是Plan→RenderPlan，不能把direct示例当成本工程已验证结果。
- [Reddit对唱标签讨论](https://www.reddit.com/r/StableDiffusion/comments/1wux9q2/yue2_anyone_know_prompting_tricks_for_a_male_and/)：用户指向官方示例并推荐style写Duet、lyrics写声部标记；同帖也有人报告不按分配演唱。社区反馈说明控制可能成功，也可能偏离，不是硬绑定协议。
- [Reddit歌手控制与ABC讨论](https://www.reddit.com/r/YueMusicAI/comments/1x32g7h/any_way_to_enforce_voices/)：Yeufonic作者报告用标签后处理ABC改善分配；这属于其应用实现，不是本工程已具备能力。

- [YuE2官方生成指南](https://github.com/multimodal-art-projection/YuE/blob/main/docs/generation.md)：style与lyrics输入、CoT/ABC生成流程。此接口说明不等于提供了角色到段落的硬绑定。
- [当前节点包的歌曲编辑说明](https://github.com/pytraveler/YuE2-ComfyUI#the-song-editor)：作者报告，命名两个声部只提出双声部请求，段落性别标签和style内角色安排的12首试验未实现分段分配。这只是作者特定试验的负面结果；不能据此否定官方示例或用户已成功的实测。
- [复现规则](https://github.com/pytraveler/YuE2-ComfyUI#reproducibility)：相同种子与设置复现同一结果。改变种子只是另一个候选，不保证达到双声部或准确轮唱。

不使用第三方营销站“加一个标签必出对唱”的说法作为已支持证据；不虚构成功率。

## 当前本地实现（源码核对）

- [当前工作流](../../../../workflows/audio/yue2_optimized_api.json)：Options → Plan → RenderPlan → SaveAudio；style 和 lyrics 各为独立字符串，不存在 male_track/female_track 或每段 singer_id 输入。
- [歌曲服务](../../../../backend/services/songs.py) 的 `generate_mv_song` 当前使用 `seed=42`，[YuE2 适配器](../../../../backend/models/adapters/audio_yue2.py) 负责写入工作流。仅点重新生成而不改输入/设置，不是随机抽新种子；歌曲 UI 暂无可保存的 song_seed 字段。
- 正常Verse/Chorus等标签标记音乐结构，不绑定男/女；`[Verse 1]`等还会被后端规范化为非编号标签。
- H3 的 S1/S2、Ref 角色与视频提示词属于视频阶段，不会传回 YuE2 歌曲生成器，不能用它们指派歌词谁来唱。

## 写歌曲参数

1. 保留用户指定的英文，不擅自改中文。男女轮唱必须同时在style描述Duet和两种声音，并在lyrics的换人位置显式写声部标记。优先沿用用户已成功的准确写法；没有该写法时，可从官方 `(male vocal)` / `(female vocal)` 开始。不得删除声部标记或只把分配留在brief里。

候选格式（自写歌词，仅示范标签位置，尚未在本工程验证）：

```text
[Verse]
(male vocal)
Last train, your hand in mine
You say you're doing fine

[Verse]
(female vocal)
If you were waiting for a sign
I think you ought to know
```

官方也展示带Him/Her/Both的结构标题，不应一概禁止扩展标题。应核对实际Plan输入、输出ABC与RenderPlan输入；遇到标签丢失或忽略再定位具体处理步骤，不能先归因于模型不支持。
2. style用简洁的语言、曲风、两种明显区别的声音、配器、节奏与编曲描述，歌词换人位置配合明确声部标记；style叙述不能代替lyrics里的分配。下面只是候选写法，不是实测成功配方：

```text
English romantic contemporary pop duet, warm male tenor and bright female alto, contrasting lead voices, conversational call-and-response vocals, melodic catchy chorus, piano and clean electric guitar, tight pop drums, warm bass, 96 BPM, intimate verses and uplifting chorus, polished vocals-forward studio production, approximately one minute.
```

3. 歌词可写对话感，但歌词中有you/I、出现两个Verse、style写duet，都不能证明成片有两名歌手。目标一分钟靠歌词密度与曲式安排靠近，不能凭BPM和小节数承诺硬时长。
4. 用户期望的男/女分配另写项目brief，明确标为intended，不写成observed。已发现单声部的候选记为不符合双人要求，不继续假定下一段会自动变声。
5. 参数写入后读回，交还用户自行点击生成。本skill和上游文档中的运行命令不是本次提交生成的授权。

## 先验收音频，再排MV

- 先让用户听成品并确认真实男/女演唱段落；若Agent没有实际听音能力，不声称自己听过。
- 以实际听到的歌手与停顿记录start/end/singer，单声部、轮唱和重叠合唱分开；没有可靠依据的段落记unknown，不猜。
- WhisperX文字对齐不是歌手识别；Demucs当前只分人声/伴奏，不按男女拆轨。音高变化也不足以证明两位不同歌手。
- 只有验收通过后再写男女演唱Shot。单人唱的区间拍对应人物；重叠合唱不能承诺一条混合人声精确控制两张嘴。

## 需要确定性时的边界

目前一次style+lyrics请求属于尝试，不提供逐句歌手硬控制。若必须严格按句交替，需另外设计并验证分轨演唱/音色转换/编辑与同一伴奏对齐的方案，或使用用户提供的合格对唱音频；当前未接入这些能力，不自动装模型、换服务或改工作流。

不能简单生成一首男版与一首女版再切着拼：两次独立生成的旋律、节奏、伴奏和句长未必一致。即便供应相同ABC，也需验证音频时序、音色与衔接，不当作已解决方案。
