---
name: credits-monitor
description: 监控 WorkBuddy 各会话（线程）的 token 与积分消耗，按会话×模型×调用来源（主对话/子代理/后台代理）统计使用量，识别多模型混用的会话并区分混用来源，输出可交互 HTML 报告。This skill should be used when the user asks about 积分监控、积分消耗、token 消耗、credits monitor、模型用量、每个会话每个模型的使用量、积分对应多少 token、积分消耗对应时间、查看消耗明细/时间线，或要求建一个线程监控积分。数据只读本地：~/.workbuddy/workbuddy.db（积分）、~/.workbuddy/traces（token/模型/来源）、~/.workbuddy/audit-log（时间）。
agent_created: true
---

# Credits Monitor（token/积分 消耗监控）

## Overview

读取 WorkBuddy 本地数据，生成"每个会话 × 每个模型 × 调用来源"的 token 与积分使用报告。
核心价值：WorkBuddy UI 只展示积分消耗；`sessions.model` 只记录会话当前模型。
本 skill 通过 traces 的每次 LLM 调用记录还原**真实的模型使用分布**，并按父 span 链把每次调用分类为：

- **主对话**（main）—— 用户直接对话的调用
- **子代理**（subagent）—— Agent 工具派生的子任务（使用独立模型，UI 不显示）
- **后台代理**（background）—— 系统后台任务（contextSummary / contentAnalyzer 等）

给出 token（精确）、积分（会话级精确）、积分↔token 换算（估算）与含来源标记的调用时间线。

**口径说明（重要）**：本地数据不记录模型切换事件（无 UI 事件日志），
主对话调用过多个模型时**无法区分用户手动切换与系统自动切换**，报告中如实标注，不做断言。

## 数据源与精度（详见 references/schema.md）

| 数据 | 来源 | 精度 |
|---|---|---|
| 会话级积分 | `~/.workbuddy/workbuddy.db` → `session_usage.credit_json` | 精确 |
| 会话×模型×来源 token/次数/时间 | `~/.workbuddy/traces/*/trace_*.json` → generation span + 父链分类 | 精确 |
| 会话元信息 | `sessions` 表 | 精确（model 字段仅当前模型） |
| 会话×模型积分 | token 占比分摊 | 估算 |
| 今日积分消耗 | `audit-log/*.jsonl` 匹配 requestId 时间戳 | 覆盖有审计事件的请求 |

## 工作流

1. **运行脚本**（默认输出到 skill 的 `reports/`，也可 `--out` 指定目录）：
   ```bash
   python3 scripts/monitor_credits.py --out <输出目录>
   ```
   可选参数：`--db <workbuddy.db路径>`、`--traces <traces目录>`、`--audit <audit-log目录>`。

2. **确认产出**：
   - `<out>/credits_YYYY-MM-DD.html`（当日报告）
   - `<out>/credits_latest.html`（最新副本）

3. **打开报告预览**（用 present_files），并总结关键发现：
   - 总 token（精确）、积分消耗合计、今日积分消耗、LLM 调用总次数
   - token 消耗 Top 会话与 Top 模型（含来源拆分：主/子代理/后台）
   - 主对话多模型的会话（★ 标注，注明手动/自动不可分）
   - 子代理/后台代理消耗了多少 token（用户通常不知道这部分存在）
   - 模型积分↔token 换算（tokens/积分，报告标注为估算）

4. **如脚本报错**：报告错误原因与解决建议（常见：数据库被占用/路径不存在/JSON 解析异常）。

## 报告能力（HTML 交互）

- 顶部全局检索：实时过滤、命中高亮、↑/↓ 跳转、计数
- 选中文字即高亮，可加批注（存浏览器 localStorage，可导出 JSON）
- 浮动"回到顶部"按钮
- `<script>` 顶部 `CONFIG` 区（highlightColor/storageKey 等，可编辑后固化）
- 浅色主题

## 已知限制（向用户说明）

- **traces 仅保留近期活跃会话**（当前约 7/12 会话有记录）；无 traces 的会话在报告中标"traces 未覆盖(仅积分)"，无法按模型拆分。
- **积分按模型为估算值**：本地无 requestId↔generation 关联键，按 token 占比分摊，受模型等级/思考模式/缓存影响。
- **模型切换来源不可分**：主对话多模型无法区分用户手动切换与系统自动切换（本地无切换事件日志）。
- 早期 trace（约 98 个文件）无 sessionId，其调用归入"未关联"并在报告顶部提示。
- 流式被取消/仍在运行的零产出调用被丢弃（仅显示数量与原因）。
- 积分是累计值；"今日新增"仅统计 audit-log 能匹配到时间的计费请求。
- 来源分类依赖父 span 链完整可追溯；父链断裂时默认归"主对话"。

## 数据只读承诺

本 skill 只读 `~/.workbuddy/` 下的数据文件，**不修改**数据库、traces、audit-log 任何内容。
