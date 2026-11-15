# -*- coding: utf-8 -*-
"""
failure_stats.py —— 失败信息的唯一分类实现（Phase 1 · 第一步）

三条互斥通道，用途完全不同：

  事实通道  check_facts()          平台检查项计数。全篇唯一的数字来源。
  解释通道  check_weak_signals()   只对被标记为 eligible 的检查项给出"弱证据"表述。
                                   硬约束：输出不含任何阿拉伯数字。
  工程通道  attempt_stats()        attempts 错误分类。含未知识别，原文保留。

为什么必须分开：
  网络抖动、限流、算子无权限，与"这个经济假设成不成立"毫无关系。
  一旦混进研究通道，AI 会把工程故障学成"这条逻辑不行"，研究记忆就被污染了。

只读约束：
  所有数据库连接必须 mode=ro。本模块不执行任何 INSERT / UPDATE / DELETE。
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- 检查项登记表
# 键为平台返回的 failed_checks 原始名称（来自 data/alphas.db 的实测取值）。
# eligible=True 才允许进入解释通道，且只能使用 weak 给出的措辞。
CHECK_REGISTRY = {
    "LOW_SHARPE": {
        "fact": "表现门槛 · 截面 Sharpe 未达标",
        "eligible": True,
        "weak": "截面表现不足，可能指向信号偏弱，也可能只是随机波动，需与回测期长度一起看。",
    },
    "LOW_FITNESS": {
        "fact": "表现门槛 · Fitness 未达标",
        "eligible": True,
        "weak": "风险调整后收益不足，需先区分是信号问题还是换手拖累，不能直接归为逻辑失效。",
    },
    "LOW_SUB_UNIVERSE_SHARPE": {
        "fact": "子域门槛 · 子域 Sharpe 未过",
        "eligible": True,
        "weak": "该信号在部分子域不成立，适用域可能偏窄，属弱证据，不构成对假设本身的否定。",
    },
    "CONCENTRATED_WEIGHT": {
        "fact": "参数结构 · 权重过度集中",
        "eligible": False,
        "weak": None,
    },
    "HIGH_TURNOVER": {
        "fact": "参数结构 · 换手过高",
        "eligible": False,
        "weak": None,
    },
    "LOW_TURNOVER": {
        "fact": "参数结构 · 换手过低",
        "eligible": False,
        "weak": None,
    },
}

# 未登记检查项的统一归类（不猜测含义，只如实呈现）
UNKNOWN_CHECK_FACT = "未收录检查项 · 待人工归类"

# ---------------------------------------------------------------- 工程错误分类
# 顺序有意义：先匹配越具体的规则。全部基于 attempts.error 实测样本。
ENGINEERING_RULES = [
    ("operator_denied", re.compile(r'inaccessible or unknown operator "([^"]+)"'), "算子无权限"),
    ("param_signature", re.compile(r"Invalid number of inputs|Required attribute \"lookback\""), "参数/签名"),
    ("unit_type", re.compile(r"Incompatible unit"), "单位/类型不匹配"),
    ("rate_limit", re.compile(r"多次限流|HTTP 429"), "限流"),
    ("timeout", re.compile(r"模拟超时|timed out|Timeout"), "超时"),
    ("network", re.compile(r"网络异常|Connection aborted|ConnectionReset|ConnectionAbortedError|Max retries exceeded"), "网络中断"),
]

UNCLASSIFIED = "unclassified"
UNCLASSIFIED_LABEL = "未知/待人工归类"


# ---------------------------------------------------------------- 基础工具

def connect_ro(db_path) -> sqlite3.Connection:
    """只读连接。这是本模块唯一的数据库入口，杜绝误写。"""
    p = Path(db_path).resolve()
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def parse_checks(value) -> list:
    """failed_checks 字段是 JSON 数组字符串，容错解析。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    try:
        arr = json.loads(value)
    except Exception:
        return []
    return [str(x) for x in arr] if isinstance(arr, list) else []


def count_checks(values) -> Counter:
    """统计检查项出现次数。values 是可迭代的 failed_checks 原始值。"""
    c = Counter()
    for v in values:
        for name in parse_checks(v):
            c[name] += 1
    return c


def classify_error(msg: str) -> tuple:
    """返回 (类别键, 中文标签, 细节)。不匹配则归入 unclassified。"""
    if not msg:
        return UNCLASSIFIED, UNCLASSIFIED_LABEL, ""
    for key, pattern, label in ENGINEERING_RULES:
        m = pattern.search(msg)
        if m:
            detail = m.group(1) if m.groups() else ""
            return key, label, detail
    return UNCLASSIFIED, UNCLASSIFIED_LABEL, ""


# ---------------------------------------------------------------- 事实通道

def check_facts(counter: Counter) -> list:
    """把 Counter 变成事实表：[{name, count, fact, eligible, known}]，按次数降序。"""
    rows = []
    for name, cnt in counter.most_common():
        meta = CHECK_REGISTRY.get(name)
        rows.append({
            "name": name,
            "count": cnt,
            "fact": meta["fact"] if meta else UNKNOWN_CHECK_FACT,
            "eligible": bool(meta and meta["eligible"]),
            "known": meta is not None,
        })
    return rows


def render_fact_block(counter: Counter) -> str:
    """事实通道文本：唯一出现数字的地方。"""
    rows = check_facts(counter)
    if not rows:
        return "（本期无平台检查失败记录）"
    lines = []
    for r in rows:
        lines.append(f"  {r['name']}: {r['count']} 次  —— {r['fact']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 解释通道

def check_weak_signals(counter: Counter) -> list:
    """只返回 eligible 项。不含数字，只含名称与固定措辞。"""
    out = []
    for r in check_facts(counter):
        if not r["eligible"]:
            continue
        meta = CHECK_REGISTRY.get(r["name"])
        if meta and meta["weak"]:
            out.append({"name": r["name"], "weak": meta["weak"]})
    return out


def render_weak_block(counter: Counter) -> str:
    """解释通道文本。硬约束：不得出现阿拉伯数字（测试会断言）。"""
    items = check_weak_signals(counter)
    if not items:
        return "（本期没有可解释的失败项；未过门槛的原因均为参数或结构类，不构成研究结论）"
    lines = []
    for it in items:
        lines.append(f"  - {it['name']}（计数见平台检查事实块）：{it['weak']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 工程通道

def attempt_stats(conn: sqlite3.Connection, since_iso: str = None, date: str = None) -> dict:
    """
    统计 attempts 失败原因。
    since_iso: 'YYYY-MM-DDTHH:MM' 起始时间（轮次复盘用）
    date:      'YYYY-MM-DD' 按日过滤（日报用）
    两者都不给则统计全量。
    """
    sql = "SELECT expression, error FROM attempts WHERE ok=0"
    params = []
    if since_iso:
        sql += " AND created_at >= ?"
        params.append(since_iso)
    elif date:
        sql += " AND created_at LIKE ?"
        params.append(date + "%")

    stats = {key: {"label": label, "count": 0, "examples": []} for key, _, label in ENGINEERING_RULES}
    stats[UNCLASSIFIED] = {"label": UNCLASSIFIED_LABEL, "count": 0, "examples": []}
    total = 0

    for expr, err in conn.execute(sql, params):
        total += 1
        key, label, detail = classify_error(err or "")
        row = stats[key]
        row["count"] += 1
        if len(row["examples"]) < 20:
            row["examples"].append({
                "expr": (expr or "")[:90],
                "error": (err or "").strip()[:120],   # 原文保留，仅截断长度
                "detail": detail,
            })
    stats["_total"] = total
    return stats


def render_engineering_block(stats: dict) -> str:
    """工程通道文本。未知项单列，附原文，并标注禁止用于判断。"""
    if not stats or not stats.get("_total"):
        return "（本期无提交失败记录）"
    lines = [f"  合计 {stats['_total']} 次提交失败。"]
    for key, _, label in ENGINEERING_RULES:
        row = stats.get(key)
        if not row or not row["count"]:
            continue
        extra = ""
        if key == "operator_denied":
            ops = sorted({e["detail"] for e in row["examples"] if e["detail"]})
            if ops:
                extra = f"（算子：{', '.join(ops)}）"
        lines.append(f"  {label}: {row['count']} 次{extra}")

    unk = stats.get(UNCLASSIFIED)
    if unk and unk["count"]:
        lines.append(f"  {UNCLASSIFIED_LABEL}: {unk['count']} 次 —— 归类未完成，禁止用于判断")
        for e in unk["examples"][:5]:
            lines.append(f"      原文：{e['error']}")
    return "\n".join(lines)


def unclassified_samples(stats: dict) -> list:
    row = (stats or {}).get(UNCLASSIFIED) or {}
    return row.get("examples", [])


# ---------------------------------------------------------------- 报告渲染

def report_sections(counter: Counter, stats: dict) -> str:
    """日报用的三段 Markdown。与 LLM 输入同源，保证数字一致。"""
    L = []
    L.append("## 平台检查失败分布（事实）")
    L.append("")
    rows = check_facts(counter)
    if rows:
        L.append("| 检查项 | 次数 | 平台归类 |")
        L.append("|---|---|---|")
        for r in rows:
            L.append(f"| {r['name']} | {r['count']} | {r['fact']} |")
    else:
        L.append("（无）")
    L.append("")

    L.append("## 研究弱证据（引用上表，不含数字）")
    L.append("")
    items = check_weak_signals(counter)
    if items:
        for it in items:
            L.append(f"- **{it['name']}**（计数见上表）：{it['weak']}")
    else:
        L.append("（本期未过门槛的项均为参数或结构类，不构成研究结论）")
    L.append("")

    L.append("## 工程噪声（提交失败，禁止用于假设判断）")
    L.append("")
    if stats and stats.get("_total"):
        L.append(f"合计 {stats['_total']} 次提交失败。")
        L.append("")
        L.append("| 类别 | 次数 | 说明 |")
        L.append("|---|---|---|")
        for key, _, label in ENGINEERING_RULES:
            row = stats.get(key)
            if row and row["count"]:
                L.append(f"| {label} | {row['count']} | 与假设真伪无关 |")
        unk = stats.get(UNCLASSIFIED)
        if unk and unk["count"]:
            L.append(f"| {UNCLASSIFIED_LABEL} | {unk['count']} | 归类未完成，禁止用于判断 |")
        unknows = unclassified_samples(stats)
        if unknows:
            L.append("")
            L.append("未归类错误原文（待人工归类）：")
            for e in unknows[:5]:
                L.append(f"- `{e['error']}`")
    else:
        L.append("（无）")
    return "\n".join(L)


def write_unclassified_report(path, stats: dict, scope: str) -> int:
    """把未归类错误写成独立清单，供人工归类。返回条数。"""
    items = unclassified_samples(stats or {})
    total = (stats or {}).get(UNCLASSIFIED, {}).get("count", 0)
    L = [f"# 未归类提交错误清单（{scope}）", "",
         "本清单只做归类辅助，**不构成任何研究结论**，也**不代表假设失败**。",
         "原文保留（截断 120 字），待人工归类后再决定是否加入分类规则。", "",
         f"- 未归类总数：{total}",
         f"- 本清单展示：{len(items)} 条（同类去重前采样）", ""]
    if items:
        L.append("| # | 表达式（截断） | 错误原文 |")
        L.append("|---|---|---|")
        for i, e in enumerate(items, 1):
            L.append(f"| {i} | `{e['expr']}` | `{e['error']}` |")
    else:
        L.append("（无未归类错误）")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return total
