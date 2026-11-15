# -*- coding: utf-8 -*-
"""
sweep.py —— 近线冲刺：把「低相关但差一口气」的种子顶过门槛

典型困局：
  高分的那批因子里，一大半自相关也高（撞已提交池）；自相关低的那些，
  Sharpe 又卡在门槛以下一点点。继续在全库撒网没用，该做的是定点修复。

做法：只对种子做几种「已知有效的过线修复」，不做盲目大搜索：
  1. rank 包层        —— 治权重集中（CONCENTRATED_WEIGHT）
  2. group_rank 包层  —— 行业中性 + 权重均匀
  3. winsorize 削极值 —— 不改排序，只砍尾部极端权重
  4. decay 调整       —— 短周期信号不该被 20 日平滑
  5. truncation=4%    —— 压单票权重，治集中度

同一类修复对不同信号的效果完全不同（rank 往往压 Sharpe，winsorize 常能保住），
所以只测这几个就够了，不要在这个层面做大规模穷举。

用法：
    python sweep.py --dry-run     # 只列出要跑什么，不烧额度
    python sweep.py --limit 20    # 实跑，最多 20 次模拟
    python sweep.py --report      # 只看库里已跑出来的结果
"""
import json
import sqlite3
import sys
import time
from datetime import datetime

import config
from brain import BrainClient, Store, log, settings_key

CUTOFF_CORR = config.FILTER["self_corr_max"]
S_MIN = config.FILTER["sharpe_min"]
F_MIN = config.FILTER["fitness_min"]
SEED_S_MIN = 1.0           # 种子的 Sharpe 下限
SEED_S_MAX = 1.25          # 种子已是过线的就不算种子了

DEGENERATE_HINTS = ("-employee/employee", "-cash, 12) - ts_rank(cash, 12)")


def load_seeds(store: Store) -> list:
    """低相关（<0.7）但 Sharpe 差一口气的种子。"""
    cur = store.conn.execute(
        "SELECT a.alpha_id, a.sharpe, a.fitness, a.expression, c.max_abs "
        "FROM alphas a JOIN selfcorr c ON a.alpha_id = c.alpha_id "
        "WHERE c.max_abs < ? AND a.sharpe >= ? AND a.sharpe < ? "
        "ORDER BY a.sharpe DESC", (CUTOFF_CORR, SEED_S_MIN, SEED_S_MAX),
    )
    out = []
    for aid, s, f, expr, corr in cur.fetchall():
        if not expr or any(h in expr for h in DEGENERATE_HINTS):
            continue
        out.append({"alpha_id": aid, "sharpe": s, "fitness": f,
                    "expression": expr, "corr": corr})
    return out


def variants(seed: dict) -> list:
    """过线修复：(标签, 表达式, 参数覆盖)"""
    e = seed["expression"]
    return [
        ("rank包层", f"rank({e})", {}),
        ("组内排序", f"group_rank({e}, subindustry)", {}),
        ("winsorize", f"winsorize({e}, std=4)", {}),
        ("zscore", f"zscore({e})", {}),
        ("组内去均值", f"group_neutralize({e}, subindustry)", {}),
        ("decay=0", e, {"decay": 0}),
        ("decay=4", e, {"decay": 4}),
        ("截断4%", e, {"truncation": 0.04}),
        ("截断4%+winsor", f"winsorize({e}, std=4)", {"truncation": 0.04}),
    ]


def simulate_patient(client, expr: str, settings: dict, tries: int = 4, wait: int = 40) -> dict:
    """平台被 daemon 占着并发时会 429。限流不算失败，等一会儿重来，别把变体废掉。"""
    res = {}
    for i in range(tries):
        res = client.simulate(expr, settings)
        err = res.get("error") or ""
        if not any(p in err for p in ("限流", "网络", "超时")):
            return res
        if i < tries - 1:
            log(f"      平台繁忙（{err[:24]}），{wait}s 后重试 {i + 2}/{tries}")
            time.sleep(wait)
    return res


def today_used(store: Store) -> int:
    d = datetime.now().strftime("%Y-%m-%d")
    return store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE created_at LIKE ?", (d + "%",)
    ).fetchone()[0]


def report(store: Store) -> None:
    rows = store.conn.execute(
        "SELECT a.expression, a.sharpe, a.fitness, a.turnover, a.failed_checks,"
        " a.alpha_id, c.max_abs "
        "FROM alphas a LEFT JOIN selfcorr c ON a.alpha_id = c.alpha_id "
        "WHERE a.source = 'sweep' AND a.sharpe IS NOT NULL "
        "ORDER BY a.sharpe DESC LIMIT 40"
    ).fetchall()
    if not rows:
        log("还没有 sweep 结果，先跑 python sweep.py --limit 20")
        return
    print(f"{'S':>5} {'F':>5} {'TO':>7} {'corr':>6}  表达式 / 卡点")
    print("-" * 86)
    good = 0
    for expr, s, f, to, fc, aid, corr in rows:
        fails = json.loads(fc or "[]")
        ok = s >= S_MIN and f >= F_MIN and not fails and (corr is None or corr < CUTOFF_CORR)
        if ok:
            good += 1
        cs = "  -  " if corr is None else f"{corr:6.3f}"
        tag = "★可交" if ok else (",".join(fails)[:26] if fails else ("待查自相关" if corr is None else ""))
        print(f"{s:5.2f} {f:5.2f} {to:6.1%} {cs}  {str(expr)[:44]:44s} {tag}")
    print("-" * 86)
    print(f"过全部门槛（含自相关）的：{good} 条")


def main():
    dry = "--dry-run" in sys.argv
    only_report = "--report" in sys.argv
    limit = 24
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    store = Store()
    if only_report:
        report(store)
        return

    seeds = load_seeds(store)
    if not seeds:
        log("没有低相关种子（需要先跑 python selfcorr.py --all 建立自相关数据）")
        return

    plan = []
    for sd in seeds:
        for label, expr, over in variants(sd):
            st = dict(config.SIM_SETTINGS)
            st.update(over)
            key = settings_key(st)
            if store.seen_real(expr, key):
                continue
            plan.append({"seed": sd, "label": label, "expression": expr,
                         "settings": st, "key": key})

    log(f"种子 {len(seeds)} 条 → 待跑变体 {len(plan)} 条（已跑过的自动跳过）")
    if dry:
        for p in plan[:30]:
            tag = " [改参数]" if p["settings"] != config.SIM_SETTINGS else ""
            log(f"  {p['seed']['alpha_id']} S={p['seed']['sharpe']:.2f} → {p['label']:10s} {p['expression'][:60]}{tag}")
        log(f"dry-run 结束。实跑命令：python sweep.py --limit {limit}（每次 1 条模拟额度）")
        return

    used = today_used(store)
    log(f"今日已用 {used}/{config.DAILY_SIM_CAP}，本次上限 {limit} 条")

    client = BrainClient()
    done = 0
    hits = []
    for p in plan:
        if done >= limit:
            break
        if today_used(store) >= config.DAILY_SIM_CAP:
            log("触到每日上限，停手")
            break
        expr, st = p["expression"], p["settings"]
        t0 = time.time()
        res = simulate_patient(client, expr, st)
        dt = time.time() - t0
        done += 1
        if "error" in res:
            store.save_attempt(expr, f"sweep:{p['label']}", None, False,
                               res["error"], dt, settings_key=p["key"])
            log(f"  [{done}/{limit}] 失败：{res['error'][:70]} | {expr[:50]}")
            continue

        store.save_attempt(expr, f"sweep:{p['label']}", None, True, None, dt,
                           settings_key=p["key"])
        store.save_alpha(res, f"sweep:{p['label']}", None, source="sweep",
                         settings_key=p["key"])
        m = res["metrics"]
        fails = res.get("failed_checks") or []
        line = (f"  [{done}/{limit}] S={m['sharpe']} F={m['fitness']} TO={m['turnover']}"
                f" | {p['label']:9s} | {expr[:56]}")
        log(line)

        if ((m.get("sharpe") or 0) >= S_MIN and (m.get("fitness") or 0) >= F_MIN and not fails):
            sc = client.self_correlation(res["alpha_id"])
            store.save_selfcorr(res["alpha_id"], sc)
            mx = sc.get("max_abs")
            if mx is None:
                log(f"      ↑ 指标全过但自相关查询失败（{sc.get('error')}），稍后用 selfcorr.py 补查")
            elif mx < CUTOFF_CORR:
                hits.append((res["alpha_id"], m["sharpe"], mx, expr))
                log(f"      ★★ 可提交：{res['alpha_id']} S={m['sharpe']} 自相关={mx:.3f} | {expr[:50]}")
            else:
                log(f"      ↑ 指标全过但自相关 {mx:.3f} ≥ {CUTOFF_CORR}（撞 {sc.get('matched_id')}）")

    log(f"\n本轮跑 {done} 条，可提交 {len(hits)} 条")
    for aid, s, c, e in hits:
        log(f"  ★ {aid}  S={s}  corr={c:.3f}  {e}")
    if not hits:
        log("没有新增可提交。下一步看 report 里哪些只差 Fitness，或考虑换 region（USA 池已占满）")


if __name__ == "__main__":
    main()
