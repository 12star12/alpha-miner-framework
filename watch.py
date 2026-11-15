# -*- coding: utf-8 -*-
"""
watch.py —— 实时监控面板

用法：
    python watch.py            # 实时刷新（Ctrl+C 退出）
    python watch.py --once     # 只看一眼，打完就退出

数据来源：data/auto.log（本轮日志）+ data/alphas.db（历史统计）
不依赖 daemon 是否运行，随时可以打开。
"""
import os
import re
import sys
import time
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import config

LOG = config.DATA_DIR / "auto.log"
DB = config.DB_PATH

SHARPE_MIN = 1.25
FIT_MIN = 1.0

ROW = re.compile(
    r"Sharpe=(-?[\d.]+)\s+Fit=(-?[\d.]+|None)\s+TO=(-?[\d.]+|None)\s*\|\s*(.+)$"
)
ROUND_START = re.compile(r"—— 第 (\d+) 轮开始，今日已用 (\d+)/(\d+)，本轮额度 (\d+)")
ROUND_END = re.compile(r"—— 第 (\d+) 轮结束，成功 (\d+) 条")
STARTED = re.compile(r"无人值守启动 · 每日上限 (\d+) 条 · 并发 (\d+)")


def python_alive():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=8,
        ).stdout
        return "python.exe" in out.lower()
    except Exception:
        return False


def read_log():
    if not LOG.exists():
        return [], []
    try:
        lines = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return [], []
    rows, rounds = [], []
    for ln in lines:
        m = ROW.search(ln)
        if m:
            try:
                s = float(m.group(1))
                f = None if m.group(2) == "None" else float(m.group(2))
                t = None if m.group(3) == "None" else float(m.group(3))
                ts = re.match(r"\[(\d\d:\d\d:\d\d)\]", ln)
                rows.append({"time": ts.group(1) if ts else "", "s": s, "f": f,
                             "t": t, "expr": m.group(4).strip()})
            except ValueError:
                pass
        r = ROUND_START.search(ln)
        if r:
            rounds.append({"no": int(r.group(1)), "used": int(r.group(2)),
                           "cap": int(r.group(3)), "batch": int(r.group(4))})
        e = ROUND_END.search(ln)
        if e:
            rounds.append({"end": int(e.group(1)), "ok": int(e.group(2))})
    return rows, rounds


def db_today():
    """今天新增的 alpha 质量分布"""
    if not DB.exists():
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cur = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN sharpe>=? THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN fitness>=? THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN sharpe>=? AND fitness>=? THEN 1 ELSE 0 END) "
            "FROM alphas WHERE created_at LIKE ?",
            (SHARPE_MIN, FIT_MIN, SHARPE_MIN, FIT_MIN, today + "%"),
        )
        r = cur.fetchone()
        conn.close()
        return {"n": r[0] or 0, "s": r[1] or 0, "f": r[2] or 0, "both": r[3] or 0}
    except Exception:
        return None


def db_recent(n=8):
    """直接读数据库最近 n 条（不受 stdout 缓冲影响）"""
    if not DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cur = conn.execute(
            "SELECT expression, sharpe, fitness, turnover, created_at "
            "FROM alphas WHERE sharpe IS NOT NULL ORDER BY rowid DESC LIMIT ?", (n,)
        )
        out = []
        for expr, s, f, t, ts in cur.fetchall():
            out.append({
                "time": (ts or "")[11:19],
                "s": s, "f": f, "t": t, "expr": expr or "",
            })
        conn.close()
        return out
    except Exception:
        return []


def bar(used, cap, width=28):
    cap = max(cap, 1)
    k = int(used / cap * width)
    k = max(0, min(width, k))
    return "[" + "#" * k + "." * (width - k) + f"] {used}/{cap}"


def render(alive=None):
    rows, rounds = read_log()
    if alive is None:
        alive = python_alive()
    today = db_today()
    recent = db_recent(8)

    started = [r for r in rounds if "used" in r]
    ends = [r for r in rounds if "end" in r]
    cur_round = started[-1]["no"] if started else 0
    used = started[-1]["used"] if started else 0
    cap = started[-1]["cap"] if started else 1200
    batch = started[-1]["batch"] if started else 40
    finished_rounds = len(ends)

    W = 66
    print("=" * W)
    print(f" WQ Daemon 实时监控            {datetime.now().strftime('%H:%M:%S')}")
    print("=" * W)

    state = "[RUNNING] PID alive" if alive else "[STOPPED] no python.exe"
    print(f" 进程状态    {state}")
    print(f" 当前轮次    第 {cur_round} 轮   (已完成 {finished_rounds} 轮, 每轮 {batch} 条)")
    print(f" 今日额度    {bar(used, cap)}")
    print("-" * W)

    if today:
        n = today["n"]
        rate = (today["both"] / n * 100) if n else 0
        print(f" 今日入库    {n} 条")
        print(f"   Sharpe>=1.25   {today['s']:4d} 条")
        print(f"   Fitness>=1.0   {today['f']:4d} 条")
        print(f"   两项都过       {today['both']:4d} 条   ({rate:.1f}%)  <- 可提交候选")
    else:
        print(" 今日入库    数据库读取失败")
    print("-" * W)

    if recent:
        print(" 最近 8 条结果（按入库时间倒序）")
        for r in recent:
            ft = "None" if r["f"] is None else f"{r['f']:.2f}"
            tt = "  -  " if r["t"] is None else f"{r['t']*100:5.1f}%"
            mark = "*" if (r["f"] is not None and r["f"] >= FIT_MIN
                           and r["s"] >= SHARPE_MIN) else " "
            print(f"  {mark} {r['time']}  S={r['s']:5.2f}  F={ft:>5}  T={tt}  {r['expr'][:30]}")
    else:
        print(" 还没有结果。首轮 40 条约需 20 分钟，请稍候。")
    print("=" * W)
    print(" 提示：* 标记的表示 Sharpe 和 Fitness 都过线")
    print("       Ctrl+C 退出面板（不影响 daemon 继续跑）")


def main():
    once = "--once" in sys.argv
    if once:
        render()
        return
    try:
        while True:
            if sys.stdout.isatty():
                os.system("cls")
            render()
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n面板已退出。daemon 仍在后台运行。")


if __name__ == "__main__":
    main()
