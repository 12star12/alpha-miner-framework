# -*- coding: utf-8 -*-
"""
剪枝与报告。

这一步的输出不是"更多因子"，是一份能拿去讲的东西：
跑多少、过多少、为什么不过、留下来的彼此是不是同一个东西。

用法：
  python analyze.py
  python analyze.py --loose      # 用更宽的门槛看分布
"""
import argparse
import json
import re
from collections import Counter
from datetime import datetime

import config
from brain import Store

FIELD_TOK = "F"
NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

IGNORE_IDENTS = set()  # 骨架化时保留的记号


def skeleton(expr: str, field_ids: set) -> str:
    """把字段名抹成 F、数字抹成 D，留下结构骨架。
    骨架重复 = 同质化，这是 AI 挖因子最容易踩的坑。"""
    s = expr
    for f in sorted(field_ids, key=len, reverse=True):
        s = re.sub(rf"\b{re.escape(f)}\b", FIELD_TOK, s)
    s = NUM_RE.sub("D", s)
    return re.sub(r"\s+", "", s)


def main_pipeline_ops(expr: str) -> str:
    """取最外层/最有个性的算子，用来给因子分组。"""
    ops = [t for t in ID_RE.findall(expr) if t.startswith(("ts_", "group_"))]
    if not ops:
        ops = [t for t in ID_RE.findall(expr) if t in ("rank", "zscore", "scale")]
    return " + ".join(sorted(set(ops))[:2]) or "plain"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loose", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    flt = config.FILTER_LOOSE if args.loose else config.FILTER
    store = Store()
    rows = store.conn.execute(
        "SELECT alpha_id, expression, template, field, sharpe, fitness, turnover,"
        " returns, drawdown, margin, failed_checks, source FROM alphas"
    ).fetchall()

    attempts = store.conn.execute(
        "SELECT ok, error FROM attempts"
    ).fetchall()

    total_attempts = len(attempts)
    ok_attempts = sum(1 for a in attempts if a[0] == 1)
    err_counter = Counter(
        (a[1] or "")[:60] for a in attempts if a[0] == 0 and a[1]
    )

    def passes(r):
        s, f, t = r[4], r[5], r[6]
        if s is None or f is None or t is None:
            return False
        return (s >= flt["sharpe_min"] and f >= flt["fitness_min"]
                and flt["turnover_min"] <= t <= flt["turnover_max"])

    good = [r for r in rows if passes(r)]
    good.sort(key=lambda r: (-(r[4] or 0), -(r[5] or 0)))

    field_ids = {r[3] for r in rows if r[3]}
    skel = Counter(skeleton(r[1], field_ids) for r in rows)
    skel_good = Counter(skeleton(r[1], field_ids) for r in good)
    ops = Counter(main_pipeline_ops(r[1]) for r in good)

    lines = []
    w = lines.append
    w(f"# Alpha 挖掘报告 · {datetime.now():%Y-%m-%d %H:%M}")
    w("")
    w("## 1. 漏斗")
    w("")
    w("| 环节 | 数量 |")
    w("| --- | --- |")
    w(f"| 实际回测（attempts） | {total_attempts} |")
    w(f"| 回测成功 | {ok_attempts} |")
    w(f"| 回测成功率 | {ok_attempts / total_attempts * 100 if total_attempts else 0:.1f}% |")
    w(f"| 入库 alpha | {len(rows)} |")
    w(f"| 通过门槛 | {len(good)} |")
    w(f"| 过线率 | {len(good) / len(rows) * 100 if rows else 0:.1f}% |")
    w("")
    w(f"门槛：Sharpe ≥ {flt['sharpe_min']}，Fitness ≥ {flt['fitness_min']}，"
      f"Turnover ∈ [{flt['turnover_min']}, {flt['turnover_max']}]")
    w("")

    w("## 2. 过线清单")
    w("")
    if good:
        w("| # | Sharpe | Fitness | Turnover | 表达式 | 假设 / 模板 |")
        w("| --- | --- | --- | --- | --- | --- |")
        for i, r in enumerate(good[:args.top], 1):
            expr = (r[1] or "").replace("|", "/")[:90]
            w(f"| {i} | {r[4]:.2f} | {r[5]:.2f} | {r[6]:.2f} | `{expr}` | {(r[2] or '')[:40]} |")
    else:
        w("没有因子过线。先看第 4 节的失败归因，通常是门槛太严或字段不适配。")
    w("")

    w("## 3. 同质化诊断")
    w("")
    w("骨架 = 抹掉字段名与数字之后的结构。骨架重复说明你（或 AI）在原地打转。")
    w("")
    dup = sum(c - 1 for c in skel_good.values() if c > 1)
    w(f"- 过线因子 {len(good)} 个，去重骨架 {len(skel_good)} 个，"
      f"重复骨架贡献了 {dup} 个冗余\n")
    if skel_good:
        w("| 骨架 | 命中的过线数 |")
        w("| --- | --- |")
        for k, c in skel_good.most_common(8):
            w(f"| `{k[:70]}` | {c} |")
        w("")
    if ops:
        w("按算子族分布（过线因子）：")
        for k, c in ops.most_common(8):
            w(f"- {k}：{c}")
        w("")

    w("## 4. 失败归因")
    w("")
    if err_counter:
        for msg, c in err_counter.most_common(8):
            w(f"- `{msg}` × {c}")
    else:
        w("无失败记录。")
    w("")

    w("## 5. 下一步")
    w("")
    if len(good) >= 3:
        w("1. 对过线因子跑 `correlations/self`，把自相关 > 0.7 的砍掉——**这一步才是真剪枝**。")
        w("2. 挑 3 个不同骨架的，逐个写清经济逻辑，写不出来就丢掉。")
        w("3. 把窗口参数 ±50% 重跑一遍，看 Sharpe 掉不掉——掉一半以上的大概率是过拟合。")
    else:
        w("1. 换字段：当前这批字段可能本来就没信号，先跑 `python miner.py datasets` 换数据集。")
        w("2. 降门槛到 --loose 看分布，判断是「这批字段没信号」还是「门槛太严」。")
        w("3. 检查失败归因里是不是语法错误占大头——那说明生成器在瞎写，先修 validate。")
    w("")

    config.REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告已写入 {config.REPORT_PATH}")


if __name__ == "__main__":
    main()
