#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
credits-monitor: WorkBuddy 积分 / token 消耗监控

数据源（只读）：
  1. ~/.workbuddy/workbuddy.db        -> session_usage(积分 credit_json) + sessions(会话元信息)
  2. ~/.workbuddy/traces/*/trace_*.json -> generation span（每次 LLM 调用: model / usage / created / sessionId）
  3. ~/.workbuddy/audit-log/*.jsonl    -> requestId -> 时间戳（用于"今日积分消耗"统计）

产出：
  <out_dir>/credits_YYYY-MM-DD.html   交互式 HTML 报告（全局检索 / 高亮批注 / 回到顶部 / CONFIG 区）
  <out_dir>/credits_latest.html       最新报告副本

用法：
  python3 monitor_credits.py [--out <输出目录>] [--db <workbuddy.db 路径>] [--traces <traces 目录>]
"""

import argparse
import datetime
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict

# ---------------- 路径默认值 ----------------
HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, ".workbuddy", "workbuddy.db")
DEFAULT_TRACES = os.path.join(HOME, ".workbuddy", "traces")
DEFAULT_AUDIT = os.path.join(HOME, ".workbuddy", "audit-log")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


# ---------------- 数据加载 ----------------

def load_sessions(db_path):
    """读取 session_usage + sessions，返回 {session_id: {...}}"""
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT s.id, s.title, s.custom_title, s.model, s.status, s.mode,
               s.created_at, s.last_activity_at,
               su.used, su.size, su.updated_at, su.credit_json
        FROM sessions s LEFT JOIN session_usage su ON su.session_id = s.id
        ORDER BY su.updated_at DESC
    """).fetchall()
    con.close()

    sessions = {}
    for r in rows:
        credits = {}
        try:
            credits = json.loads(r["credit_json"] or "{}")
        except Exception:
            credits = {}
        sessions[r["id"]] = {
            "id": r["id"],
            "title": r["custom_title"] or r["title"] or "(未命名会话)",
            "model": r["model"],
            "status": r["status"],
            "mode": r["mode"],
            "created_at": r["created_at"],
            "last_activity_at": r["last_activity_at"],
            "used": r["used"] or 0,
            "size": r["size"] or 0,
            "su_updated_at": r["updated_at"],
            "credits": credits,                       # {requestId: credits}
            "credits_total": round(sum(credits.values()), 2),
            "models": {},                             # {model: {...}} 由 traces 填充
            "timeline": [],                           # [(ts, model, prompt, comp)]
            "traces_covered": False,
            "has_multi_model": False,
        }
    return sessions


def classify_source(span, spans_by_id):
    """
    按父 span 链判断 generation 调用来源：
      main       —— 主对话（父链只有 agent:cli）
      subagent   —— 用户通过 Agent 工具派生的子代理（父链含 function:Agent）
      background —— 系统后台代理（contextSummary / contentAnalyzer 等，父链含非 cli 的 agent span）
    注意：spans_by_id 必须是跨文件的全局索引——同一会话的 generation 与其父 agent span
    可能分布在不同的 trace 文件中（每个 trace 文件对应一次用户请求）。
    """
    cur = span.get("parentId")
    has_agent_tool = False   # 父链含 function:Agent（用户派生的子代理）
    has_bg_agent = False     # 父链含非 cli 的 agent span（系统后台代理）
    for _ in range(12):
        if not cur or cur not in spans_by_id:
            break
        ps = spans_by_id[cur]
        if ps.get("type") == "function" and ps.get("name") == "Agent":
            has_agent_tool = True
        elif ps.get("type") == "agent" and ps.get("name") not in ("cli",):
            has_bg_agent = True
        cur = ps.get("parentId")
    if has_agent_tool:
        return "subagent"
    if has_bg_agent:
        return "background"
    return "main"


def load_generations(traces_dir):
    """
    扫描全部 trace 文件，提取 generation span，并分类调用来源（main / subagent / background）。
    两遍扫描：第一遍建全局 span 索引（spanId 在全库唯一，跨文件追父链），
    第二遍提取 generation 并分类。
    过滤规则：toolOutput 为空 / 解析失败 / model 缺失且 0 token 的"半完成"调用全部丢弃。
    """
    per_session = defaultdict(list)
    unlinked = {"files": 0, "generations": 0, "calls": 0, "prompt": 0, "comp": 0}
    dropped = {"cancelled": 0, "no_model_no_tok": 0, "parse_fail": 0, "empty_out": 0}
    files = glob.glob(os.path.join(traces_dir, "*", "trace_*.json"))

    # 第一遍：全局 span 索引
    global_spans = {}
    parsed_files = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        parsed_files.append(d)
        for s in d.get("spans", []):
            sid = s.get("spanId")
            if sid and sid not in global_spans:
                global_spans[sid] = s

    # 第二遍：提取 generation 并分类
    for d in parsed_files:
        sid = d.get("trace", {}).get("sessionId")
        for span in d.get("spans", []):
            if span.get("type") != "generation":
                continue
            out = span.get("toolOutput")
            if not out:
                dropped["empty_out"] += 1
                continue
            try:
                arr = json.loads(out)
            except Exception:
                dropped["parse_fail"] += 1
                continue
            if not isinstance(arr, list) or not arr or not isinstance(arr[0], dict):
                dropped["parse_fail"] += 1
                continue
            item = arr[0]
            model = item.get("model")
            u = item.get("usage") or {}
            prompt = u.get("prompt_tokens", 0) or 0
            comp = u.get("completion_tokens", 0) or 0
            created = item.get("created")
            if not model and prompt == 0 and comp == 0:
                dropped["no_model_no_tok"] += 1
                continue
            rec = {
                "model": model or "unknown",
                "prompt": prompt,
                "comp": comp,
                "created": created,
                "source": classify_source(span, global_spans),
            }
            if sid:
                per_session[sid].append(rec)
            else:
                unlinked["files"] += 1
                unlinked["generations"] += 1
                unlinked["calls"] += 1
                unlinked["prompt"] += prompt
                unlinked["comp"] += comp
    return per_session, unlinked, dropped


def load_request_times(audit_dir):
    """扫描 audit-log，构建 requestId -> timestamp(毫秒) 映射（仅增量小表，全扫可接受）"""
    mapping = {}
    files = glob.glob(os.path.join(audit_dir, "*.jsonl"))
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    rid = e.get("requestId")
                    ts = e.get("timestamp")
                    if rid and ts and rid not in mapping:
                        mapping[rid] = ts
        except Exception:
            continue
    return mapping


# ---------------- 统计与估算 ----------------

def merge_data(sessions, per_session, request_times, today_ts_ms):
    """把 traces 的 generation 并入会话，按 会话x模型x来源 聚合，估算积分分摊"""
    for sid, recs in sessions.items():
        gens = per_session.get(sid, [])
        if gens:
            sessions[sid]["traces_covered"] = True
        # 按模型×来源聚合
        agg = defaultdict(lambda: {
            "calls": 0, "prompt": 0, "comp": 0, "first": None, "last": None,
            "calls_main": 0, "tok_main": 0,
            "calls_sub": 0, "tok_sub": 0,
            "calls_bg": 0, "tok_bg": 0,
        })
        main_models = set()
        for g in gens:
            m = agg[g["model"]]
            src = g.get("source", "main")
            m["calls"] += 1
            m["prompt"] += g["prompt"]
            m["comp"] += g["comp"]
            tok = g["prompt"] + g["comp"]
            if src == "main":
                m["calls_main"] += 1
                m["tok_main"] += tok
                main_models.add(g["model"])
            elif src == "subagent":
                m["calls_sub"] += 1
                m["tok_sub"] += tok
            else:
                m["calls_bg"] += 1
                m["tok_bg"] += tok
            ts = g["created"]
            if ts:
                if m["first"] is None or ts < m["first"]:
                    m["first"] = ts
                if m["last"] is None or ts > m["last"]:
                    m["last"] = ts
        for model, m in agg.items():
            m["prompt"] = round(m["prompt"] / 1e6, 3)   # 百万 tokens
            m["comp"] = round(m["comp"] / 1e6, 3)
            m["tok_main"] = round(m["tok_main"] / 1e6, 3)
            m["tok_sub"] = round(m["tok_sub"] / 1e6, 3)
            m["tok_bg"] = round(m["tok_bg"] / 1e6, 3)
        sessions[sid]["models"] = dict(agg)
        sessions[sid]["main_models"] = main_models

        # 时间线：按时间排序（含来源标记）
        timeline = sorted(
            [(g["created"], g["model"], g["prompt"], g["comp"], g.get("source", "main")) for g in gens
             if g["created"]],
            key=lambda x: x[0],
        )
        sessions[sid]["timeline"] = timeline

        # 多模型标记（区分：主对话换过模型 vs 仅子代理引入其他模型）
        sessions[sid]["has_multi_model"] = len(agg) > 1
        sessions[sid]["main_multi"] = len(main_models) > 1
        sessions[sid]["has_subagent"] = any(m["calls_sub"] > 0 for m in agg.values())

        # 积分按 token 占比分摊到模型（估算）
        total_tokens = sum((m["prompt"] + m["comp"]) * 1e6 for m in agg.values())
        if total_tokens > 0:
            for model, m in agg.items():
                share = (m["prompt"] + m["comp"]) * 1e6 / total_tokens
                m["est_credits"] = round(sessions[sid]["credits_total"] * share, 2)
        else:
            for model, m in agg.items():
                m["est_credits"] = 0.0

        # 今日积分消耗（requestId 时间戳在今天的 credit 合计）
        today_new = 0.0
        for rid, cr in sessions[sid]["credits"].items():
            ts = request_times.get(rid)
            if ts and today_ts_ms - 86400000 < ts <= today_ts_ms:
                today_new += cr
        sessions[sid]["today_new_credits"] = round(today_new, 2)

    # 模型全局汇总（含来源拆分 + 时间窗口）
    model_agg = defaultdict(lambda: {
        "calls": 0, "prompt": 0, "comp": 0, "est_credits": 0.0, "sessions": set(),
        "calls_main": 0, "tok_main": 0, "calls_sub": 0, "tok_sub": 0, "calls_bg": 0, "tok_bg": 0,
        "first_ts": None, "last_ts": None, "active_secs": 0,  # 时间窗口 + 各会话内部活跃秒数
    })
    for sid, s in sessions.items():
        for model, m in s["models"].items():
            a = model_agg[model]
            a["calls"] += m["calls"]
            a["prompt"] += m["prompt"]
            a["comp"] += m["comp"]
            a["est_credits"] += m.get("est_credits", 0.0)
            a["sessions"].add(sid)
            for k_src, k_calls, k_tok in [("main", "calls_main", "tok_main"),
                                           ("subagent", "calls_sub", "tok_sub"),
                                           ("background", "calls_bg", "tok_bg")]:
                a[k_calls] += m[k_calls]
                a[k_tok] += m[k_tok]
            # 时间窗口：跨会话的 first/last + 该会话内此模型活跃秒数
            if m.get("first") is not None:
                if a["first_ts"] is None or m["first"] < a["first_ts"]:
                    a["first_ts"] = m["first"]
            if m.get("last") is not None:
                if a["last_ts"] is None or m["last"] > a["last_ts"]:
                    a["last_ts"] = m["last"]
            if m.get("first") is not None and m.get("last") is not None:
                a["active_secs"] += max(0, m["last"] - m["first"])
    for m in model_agg.values():
        m["sessions"] = len(m["sessions"])
        m["prompt"] = round(m["prompt"], 3)
        m["comp"] = round(m["comp"], 3)
        m["est_credits"] = round(m["est_credits"], 2)
        m["tok_main"] = round(m["tok_main"], 3)
        m["tok_sub"] = round(m["tok_sub"], 3)
        m["tok_bg"] = round(m["tok_bg"], 3)
        toks = (m["prompt"] + m["comp"]) * 1e6
        m["tokens_per_credit"] = round(toks / m["est_credits"]) if m["est_credits"] > 0 else 0

    return sessions, model_agg


def fmt_duration(secs):
    """把秒数压成短人类可读：< 1分 → 'Ns秒'; < 1时 → 'N分钟'; < 1天 → 'N小时 M分钟'; 否则 'N天 M小时'"""
    if secs is None or secs <= 0:
        return "-"
    s = int(secs)
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s//60}分{s%60}秒" if s%60 else f"{s//60}分钟"
    h, rem = divmod(s, 3600)
    if h < 24:
        return f"{h}小时{rem//60}分" if rem >= 60 else f"{h}小时"
    d, rem = divmod(s, 86400)
    return f"{d}天{rem//3600}小时" if rem >= 3600 else f"{d}天"


def fmt_source_split(m):
    """压缩显示 来源拆分：主 x.xM · 子 x.xM · 后台 x.xM（省略为 0 的部分）"""
    parts = []
    if m.get("tok_main", 0) > 0:
        parts.append(f"主 {m['tok_main']:,.2f}M")
    if m.get("tok_sub", 0) > 0:
        parts.append(f"子 {m['tok_sub']:,.2f}M")
    if m.get("tok_bg", 0) > 0:
        parts.append(f"后台 {m['tok_bg']:,.2f}M")
    return " · ".join(parts) if parts else "-"


# ---------------- 时间工具 ----------------

def fmt_ts(ts_ms):
    if not ts_ms:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def fmt_ts_s(ts_s):
    if not ts_s:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(ts_s).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def fmt_credits(v):
    return f"{v:.2f}"


# ---------------- HTML 渲染 ----------------

def build_html(sessions, model_agg, unlinked, dropped, report_date, out_path, db_path):
    total_credits = sum(s["credits_total"] for s in sessions.values())
    total_today = sum(s["today_new_credits"] for s in sessions.values())
    total_calls = sum(m["calls"] for m in model_agg.values())
    total_prompt = sum(m["prompt"] for m in model_agg.values())
    total_comp = sum(m["comp"] for m in model_agg.values())
    total_tokens = (total_prompt + total_comp) * 1e6
    # 全局比值：分子分母口径必须对齐——只用 traces 覆盖会话的积分做分母
    # （未覆盖会话有积分但无 token，混入会稀释/拉低比值）
    covered_credits = sum(s["credits_total"] for s in sessions.values() if s["traces_covered"])
    tokens_per_credit = round(total_tokens / covered_credits) if covered_credits > 0 else 0
    covered = sum(1 for s in sessions.values() if s["traces_covered"])
    total_dropped = sum(dropped.values())

    # 拆成两张表：纯 token 主表 + 积分反推次表——单一职责避免混淆
    token_rows = ""
    credit_rows = ""
    for model, m in sorted(model_agg.items(), key=lambda x: -(x[1]["prompt"] + x[1]["comp"])):
        model_total_m = m["prompt"] + m["comp"]
        model_total = model_total_m * 1e6
        share = model_total / total_tokens * 100 if total_tokens > 0 else 0
        span_secs = (m["last_ts"] - m["first_ts"]) if (m["first_ts"] and m["last_ts"]) else None
        avg_kh = (model_total / 1e3) / (span_secs / 3600) if span_secs and span_secs > 0 else 0
        token_rows += f"""
        <tr>
          <td><code>{model}</code></td>
          <td>{m['calls']:,}</td>
          <td>{m['prompt']:,.3f}M</td>
          <td>{m['comp']:,.3f}M</td>
          <td><b>{model_total_m:,.2f}M</b></td>
          <td>{share:.1f}%</td>
          <td>{fmt_source_split(m)}</td>
          <td>{fmt_ts_s(m['first_ts']) if m['first_ts'] else '-'}</td>
          <td>{fmt_ts_s(m['last_ts']) if m['last_ts'] else '-'}</td>
          <td>{fmt_duration(span_secs)}</td>
          <td>{fmt_duration(m['active_secs'])}</td>
          <td>{avg_kh:,.0f}</td>
        </tr>"""
    # 积分反推表按估算积分降序（哪模型"贵"看这张）
    for model, m in sorted(model_agg.items(), key=lambda x: -x[1]["est_credits"]):
        model_total_m = m["prompt"] + m["comp"]
        model_total = model_total_m * 1e6
        share = model_total / total_tokens * 100 if total_tokens > 0 else 0
        credit_rows += f"""
        <tr>
          <td><code>{model}</code></td>
          <td><b>{fmt_credits(m['est_credits'])}</b></td>
          <td>{share:.1f}%</td>
          <td>{m['tokens_per_credit']:,}</td>
          <td>{model_total_m:,.2f}M</td>
        </tr>"""

    # 会话卡片（按 token 消耗降序——token 为主线）
    cards = ""
    for sid, s in sorted(sessions.items(), key=lambda x: -sum((m["prompt"] + m["comp"]) * 1e6 for m in x[1]["models"].values())):
        badges = ""
        if s.get("main_multi"):
            badges += '<span class="badge badge-multi">★ 主对话多模型（手动/自动不可分）</span>'
        if s.get("has_subagent"):
            badges += '<span class="badge badge-sub">子代理调用</span>'
        if s.get("has_multi_model") and not s.get("main_multi"):
            badges += '<span class="badge badge-weak">多模型（来自子代理/后台）</span>'
        badges += '<span class="badge badge-trace">traces 已覆盖</span>' if s["traces_covered"] else '<span class="badge badge-weak">traces 未覆盖(仅积分)</span>'

        # 模型明细表（按 token 降序）
        model_detail = ""
        for model, m in sorted(s["models"].items(), key=lambda x: -(x[1]["prompt"] + x[1]["comp"])):
            model_total_m = m["prompt"] + m["comp"]
            model_detail += f"""
            <tr>
              <td><code>{model}</code></td>
              <td>{m['calls']:,}</td>
              <td>{m['prompt']:,.3f}M</td>
              <td>{m['comp']:,.3f}M</td>
              <td><b>{model_total_m:,.2f}M</b></td>
              <td>{fmt_source_split(m)}</td>
              <td>{fmt_ts_s(m['first'])}</td>
              <td>{fmt_ts_s(m['last'])}</td>
              <td>{fmt_credits(m.get('est_credits', 0))}</td>
            </tr>"""
        if not s["models"]:
            model_detail = '<tr><td colspan="9" class="muted">无 trace 数据（会话已不在 traces 保留窗口内）</td></tr>'

        # 时间线（最近 20 条，其余折叠）
        tl = s["timeline"]
        src_label = {"main": "主", "subagent": "子", "background": "后"}
        tl_rows = ""
        tl_latest = tl[-20:]
        for ts, model, pr, co, src in tl_latest:
            tl_rows += f"""
            <tr>
              <td>{fmt_ts_s(ts)}</td>
              <td>{src_label.get(src, '?')}</td>
              <td><code>{model}</code></td>
              <td>{pr:,}</td>
              <td>{co:,}</td>
            </tr>"""
        hidden_rows = ""
        if len(tl) > 20:
            for ts, model, pr, co, src in tl[:-20]:
                hidden_rows += f"""
                <tr>
                  <td>{fmt_ts_s(ts)}</td>
                  <td>{src_label.get(src, '?')}</td>
                  <td><code>{model}</code></td>
                  <td>{pr:,}</td>
                  <td>{co:,}</td>
                </tr>"""
            tl_block = f"""
            <details class="timeline-detail">
              <summary>调用时间线（共 {len(tl):,} 次，默认显示最近 20 次；来源: 主=主对话 子=子代理 后=后台代理）</summary>
              <table class="tbl">
                <thead><tr><th>时间</th><th>来源</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th></tr></thead>
                <tbody>{hidden_rows}{tl_rows}</tbody>
              </table>
            </details>"""
        else:
            tl_block = f"""
            <details class="timeline-detail" open>
              <summary>调用时间线（共 {len(tl):,} 次；来源: 主=主对话 子=子代理 后=后台代理）</summary>
              <table class="tbl">
                <thead><tr><th>时间</th><th>来源</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th></tr></thead>
                <tbody>{tl_rows}</tbody>
              </table>
            </details>"""
        if not tl:
            tl_block = '<p class="muted">无调用时间线数据</p>'

        tokens_total = sum((m["prompt"] + m["comp"]) * 1e6 for m in s["models"].values())
        est_ratio = round(tokens_total / s["credits_total"]) if s["credits_total"] > 0 and tokens_total > 0 else 0

        # 主对话模型时间轴（压缩连续段）：直接回答"什么时候从模型A切到模型B"
        main_tl = [(ts, model) for ts, model, pr, co, src in s["timeline"] if src == "main"]
        main_runs = []
        for ts, model in main_tl:
            if main_runs and main_runs[-1][0] == model:
                main_runs[-1][2] += 1
            else:
                main_runs.append([model, ts, 1])
        if len(main_runs) > 1:
            run_items = " → ".join(
                f"{fmt_ts_s(t)} <code>{m}</code>×{n}" for m, t, n in main_runs
            )
            main_tl_block = f"""
          <div class="main-model-tl">
            <b>主对话模型切换时间轴</b>（共 {len(main_runs)} 段；切换原因不记录，无法区分手动/自动/auto 兜底）：<br>{run_items}
          </div>"""
        elif len(main_runs) == 1:
            main_tl_block = f"""
          <div class="main-model-tl"><b>主对话模型</b>：全程 <code>{main_runs[0][0]}</code>（无切换）</div>"""
        else:
            main_tl_block = ""

        cards += f"""
        <div class="hcard" data-tokens="{tokens_total}">
          <div class="card-head">
            <h3>{s['title']}</h3>
            <div class="badges">{badges}</div>
          </div>
          <div class="card-meta">
            <span>状态: {s['status']}</span>
            <span>当前模型: <code>{s['model'] or '-'}</code></span>
            <span>创建: {fmt_ts(s['created_at'])}</span>
            <span>活跃: {fmt_ts(s['last_activity_at'])}</span>
          </div>
          <div class="stat-row">
            <div class="stat stat-primary"><div class="num">{tokens_total:,.0f}</div><div class="lbl">tokens(精确)</div></div>
            <div class="stat"><div class="num">{sum(m['calls'] for m in s['models'].values()):,}</div><div class="lbl">LLM 调用</div></div>
            <div class="stat"><div class="num">{len(s['credits'])}</div><div class="lbl">计费请求</div></div>
            <div class="stat"><div class="num">{fmt_credits(s['credits_total'])}</div><div class="lbl">积分消耗（仅扣除）</div></div>
            <div class="stat"><div class="num">{est_ratio:,}</div><div class="lbl">该会话：每 1 积分 ≈ ? token</div></div>
            <div class="stat"><div class="num">{fmt_credits(s['today_new_credits'])}</div><div class="lbl">今日积分消耗</div></div>
          </div>
          <h4>模型使用明细（按 token 降序；来源: 主=主对话 子=Agent工具派生的子代理 后=系统后台代理）</h4>
          <table class="tbl">
            <thead><tr><th>模型</th><th>调用次数</th><th>prompt tokens</th><th>completion tokens</th><th>合计</th><th>来源拆分</th><th>首次调用</th><th>末次调用</th><th>估算积分</th></tr></thead>
            <tbody>{model_detail}</tbody>
          </table>
          {main_tl_block}
          {tl_block}
        </div>"""

    multi_notes = ""
    main_multi_sessions = [s for s in sessions.values() if s.get("main_multi")]
    sub_only_sessions = [s for s in sessions.values() if s.get("has_multi_model") and not s.get("main_multi")]
    if main_multi_sessions:
        items = "、".join(f"「{s['title'][:20]}」" for s in main_multi_sessions)
        multi_notes = f'<div class="note warn">⚠️ <b>{len(main_multi_sessions)}</b> 个会话的<b>主对话</b>先后调用过多个模型：{items}。<b>注意：本地数据不记录模型切换事件，无法区分是用户手动切换还是系统自动切换</b>（sessions.model 仅记录当前模型）。</div>'
    if sub_only_sessions:
        items2 = "、".join(f"「{s['title'][:20]}」" for s in sub_only_sessions)
        multi_notes += f'<div class="note">ℹ️ 另有 <b>{len(sub_only_sessions)}</b> 个会话的多模型来自<b>子代理/后台代理</b>（Agent 工具派生的子任务使用独立模型）：{items2}。这不代表主对话被切换。</div>'

    unlinked_note = ""
    if unlinked["generations"] > 0:
        unlinked_note = f'<div class="note">ℹ️ <b>{unlinked["generations"]:,}</b> 次 LLM 调用（{unlinked["prompt"]/1e6:.1f}M prompt / {unlinked["comp"]/1e6:.1f}M comp tokens）来自早期未关联 sessionId 的 trace，未能归到任何会话。</div>'

    dropped_note = ""
    if total_dropped > 0:
        reasons = []
        if dropped.get("empty_out"): reasons.append(f'streaming 中断/取消 <b>{dropped["empty_out"]}</b> 次')
        if dropped.get("no_model_no_tok"): reasons.append(f'模型未知且 0 token <b>{dropped["no_model_no_tok"]}</b> 次')
        if dropped.get("parse_fail"): reasons.append(f'response 解析失败 <b>{dropped["parse_fail"]}</b> 次')
        reason_text = "、".join(reasons) if reasons else f'<b>{total_dropped}</b> 次'
        dropped_note = f'<div class="note muted-note">🗑️ 已丢弃的零产出调用（{reason_text}）：未产生 token、无法归因模型，<b>不计入任何模型/会话统计</b>，仅作为数据完整性提示。</div>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy Token 消耗 & 积分反推 · {report_date}</title>
<style>
:root {{
  --blue: #1a56db; --blue2: #e8f0fe; --bg: #f7f8fa; --ink: #1f2328;
  --panel: #ffffff; --line: #e5e7eb; --muted: #6b7280; --red: #d92d20;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--ink); }}
#topbar {{ position: sticky; top: 0; z-index: 50; background: var(--blue2); border-bottom: 1px solid var(--line); padding: 10px 20px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
#searchInput {{ flex: 1; min-width: 200px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 6px; font-size: 14px; }}
#topbar button {{ padding: 8px 12px; border: 1px solid var(--line); background: var(--panel); border-radius: 6px; cursor: pointer; font-size: 13px; }}
#topbar button:hover {{ background: #eef2ff; }}
#searchCount {{ font-size: 12px; color: var(--muted); white-space: nowrap; }}
.wrap {{ max-width: 1100px; margin: 24px auto; padding: 0 20px; }}
h1 {{ font-size: 22px; }}
h2 {{ font-size: 17px; margin-top: 28px; border-left: 4px solid var(--blue); padding-left: 10px; }}
h3 {{ font-size: 15px; margin: 0; }}
h4 {{ font-size: 13px; margin: 14px 0 6px; color: #374151; }}
.overview {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 16px 0; }}
.ov {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }}
.ov .num {{ font-size: 22px; font-weight: 700; color: var(--blue); }}
.ov .lbl {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
.ov-primary {{ background: #e8f0fe; border-color: #b9d2f8; }}
.ov-primary .num {{ color: #0b3d91; font-size: 26px; }}
.ov-primary .lbl {{ color: #1e3a8a; font-weight: 600; }}
.ov-secondary {{ background: #fafafa; border-style: dashed; }}
.ov-secondary .num {{ color: var(--muted); font-size: 19px; }}
.tbl {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-size: 13px; }}
.tbl th {{ background: #f3f4f6; text-align: left; padding: 8px 10px; font-weight: 600; }}
.tbl td {{ padding: 7px 10px; border-top: 1px solid var(--line); }}
.tbl tr:hover td {{ background: #f9fafb; }}
code {{ background: #f3f4f6; padding: 1px 6px; border-radius: 4px; font-size: 12px; }}
.hcard {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin: 18px 0; box-shadow: 0 1px 3px rgba(0,0,0,.04); }}
.card-head {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }}
.badges {{ display: flex; gap: 6px; }}
.badge {{ font-size: 11px; padding: 2px 8px; border-radius: 20px; }}
.badge-multi {{ background: #fff1f0; color: var(--red); border: 1px solid #ffccc7; }}
.badge-sub {{ background: #fffbe6; color: #ad6800; border: 1px solid #ffe58f; }}
.badge-trace {{ background: #e6f4ff; color: #0958d9; border: 1px solid #91caff; }}
.badge-weak {{ background: #f0f0f0; color: var(--muted); border: 1px solid #d9d9d9; }}
.card-meta {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin: 8px 0 12px; }}
.stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 8px; }}
.stat {{ background: var(--bg); border-radius: 8px; padding: 10px; text-align: center; }}
.stat .num {{ font-size: 17px; font-weight: 700; }}
.stat .lbl {{ font-size: 11px; color: var(--muted); }}
.stat-primary {{ background: #e8f0fe; }}
.stat-primary .num {{ color: #0b3d91; font-size: 20px; }}
.stat-primary .lbl {{ color: #1e3a8a; font-weight: 600; }}
.muted-note {{ background: #fafafa; border-color: #e5e7eb; color: var(--muted); font-size: 12px; }}
.timeline-detail {{ margin-top: 12px; }}
.timeline-detail summary {{ cursor: pointer; font-size: 13px; color: var(--blue); }}
.main-model-tl {{ background: #f6f8ff; border: 1px dashed #b9d2f8; border-radius: 8px; padding: 10px 12px; margin-top: 10px; font-size: 12.5px; line-height: 1.8; }}
.note {{ background: #e6f4ff; border: 1px solid #91caff; border-radius: 8px; padding: 10px 14px; font-size: 13px; margin: 12px 0; }}
.note.warn {{ background: #fff1f0; border-color: #ffccc7; }}
.muted {{ color: var(--muted); }}
mark.search-hit {{ background: #FFE58F; }}
mark.search-cur {{ background: #FF8C00; color: #fff; }}
.user-hl {{ background: #C6F0D2 !important; }}
.user-hl.has-note {{ background: #FFE0B2 !important; }}
#notePanel {{ position: fixed; right: 0; top: 0; width: 320px; height: 100vh; background: var(--panel); border-left: 1px solid var(--line); transform: translateX(100%); transition: transform .25s; z-index: 60; overflow-y: auto; padding: 16px; }}
#notePanel.open {{ transform: none; }}
#notePanel h3 {{ margin-top: 0; }}
#toTop {{ position: fixed; right: 24px; bottom: 30px; width: 42px; height: 42px; border-radius: 50%; background: var(--blue); color: #fff; border: none; font-size: 20px; cursor: pointer; display: none; box-shadow: 0 2px 8px rgba(0,0,0,.2); z-index: 55; }}
.footer {{ text-align: center; color: var(--muted); font-size: 12px; padding: 30px 0; }}
</style>
</head>
<body>
<div id="topbar">
  <input id="searchInput" placeholder="全局检索（会话标题 / 模型 / 数字）…" autocomplete="off">
  <button onclick="doSearch(1)">↑</button>
  <button onclick="doSearch(-1)">↓</button>
  <button onclick="exportNotes()">导出批注 JSON</button>
  <button onclick="toggleNotes()">批注列表</button>
  <span id="searchCount"></span>
</div>

<div class="wrap">
  <h1>WorkBuddy Token 消耗 & 积分反推 <span style="font-weight:400;color:var(--muted);font-size:15px">· {report_date}</span></h1>
  <div class="note">WorkBuddy UI 只向你展示 <b>积分消耗</b>。本报告以 <b>token 消耗为主线</b>（精确，来自 traces generation 调用记录），把积分按 token 占比分摊到各会话×模型作为反推参照——帮你看清「为这些积分实际消耗了多少 token」。每次调用按来源标注：<b>主对话</b> / <b>子代理</b>（Agent 工具派生，使用独立模型）/ <b>后台代理</b>（系统压缩/摘要等）。本地数据不记录模型切换事件，主对话多模型<b>无法区分手动/自动切换</b>。详见 <code>references/schema.md</code>。</div>
  <div class="note muted-note">⚠️ <b>关于积分数字</b>：本报告的"积分消耗" = <code>session_usage.credit_json</code> 之和，<b>仅含 LLM 调用扣除的积分</b>，不含签到 / 邀约 / 任务 / 退款等正向流入（这些只存在 workbuddy.cn 服务端，本地拿不到）。<br>
  &nbsp;&nbsp;&nbsp;<b>UI 余额 = 起始余额 + 期间获得 − 期间消耗</b>，所以本报告数字与 UI 余额之差 = 期间获得的积分。</div>

  <h2>总览</h2>
  <div class="overview">
    <div class="ov ov-primary"><div class="num">{total_prompt + total_comp:,.2f}M</div><div class="lbl">总 token（精确）</div></div>
    <div class="ov"><div class="num">{total_calls:,}</div><div class="lbl">LLM 调用次数</div></div>
    <div class="ov"><div class="num">{len(sessions)}</div><div class="lbl">会话数（traces 覆盖 {covered}）</div></div>
    <div class="ov"><div class="num">{len(model_agg)}</div><div class="lbl">使用模型种类</div></div>
    <div class="ov ov-secondary"><div class="num">{fmt_credits(total_credits)}</div><div class="lbl">积分消耗合计（仅 LLM 调用扣除）</div></div>
    <div class="ov"><div class="num">{fmt_credits(total_today)}</div><div class="lbl">今日积分消耗</div></div>
    <div class="ov"><div class="num">{tokens_per_credit:,}</div><div class="lbl">1 积分 ≈ ? token（仅覆盖会话，混合平均）</div></div>
  </div>
  {multi_notes}
  {unlinked_note}
  {dropped_note}

  <h2>模型 token 消耗（精确）</h2>
  <table class="tbl">
    <thead><tr><th>模型</th><th>调用次数</th><th>prompt tokens</th><th>completion tokens</th><th>合计</th><th>占比</th><th>来源拆分</th><th>首次</th><th>末次</th><th>总跨度</th><th>活跃时长</th><th>k tokens/时（峰值估算）</th></tr></thead>
    <tbody>{token_rows}</tbody>
  </table>

  <h2>积分反推（按 token 占比分摊估算）</h2>
  <div class="note muted-note">以下积分是按会话总积分 × 模型 token 占比分摊的<b>估算值</b>，用于回答"为这些 token 实际花了多少积分"。仅在 traces 覆盖的会话范围内计算。</div>
  <table class="tbl">
    <thead><tr><th>模型</th><th>估算积分</th><th>占 token 总数</th><th>每 1 积分 ≈ ? token</th><th>对应 token</th></tr></thead>
    <tbody>{credit_rows}</tbody>
  </table>

  <h2>会话明细（按 token 降序）</h2>
  {cards}
  <div class="footer">由 credits-monitor skill 生成 · 打开报告即可检索、高亮、批注（存储于浏览器本地）</div>
</div>

<div id="notePanel"><h3>批注列表</h3><div id="noteList"></div></div>
<button id="toTop" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑</button>

<script>
// ===== CONFIG（可编辑区：确认后可将默认值固化到样式/逻辑） =====
const CONFIG = {{
  highlightColor: "#C6F0D2",
  noteColor: "#FFE0B2",
  storageKey: "credits_monitor_annotations",
  searchMinLen: 1,
  scrollTopShow: 300,
  autoExpandOnSearch: false
}};

let curHit = 0, hits = [];
const $ = id => document.getElementById(id);

// ---------- 全局检索 ----------
function doSearch(dir) {{
  const q = $('searchInput').value.trim();
  document.querySelectorAll('mark.search-hit, mark.search-cur').forEach(m => {{
    const t = document.createTextNode(m.textContent);
    m.parentNode.replaceChild(t, m);
  }});
  if (!q || q.length < CONFIG.searchMinLen) {{ $('searchCount').textContent = ''; hits = []; return; }}
  hits = [];
  const re = new RegExp(q.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&'), 'gi');
  const walker = document.createTreeWalker(document.querySelector('.wrap'), NodeFilter.SHOW_TEXT, {{
    acceptNode(n) {{
      const p = n.parentNode;
      if (p.closest && (p.closest('#topbar') || p.closest('#notePanel') || p.tagName === 'SCRIPT' || p.tagName === 'STYLE')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }}
  }});
  let n, did = false;
  while (n = walker.nextNode()) {{
    if (re.test(n.nodeValue)) {{
      re.lastIndex = 0;
      const span = document.createElement('span');
      span.innerHTML = n.nodeValue.replace(re, m => `<mark class="search-hit">${{m}}</mark>`);
      n.parentNode.replaceChild(span, n);
      span.querySelectorAll('mark.search-hit').forEach(mk => hits.push(mk));
      did = true;
    }}
  }}
  curHit = 0;
  $('searchCount').textContent = hits.length ? `${{hits.length}} 处命中` : '无命中';
  if (hits.length) jump(dir);
}}

function jump(dir) {{
  if (!hits.length) return;
  curHit = (curHit + dir + hits.length) % hits.length;
  hits.forEach((h, i) => h.classList.toggle('search-cur', i === curHit));
  hits[curHit].scrollIntoView({{ behavior: 'smooth', block: 'center' }});
}}

$('searchInput').addEventListener('input', () => doSearch(1));
$('searchInput').addEventListener('keydown', e => {{
  if (e.key === 'Enter') doSearch(e.shiftKey ? -1 : 1);
}});

// ---------- 高亮 + 批注 ----------
let notes = [];
try {{ notes = JSON.parse(localStorage.getItem(CONFIG.storageKey) || '[]'); }} catch (e) {{ notes = []; }}

function saveNotes() {{ localStorage.setItem(CONFIG.storageKey, JSON.stringify(notes)); renderNotes(); }}

document.addEventListener('mouseup', () => {{
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.toString().trim()) return;
  const text = sel.toString().trim().slice(0, 200);
  const note = prompt('给这段文字加批注（留空 = 仅高亮）:\\n"' + text + '"');
  if (note === null) return;
  const id = Date.now().toString(36);
  notes.push({{ id, quote: text, text: note }});
  saveNotes();
  try {{
    const range = sel.getRangeAt(0);
    const span = document.createElement('mark');
    span.className = 'user-hl' + (note ? ' has-note' : '');
    span.title = note || '已高亮';
    span.dataset.nid = id;
    span.appendChild(range.extractContents());
    range.insertNode(span);
  }} catch (e) {{}}
  sel.removeAllRanges();
}});

function renderNotes() {{
  const box = $('noteList');
  if (!notes.length) {{ box.innerHTML = '<p class="muted">暂无批注</p>'; return; }}
  box.innerHTML = notes.map(n =>
    `<div style="border-bottom:1px solid var(--line);padding:8px 0;">
      <div style="font-size:11px;color:var(--muted)">「${{n.quote.slice(0, 50)}}」</div>
      <div style="font-size:13px">${{n.text || '<i>仅高亮</i>'}}</div>
      <button onclick="delNote('${{n.id}}')" style="font-size:11px;color:var(--red);border:none;background:none;cursor:pointer;padding:0">删除</button>
    </div>`).join('');
}}

function delNote(id) {{
  notes = notes.filter(n => n.id !== id);
  saveNotes();
  const el = document.querySelector(`mark.user-hl[data-nid="${{id}}"]`);
  if (el) {{
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  }}
}}

function exportNotes() {{
  const blob = new Blob([JSON.stringify(notes, null, 2)], {{ type: 'application/json' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'credits_monitor_notes.json';
  a.click();
}}

function toggleNotes() {{ $('notePanel').classList.toggle('open'); }}
renderNotes();

// ---------- 回到顶部 ----------
window.addEventListener('scroll', () => {{
  $('toTop').style.display = window.scrollY > CONFIG.scrollTopShow ? 'block' : 'none';
}});
</script>
</body>
</html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 积分/token 消耗监控")
    ap.add_argument("--out", default=DEFAULT_OUT, help="报告输出目录")
    ap.add_argument("--db", default=DEFAULT_DB, help="workbuddy.db 路径")
    ap.add_argument("--traces", default=DEFAULT_TRACES, help="traces 目录")
    ap.add_argument("--audit", default=DEFAULT_AUDIT, help="audit-log 目录")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[错误] 数据库不存在: {args.db}")
        sys.exit(1)

    today = datetime.date.today()
    report_date = today.isoformat()
    now = datetime.datetime.now()
    today_ts_ms = int(now.timestamp() * 1000)

    print(f"[1/4] 读取会话与积分: {args.db}")
    sessions = load_sessions(args.db)

    print(f"[2/4] 扫描 traces 调用记录: {args.traces}")
    per_session, unlinked, dropped = load_generations(args.traces)

    print(f"[3/4] 构建 requestId→时间索引（audit-log）")
    request_times = load_request_times(args.audit)

    sessions, model_agg = merge_data(sessions, per_session, request_times, today_ts_ms)

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, f"credits_{report_date}.html")
    latest_path = os.path.join(args.out, "credits_latest.html")

    print(f"[4/4] 渲染报告")
    build_html(sessions, model_agg, unlinked, dropped, report_date, out_path, args.db)

    # 副本
    with open(out_path, encoding="utf-8") as f:
        content = f.read()
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅ 报告已生成:")
    print(f"   {out_path}")
    print(f"   {latest_path}")
    print(f"\n汇总: 会话 {len(sessions)} 个 | 积分消耗(仅扣除) {sum(s['credits_total'] for s in sessions.values()):.2f} | "
          f"LLM 调用 {sum(m['calls'] for m in model_agg.values()):,} 次 | "
          f"prompt {sum(m['prompt'] for m in model_agg.values()):.2f}M / comp {sum(m['comp'] for m in model_agg.values()):.2f}M tokens")


if __name__ == "__main__":
    main()
