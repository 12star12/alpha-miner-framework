# -*- coding: utf-8 -*-
"""
daily_review.py —— 每日复盘 + LLM 解读 + 明日建议（带护栏自动应用）

流程：
  1. 从 alphas.db 读指定日期的有效结果
  2. 按字段 / 模板 / 窗口统计（只给 LLM 统计数字，不给原始数据）
  3. GLM 解读：哪些字段浪费额度、哪组窗口出活
  4. 产出：
     data/reviews/YYYY-MM-DD.md   人读的日报
     data/advice.json             daemon 启动时读取并自动应用

护栏（sanitize）：建议只能涉及白名单字段；至少保留 3 个字段；窗口必须是 2~504 的整数。
LLM 永远碰不到额度上限、认证、并发这些关键配置。

用法：
  python daily_review.py                    # 复盘今天
  python daily_review.py --date 2025-01-15  # 补复盘某天
"""
import argparse
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

import config
import failure_stats

DB = config.DB_PATH
REVIEW_DIR = config.DATA_DIR / "reviews"
ADVICE_FILE = config.DATA_DIR / "advice.json"

PASS_S, PASS_F = 1.25, 1.0
MIN_ROWS_FOR_LLM = 15


# ---------------------------------------------------------------- 数据层

def load_rows(date: str) -> list:
    if not DB.exists():
        return []
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT expression, field, template, sharpe, fitness, turnover, failed_checks "
            "FROM alphas WHERE created_at LIKE ? AND sharpe IS NOT NULL",
            (date + "%",),
        ).fetchall()
    finally:
        conn.close()


def windows_in(expr: str) -> set:
    out = set()
    for m in re.finditer(r"[A-Za-z_]\w*\s*\(([^()]*)\)", expr):
        for tok in m.group(1).split(","):
            tok = tok.strip()
            if tok.isdigit() and 2 <= int(tok) <= 504:
                out.add(int(tok))
    return out


def agg(rows: list, key_fn) -> list:
    groups = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        groups.setdefault(k, []).append(r)
    out = []
    for k, g in groups.items():
        ss = [x[3] for x in g]
        fs = [x[4] for x in g]
        best = max(g, key=lambda x: x[3])
        out.append((k, len(g), statistics.mean(ss), max(ss), max(fs), best[0]))
    out.sort(key=lambda x: -x[2])
    return out


# ---------------------------------------------------------------- 报告层

def build_warn_section(date: str) -> list:
    """WARN 判定统计（第二步接入）：只统计，不阻断。"""
    import expr_guard
    rep = expr_guard.read_warnings(date)
    L = ["", "## WARN 判定统计（筛查提示，非无效判定）", ""]
    if not rep["total"]:
        L.append("（本期没有 WARN 记录）")
        return L
    L.append("| 判定码 | 含义 | 条数 |")
    L.append("|---|---|---|")
    desc = expr_guard._CODE_DESC
    for code, n in rep["counts"].most_common():
        L.append(f"| {code} | {desc.get(code, '')} | {n} |")
    if rep["samples"]:
        L.append("")
        L.append("样例：")
        for it in rep["samples"]:
            L.append(f"- `{it.get('expr', '')[:70]}` —— {it.get('code')}：{it.get('reason', '')[:60]}")
    return L


def build_report(date, rows, by_field, by_tpl, by_win, advice,
                 check_counter=None, eng_stats=None) -> str:
    n = len(rows)
    ps = sum(1 for r in rows if r[3] >= PASS_S)
    pf = sum(1 for r in rows if r[3] >= PASS_S and r[4] >= PASS_F)
    top = sorted(rows, key=lambda r: r[3], reverse=True)[:5]

    L = [f"# 每日复盘 {date}", ""]
    L.append(f"- 入库 {n} 条；Sharpe≥{PASS_S}：{ps} 条；双达标：{pf} 条"
             + (f"（{pf / n * 100:.1f}%）" if n else ""))
    L.append("")

    L.append("## 按字段")
    L.append("| 字段 | 条数 | 平均S | 最高S | 最高F | 最佳表达式 |")
    L.append("|---|---|---|---|---|---|")
    for k, c, a, mx, mf, be in by_field[:12]:
        L.append(f"| {k} | {c} | {a:.2f} | {mx:.2f} | {mf:.2f} | {be[:44]} |")

    L.append("")
    L.append("## 按模板")
    L.append("| 模板 | 条数 | 平均S | 最高S | 最佳表达式 |")
    L.append("|---|---|---|---|---|")
    for k, c, a, mx, mf, be in by_tpl[:12]:
        L.append(f"| {k} | {c} | {a:.2f} | {mx:.2f} | {be[:44]} |")

    if by_win:
        L.append("")
        L.append("## 按窗口")
        L.append("| 窗口 | 条数 | 平均S | 最高S |")
        L.append("|---|---|---|---|")
        for k, c, a, mx, mf, be in by_win[:10]:
            L.append(f"| {k} | {c} | {a:.2f} | {mx:.2f} |")

    L.append("")
    L.append(failure_stats.report_sections(check_counter or {}, eng_stats or {}))
    L += build_warn_section(date)

    L.append("")
    L.append("## 今日 Sharpe Top 5")
    for r in top:
        L.append(f"- S={r[3]:.2f} F={r[4]:.2f} T={r[5] * 100:.0f}%  `{r[0][:70]}`")

    L.append("")
    L.append("## AI 解读与明日建议")
    if advice:
        L.append(f"> {advice.get('summary', '')}")
        L.append("")
        if advice.get("keep_fields"):
            L.append(f"- 优先字段：{', '.join(advice['keep_fields'])}")
        if advice.get("stop_fields"):
            L.append(f"- 停跑字段：{', '.join(advice['stop_fields'])}")
        if advice.get("windows"):
            L.append(f"- 建议窗口组：{advice['windows']}")
        L.append(f"- 依据：{advice.get('reason', '')}")
        if advice.get("notes"):
            L.append("")
            L.append("### Top 因子逻辑解读")
            for n in advice["notes"]:
                L.append(f"- `{n['expr']}…`：{n['comment']}")
    else:
        L.append("（数据不足或 LLM 解读失败，无建议）")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- LLM 层

ADVICE_SYSTEM = (
    "你是量化研究的数据分析员。你只能基于给定的统计数字下结论；"
    "数字之外的任何猜测必须写明'推测'；绝不编造数字。"
    "回答的最后必须单独一行输出一个 JSON 对象（不要包代码块）：\n"
    '{"keep_fields": ["..."], "stop_fields": ["..."], "windows": [n, n, n], '
    '"summary": "一句话日报", "reason": "建议依据", '
    '"notes": [{"expr": "表达式开头20字", "comment": "这条因子的经济逻辑是什么，'
    '数据说明它为什么行/不行"}]}\n'
    "keep_fields / stop_fields 只能从给出的字段列表里选；windows 是 3 个 2~504 的整数；"
    "stop_fields 最多停掉一半字段（挖矿需要多样性，别把鸡蛋停光）；"
    "notes 最多 5 条，只评给出的 Top 因子，comment 每条不超过 60 字。\n"
    "输入分成互斥的几块：表现统计、平台检查事实、研究弱证据、工程噪声。\n"
    "平台检查事实是唯一数字来源；研究弱证据只能沿用给定措辞，不得升级为结论；\n"
    "工程噪声（网络/限流/超时/算子无权限/未归类）与假设真伪无关，"
    "禁止据此调整字段或模板，只能用于提示校验漏洞或平台状态。\n"
    "严禁把任何检查项表述为『经济假设失败』或『逻辑不成立』。"
)


def build_llm_input(date, rows, by_field, by_tpl, by_win,
                    check_counter=None, eng_stats=None) -> str:
    allowed = list(dict.fromkeys(config.FIELD_ONLY))
    L = [f"日期 {date}，共 {len(rows)} 条回测结果（规则模板产出）。", ""]
    L.append("【表现统计】")
    L.append("按字段（字段: 条数, 平均Sharpe, 最高Sharpe, 最高Fitness）：")
    for k, c, a, mx, mf, _ in by_field[:12]:
        L.append(f"  {k}: {c}, {a:.2f}, {mx:.2f}, {mf:.2f}")
    L.append("")
    L.append("按模板（模板: 条数, 平均Sharpe, 最高Sharpe）：")
    for k, c, a, mx, mf, _ in by_tpl[:10]:
        L.append(f"  {k}: {c}, {a:.2f}, {mx:.2f}")
    if by_win:
        L.append("")
        L.append("按窗口（窗口: 条数, 平均Sharpe, 最高Sharpe）：")
        for k, c, a, mx, mf, _ in by_win[:10]:
            L.append(f"  {k}: {c}, {a:.2f}, {mx:.2f}")
    L.append("")
    L.append(f"当前白名单字段（keep/stop 只能从这里选）：{', '.join(allowed)}")
    L.append("请判断：哪些字段在浪费额度该停、哪些值得优先、哪组窗口出活，给出理由。")
    top = sorted(rows, key=lambda r: r[3], reverse=True)[:8]
    L.append("")
    L.append("今日 Sharpe 最高的 8 条（请对前 5 条逐条给出 notes 解读）：")
    for i, r in enumerate(top, 1):
        L.append(f"  {i}. S={r[3]:.2f} F={r[4]:.2f} T={r[5] * 100:.0f}%  {r[0][:80]}")

    L.append("")
    L.append("【平台检查事实 · 唯一数字来源】")
    L.append(failure_stats.render_fact_block(check_counter or {}))

    L.append("")
    L.append("【研究弱证据 · 引用上块，弱证据非结论】")
    L.append(failure_stats.render_weak_block(check_counter or {}))

    L.append("")
    L.append("【工程噪声 · 禁止用于判断假设】")
    L.append(failure_stats.render_engineering_block(eng_stats or {}))
    return "\n".join(L)


def ask_llm(llm_input: str):
    import requests
    if not config.LLM_API_KEY:
        return None
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": ADVICE_SYSTEM},
            {"role": "user", "content": llm_input},
        ],
        "temperature": 0.3,
        "max_tokens": 900,
    }
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}",
               "Content-Type": "application/json"}
    try:
        r = requests.post(config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
                          json=payload, headers=headers, timeout=90)
        if r.status_code != 200:
            print(f"  LLM 返回 {r.status_code}: {r.text[:150]}")
            return None
        text = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:
        print(f"  LLM 调用失败：{e}")
        return None


def sanitize(advice, allowed: set):
    """护栏：白名单校验 + 至少留一半字段 + 窗口格式校验。"""
    if not isinstance(advice, dict):
        return None
    keep = [x for x in advice.get("keep_fields", []) if x in allowed]
    stop = [x for x in advice.get("stop_fields", []) if x in allowed]
    if len(stop) > len(allowed) // 2:      # 最多停一半
        stop = stop[: len(allowed) // 2]
    wins = [w for w in advice.get("windows", []) if isinstance(w, int) and 2 <= w <= 504]
    notes = []
    for n in (advice.get("notes") or [])[:5]:
        if isinstance(n, dict) and n.get("comment"):
            notes.append({"expr": str(n.get("expr", ""))[:24],
                          "comment": str(n["comment"])[:90]})
    return {
        "keep_fields": keep,
        "stop_fields": stop,
        "windows": wins[:3] if len(wins) == 3 else [],
        "summary": str(advice.get("summary", ""))[:200],
        "reason": str(advice.get("reason", ""))[:400],
        "notes": notes,
        "date": advice.get("_date", ""),
    }


# ---------------------------------------------------------------- 主流程

def run(date: str):
    rows = load_rows(date)
    print(f"[daily_review] {date}: 有效结果 {len(rows)} 条")

    by_field = agg(rows, lambda r: r[1])
    by_tpl = agg(rows, lambda r: r[2])
    by_win = [x for x in agg(rows, lambda r: sorted(windows_in(r[0]))[0]
                             if windows_in(r[0]) else None) if x[0] is not None]

    # 失败信息：事实 / 弱证据 / 工程噪声，三通道分开取数
    check_counter = failure_stats.count_checks(
        r[6] if len(r) > 6 else None for r in rows)
    conn = failure_stats.connect_ro(DB)
    try:
        eng_stats = failure_stats.attempt_stats(conn, date=date)
    finally:
        conn.close()
    failure_stats.write_unclassified_report(
        Path(__file__).resolve().parent / "docs" / "unclassified-errors.md",
        eng_stats, scope=date)

    advice = None
    if len(rows) >= MIN_ROWS_FOR_LLM:
        raw = ask_llm(build_llm_input(date, rows, by_field, by_tpl, by_win,
                                      check_counter, eng_stats))
        if raw:
            raw["_date"] = date
            advice = sanitize(raw, set(config.FIELD_ONLY))
            if advice:
                ADVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
                ADVICE_FILE.write_text(json.dumps(advice, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
                print(f"[daily_review] 明日建议已写入 {ADVICE_FILE}")
            else:
                print("[daily_review] LLM 建议未通过护栏，丢弃")
    else:
        print(f"[daily_review] 结果少于 {MIN_ROWS_FOR_LLM} 条，不做 LLM 解读")

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"{date}.md"
    path.write_text(build_report(date, rows, by_field, by_tpl, by_win, advice,
                                 check_counter, eng_stats),
                    encoding="utf-8")
    print(f"[daily_review] 日报已写入 {path}")
    return advice


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run(a.date)
