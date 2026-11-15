# -*- coding: utf-8 -*-
"""
单因子验证器。

一条因子过了线，不等于它是真的。要过三关：
  1. 窗格单调：参数连续变化时，指标平滑衰减，不是孤峰（孤峰 = 参数运气）
  2. 宇宙稳健：大盘股里还活着，说明不是小盘股效应
  3. 样本稳定：换个时间段，指标不崩

本脚本做前两关。第三关靠平台的年度分组数据，人工看。

用法：
  python verify.py sweep   --base "-ts_delta(close, {d})" --windows 2,3,4,5,10
  python verify.py universe --expr "-ts_delta(close, 3)" --list TOP3000,TOP1000,TOP500
  python verify.py both    --base "-ts_delta(close, {d})" --windows 3 --list TOP3000,TOP1000,TOP500
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import config
from brain import BrainClient, log, AuthError


def run(client, expr, universe, workers=3):
    st = dict(config.SIM_SETTINGS)
    st["universe"] = universe
    res = client.simulate(expr, settings=st)
    if "error" in res:
        return {"universe": universe, "expr": expr, "error": res["error"]}
    m = res["metrics"]
    return {"universe": universe, "expr": expr, **m}


def show(rows, title):
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)
    print(f"{'宇宙':<10} {'Sharpe':>7} {'Fitness':>8} {'换手':>6} {'年化':>7}  表达式")
    print("-" * 68)
    ok_any = False
    for r in rows:
        if r.get("error"):
            print(f"{r['universe']:<10} {'ERR':>7} {'':>8} {'':>6} {'':>7}  {r['expr']}  ({r['error'][:36]})")
            continue
        s, f, t = r.get("sharpe"), r.get("fitness"), r.get("turnover")
        flag = "  <= 过线" if (s and f and t and s >= 1.25 and f >= 1.0 and 0.01 <= t <= 0.70) else ""
        if flag:
            ok_any = True
        print(f"{r['universe']:<10} {s if s is None else round(s,2):>7} "
              f"{f if f is None else round(f,2):>8} "
              f"{t if t is None else round(t,2):>6} "
              f"{r.get('returns') if r.get('returns') is None else round(r['returns'],3):>7}  "
              f"{r['expr']}{flag}")
    print()
    return ok_any


def run_neut(client, expr, universe, neut):
    st = dict(config.SIM_SETTINGS)
    st["universe"] = universe
    st["neutralization"] = neut
    res = client.simulate(expr, settings=st)
    if "error" in res:
        return {"universe": universe, "neut": neut, "expr": expr, "error": res["error"]}
    return {"universe": universe, "neut": neut, "expr": expr, **res["metrics"]}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["sweep", "universe", "both", "neutral"])
    ap.add_argument("--base", default="-ts_delta(close, {d})", help="含 {d} 的模板")
    ap.add_argument("--expr", default="-ts_delta(close, 3)", help="固定表达式")
    ap.add_argument("--windows", default="2,3,4,5,10")
    ap.add_argument("--list", default="TOP3000,TOP1000,TOP500", help="宇宙列表")
    ap.add_argument("--neuts", default="SUBINDUSTRY,INDUSTRY,SECTOR,MARKET,NONE",
                    help="中性化方式列表")
    args = ap.parse_args()

    client = BrainClient()
    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    universes = [x.strip() for x in args.list.split(",") if x.strip()]

    if args.mode in ("sweep", "both"):
        exprs = [args.base.replace("{d}", str(w)) for w in windows]
        with ThreadPoolExecutor(max_workers=3) as ex:
            rows = list(ex.map(lambda e: run(client, e, "TOP3000"), exprs))
        import re
        def win_of(expr):
            m = re.findall(r",\s*(\d+)\s*\)", expr)
            return int(m[-1]) if m else 0
        rows.sort(key=lambda r: win_of(r["expr"]))
        print("\n【第一关】窗格单调性（宇宙固定 TOP3000）")
        print("期望：指标随窗口平滑衰减。若某个窗口突然冒尖、邻近窗口很烂，说明是参数运气。")
        show(rows, "窗格扫描")

    if args.mode in ("universe", "both"):
        with ThreadPoolExecutor(max_workers=3) as ex:
            rows = list(ex.map(lambda u: run(client, args.expr, u), universes))
        print("\n【第二关】宇宙稳健性")
        print("期望：TOP500（大盘股）里 Sharpe 仍 > 1.0。若只剩 TOP3000 有效，说明靠小票撑，含金量低。")
        show(rows, "宇宙扫描")


    if args.mode == "neutral":
        neuts = [x.strip().upper() for x in args.neuts.split(",") if x.strip()]
        combos = [(u, n) for u in universes for n in neuts]
        print(f"\n【第三关】中性化矩阵：{len(combos)} 组")
        print("目的：找出信号里有多少是「行业暴露」、多少是真正选股能力。")
        with ThreadPoolExecutor(max_workers=3) as ex:
            rows = list(ex.map(lambda c: run_neut(client, args.expr, c[0], c[1]), combos))
        print()
        print("=" * 74)
        print(f"表达式：{args.expr}")
        print("=" * 74)
        print(f"{'宇宙':<10} {'中性化':<14} {'Sharpe':>7} {'Fitness':>8} {'换手':>6} {'年化':>7}")
        print("-" * 74)
        for r in rows:
            if r.get("error"):
                print(f"{r['universe']:<10} {r['neut']:<14} {'ERR':>7}  {r['error'][:34]}")
                continue
            s, f, t = r.get("sharpe"), r.get("fitness"), r.get("turnover")
            flag = "  <= 过线" if (s and f and t and s >= 1.25 and f >= 1.0 and 0.01 <= t <= 0.70) else ""
            print(f"{r['universe']:<10} {r['neut']:<14} {round(s,2):>7} {round(f,2):>8} "
                  f"{round(t,2):>6} {round(r.get('returns') or 0,3):>7}{flag}")
        print()
        return


if __name__ == "__main__":
    try:
        main()
    except AuthError as e:
        log(f"认证失败：{e}")
        sys.exit(2)
