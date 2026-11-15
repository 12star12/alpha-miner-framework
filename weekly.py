# -*- coding: utf-8 -*-
"""
每周研究报告。汇总这一周机器干了什么、哪些留着、哪些该死。

用法：
  python weekly.py            # 生成最近 7 天
  python weekly.py --days 3
  python weekly.py --print    # 只打印不写文件
"""
import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

import config
from brain import Store

FIELDS = ("close", "open", "high", "low", "vwap", "volume", "returns", "cap", "adv20")


def skeleton(e: str) -> str:
    s = re.sub(r"\b(" + "|".join(FIELDS) + r")\b", "F", e)
    return re.sub(r"\s+", "", re.sub(r"\b\d+(\.\d+)?\b", "D", s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    store = Store()
    q = store.conn.execute

    rows = q(
        "SELECT expression, sharpe, fitness, turnover, returns, created_at FROM alphas "
        "WHERE created_at >= ? AND sharpe IS NOT NULL ORDER BY fitness DESC", (since,)
    ).fetchall()
    att = q("SELECT COUNT(*), SUM(ok) FROM attempts WHERE created_at >= ?", (since,)).fetchone()

    flt = config.FILTER
    def ok(r):
        s, f, t = r[1], r[2], r[3]
        return None not in (s, f, t) and s >= flt["sharpe_min"] and f >= flt["fitness_min"] \
            and flt["turnover_min"] <= t <= flt["turnover_max"]

    passed = [r for r in rows if ok(r)]
    sk = Counter(skeleton(r[0]) for r in rows)
    skp = Counter(skeleton(r[0]) for r in passed)

    L = []
    L.append(f"# 本周研究报告 · {datetime.now():%Y-%m-%d}")
    L.append("")
    L.append(f"统计区间：{since} 至今（{args.days} 天）")
    L.append("")
    L.append("## 一句话结论")
    L.append("")
    rate = len(passed) / len(rows) * 100 if rows else 0
    if not rows:
        L.append("本周没有新回测。检查后台任务是否还在跑。")
    elif not passed:
        L.append(f"跑了 {len(rows)} 条，一条都没过线。这是常态，不是失败——要看的是骨架有没有变多。")
    else:
        L.append(f"跑了 {len(rows)} 条，过线 {len(passed)} 条（{rate:.1f}%），"
                 f"但去重后只有 {len(skp)} 个独立骨架。")
    L.append("")

    L.append("## 数字")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("| --- | --- |")
    L.append(f"| 回测次数 | {att[0] or 0} |")
    L.append(f"| 回测成功 | {att[1] or 0} |")
    L.append(f"| 入库因子 | {len(rows)} |")
    L.append(f"| 过线 | {len(passed)} |")
    L.append(f"| 过线率 | {rate:.1f}% |")
    L.append(f"| 不同骨架 | {len(sk)} |")
    div = len(sk) / len(rows) * 100 if rows else 0
    L.append(f"| 骨架多样性 | {div:.1f}% |")
    L.append("")
    L.append("> 骨架多样性低于 20%，说明在同一个想法上反复跑，该换模板了。")
    L.append("")

    if passed:
        L.append("## 过线清单")
        L.append("")
        L.append("| Sharpe | Fitness | 换手 | 表达式 | 骨架 |")
        L.append("| --- | --- | --- | --- | --- |")
        for r in passed[:10]:
            L.append(f"| {r[1]:.2f} | {r[2]:.2f} | {r[3]:.2f} | `{r[0][:70]}` | `{skeleton(r[0])[:34]}` |")
        L.append("")

    L.append("## 最有希望的三条（不论过没过线）")
    L.append("")
    for r in rows[:3]:
        L.append(f"- `{r[0]}` — Sharpe {r[1]:.2f} / Fitness {r[2]:.2f} / 换手 {r[3]:.2f}")
    L.append("")

    critic_path = config.DATA_DIR / "critic.md"
    if critic_path.exists():
        txt = critic_path.read_text(encoding="utf-8")
        verdicts = re.findall(r"\*\*判定：(\w+)\*\*", txt)
        if verdicts:
            c = Counter(verdicts)
            L.append("## 审查结果")
            L.append("")
            for k, v in c.items():
                L.append(f"- {k}：{v} 条")
            L.append("")

    L.append("## 本周该做的三件事")
    L.append("")
    if div < 20 and rows:
        L.append("1. 骨架多样性太低。往 `gen.py` 的 `TEMPLATES` 里加结构不同的新模板，别再调窗口。")
    else:
        L.append("1. 骨架多样性尚可，继续跑，但每轮结束看一眼 `critic.md`。")
    if passed:
        L.append("2. 对过线的那几条跑 `python critic.py`，先过风格检验再谈提交。")
    else:
        L.append("2. 没有过线因子。检查是否字段选错，或 decay 参数过重把信号磨平了。")
    L.append("3. 记一句本周学到的机制（谁被迫交易 / 哪个科目对不上），下轮用它生成新模板。")
    L.append("")

    text = "\n".join(L)
    if not args.print:
        out = config.DATA_DIR / "weekly.md"
        out.write_text(text, encoding="utf-8")
        print(f"已写入 {out}")
    print(text)


if __name__ == "__main__":
    main()
