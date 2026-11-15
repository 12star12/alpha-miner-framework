# -*- coding: utf-8 -*-
"""
selfcorr.py —— 提交前自相关预检（平台 cutoff 0.7）

作用：在 pick.py 筛出的「平台检查全过」候选之上，再补一道自相关条件：
      与自己已提交池的最大自相关 |corr| 必须 < 0.7，否则交了也会被拒
      （除非 Sharpe 比池里那条高出 10% 以上，豁免由平台判，本地只做预检）。

只读操作，不消耗模拟额度。

用法：
    python selfcorr.py              # 自动查库：硬门槛全过的候选逐条预检
    python selfcorr.py --all        # 诊断：库里所有 Sharpe>=1.0 的都查一遍（找哪族撞车）
    python selfcorr.py ID1 ID2 ...  # 指定 alpha_id 预检
"""
import sys

import config
from brain import BrainClient, Store, log

CUTOFF = config.FILTER["self_corr_max"]
DIAG_MIN_SHARPE = 1.0


def candidates_from_db(store: Store) -> list:
    """库里硬门槛全过（Sharpe/Fitness 达标、无 FAIL 检查项）的候选。"""
    cur = store.conn.execute(
        "SELECT alpha_id, sharpe, fitness, expression FROM alphas "
        "WHERE sharpe >= ? AND fitness >= ? "
        "AND (failed_checks = '[]' OR failed_checks IS NULL OR failed_checks = '') "
        "ORDER BY sharpe DESC",
        (config.FILTER["sharpe_min"], config.FILTER["fitness_min"]),
    )
    return cur.fetchall()


def diag_from_db(store: Store) -> list:
    """诊断用：所有 Sharpe>=1.0 的，不管 Fitness 和检查项。"""
    cur = store.conn.execute(
        "SELECT alpha_id, sharpe, fitness, expression FROM alphas "
        "WHERE sharpe >= ? ORDER BY sharpe DESC", (DIAG_MIN_SHARPE,)
    )
    return cur.fetchall()


def family_of(expr: str) -> str:
    """粗分信号族：用表达式里的核心字段 + 结构特征，够用来做归因。"""
    e = (expr or "").lower()
    if "/assets" in e or "/liabilities" in e or "liabilities" in e or "/equity" in e:
        return "基本面-杠杆/资本结构"
    if "capxv" in e or "/cap" in e or "drlt" in e:
        return "基本面-估值(现金流/市值)"
    if "eps" in e:
        return "基本面-盈利(EPS)"
    if "cashflow" in e:
        return "基本面-现金流质量"
    if "sales" in e:
        return "基本面-收入"
    if "cogs" in e:
        return "基本面-成本毛利"
    if "ebit" in e:
        return "基本面-经营利润"
    if "open" in e and "close" in e:
        return "价量-日内反转"
    if "close" in e or "vwap" in e or "high" in e or "low" in e:
        return "价量-价格动量/反转"
    if "volume" in e:
        return "价量-成交量"
    return "其它"


def check_one(client: BrainClient, store: Store, aid: str, note: str = "") -> bool:
    """查一条并落旁表。返回 True = 过线可提交。"""
    sc = client.self_correlation(aid)
    store.save_selfcorr(aid, sc)
    mx = sc.get("max_abs")
    who = ""
    if sc.get("matched_id"):
        who = f"（撞已提交 {sc['matched_id']} S={sc.get('matched_sharpe')}）"
    if mx is None:
        log(f"  {aid}  查询失败（{sc.get('error')}）{note}")
        return False
    if mx >= CUTOFF:
        hint = ""
        ms = sc.get("matched_sharpe")
        if isinstance(ms, (int, float)):
            # 平台规则：候选 Sharpe 比撞上的那条高出 10% 可豁免
            hint = f"，豁免需本条 S ≥ {ms * 1.1:.2f}"
        log(f"  {aid}  ⚠ 自相关 {mx:.3f}{who} ≥ {CUTOFF}，不可提交{hint}{note}")
        return False
    log(f"  {aid}  ✓ 自相关 {mx:.3f} < {CUTOFF}，可提交{note}")
    return True


def main():
    ids = [x for x in sys.argv[1:] if not x.startswith("--")]
    diag = "--all" in sys.argv
    client = BrainClient()
    store = Store()

    if ids:
        targets = [(i, None, None, "") for i in ids]
    else:
        rows = diag_from_db(store) if diag else candidates_from_db(store)
        if not rows:
            log(f"库里没有 Sharpe≥{config.FILTER['sharpe_min']} 且 Fitness≥{config.FILTER['fitness_min']}"
                f" 且无 FAIL 的候选，无事可做")
            return
        targets = [(r[0], r[1], r[2], f" S={r[1]:.2f}") for r in rows]
        log(f"{'诊断模式：' if diag else ''}共 {len(targets)} 条候选，逐条预检自相关（只读，不烧额度）")

    ok = bad = err = 0
    for aid, _s, _f, note in targets:
        try:
            if check_one(client, store, aid, note):
                ok += 1
            else:
                sc = store.conn.execute(
                    "SELECT max_abs, error FROM selfcorr WHERE alpha_id=?", (aid,)
                ).fetchone()
                if sc and sc[0] is None:
                    err += 1
                else:
                    bad += 1
        except Exception as e:
            log(f"  {aid}  异常：{e}")
            err += 1

    log(f"\n结论：可提交 {ok} 条，自相关过线 {bad} 条，查询失败 {err} 条")
    if diag and not ids:
        render_family_report(store)
    elif targets and not ids:
        log("查询失败的可以稍后再跑一次 python selfcorr.py 补查（平台算完就有数了）")
    log("注意：候选之间的互相关是另一道关，跑 python corr.py 看两两矩阵")


def render_family_report(store: Store) -> None:
    """按信号族聚合自相关，回答「往哪挖不收撞」。"""
    rows = store.conn.execute(
        "SELECT a.alpha_id, a.sharpe, a.fitness, a.expression, c.max_abs "
        "FROM alphas a JOIN selfcorr c ON a.alpha_id = c.alpha_id "
        "WHERE c.max_abs IS NOT NULL ORDER BY a.sharpe DESC"
    ).fetchall()
    if not rows:
        return
    buckets = {}
    for aid, s, f, expr, corr in rows:
        fam = family_of(expr)
        b = buckets.setdefault(fam, {"n": 0, "ok": 0, "corrs": [], "best_ok": None})
        b["n"] += 1
        b["corrs"].append(corr)
        if corr < CUTOFF:
            b["ok"] += 1
            if b["best_ok"] is None or (s or 0) > b["best_ok"][1]:
                b["best_ok"] = (aid, s, f, expr)

    log("\n" + "=" * 72)
    log(f"信号族归因（自相关 < {CUTOFF} 才算可用）")
    log("=" * 72)
    log(f"{'信号族':<26}{'条数':>5}{'不撞':>5}{'最低相关':>9}{'最高相关':>9}")
    for fam, b in sorted(buckets.items(), key=lambda kv: (-kv[1]["ok"], min(kv[1]["corrs"]))):
        log(f"{fam:<26}{b['n']:>5}{b['ok']:>5}{min(b['corrs']):>9.3f}{max(b['corrs']):>9.3f}")
    log("-" * 72)
    for fam, b in sorted(buckets.items(), key=lambda kv: (-kv[1]["ok"], min(kv[1]["corrs"]))):
        if b["best_ok"]:
            aid, s, f, expr = b["best_ok"]
            log(f"  {fam} 最优可用：{aid} S={s:.2f} F={f:.2f}  {str(expr)[:70]}")
    log("=" * 72)
    log("读法：'不撞'列 > 0 的族才有提交价值；全是 0 的族先别加额度，换族或换 region")


if __name__ == "__main__":
    main()
