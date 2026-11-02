---
name: mv
description: 在 AI Anime Studio 中建立和准备 MV、剧情及普通短片项目，选择音频模式、配置参考图、组织分镜并验收合并。数字人口播与访谈使用 digital-human-koubo。
---

# MV 与短片制作流程

操作边界遵循 [AGENTS.md](../../../AGENTS.md)；模型能力、参考图作用域、队列和接口见 [技术速查](../../../AI_QUICK_CONTEXT.md)。这里只维护制作顺序。

## 先选声音与模型

- **外部原声 MV / 原片音轨**：使用支持音频锁定的 B15，开启项目 `native_audio_enabled`。先确定并分析音频，再依据真实时间轴建镜。
- **原创有声短片**：关闭该开关，由模型按视频提示词生成声音；台词与环境音写进 `prompt`，`dialogue` 只作界面备注。
- **Pika**：当前只用于普通有声视频，不能拿音频参考或后期配音冒充外部原声锁口型。参数限制见技术速查，不套 B15 二采配置。
- **数字人口播/访谈**：转用 [digital-human-koubo](../digital-human-koubo/SKILL.md)，使用配音工作台。
- **YuE2 男女对唱**：需要对唱时读取 [yue2-duet.md](references/yue2-duet.md)，先验收实际声部，再排演唱镜头。

## 建立项目与参考图

普通批量建剧编写 UTF-8 JSON，运行：

```powershell
.venv\Scripts\python.exe tools\quick_seed_project.py tools\spec_xxx.json
```

最小结构示意；人物、提示词、时长和画幅按本次需求填写：

```json
{
  "name": "项目名称",
  "aspect_ratio": "9:16",
  "image_model": "moss_image_5b",
  "lora_mode": "none",
  "video_model": "b15_49",
  "second_pass_enabled": false,
  "native_audio_enabled": false,
  "audio_linked": false,
  "ref1_prompt": "跨场景主角的完整生图提示词",
  "scenes": [{
    "name": "第一幕",
    "ref3_prompt": "本幕无人物环境图提示词",
    "shots": [{
      "title": "镜头 1",
      "mode": "CUT",
      "seconds": 5,
      "prompt": "按视频提示词 Skill 写好的完整正文"
    }]
  }]
}
```

- 新项目省略 `project_id`。工具收到已有 `project_id` 时默认会替换场景；只有用户明确要求覆盖才使用 `replace_scenes=true`。追加场景需显式设为 `false`。
- 顶层 `ref1_prompt/ref2_prompt` 用作项目共享人物；场景可设置单独提示词与 `ref1_scope/ref2_scope`。作用域的实际行为由技术速查维护。
- `ref3/ref4` 是本场景环境，未使用的槽位关闭。参考图上传或应用会影响未运行的 CUT，先核对作用域和已有镜头。
- 人物与场景提示词使用 [MOSS 生图 Skill](../moss-image-realism/SKILL.md)。跨场景复用人物时保持服装、发型和身份一致。

## 按制作顺序准备

1. 保存项目、场景和参考图配置；已有可靠母图优先复用。
2. 有外部音频时，先上传/选定音轨并完成分析，再保存最终分镜时间；不要先绑时间后替换音频。
3. 按 [H3 视频提示词 Skill](../minimax-h3-ref-animator/SKILL.md) 编写每镜正文。CUT 用实际参考图，CONTINUE 从上一镜末帧延续；换人物或重新建立构图时安排 CUT。
4. 依据真实时间轴填写演唱/说话镜头，不用累计估算代替音频对齐；可用 `from-timeline` 预览后带完整提示词保存。
5. 读回镜头、参考图、音频开关和模型配置。先用一采检查人物、动作与声音，再按需要切换 B15 二采；二采会重跑哪些镜头见技术速查。
6. 按用户授权执行生成与合并；场景完成后检查代表镜头和接缝，再交付全剧成片。

## MV 验收重点

- 驱动嘴型的文件必须是真实纯人声，不能把混音原轨改名冒充。原片说话窗口逐秒检查人声能量；项目排查经验阈值为 `voc_rms >= 0.03`，低值段先确认是否为停顿，不能配持续说话动作。
- 保留系统生成的音频内容哈希文件名，避免重生成命中旧音轨缓存。切片与原轨合回机制由技术速查维护，不手改工作流节点。
- 连续镜头核对脸、服装、光线和左右方位；动作短片剪辑节奏由视频 Skill 维护，不能套到固定机位口播。
- 检查口型对应实际人声、镜头边界无吞字、CONTINUE 接缝无明显重影；不能仅凭 DONE 宣称画质或口型合格。
