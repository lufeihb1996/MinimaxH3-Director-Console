# AI Anime Studio：AI 操作约定

作用范围：本项目目录。这里只维护共用操作规则；实现事实见 [AI_QUICK_CONTEXT.md](AI_QUICK_CONTEXT.md)。

## 按任务选择 Skill

| 任务 | 读取 |
|---|---|
| MV、剧情、普通短片的建项与制作流程 | [mv](.agents/skills/mv/SKILL.md) |
| 数字人口播、访谈、配音与口播包装 | [digital-human-koubo](.agents/skills/digital-human-koubo/SKILL.md) |
| MOSS 真人生图提示词 | [moss-image-realism](.agents/skills/moss-image-realism/SKILL.md) |
| H3 视频提示词、镜头与运镜 | [minimax-h3-ref-animator](.agents/skills/minimax-h3-ref-animator/SKILL.md) |

只加载当前任务需要的 Skill 和参考文档。口播的固定机位规则由口播 Skill 维护，动作片剪辑规则由视频提示词 Skill 维护，不互相套用。普通代码维护按相关模块读取技术文档，无需加载制作 Skill。

## 操作边界

- 默认准备配置、文案、分镜和预览；生成、音频分析、合并由用户触发。用户已明确授权代为执行时，按授权范围完成，不重复索取同一授权。
- 业务数据写入走后端 API 或现有建项工具；SQLite 用于只读核验，不用临时 SQL 绕过业务逻辑。
- 普通批量建剧使用 `tools/quick_seed_project.py`；按音频建镜使用 `shots/from-timeline`。覆盖已有项目必须属于用户明确要求的范围。
- 复用已有 `http://127.0.0.1:18081` 服务。后端启用 `--reload`；有生成、分析或合并任务运行时，不修改会触发重启的 Python 文件。
- 重新生成失败仍保留旧成功产物；不新增暂停、恢复、中断或重置入口。远端任务丢失的确认规则见技术文档。
- 不修改已排队任务的冻结输入，不把离线校验说成画质或口型实测。

## 代码规则

- 依赖方向：`core ← models ← services ← routers`。可变运行状态放 `backend/core/runtime.py` 的 `rt`。
- 模型登记在 `config/models.json`，模型差异放 `backend/models/adapters/`；新增执行后端扩展通用后端注册，不在业务流程散落模型 ID 分支。
- 工作流放 `workflows/{image,video,audio}/`，路径在模型目录登记。模型能力、参数与节点以配置和实际工作流为准，不虚构远端能力。
- 保留统一 GPU 调度、参考图作用域、接续与产物保护行为；修改前查技术文档对应流程。ComfyUI 的卸载接口不能发给 Pika。

## 验证与文档维护

- 后端改动跑 `tools/tests/` 中相关隔离测试；前端改动运行 `node --check frontend/app.js`。未获授权不提交真实 GPU 任务。
- 文档改动检查本地链接、Skill 元数据与规则适用范围，无需跑生成。
- 同一事实只维护一处：技术事实写入 `AI_QUICK_CONTEXT.md`；制作步骤、提示词和模板写入对应 Skill。其他入口使用链接引用。
