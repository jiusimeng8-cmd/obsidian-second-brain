# Obsidian Second Brain

把 OpenCode / OpenChamber 里的历史对话，自动整理成一座可检索的 Obsidian 笔记库。

一套在你本机常驻运行的「第二大脑」：把分散在各个项目会话里的决定、偏好、结论、进度和未完成事项，
提炼进 Markdown 笔记，并保留来源、时间与核验状态。你继续按项目隔离聊天，不用手工整理。

```
OpenCode 会话（任意目录、含已归档）
        │  本机回环 API，只读取
        ▼
采集器：分页取消息 → 冻结已结束的回合 → 重要窗口抽样
        │
        ▼
独立 SQLite 台账：来源游标 / 任务 / 证据 / 核验记录
        │  串行派发，一次只跑一个
        ▼
一个可见会话「OBS 记忆整理」←→ Obsidian MCP 工具
        │  写笔记 + 回读 + 逐字引文 + 来源 ID
        ▼
验证器：文件真的变了？引文真的在？回读真的发生？来源真的对？
        │  通过才算完成；否则保留重试
        ▼
Obsidian 笔记库（长期知识）+ 处理进度看板
```

## 它解决什么问题

- 按项目分聊天时，跨会话的知识会被切断：服务器问题在一个会话、项目决策在另一个会话。
- 让模型「记住」整段历史既慢又贵，而且大量内容是一次性调试噪声。
- 直接把聊天记录塞进模型上下文，会得到大量重复笔记、无来源断言、以及「说写了其实没写」。

本项目的取舍是：**不追求完整精读，只保留重要的那一部分，但每一部分都可核验、可回溯。**
未选中的原文仍在原会话里，可随时回查；它们被明确标记为「未精读」，而不是假装已经审阅过。

## 核心设计

| 设计 | 原因 |
| --- | --- |
| 独立 Python 后台进程 + OS 文件锁 | 插件会在每个项目位置各加载一次；文件锁保证只有真是单写者。界面关掉也继续跑。 |
| 自带 SQLite 台账 | 采集游标、任务状态、证据、核验记录都不能依赖会话本身；也绝不改写 OpenCode 的数据库。 |
| 单一可见整理会话 | 不再为每次整理新建子对话，列表里始终只有一个「OBS 记忆整理」。可以直接和它对话。 |
| 重要窗口抽样 | 每段会话只取：开头需求、中间明确决定/偏好/纠正、最近结论与待办。工具日志、反复试错被丢给原文。 |
| 批次而不是逐条 | 相关会话合并成一批，一次调用整理多个主题，显著降低调用次数。 |
| 写后验证 | 没有真实回读、没有逐字引文、没有来源会话与消息 ID，就不算写入完成。 |
| 选择游标 ≠ 事实游标 | 「这段看过了」和「这些事实已经进笔记」是两件事，避免误报「全部处理完」。 |
| 每日提交预算 | 防失控：所有模型调用（含重试）计入滚动 24 小时配额。 |

## 快速开始

要求：Windows（Linux/macOS 亦可，见 [docs/安装.md](docs/安装.md)）、Python 3.10+、Node.js、Obsidian 笔记库、
OpenCode 或 OpenChamber 桌面端，以及一个 Obsidian MCP 服务（`@bitbonsai/mcpvault` 或等价实现）。

```powershell
# 1. 放到 OpenCode 的插件同级目录
git clone <this-repo> "$env:USERPROFILE\.config\opencode\second-brain"
Copy-Item "$env:USERPROFILE\.config\opencode\second-brain\config.example.json" `
          "$env:USERPROFILE\.config\opencode\second-brain\second-brain\config.json"

# 2. 编辑 config.json，至少改 vault 为你笔记库的绝对路径

# 3. 首次运行建议先慢：dispatch=false 只采集，不调用模型
powershell -File "$env:USERPROFILE\.config\opencode\second-brain\second-brain\start-worker.ps1"

# 4. 观察，再打开派发
#    config.json: "dispatch": true
```

插件 `plugin/obsidian-memory.js` 负责守护：心跳超过 90 秒就重启后台进程；它本身不说话、不产生对话。

验证是否真的在工作：

```powershell
# 进程 + 心跳 + 健康状态
Get-Content "$env:USERPROFILE\.config\opencode\second-brain\second-brain\state\health.json"
# 或直接看笔记库里的看板
# <vault>/01-看板/第二大脑处理进度.md
```

完整步骤（含 MCP 配置、笔记库规则安装、权限与回滚）见 [docs/安装.md](docs/安装.md)。

## 文档

- [docs/安装.md](docs/安装.md) — 从零部署，含 MCP、笔记库规则、验证清单
- [docs/架构.md](docs/架构.md) — 模块划分、数据表、状态机、为什么这样分工
- [docs/配置.md](docs/配置.md) — 每个配置项的含义与推荐值
- [docs/运行与运维.md](docs/运行与运维.md) — 看板指标、暂停/恢复/回滚、常见故障
- [docs/笔记库规则.md](docs/笔记库规则.md) — 让业务会话主动检索记忆的入口与规则
- [docs/AI复刻指南.md](docs/AI复刻指南.md) — 给另一个 AI 的完整复刻指令

## 目录结构

```
second-brain/
  worker.py        调度：写者会话、模型选择、轮换、派发、心跳、看板
  collector.py     采集：会话盘点、游标、冻结回合、消息落库
  focus.py         选择：重要窗口、批次合并、旧任务接管
  quality.py       验证：回执解析、真实工具调用、文件/引文/来源核对
  api.py           本机回环 API 适配（读取凭据从不打印）
  storage.py       台账 schema、脱敏、日志、文件锁
  prompt.txt       完整精读模式的任务提示词
  focus-prompt.txt 重要窗口模式的任务提示词
  start-worker.ps1 手动启动
plugin/
  obsidian-memory.js  OpenCode V2 插件，仅做进程守护
docs/vault/        可直接放进笔记库的入口、规则与节点模板
```

## 设计边界（请先读）

- **不是「记住一切」。** 它做的是重要信息提炼，未选中的原文保留在原会话，标记为可回查。
- **验证的是过程，不是真理。** 它证明「笔记被写过、被读过、引文存在、来源对得上」，不证明结论在业务上正确。
- **凭据不进笔记。** 采集时做启发式脱敏，笔记只允许写普通 Markdown；凭据目录被排除在读写之外。
- **只接 OpenCode / OpenChamber。** 其他客户端需要各自的适配器，本项目不假装已支持。
- **附件只留元数据。** 文档/图片正文不解析。
- **调用有预算。** 全历史回补是长期任务，不要用「多久跑完」当验收标准。

## 测试

```powershell
cd second-brain
python -X utf8 -m unittest test_recovery test_focus -v
```

回归覆盖：长消息无损切分与断点续跑、结束后只推进连续核验游标、拒绝「声称已写但无工具证据」、
不允许实质内容伪装成无价值、人工回合阻止自动派发、模型冷却回退、脱敏、跨进程锁、逐行回读覆盖、
归档后新建、多来源批次、共享历史 fork 的新决定、抽样缺页标记。

## 许可

MIT，见 [LICENSE](LICENSE)。
