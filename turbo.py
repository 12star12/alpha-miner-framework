# -*- coding: utf-8 -*-
"""
turbo.py —— 定向攻坚：救活已经有了的高分因子

诊断思路：
  - Fitness = Sharpe × √(|Returns| / max(Turnover, 0.125))
    也就是说，Turnover 高会直接压死 Fitness —— 很多高分因子不是信号差，
    是换手太高。
  - decay（线性衰减平滑）是压换手最有效的手段之一，但很容易被固定成某个值
    然后一直没扫过。
  - 所以：拿 Sharpe 已经过线、只卡 Fitness 的那批，只扫 decay 和窗口。

结果写 data/turbo.db，不污染主库 alphas.db。

用法：
    python turbo.py            # 实跑
    python turbo.py --dry-run  # 只打印矩阵，不消耗额度
    python turbo.py --report   # 只看已有结果，不跑新的
"""
import sys
import time
import json
import sqlite3
import itertools
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from brain import BrainClient, Store, log

DB = config.DATA_DIR / "turbo.db"

# ---------------------------------------------------------------- 搜索空间
# 只覆盖一条线：短期反转。把要攻坚的表达式换成你自己的。
EXPRESSIONS = [
    ("reversal",        "-ts_delta(close, {w})"),
    ("ts-rank",         "-ts_rank(close, {w})"),
    ("reversal-vs-mean","-rank(close / ts_mean(close, {w}))"),
]

# 重点救这两条：Sharpe 最高但被换手拖死，扫大 decay 压换手
WINDOWS = [3, 5]
DECAYS = [20, 32, 48, 64, 96, 128, 192, 256]

# 目标区间：Turnover 12.5%–30%，Fitness 越高越好
TARGET_S_MIN = 1.25
TARGET_F_MIN = 1.00


def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            expr_hash TEXT PRIMARY KEY,
            expression TEXT,
            template TEXT,
            decay INTEGER,
            sharpe REAL,
            fitness REAL,
            turnover REAL,
            returns REAL,
            alpha_id TEXT,
            error TEXT,
            created_at TEXT,
            checks TEXT
        )
    """)
    # 老库补列，忽略重复
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN checks TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def key_of(expr, settings):
    return f"{expr}|{settings['decay']}"


def build_matrix():
    out = []
    for name, tpl in EXPRESSIONS:
        for w, d in itertools.product(WINDOWS, DECAYS):
            expr = tpl.format(w=w)
            st = dict(config.SIM_SETTINGS)
            st["decay"] = d
            out.append({
                "expression": expr,
                "template": f"{name} | w={w} | decay={d}",
                "decay": d,
                "settings": st,
                "key": key_of(expr, st),
            })
    return out


def report(conn):
    rows = conn.execute("""
        SELECT expression, decay, sharpe, fitness, turnover, returns, alpha_id, checks
        FROM runs WHERE error IS NULL AND sharpe IS NOT NULL
        ORDER BY fitness DESC
    """).fetchall()
    if not rows:
        print("还没有结果。先跑 python turbo.py")
        return

    print("\n" + "=" * 84)
    print("按 Fitness 排序    [ok]=三项达标且平台检查全过    [卡]=指标够了但有检查项未通过")
    print("=" * 84)
    print(f"{'S':>5} {'F':>5} {'Turnover':>9} {'decay':>6}  {'判':>4}  表达式")
    print("-" * 84)

    clean, blocked = [], []
    for expr, decay, s, f, t, r, aid, checks in rows:
        try:
            failed = json.loads(checks) if checks else []
        except (ValueError, TypeError):
            failed = []
        three_ok = (s >= TARGET_S_MIN and f >= TARGET_F_MIN and 0.125 <= t <= 0.30)
        if three_ok and not failed:
            tag = "[ok]"
            clean.append((expr, decay, s, f, t, aid))
        elif three_ok and failed:
            tag = "[卡]"
            blocked.append((expr, decay, s, f, t, aid, failed))
        else:
            tag = ""
        print(f"{s:5.2f} {f:5.2f} {t:9.2%} {decay:6d}  {tag:>4}  {expr[:40]}")

    print("\n" + "=" * 84)
    if clean:
        print(f"可提交 {len(clean)} 条（指标达标 + 平台检查全过）：\n")
        for expr, decay, s, f, t, aid in clean:
            print(f"  S={s:.2f}  F={f:.2f}  T={t:.2%}  decay={decay}")
            print(f"  {expr}")
            print(f"  alpha_id: {aid}\n")
    else:
        print("暂无「指标达标 + 检查全过」的组合。")

    if blocked:
        print("-" * 84)
        print(f"另有 {len(blocked)} 条指标够了、但被平台检查卡住（提交会失败）：\n")
        for expr, decay, s, f, t, aid, failed in blocked:
            print(f"  S={s:.2f} F={f:.2f} T={t:.2%} decay={decay}  {expr}")
            print(f"    未通过: {', '.join(failed)}")

    if not clean and not blocked:
        print("也没有「指标够但被卡」的，说明还差在 Sharpe 或 Fitness 上。")
        print("看上面 Fitness 最高的那条，离 1.25 / 1.0 / 30% 各差多少。")


def main():
    dry = "--dry-run" in sys.argv
    only_report = "--report" in sys.argv

    conn = init_db()
    if only_report:
        report(conn)
        return

    matrix = build_matrix()
    print(f"搜索空间：{len(matrix)} 个组合")
    print(f"  {len(EXPRESSIONS)} 个表达式 × {len(WINDOWS)} 个窗口 × {len(DECAYS)} 个 decay")
    print(f"  目标：Sharpe>={TARGET_S_MIN}, Fitness>={TARGET_F_MIN}, Turnover 12.5%-30%")
    print(f"  预估耗时：{len(matrix) * 45 / config.MAX_WORKERS / 60:.0f} 分钟\n")

    if dry:
        for m in matrix[:6]:
            print(f"  {m['expression']:40s} decay={m['decay']}")
        print(f"  ... 共 {len(matrix)} 条")
        return

    done = {r[0] for r in conn.execute(
        "SELECT expr_hash FROM runs WHERE sharpe IS NOT NULL")}
    todo = [m for m in matrix if m["key"] not in done]
    print(f"已完成 {len(matrix) - len(todo)}，待跑 {len(todo)}\n")

    if not todo:
        report(conn)
        return

    client = BrainClient()

    def work(m):
        try:
            res = client.simulate(m["expression"], m["settings"])
        except Exception as e:
            res = {"error": str(e)[:200]}

        met = res.get("metrics") or {}
        err = res.get("error")
        failed = res.get("failed_checks") or []
        row = (
            m["key"], m["expression"], m["template"], m["decay"],
            met.get("sharpe"), met.get("fitness"), met.get("turnover"),
            met.get("returns"), res.get("alpha_id"), err,
            datetime.now().isoformat(timespec="seconds"),
            json.dumps(failed, ensure_ascii=False),
        )
        if err:
            log(f"  失败 {m['expression']} decay={m['decay']} :: {str(err)[:120]}")
        else:
            tag = f"  检查未过:{','.join(failed)}" if failed else ""
            log(f"  S={met.get('sharpe')} F={met.get('fitness')} "
                f"T={met.get('turnover')} | {m['expression']} decay={m['decay']}{tag}")
        with sqlite3.connect(DB) as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        time.sleep(config.SLEEP_BETWEEN)
        return row

    n = 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        futs = {ex.submit(work, m): m for m in todo}
        for fut in as_completed(futs):
            n += 1
            try:
                r = fut.result()
                print(f"  [{n}/{len(todo)}] F={r[5]} T={r[6]} | {r[1][:36]} d={r[3]}")
            except Exception as e:
                print(f"  [{n}/{len(todo)}] 异常 {e}")

    report(conn)


if __name__ == "__main__":
    main()
