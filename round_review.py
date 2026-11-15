# -*- coding: utf-8 -*-
"""
round_review.py —— 每轮复盘（40 条一评）

每轮结束后：
  1. 取本轮实际回测结果（按时间戳）
  2. 按字段 / 模板家族 / 窗口统计
  3. LLM 解读：下一轮 40 条该围绕什么逻辑、哪些字段/家族该减、哪组窗口
  4. 产出 data/round_advice.json，daemon 读取后调整下一轮的候选构造

护栏：字段/家族/窗口全部白名单校验；stop 类建议最多停一半，保证多样性。
LLM 只影响"下一轮选什么"，永远碰不到额度、并发、认证。
"""
import json
import re
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

import config
import failure_stats

DB = config.DB_PATH
ADVICE_FILE = config.DATA_DIR / "round_advice.json"

SYSTEM = (
    "你是量化研究的轮次分析员。每轮回测 40 条候选，你要根据本轮统计，"
    "调整下一轮 40 条的选样逻辑。只能基于给出的数字下结论，不编造数字。\n"
    "输入分成互斥的几块，用途截然不同，禁止混用：\n"
    "  表现统计块：字段/模板/窗口的 Sharpe 与 Fitness，可作为取舍依据。\n"
    "  平台检查事实块：平台判定的失败项计数，是全篇唯一的数字来源。\n"
    "  研究弱证据块：只把部分检查项解释为『弱证据』，措辞已给定，不得升级为结论。\n"
    "  工程噪声块：网络、限流、超时、算子无权限、未归类错误。\n"
    "     这些与假设真伪无关，禁止据此调整字段或模板取舍；"
    "只能用于提示本地校验有漏洞或平台状态异常。\n"
    "  家族自相关块：平台口径的相对重叠事实（可交=自相关<0.7）。"
    "可用于决定 families_stop / families_keep，但不得表述为经济假设失败。\n"
    "严禁把任何检查项表述为『经济假设失败』或『逻辑不成立』。\n"
    "回答最后单独一行输出 JSON（不要代码块）：\n"
    '{"keep_fields": ["..."], "stop_fields": ["..."], '
    '"families_keep": ["..."], "families_stop": ["..."], '
    '"windows": [n, n, n], "focus": "下一轮 40 条应围绕什么逻辑（一句话）", '
    '"summary": "本轮一句话总结"}\n'
    "约束：keep_fields/stop_fields 只能从给定字段列表选；"
    "families_keep/families_stop 只能从给定模板家族选；"
    "windows 是 3 个 2~504 的整数；stop 类建议最多停一半（保持多样性）。"
)


def load_rows_since(since: datetime) -> list:
    if not DB.exists():
        return []
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT expression, field, template, sharpe, fitness, turnover, failed_checks "
            "FROM alphas WHERE created_at >= ? AND sharpe IS NOT NULL",
            (since.strftime("%Y-%m-%dT%H:%M"),),
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


def family(template: str) -> str:
    return (template or "").split(" | ")[0].strip()


def agg(rows, key_fn):
    groups = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        groups.setdefault(k, []).append(r)
    out = []
    for k, g in groups.items():
        ss = [x[3] for x in g]
        best = max(g, key=lambda x: x[3])
        out.append((k, len(g), statistics.mean(ss), max(ss), best[0]))
    out.sort(key=lambda x: -x[2])
    return out


def family_corr_stats() -> list:
    """家族级自相关事实：读 selfcorr 旁表，回答「哪些族产出的因子交不上去」。
    只读旁表，不动 alphas/attempts。"""
    if not DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT a.expression, c.max_abs FROM alphas a "
                "JOIN selfcorr c ON a.alpha_id = c.alpha_id "
                "WHERE c.max_abs IS NOT NULL").fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    from selfcorr import family_of
    buckets = {}
    for expr, corr in rows:
        b = buckets.setdefault(family_of(expr), {"n": 0, "ok": 0, "min": corr})
        b["n"] += 1
        b["ok"] += 1 if corr < config.FILTER["self_corr_max"] else 0
        b["min"] = min(b["min"], corr)
    return sorted(((k, v["n"], v["ok"], v["min"]) for k, v in buckets.items()),
                  key=lambda x: (-x[2], x[3]))


def build_input(rows, by_field, by_fam, by_win, allowed_fields, families,
                check_counter=None, eng_stats=None, fam_corr=None):
    L = [f"本轮共 {len(rows)} 条有效回测。", ""]

    # ---- 表现统计块（结果类，不含失败归因）
    L.append("【表现统计】")
    L.append("按字段（字段: 条数, 平均Sharpe, 最高Sharpe）：")
    for k, c, a, mx, _ in by_field[:10]:
        L.append(f"  {k}: {c}, {a:.2f}, {mx:.2f}")
    L.append("")
    L.append("按模板家族（家族: 条数, 平均Sharpe, 最高Sharpe）：")
    for k, c, a, mx, _ in by_fam[:12]:
        L.append(f"  {k}: {c}, {a:.2f}, {mx:.2f}")
    if by_win:
        L.append("")
        L.append("按窗口（窗口: 条数, 平均Sharpe, 最高Sharpe）：")
        for k, c, a, mx, _ in by_win[:8]:
            L.append(f"  {k}: {c}, {a:.2f}, {mx:.2f}")

    # ---- 平台检查事实块（唯一数字来源）
    L.append("")
    L.append("【平台检查事实 · 唯一数字来源】")
    L.append(failure_stats.render_fact_block(check_counter or {}))

    # ---- 研究弱证据块（不含数字）
    L.append("")
    L.append("【研究弱证据 · 引用上块，弱证据非结论】")
    L.append(failure_stats.render_weak_block(check_counter or {}))

    # ---- 工程噪声块（禁止用于判断假设）
    L.append("")
    L.append("【工程噪声 · 禁止用于判断假设】")
    L.append(failure_stats.render_engineering_block(eng_stats or {}))

    # ---- 家族自相关事实块（平台口径 cutoff 0.7）
    L.append("")
    L.append("【家族自相关事实 · 平台 cutoff 0.7】")
    if fam_corr:
        L.append("家族: 已查条数, 可交条数(自相关<0.7), 最低自相关")
        for k, n, ok, mn in fam_corr[:12]:
            L.append(f"  {k}: {n}, {ok}, {mn:.2f}")
        L.append("说明：可交条数为 0 的家族，表示该族产出的 PnL 已被已提交池覆盖，"
                 "同族换窗口大概率交不上去。这是平台事实，不是经济假设失败。")
    else:
        L.append("暂无自相关数据（可跑 python selfcorr.py --all 建立）")

    L.append("")
    L.append(f"可选字段：{', '.join(allowed_fields)}")
    L.append(f"可选模板家族：{', '.join(families)}")
    L.append("请给出下一轮 40 条的调整建议。工程噪声不得作为取舍依据。")
    return "\n".join(L)


def ask_llm(llm_input: str):
    import requests
    if not config.LLM_API_KEY:
        return None
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": llm_input},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}",
               "Content-Type": "application/json"}
    try:
        r = requests.post(config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
                          json=payload, headers=headers, timeout=90)
        if r.status_code != 200:
            print(f"  LLM 返回 {r.status_code}: {r.text[:120]}")
            return None
        text = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"  LLM 调用失败：{e}")
        return None


def sanitize(advice, allowed_fields: set, families: list):
    if not isinstance(advice, dict):
        return None
    fam_set = set(families)
    keep_f = [x for x in advice.get("keep_fields", []) if x in allowed_fields]
    stop_f = [x for x in advice.get("stop_fields", []) if x in allowed_fields]
    if len(stop_f) > len(allowed_fields) // 2:
        stop_f = stop_f[: len(allowed_fields) // 2]
    fam_keep = [x for x in advice.get("families_keep", []) if x in fam_set]
    fam_stop = [x for x in advice.get("families_stop", []) if x in fam_set]
    if len(fam_stop) > len(fam_set) // 2:
        fam_stop = fam_stop[: len(fam_set) // 2]
    wins = [w for w in advice.get("windows", []) if isinstance(w, int) and 2 <= w <= 504]
    return {
        "keep_fields": keep_f,
        "stop_fields": stop_f,
        "families_keep": fam_keep,
        "families_stop": fam_stop,
        "windows": wins[:3] if len(wins) == 3 else [],
        "focus": str(advice.get("focus", ""))[:200],
        "summary": str(advice.get("summary", ""))[:200],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def review_round(since: datetime):
    """复盘本轮，返回 advice dict（失败返回 None）。"""
    rows = load_rows_since(since)
    if len(rows) < 8:                      # 本轮几乎没跑出东西，跳过
        return None

    allowed_fields = list(dict.fromkeys(config.FIELD_ONLY))
    families = sorted({family(r[2]) for r in rows if r[2]})
    by_field = agg(rows, lambda r: r[1])
    by_fam = agg(rows, lambda r: family(r[2]))
    by_win = [x for x in agg(rows, lambda r: sorted(windows_in(r[0]))[0]
                             if windows_in(r[0]) else None) if x[0] is not None]

    # 失败信息：事实 / 弱证据 / 工程噪声，三通道分开取数
    check_counter = failure_stats.count_checks(
        r[6] if len(r) > 6 else None for r in rows)
    conn = failure_stats.connect_ro(DB)
    try:
        eng_stats = failure_stats.attempt_stats(
            conn, since_iso=since.strftime("%Y-%m-%dT%H:%M"))
    finally:
        conn.close()

    llm_input = build_input(rows, by_field, by_fam, by_win,
                            allowed_fields, families, check_counter, eng_stats,
                            fam_corr=family_corr_stats())

    raw = ask_llm(llm_input)
    advice = sanitize(raw, set(allowed_fields), families) if raw else None
    if advice:
        ADVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ADVICE_FILE.write_text(json.dumps(advice, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(f"  [round_review] 下一轮建议：{advice['focus'][:60]}")
    else:
        print("  [round_review] LLM 建议缺失或未过护栏，下一轮维持原配置")
    return advice
