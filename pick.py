# -*- coding: utf-8 -*-
"""
pick.py —— 从你已有的未提交 alpha 里，挑出「平台检查全过、可以直接提交」的

背景：账号里 510 个 UNSUBMITTED、0 个 SUBMITTED。
顾问申请要累计提交 20 个，所以现在最缺的不是新因子，是把能过的交出去。

用法：
    python pick.py            # 全量筛查，输出可提交清单
    python pick.py --limit 200  # 只查前 200 个（快一点）
"""
import sys
import time
import json
from brain import BrainClient

PAGE = 100      # 每页条数
SLEEP = 1.0     # 页间停顿，避免 429


def fetch_all(client, limit=None):
    """按 Sharpe 降序拉取。平台限制只能看前 1000 个，
    按 -is.sharpe 排序可保证前 1000 就是最强的那批。"""
    out, offset, total = [], 0, None
    while True:
        r = client.s.get(
            client.base + "/users/self/alphas",
            params={"status": "UNSUBMITTED", "limit": PAGE,
                    "offset": offset, "order": "-is.sharpe"},
        )
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 3)))
            continue
        if r.status_code != 200:
            print(f"  拉取失败 HTTP {r.status_code}: {r.text[:150]}")
            break
        d = r.json()
        total = d.get("count", 0)
        batch = d.get("results", [])
        out.extend(batch)
        offset += len(batch)
        print(f"  已拉取 {len(out)}/{total}", flush=True)
        if not batch or len(out) >= total:
            break
        if limit and len(out) >= limit:
            break
        time.sleep(SLEEP)
    return out, total


def analyze(a):
    isd = a.get("is") or {}
    checks = isd.get("checks") or []
    fails = [c for c in checks if str(c.get("result")).upper() == "FAIL"]
    warns = [c for c in checks if str(c.get("result")).upper() == "WARNING"]
    return {
        "id": a.get("id"),
        "expr": (a.get("regular") or {}).get("code", "")
                if isinstance(a.get("regular"), dict) else str(a.get("regular")),
        "sharpe": isd.get("sharpe"),
        "fitness": isd.get("fitness"),
        "turnover": isd.get("turnover"),
        "returns": isd.get("returns"),
        "fails": [c.get("name") for c in fails],
        "warns": [c.get("name") for c in warns],
        "all_pass": len(fails) == 0,
        "created": a.get("dateCreated", "")[:10],
    }


def main():
    limit = None
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        if i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    client = BrainClient()
    print("拉取未提交 alpha…")
    alphas, total = fetch_all(client, limit)
    print(f"\n共拿到 {len(alphas)} 条（平台总数 {total}）\n")

    rows = [analyze(a) for a in alphas]
    ok = [r for r in rows if r["all_pass"]]
    ok.sort(key=lambda x: (x["sharpe"] or 0), reverse=True)

    # FAIL 原因分布
    from collections import Counter
    cnt = Counter()
    for r in rows:
        for f in r["fails"]:
            cnt[f] += 1

    print("=" * 80)
    print(f"检查全过（可以直接提交）：{len(ok)} 条 / 共 {len(rows)} 条")
    print("=" * 80)
    if ok:
        print(f"{'S':>5} {'F':>5} {'Turnover':>9}  {'创建':>10}  alpha_id    表达式")
        print("-" * 80)
        for r in ok:
            print(f"{r['sharpe']:5.2f} {r['fitness']:5.2f} {r['turnover']:9.2%}  "
                  f"{r['created']:>10}  {r['id']:12s} {r['expr'][:34]}")
    else:
        print("一条都没有。往下看 FAIL 分布，那里告诉你是哪里卡住的。")

    print("\n" + "=" * 80)
    print("没过的都卡在哪（按出现次数）")
    print("=" * 80)
    for name, n in cnt.most_common():
        print(f"  {name:28s} {n:4d} 条")

    print("\n" + "-" * 80)
    print("说明：")
    print("  LOW_SHARPE / LOW_FITNESS  → 指标不够，只能换参数或换逻辑")
    print("  CONCENTRATED_WEIGHT       → 收益过度集中（就是 4/18/2023 那天的问题）")
    print("  LOW_SUB_UNIVERSE_SHARPE   → 子域表现差，样本外大概率崩")
    print("  MATCHES_COMPETITION       → 和竞赛已有因子撞车")
    print()
    print(f"距顾问申请还差 {max(0, 20 - 0)} 个提交（当前 submitted = 0）")
    if ok:
        print(f"这 {len(ok)} 条全交掉的话，进度 {min(len(ok),20)}/20")


if __name__ == "__main__":
    main()
