# -*- coding: utf-8 -*-
"""
corr.py —— 算清「哪几条能同时交」

用途：平台的自相关门槛是 0.7（Sharpe 高出 10% 可豁免）。
      但那是针对「已提交池」的。你自己的候选之间如果本身就 0.99，
      交第二条必然被拒。

这个脚本算候选之间的 PnL 相关性，用贪心法挑出「互不冲突的最大集合」，
这就是你能一次性交上去的清单。

用法：
    python corr.py                 # 分析 pick.py 筛出的默认清单
    python corr.py A1 A2 A3 ...    # 指定 alpha_id
"""
import sys
import time
import numpy as np
import pandas as pd
from brain import BrainClient

DEFAULT: list = []      # 传 alpha_id 当参数；留空则直接提示用法

# 想给候选起人类可读的标签，在这里加：
# LABELS = {"<alpha_id>": "short description"}
LABELS = {}

CUTOFF = 0.70


def as_daily(s):
    """累计 PnL → 每日增量。平台返回的是累计曲线，直接对累计值求相关
    会得到全员 0.9+（都是上升趋势），必须差分后再算。"""
    if s is None or len(s) < 2:
        return None
    d = s.diff().dropna()
    return d if len(d) > 30 else None


def fetch_pnl(client, aid, tries=6):
    for i in range(tries):
        r = client.s.get(client.base + f"/alphas/{aid}/recordsets/pnl")
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 3)))
            continue
        if r.status_code != 200:
            return None
        try:
            rows = r.json().get("records") or []
        except ValueError:
            time.sleep(2)
            continue
        if not rows:
            return None
        return pd.Series({x[0]: x[1] for x in rows if len(x) >= 2},
                         dtype=float).sort_index()
    return None


def greedy_independent(ids, corr, cutoff):
    """按顺序贪心：保留与已选项相关性都低于 cutoff 的"""
    kept = []
    for a in ids:
        if all(abs(corr.loc[a, b]) < cutoff for b in kept):
            kept.append(a)
    return kept


def main():
    ids = sys.argv[1:] or DEFAULT
    if len(ids) < 2:
        print(__doc__.strip().split("用法：")[-1].strip())
        print("\n需要至少 2 个 alpha_id。例：python corr.py abc12345 def67890")
        return
    client = BrainClient()

    series = {}
    for aid in ids:
        s = fetch_pnl(client, aid)
        if s is not None:
            s = as_daily(s)
        if s is not None:
            series[aid] = s
            print(f"  {aid}  {len(s)} 天", flush=True)
        else:
            print(f"  {aid}  取不到，跳过", flush=True)
        time.sleep(1.2)

    if len(series) < 2:
        print("拿到的 PnL 不足 2 条，无法比较")
        return

    ks = list(series)
    m = pd.DataFrame(index=ks, columns=ks, dtype=float)
    for a in ks:
        for b in ks:
            x, y = series[a].align(series[b], join="inner")
            m.loc[a, b] = 1.0 if a == b else (
                np.corrcoef(x.values, y.values)[0, 1] if len(x) > 30 else np.nan)

    print("\n" + "=" * 76)
    print("两两相关性（|r| >= 0.7 就是冲突，不能同时交）")
    print("=" * 76)
    print("            " + "".join(f"{k[:6]:>8}" for k in ks))
    for a in ks:
        line = f"{a:11s} "
        for b in ks:
            v = m.loc[a, b]
            line += f"{'  ---  ' if a == b else f'{v:8.2f}'}"
        print(line)

    sets = greedy_independent(ks, m, CUTOFF)

    print("\n" + "=" * 76)
    print(f"可以一次性交的（互相 |r| 都 < {CUTOFF}）：{len(sets)} 条")
    print("=" * 76)
    for i, aid in enumerate(sets, 1):
        print(f"  {i}. {aid}   {LABELS.get(aid, '')}")
        conflicts = [b for b in ks if b != aid and abs(m.loc[aid, b]) >= CUTOFF]
        if conflicts:
            print(f"       与 {', '.join(conflicts)} 冲突，只交这一条")

    print(f"\n结论：这 {len(sets)} 条交完，你的提交数从 0 → {len(sets)}。")
    print("剩下的同类项不要交，交了也是被自相关拒。")

    out = m.round(3).to_csv()
    open("data/corr.csv", "w", encoding="utf-8-sig").write(out)
    print("完整矩阵已存 data/corr.csv")


if __name__ == "__main__":
    main()
