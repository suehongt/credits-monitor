# Credits Monitor · WorkBuddy Token 消耗 & 积分反推

> 从 WorkBuddy 本地数据反推每个会话 × 每个模型的**精确 token 消耗**，
> 并把 WorkBuddy UI 展示的**积分消耗**作为反推参照——看清「为这些积分实际消耗了多少 token」。
> 同时检测会话中途的**模型自动切换**（如 `kimi-k3` 变 `auto`、`hy3-x` 与其他模型混用），
> 输出一份可交互的 HTML 报告。

[English docs below](#english)

---

## ✨ 为什么需要它

WorkBuddy UI 只向你展示**积分消耗**，不展示 token 数量。但 LLM 的真实工作单元是 token——
你想知道"为这些积分实际消耗了多少 token、按什么模型消耗的"。

同时，WorkBuddy 的 `sessions.model` 字段只记录会话**当前**所使用的模型，**中途会自动切换模型**（有实证），
因此该字段不可信。本 skill 通过 `traces/` 目录中**每次 LLM 调用**的原始记录，还原真实的模型使用分布，
例如同一个会话同时混用了 `hy3-x`、`kimi-k3-1`、`kimi-k3-2` 三种模型。

核心价值：

- **每个会话 × 每个模型**的调用次数、prompt / completion tokens、首末时间 → 精确（来自 traces）
- **会话级积分消耗**（`credit_json`）→ 精确（来自 workbuddy.db）
- **积分 ↔ token 换算** → 按 token 占比分摊的**估算值**
- **模型自动切换检测**：报告中用 ★ 标注发生切换的会话
- 一份自带全局检索 / 高亮批注 / 回到顶部的交互式 HTML 报告

---

## 🗃️ 数据源与精度

| 数据 | 来源 | 精度 | 在报告中 |
|---|---|---|---|
| 会话×模型 token / 次数 / 时间 | `~/.workbuddy/traces/*/trace_*.json` → generation span | **精确**（主指标） | 模型表 + 会话明细表 |
| 会话级积分 | `~/.workbuddy/workbuddy.db` → `session_usage.credit_json` | 精确 | overview 卡片 + 估算列 |
| 会话×模型积分 | 按 token 占比分摊 | **估算** | 「估算积分」列（明确标 "估算"） |
| 会话元信息 | `sessions` 表（标题 / 状态 / 创建时间） | 精确（但 `model` 字段不可信） | 卡片头部 |
| 今日新增积分 | `audit-log/*.jsonl` 匹配 requestId 时间戳 | 覆盖有审计事件的请求 | 卡片「今日新增积分」列 |

> **数据处理**：流式被取消 / 仍在运行的零产出调用自动丢弃，不计入任何模型 / 会话统计，
> 仅在报告底部「已丢弃的零产出调用」中显示数量与原因。

数据格式详见 [`references/schema.md`](references/schema.md)。

---

## 📦 安装（作为 WorkBuddy Skill）

将本仓库克隆 / 复制到用户级 skill 目录即可被 WorkBuddy 自动识别：

```bash
# 用户级（推荐，跨项目可用）
git clone https://github.com/suehongt/credits-monitor.git ~/.workbuddy/skills/credits-monitor

# 或项目级（仅当前项目）
git clone https://github.com/suehongt/credits-monitor.git <项目>/.workbuddy/skills/credits-monitor
```

之后在对话中说「看积分报告」「运行 credits-monitor」「监控每个会话的 token 消耗」等即可触发。

---

## 🚀 用法（命令行直接运行脚本）

```bash
python3 scripts/monitor_credits.py --out <输出目录>
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--out` | `<skill>/reports/` | HTML 报告输出目录 |
| `--db` | `~/.workbuddy/workbuddy.db` | 主数据库路径 |
| `--traces` | `~/.workbuddy/traces` | trace 目录 |
| `--audit` | `~/.workbuddy/audit-log` | 审计日志目录 |

### 产出

- `<out>/credits_YYYY-MM-DD.html` —— 当日报告
- `<out>/credits_latest.html` —— 最新报告副本

### 配合每日自动化

可在 WorkBuddy 中创建一个每日定时任务（如每天 23:30），自动运行脚本并预览报告：

```
使用 credits-monitor skill 生成当日积分/token 消耗监控报告：
运行 scripts/monitor_credits.py（输出到工作区 reports/），
生成 credits_YYYY-MM-DD.html 并用 present_files 打开预览。
```

---

## 🖥️ 报告内容（以 token 为主线）

打开 HTML 报告后看到的层级（自上而下）：

1. **总览卡片**（按优先级排序）
   - 总 token（精确，主指标）| LLM 调用次数 | 会话数 | 模型种类
   - 积分合计（来源 DB）| 今日新增积分 | 全局 tokens/积分
2. **模型自动切换提示** —— 列出发生切换的会话
3. **未关联调用提示** —— 早期无 sessionId 的 trace 统计
4. **已丢弃的零产出调用** —— 流式被取消等无法归因的调用数
5. **模型 token 消耗汇总** —— 按合计 token 降序，含占比、估算积分、tokens/积分
6. **会话明细** —— 按合计 token 降序的卡片，每张卡片含：
   - tokens(精确) 置顶 | LLM 调用 | 计费请求 | 积分合计 | tokens/积分 | 今日新增积分
   - 模型使用明细表（按 token 降序，含首次/末次调用时间）
   - 调用时间线（最近 20 次 + 折叠历史）

**交互能力**

- **顶部全局检索**：实时过滤、命中高亮、↑/↓ 跳转、计数
- **高亮 + 批注**：选中文字即高亮，可加批注（存浏览器 `localStorage`，可导出 JSON）
- **回到顶部**：浮动按钮
- **可编辑参数**：`<script>` 顶部 `CONFIG` 区（高亮色 / storageKey 等），可直接改后固化
- 浅色主题

---

## ⚠️ 已知限制

- **traces 仅保留近期活跃会话**（当前约 7/11 会话有记录）；无 traces 的会话在报告中标「traces 未覆盖（仅积分）」，无法按模型拆分。
- **积分按模型为估算值**：本地无 `requestId ↔ generation` 关联键，按 token 占比分摊，受模型等级 / 思考模式 / 缓存影响。
- 早期 trace（约 98 个文件）无 `sessionId`，其调用归入「未关联」并在报告顶部提示。
- 流式被取消 / 仍在运行的零产出调用被自动丢弃，不计入统计（仅显示数量）。
- 积分为累计值；「今日新增」仅统计 audit-log 能匹配到时间的计费请求。

---

## 🔒 数据只读承诺

本 skill 只读 `~/.workbuddy/` 下的数据文件（数据库 / traces / audit-log），**不修改**任何内容。

---

## 📁 目录结构

```
credits-monitor/
├── SKILL.md                    # Skill 定义（触发词 / 工作流 / 限制）
├── README.md                   # 本文件
├── scripts/
│   └── monitor_credits.py      # 核心脚本（读库 → 读 traces → 按会话×模型统计 → 渲染 HTML）
├── references/
│   └── schema.md               # 本地数据源表结构与字段说明
└── .gitignore                  # 排除 reports/、__pycache__/、.DS_Store
```

---

## 📜 License

MIT —— 可自由使用、修改、分发。

---

## English

**Credits Monitor** is a WorkBuddy skill that **reconstructs precise per-session × per-model token consumption**
from local usage data, with **credits consumption** (the only number the WorkBuddy UI shows) as a derived
reference. It also detects **mid-session model auto-switching** (e.g. `kimi-k3` falling back to `auto`,
or `hy3-x` mixed with other models).

WorkBuddy's UI exposes only credit charges. This skill tells you "how many tokens did those credits buy,
and which models were used." WorkBuddy's `sessions.model` only records the *current* model and silently
switches mid-conversation, so it is unreliable — we reconstruct the true model distribution from raw
LLM-call logs in `traces/`.

**Highlights**

- Per-session × per-model call count, prompt/completion tokens, and timestamps — **exact**
- Per-session credits — **exact**
- Credits ↔ token conversion — **estimated** (token-share allocation)
- Model auto-switch detection (★ badge in the report)
- Interactive HTML report (global search, highlight + annotate, back-to-top)
- Dropped-call tracking: streaming cancellations and zero-output calls are filtered out and reported separately

**Run**

```bash
python3 scripts/monitor_credits.py --out <output_dir>
```

Outputs `credits_YYYY-MM-DD.html` and `credits_latest.html`. See `references/schema.md` for the data schema.
The skill is **read-only** — it never modifies `~/.workbuddy/` data.
