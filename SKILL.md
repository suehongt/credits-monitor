---
name: credits-monitor
description: 监控 WorkBuddy 各会话（线程）的积分与 token 消耗，按会话×模型维度统计使用量，检测会话中途的模型自动切换（如 kimi-k3 变 auto / hy3-x 混用），输出可交互 HTML 报告。This skill should be used when the user asks about 积分监控、积分消耗、token 消耗、credits monitor、模型用量、每个会话每个模型的使用量、积分对应多少 token、积分消耗对应时间、查看消耗明细/时间线，或要求建一个线程监控积分。数据只读本地：~/.workbuddy/workbuddy.db（积分）、~/.workbuddy/traces（token/模型）、~/.workbuddy/audit-log（时间）。
agent_created: true
---

# Credits Monitor（积分/token 消耗监控）

## Overview

读取 WorkBuddy 本地数据，生成"每个会话 × 每个模型"的积分与 token 使用报告。
核心价值：`sessions.model` 只记录会话当前模型（中途会自动切换，不可信）；
本 skill 通过 traces 的每次 LLM 调用记录还原**真实的模型使用分布**（如 kimi-k3-1/kimi-k3-2/hy3-x/glm-5.3 混用），
并给出积分（精确）、token（精确）、积分↔token 换算（估算）与调用时间线。

## 数据源与精度（详见 references/schema.md）

| 数据 | 来源 | 精度 |
|---|---|---|
| 会话级积分 | `~/.workbuddy/workbuddy.db` → `session_usage.credit_json` | 精确 |
| 会话×模型 token/次数/时间 | `~/.workbuddy/traces/*/trace_*.json` → generation span | 精确 |
| 会话元信息 | `sessions` 表 | 精确（model 字段不可信） |
| 会话×模型积分 | token 占比分摊 | 估算 |
| 今日新增积分 | `audit-log/*.jsonl` 匹配 requestId 时间戳 | 覆盖有审计事件的请求 |

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
   - 总积分、今日新增积分、LLM 调用总次数、总 prompt/completion tokens
   - 积分消耗 Top 会话
   - **发生模型自动切换的会话**（报告中有 ★ 标注）与模型分布
   - 模型积分↔token 换算（tokens/积分，报告标注为估算）

4. **如脚本报错**：报告错误原因与解决建议（常见：数据库被占用/路径不存在/JSON 解析异常）。

## 报告能力（HTML 交互）

- 顶部全局检索：实时过滤、命中高亮、↑/↓ 跳转、计数
- 选中文字即高亮，可加批注（存浏览器 localStorage，可导出 JSON）
- 浮动"回到顶部"按钮
- `<script>` 顶部 `CONFIG` 区（highlightColor/storageKey 等，可编辑后固化）
- 浅色主题

## 已知限制（向用户说明）

- **traces 仅保留近期活跃会话**（当前约 7/10 会话有记录）；无 traces 的会话在报告中标"traces 未覆盖(仅积分)"，无法按模型拆分。
- **积分按模型为估算值**：本地无 requestId↔generation 关联键，按 token 占比分摊，受模型等级/思考模式/缓存影响。
- 早期 trace（约 98 个文件）无 sessionId，其调用归入"未关联"并在报告顶部提示。
- 积分是累计值；"今日新增"仅统计 audit-log 能匹配到时间的计费请求。

## 数据只读承诺

本 skill 只读 `~/.workbuddy/` 下的数据文件，**不修改**数据库、traces、audit-log 任何内容。
