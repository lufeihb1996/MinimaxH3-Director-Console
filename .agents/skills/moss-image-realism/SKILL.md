---
name: moss-image-realism
description: 为 AI Anime Studio 的 MOSS-Image SFT 编写真人、纪实和手机抓拍风格的参考图提示词，处理皮肤质感、人物构图和环境图。其他模型或非写实题材不自动套用此风格。
---

# MOSS 真人参考图提示词

当前模型参数、负向提示词和尺寸统一读取 [模型目录](../../../config/models.json)；能力说明见 [技术速查](../../../AI_QUICK_CONTEXT.md#模型能力)。保持已配置的 CFG、步数和 SLG，不在口播或 MV 文档另写一套默认值。

## 写人物图

用连贯自然语言覆盖以下五项，按本次人物与构图改写，不把示例人物当成所有项目的默认主角：

1. **媒介与构图**：原相机/纪实照片、画幅、景别、机位；需要清晰环境时写全景深。
2. **身份与皮肤**：脸型、眼型、发型及自然纹理：微毛孔、面部绒毛、T 区轻微油光、眼下微阴影与轻微不对称、未经磨皮的皮肤。
3. **服装**：具体款式、颜色、面料和重力褶皱；与既有人设保持一致。
4. **瞬间动作**：轻微不对称姿势、眼神和一个自然小动作，避免僵硬摆拍。
5. **环境与光线**：明确家具和杂物、光源方位、真实曝光与细微噪点。

手机抓拍风格起手式可用：

```text
An unpolished, candid smartphone snapshot, raw unedited everyday photography, eye-level medium waist-up shot, deep depth of field with the room clearly in focus, no artificial bokeh.
```

皮肤描述可组合：

```text
Visible micro-pores across the cheeks and nose bridge, fine vellus facial hairs catching the light, natural skin oils with a soft T-zone sheen, subtle under-eye shadows, slight natural facial asymmetry, realistic dry lip texture, zero beauty filter and no airbrushing.
```

这套写实目标下，少用 `flawless skin`、`beauty filter`、`creamy bokeh`、`masterpiece` 等容易把画面推向精修影楼的词。不要把某个词、手机型号或一个 CFG 值承诺为真实感保证。

## 按用途选景别

| 用途 | 构图重点 |
|---|---|
| 口播/访谈人物 Ref1、Ref2 | 半身中景或中近景，脸清晰；需要后期标题时留自然头部空间 |
| 动作片人物 Ref1、Ref2 | 全身或七分身，主体约占画面 35%–45%，双手、四肢和衣摆完整 |
| 环境 Ref3、Ref4 | 纯环境、明确前中后景与光源方位，不混入人物 |

环境图末尾可写：

```text
Empty environment scene, no people, no characters in frame, no text, no watermark.
```

## 人设复用与改图

- 有固定人物资产时优先上传复用原图；相同 seed 重生成不能保证同一张脸。
- 改构图重生图时换 seed 做候选，并人工核对身份；固定资产的原始 seed/参数仅作为复现记录。
- 跨镜头保持服装、发型与不可变面部特征。若用户要换人设，另建资产，不覆盖原人物。
- 四槽共享与自动应用行为见[参考图与队列说明](../../../AI_QUICK_CONTEXT.md#参考图队列与产物)，本 Skill 只维护画面和提示词。
