# AI 复刻指南

给另一个 AI（或未来的你）的完整规格。目标：读完本文 + 仓库代码，能在另一台机器上重建同样的系统，
并且知道哪些地方不能简化。

## 1. 先确认环境事实，不要假设

在动手前必须实际读取，不要凭接口文档猜测：

1. 客户端版本与插件 API 形态（本仓库对应 `export default { id, setup(ctx) }`）。
2. 本机客户端 API 的端口与本地客户端令牌存放位置（本项目读取
   `~/.config/openchamber/settings.json` 的 `desktopLocalPort` 与 `desktopLocalClientToken`）。
3. 真实可用的模型清单（`GET /api/model`），不是配置里写了什么。
4. MCP 服务真的能读写一个笔记（做一次真实 `read_note`），而不是「列表里显示 connected」。
5. 会话消息接口返回的真实结构：助手正文常在 `content[]` 里，`text` 可能为空。

## 2. 必须实现的接口

| 用途 | 接口 |
| --- | --- |
| 会话列表（含归档） | `GET /api/session?limit=100&order=desc&archived=true|false`，游标分页 |
| 会话详情 | `GET /api/session/{id}` |
| 消息（完整历史） | `GET /api/session/{id}/message?limit=100&order=desc`，游标分页，按时间反转 |
| 新建整理会话 | `POST /api/session` `{title, agent, location:{directory}}` |
| 改标题 / 归档 | `PATCH /api/session/{id}` `{title}` 或 `{archived:true}` |
| 切模型 | `POST /api/session/{id}/model` `{model:{id:"裸模型ID", providerID}}` |
| 投递任务 | `POST /api/session/{id}/prompt` `{id, text, delivery:"queue", metadata}` |
| 收件箱（判断是否有人工输入） | `GET /api/session/{id}/inbox` |
| 中断（仅限自己的任务） | `POST /api/session/{id}/interrupt?resume=false` |
| 模型目录 | `GET /api/model` |
| MCP 状态（可选） | `GET /api/mcp?location[directory]=...` |

要点：

- `model.id` 必须是**裸模型 ID**，`id: "provider/model"` 会被路由成重复前缀并报 `no-route`。
- `prompt` 只是「入队成功」，不代表生成完成；完成要靠会话 `time.idle` 或消息 `finish/completed`。
- 客户端会拒绝 `-free` 模型（桌面端策略），选择器要排除。
- 绝不要直接读写客户端的数据库。所有读取走 API，所有状态写进自己的台账。

## 3. 台账 schema（SQLite）

```sql
meta(key PK, value)                      -- 写者ID、上一位写者、提交时间戳、MCP标识、目录轮转
sources(id PK, title, directory, archived, updated, scanned_update, scanned_at,
        captured_id, acknowledged_id, status, error, info)
messages(source, id, position, created, kind, hash, text, PRIMARY KEY(source,id))
jobs(id PK, source, first_id, last_id, position, evidence, refs, status, attempts,
     next_at, created, started, finished, writer, prompt_id, model, receipt, error)
verified_messages(id, hash, job, PRIMARY KEY(id,hash))
models(key PK, cooldown, successes, failures, error)
focus_ranges(job, source, first_position, last_position, selected, selected_chars,
             original_chars, status, PRIMARY KEY(job,source))
```

- `WAL` + `synchronous=FULL`，20 秒超时。
- 单实例靠 OS 文件锁（Windows `msvcrt.locking`，POSIX `fcntl.flock`），拿不到锁就退出。
- 所有「状态 + 游标 + 任务」变更必须在同一事务里完成。

## 4. 采集算法

```
对每个来源（活跃 + 已归档，排除写者/测试/显式排除/用户说过不要记录）：
  focus 模式：
    取最新 100 条；若游标不在其中且有下一页，再取 100 条
    首次扫描且仍有更早页 → 另取最早 30 条，并记录「中间未精读」
    游标在结果中 → 只保留游标之后的消息
  找到最后一个「已结束回合」：
    idle 消息，或旧记录里 assistant 且有 completed + finish ∈ {stop,length,error}
  该回合结束时间距今 < quiet_seconds → 本轮跳过（可能还在对话）
  超过 chunk 限制的单条消息无损切分为 id#partN/total
  脱敏 → 落库 → 原子推进 captured_id / scanned_at / 生成任务
```

不变量：

- 原文不截断、不删除；未选中的只是「未被选择」，不是「被判无价值」。
- 没有已结束回合时不得推进任何「已处理」游标。
- 用户明确说「不要记住/别记录」→ 排除整个来源，并停掉它待处理任务（不删历史笔记）。

## 5. 选择算法（重点模式）

单个来源的窗口：

```
锚点：首条用户消息 + 最近 3 条有意义的正文（防止中段噪声挤掉首尾）
其次：中间命中信号的用户消息（决策/偏好/纠正/最终结论/未决问题）
排除：工具输出块、围栏代码、超长日志行、纯确认词
每条 ≤1100 字符，超出加省略标记，并注明「未选原文可回查」
窗口内按正文指纹去重，最终按时间排序
```

批次：相关来源合并，受 `focus_window_chars` / `focus_batch_sources` / `focus_batch_chars` 约束；
稀疏来源允许跨目录补齐；已处理过的目录降低优先级，避免当前调试会话垄断预算。

## 6. 提交前必须做的事

1. 恢复优先：存在 `processing` 任务时先核验它，再开新任务。
2. 预算：滚动 24 小时提交数（含重试）< `daily_job_limit`。
3. 同来源更早的未核验任务存在 → 不跳过它去处理后面的范围。
4. 写者会话：复用未归档的；否则按精确标题找；都没有就新建（目录设为笔记库）。
5. 人工优先：收件箱非空，或最后一条用户消息尚未结束 → 不派发。
6. 轮换：有效发言数达阈值 → 归档旧会话（先验证归档成功）→ 新建并写入 `previous` 交接。
7. 选模型：现场目录 + 冷却过滤 + 族/供应商偏好 + 版本倒序。
8. 记录快照（笔记哈希）→ 事务内写状态与 attempts → 再投递。
9. 提示词必须包含：任务 ID、来源 ID/标题/目录、完整窗口引用清单、证据正文、此前失败原因、交接说明。

## 7. 核验（不能简化）

```
回执：整理会话最后一行 BRAIN_RECEIPT {job,status,reason,reviewed_refs,notes[{path,quote}]}
      job 必须等于本次任务；reviewed_refs 必须与服务端提供的引用集合一致

written:
  文件存在、在库内、非凭据/隐藏/回收站目录
  前后哈希不同
  存在完成的写入工具调用（write_note / patch_note / update_frontmatter），路径一致
  存在完成的回读调用（read_note，或覆盖引文行范围的 read_note_lines），路径一致
  引文 ≥12 字符且逐字存在于当前文件
  正文含来源会话 ID 与至少一个原始消息 ID（索引类可指向本次同时回读的带来源主笔记）

duplicate:
  存在完成的回读 + 引文逐字存在 + 指向已有笔记

no_value:
  reviewed_refs 完全一致 + 具体原因；正文只能是寒暄/确认类
  → 记为 focus_screened，绝不记为「已完整审阅」

任一不满足 → failed，保留任务与证据，指数退避重试
```

## 8. 测试（先写这些，再上线）

用临时目录 + 临时台账，不碰真实状态：

1. 长消息 250 字符按 100 切分后无损重组；重启后不重复建任务。
2. 只有**连续**核验通过才推进已确认游标。
3. 「声称已写但没有工具轨迹」必须被拒。
4. 带 `[` 前缀的实质内容不能被当成无价值。
5. 人工未结束的回合阻止自动派发。
6. 同族新版本优先；冷却后回退到下一候选。
7. reasoning 与密钥不进入证据。
8. 两个进程同时启动只有一个拿到锁。
9. `read_note_lines` 的行范围必须真的覆盖引文。
10. 轮换先归档成功再新建。
11. 多来源批次的来源标注各自正确。
12. fork 共享历史只跳过已核验部分，新决定仍要处理。
13. 抽样缺页必须被标记为未精读。

## 9. 上线顺序

1. `dispatch:false` 只采集，确认台账在长、没有报错。
2. 3 个不同类型会话试点（技术故障 / 项目决策 / 个人偏好），逐一人工比对笔记与原文。
3. 修正提示词与核验，直到「假写入」被稳定拦截。
4. `validation_only:false` 打开全量，保持低预算观察失败率。
5. 依据真实平均耗时与失败原因调批次大小和预算，不要预设完成日期。

## 10. 不要做的事

- 不要用插件直接建子会话做整理（列表污染 + 无状态 + 无核验）。
- 不要把整段聊天塞进模型（噪声主导，成本失控）。
- 不要相信模型的「已完成」；只信文件、工具轨迹、引文、来源 ID。
- 不要把「会话看过」当成「事实已入库」。
- 不要为了速度砍掉核验或来源字段。
- 不要写入客户端数据库，不要在笔记里保存凭据值。
