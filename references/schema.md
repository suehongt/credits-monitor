# WorkBuddy 本地数据源 Schema（credits-monitor 参考）

> 本文件固化 credits-monitor 所需数据源的表结构与字段说明，避免每次运行时重复探索。
> 所有路径均在 `~/.workbuddy/` 下。数据为只读使用，禁止写操作。

## 1. 主数据库 `~/.workbuddy/workbuddy.db`（SQLite）

### sessions 表 —— 会话元信息
| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 会话 UUID |
| title / custom_title | TEXT | 会话标题（报告优先用 custom_title） |
| model | TEXT | **当前模型**（⚠️ 会话中途自动切换后只记录最后一次，不可信） |
| status | TEXT | Pending / working / completed / planning 等 |
| mode | TEXT | craft / plan / ask |
| created_at / last_activity_at | INTEGER | Unix 毫秒时间戳 |
| deleted_at | INTEGER | 已删除标记 |
| cwd / project_id / expert_id | TEXT | 工作目录、项目、专家 |

### session_usage 表 —— 每会话积分（核心）
| 字段 | 类型 | 说明 |
|---|---|---|
| session_id | TEXT PK | 会话 UUID |
| used | INTEGER | **上下文 token 占用快照**（实时更新，非累计消耗） |
| size | INTEGER | 模型上下文窗口（如 1000000 / 192000） |
| updated_at | INTEGER | 最后扣费时间（毫秒） |
| credit_json | TEXT | JSON `{requestId: 积分}` —— **每次计费请求的积分明细**，key 为 32 位 hex requestId |

### automations 表 —— 定时任务
| 字段 | 说明 |
|---|---|
| id / name / prompt | 任务标识与提示词 |
| schedule_type | recurring / once |
| rrule | 如 `FREQ=DAILY;BYHOUR=23;BYMINUTE=30` |
| next_run_at / last_run_at | 下次/上次运行时间 |
| cwds | 工作目录 JSON 数组 |
| status | ACTIVE / PAUSED |

## 2. traces 目录 `~/.workbuddy/traces/<pid>/trace_*.json`（token 精确来源）

每次 agent 运行生成一个 trace 文件（181 个左右，仅保留近期活跃会话）。

### trace 顶层
```json
{
  "trace": { "traceId": "...", "sessionId": "会话UUID", "startedAt": "...", "totalTokens": 0,
             "modelInfo": {"models": ["glm-5.3"], "totalInputTokens": 373208, "totalOutputTokens": 4064, "totalCachedTokens": ...} },
  "spans": [...]
}
```
- `trace.sessionId`：**关联会话的关键**（早期约 54% trace 无此字段）
- `trace.totalTokens` 常为 0，不可用；真实 token 在 generation span 中
- `trace.modelInfo.models`：**本次请求实际用到的模型数组**（约 102/216 覆盖，新格式 trace 才有）；
  可与 generation 明细交叉验证。`modelInfo` 不区分主对话/子代理，按 span 父链分类更精确

### generation span（type == "generation"）—— 每次 LLM 调用
```json
{
  "type": "generation",
  "startedAt": "ISO8601", "endedAt": "ISO8601", "duration": 8833,
  "toolOutput": "[{\"id\":\"msg_id\",\"created\":1787707220,\"model\":\"deepseek-v4-flash\",
     \"usage\":{\"prompt_tokens\":38657,\"completion_tokens\":853,
       \"prompt_tokens_details\":{\"cached_tokens\":0,\"reasoning_tokens\":0},
       \"completion_tokens_details\":{\"reasoning_tokens":654}}} ]"
}
```
- `toolOutput[0].model`：**本次调用实际模型**——UI 显示 "auto" 时系统实际选的模型就在这里（含主对话/子代理每次调用）
- `toolOutput[0].usage`：**精确 token**（prompt/completion，含缓存/推理明细）
- `toolOutput[0].created`：Unix 秒时间戳
- `toolInput` 为消息数组（系统提示词首行 "This conversation is powered by XXX" 亦标注当前模型）
- 一个 agent 回合可含多个 generation（多轮工具调用）
- **调用来源分类**（按父 span 链）：父链含 `function:Agent` → 子代理；父链含非 `cli` 的 agent span（contextSummary/contentAnalyzer 等）→ 后台代理；否则 → 主对话

### span 父链示例
```
主对话:     generation ← agent:cli
子代理:     generation ← agent:general-purpose ← function:Agent ← agent:cli
后台代理:   generation ← agent:contextSummary ← agent:cli
```
注意：同一会话的 generation 与其父 agent span 可能分布在**不同 trace 文件**中，追父链需跨文件建全局 spanId 索引。

## 3. audit-log 目录 `~/.workbuddy/audit-log/YYYY-MM-DD*.jsonl`（时间补充）

- 记录 command-safety / network 事件
- 字段含 `requestId`（与 credit_json 的 key 一致）与 `timestamp`（毫秒）
- 用途：把积分 requestId 关联到时间（"今日新增积分"）；匹配率约 78%（部分请求无审计事件）
- 不含 model 与 token 字段

## 4. 数据关联与精确度

| 目标 | 方法 | 精度 |
|---|---|---|
| 会话级积分 | `session_usage.credit_json` 求和 | 精确 |
| 会话×模型 token/次数/时间 | traces generation（sessionId → model/usage/created 分组） | 精确 |
| 会话×模型积分 | 按 token 占比分摊会话积分（`est_credits`） | **估算**（本地无 requestId↔generation 关联键） |
| 今日新增积分 | audit-log 匹配 requestId 时间戳，筛今天 | 仅覆盖有审计事件的请求 |
| 会话当前模型 | sessions.model | 记录"实际解析后的当前模型"（= 主对话最后一次调用的模型，已验证一致）；**不记录用户选择的是哪个模型** |

## 5. 已知限制
- traces 只保留近期活跃会话（约 7/12 会话有记录），历史会话无法按模型拆分，报告标"traces 未覆盖(仅积分)"
- 早期 trace 无 sessionId（约 98 个文件），其 generation 归入"未关联"并在报告中提示
- 模型估算积分基于"定价∝token"假设，实际受模型等级/思考模式/缓存影响，仅作参考
- **模型切换来源不可分**：本地无 UI 事件日志，主对话多模型无法区分用户手动切换 / 系统自动切换 / auto 模式兜底；`sessions.model` 与 traces 均只记实际模型，不记"requested model"
- 来源分类依赖父 span 链完整；父链断裂时默认归"主对话"
