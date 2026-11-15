# -*- coding: utf-8 -*-
"""
blend.py —— 用「负相关对冲」把弱信号顶过线

思路：
  单条信号差一口气（Sharpe 1.0~1.2），与其继续在同一个族里换窗口，
  不如找一条跟它**负相关**的信号叠起来 —— 组合的波动被对冲掉，Sharpe 上升。

  等波动（各自 zscore）相加后：
      S_combo ≈ (S1 + S2) / sqrt(2 + 2*rho)
  rho < 0 时，组合 Sharpe 高于两者中的任意一条；rho = 0 时也有 √2 的增益。
  副作用通常是好的：Turnover 被摊薄，Fitness 跟着上来。

  配对对象用 corr.py 先量出来（对日增量求相关，不要拿累计 PnL 算）。

用法：
    python blend.py --dry-run      # 只列计划
    python blend.py --limit 8      # 实跑（每条 1 次模拟额度）
    python blend.py --report       # 看已有结果

**用之前先把下面 ANCHOR / PARTNERS 换成你自己的表达式与实测 rho。**
"""
import json
import sys
import time
from datetime import datetime

import config
from brain import BrainClient, Store, log, settings_key
from sweep import simulate_patient

S_MIN = config.FILTER["sharpe_min"]
F_MIN = config.FILTER["fitness_min"]
CUTOFF = config.FILTER["self_corr_max"]

# 锚：已经过了全部硬检查的那一条（填你自己的）
ANCHOR = "rank(-ts_delta(close, 5))"

# 伙伴：(标签, 表达式, 与锚的日增量相关)。rho 用 corr.py 实测后填，负值才有对冲收益。
PARTNERS = [
    ("example-A", "ts_rank(eps, 20)",  0.0),
    ("example-B", "ts_zscore(assets, 15)", 0.0),
]


def variants():
    """(标签, 表达式, 参数覆盖) —— 先等权，再给最优的两档倾斜。"""
    out = []
    za = f"zscore({ANCHOR})"
    for name, expr, rho in PARTNERS:
        zb = f"zscore({expr})"
        out.append((f"等权+{name}", f"{za} + {zb}", {}))
    return out


def today_used(store):
    d = datetime.now().strftime("%Y-%m-%d")
    return store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE created_at LIKE ?", (d + "%",)
    ).fetchone()[0]


def report(store):
    rows = store.conn.execute(
        "SELECT a.expression, a.sharpe, a.fitness, a.turnover, a.failed_checks,"
        " a.alpha_id, c.max_abs FROM alphas a"
        " LEFT JOIN selfcorr c ON a.alpha_id = c.alpha_id"
        " WHERE a.source = 'blend' ORDER BY a.sharpe DESC"
    ).fetchall()
    if not rows:
        log("还没有 blend 结果，先跑 python blend.py --limit 8")
        return
    print(f"{'S':>5} {'F':>5} {'TO':>7} {'corr':>6}  表达式 / 卡点")
    print("-" * 96)
    for expr, s, f, to, fc, aid, corr in rows:
        fails = json.loads(fc or "[]")
        ok = (s or 0) >= S_MIN and (f or 0) >= F_MIN and not fails and (
            corr is not None and corr < CUTOFF)
        cs = "  -  " if corr is None else f"{corr:6.3f}"
        tag = "★可交" if ok else (",".join(fails)[:24] if fails else "待查自相关")
        print(f"{s:5.2f} {f:5.2f} {to:6.1%} {cs}  {str(expr)[:56]:56s} {tag}")
    print("-" * 96)


def main():
    dry = "--dry-run" in sys.argv
    if "--report" in sys.argv:
        report(Store())
        return
    limit = 8
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    store = Store()
    plan = []
    for label, expr, over in variants():
        st = dict(config.SIM_SETTINGS)
        st.update(over)
        key = settings_key(st)
        if store.seen_real(expr, key):
            log(f"  跳过（跑过）：{label}")
            continue
        plan.append({"label": label, "expression": expr, "settings": st, "key": key})

    if not plan:
        log("全部跑过了，用 python blend.py --report 看结果")
        return
    if dry:
        for p in plan:
            log(f"  {p['label']:14s} {p['expression']}")
        log(f"dry-run 结束。实跑：python blend.py --limit {limit}")
        return

    used = today_used(store)
    log(f"今日已用 {used}/{config.DAILY_SIM_CAP}，本次上限 {limit} 条（组合实验）")

    client = BrainClient()
    done = 0
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
            store.save_attempt(expr, f"blend:{p['label']}", None, False,
                               res["error"], dt, settings_key=p["key"])
            log(f"  [{done}/{limit}] 失败：{res['error'][:70]}")
            continue

        store.save_attempt(expr, f"blend:{p['label']}", None, True, None, dt,
                           settings_key=p["key"])
        store.save_alpha(res, f"blend:{p['label']}", None, source="blend",
                         settings_key=p["key"])
        m = res["metrics"]
        fails = res.get("failed_checks") or []
        log(f"  [{done}/{limit}] S={m['sharpe']} F={m['fitness']} TO={m['turnover']}"
            f" | {p['label']:14s} | {expr[:54]}")

        if (m.get("sharpe") or 0) >= S_MIN and (m.get("fitness") or 0) >= F_MIN and not fails:
            sc = client.self_correlation(res["alpha_id"])
            store.save_selfcorr(res["alpha_id"], sc)
            mx = sc.get("max_abs")
            if mx is None:
                log(f"      ↑ 指标全过但自相关未算完，稍后 python selfcorr.py {res['alpha_id']}")
            elif mx < CUTOFF:
                log(f"      ★★ 可提交：{res['alpha_id']} S={m['sharpe']} 自相关={mx:.3f}")
            else:
                log(f"      ↑ 指标全过但自相关 {mx:.3f}（撞 {sc.get('matched_id')}）")


if __name__ == "__main__":
    main()
