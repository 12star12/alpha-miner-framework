# -*- coding: utf-8 -*-
"""
无人值守模式。开机运行，自己排轮次，跑到刹车线自己停。

它做的三件事：
  1. 每轮自动挑一批「没跑过的」候选去回测
  2. 候选跑完了就换窗口组合，再跑完就调 LLM 生成新的
  3. 触到任何一条刹车线就停下来，写日志

刹车线（都在 config.py 里，随时能改）：
  DAILY_SIM_CAP   每天最多跑多少条，到了就睡到第二天
  ROUND_GAP_MIN   每轮之间歇多久
  MAX_DRY_ROUNDS  连续几轮没产出就收工

用法：
  python daemon.py            # 无限轮
  python daemon.py --rounds 3 # 只跑 3 轮，试水用
"""
import argparse
import json
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import gen
import miner
import operators
import round_review
from brain import BrainClient, Store, log, AuthError

# 窗口组合轮换：每轮换一组，池子立刻厚三倍
WINDOW_SETS = [
    [5, 20, 60],
    [10, 40, 90],
    [3, 15, 120],
    [20, 60, 120],
]

ROUND_GAP_MIN = 3        # 轮间隔（分钟）。原 20 分钟太保守，纯等待浪费时间
MAX_DRY_ROUNDS = 3       # 连续几轮零产出就收工
ROUND_BATCH = 40        # 每轮跑多少条
FAMILY_QUOTA = 6        # 每轮每个模板家族最多几条：防止 40 条全是一个信号的窗口变体

ADVICE_FILE = config.DATA_DIR / "advice.json"
REVIEW_DIR = config.DATA_DIR / "reviews"
ADVICE = None            # 最近一次 LLM 建议（日级启动加载 / 轮级每轮更新）
NEXT_WINDOWS = None      # 轮级建议的窗口组，只对下一轮生效一次


def load_advice():
    """读取昨天 LLM 的复盘建议。只认今天/昨天的日期，过期的丢弃。"""
    global ADVICE
    if not ADVICE_FILE.exists():
        return
    try:
        a = json.loads(ADVICE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if a.get("date") not in (today, _yesterday()):
        return
    ADVICE = a
    # 建议窗口组插到轮换最前面
    if a.get("windows"):
        try:
            w = sorted(int(x) for x in a["windows"])
            if len(w) == 3:
                existing = [sorted(int(x) for x in s) for s in WINDOW_SETS]
                if w in existing:
                    log(f"建议窗口组 {w} 已在轮换表中，跳过插入")
                else:
                    WINDOW_SETS.insert(0, w)
        except Exception:
            pass
    log(f"已应用昨日 AI 建议：优先 {a.get('keep_fields') or '默认'}，"
        f"停跑 {a.get('stop_fields') or '无'}，窗口 {a.get('windows') or '默认轮换'}")


def _yesterday() -> str:
    from datetime import timedelta
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def fields_with_advice(pick: list) -> list:
    """按建议重排字段顺序：keep 在前，stop 剔除（至少留 3 个）。"""
    if not ADVICE:
        return pick
    stop = set(ADVICE.get("stop_fields") or [])
    keep = [x for x in (ADVICE.get("keep_fields") or []) if x in pick]
    rest = [x for x in pick if x not in stop and x not in keep]
    out = keep + rest
    if len(out) < 3:                       # 护栏：别把字段全停光
        return pick
    return out


def maybe_daily_review():
    """每天自动复盘一次：有 ≥15 条当日结果且今日还没写过日报时触发。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if (REVIEW_DIR / f"{today}.md").exists():
        return
    try:
        store_probe = Store()
        n = store_probe.conn.execute(
            "SELECT COUNT(*) FROM alphas WHERE created_at LIKE ? AND sharpe IS NOT NULL",
            (today + "%",),
        ).fetchone()[0]
        store_probe.conn.close()
    except Exception as e:
        log(f"复盘预检失败：{e}")
        return
    if n < 15:
        return
    log("触发每日 AI 复盘…")
    try:
        subprocess.run([sys.executable, "-u", "daily_review.py", "--date", today],
                       cwd=str(config.ROOT), timeout=300)
    except Exception as e:
        log(f"每日复盘失败：{e}")


def today_count(store: Store) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    return store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE created_at LIKE ?", (today + "%",)
    ).fetchone()[0]


def order_by_family(tasks: list) -> list:
    """按轮级建议重排模板家族：keep 在前，stop 剔除（至少留 3 个家族）。"""
    if not ADVICE:
        return tasks
    fam_keep = set(ADVICE.get("families_keep") or [])
    fam_stop = set(ADVICE.get("families_stop") or [])
    if not fam_keep and not fam_stop:
        return tasks

    def fam(t):
        return (t.get("template") or "").split(" | ")[0].strip()

    kept = [t for t in tasks if fam(t) not in fam_stop]
    if len({fam(t) for t in kept}) < 3:        # 护栏：家族不能全停光
        return tasks
    front = [t for t in kept if fam(t) in fam_keep]
    rest = [t for t in kept if fam(t) not in fam_keep]
    return front + rest


def quota_by_family(tasks: list, quota: int = FAMILY_QUOTA) -> list:
    """同一家族的窗口变体只留前 quota 条。

    原因：同一家族换窗口的变体，PnL 曲线几乎重合 —— 等于拿 5 倍额度买同一个信号，
    而且必然撞已提交池的自相关。把额度摊到更多家族，才可能挖出正交的信息。
    """
    counts, out = {}, []
    for t in tasks:
        fam = (t.get("template") or "").split(" | ")[0].strip()
        if counts.get(fam, 0) >= quota:
            continue
        counts[fam] = counts.get(fam, 0) + 1
        out.append(t)
    return out


def build_candidates(fields_meta: list, round_no: int) -> list:
    """一轮的候选：换一组窗口 + 轮换字段顺序。带 LLM key 时追加 AI 生成。"""
    blacklist = config.FIELD_BLACKLIST
    ids = [f["id"] for f in fields_meta if f["id"] and f["id"] not in blacklist]
    priority = [x for x in config.FIELD_PRIORITY if x in ids]
    rest = [x for x in ids if x not in priority]

    # FIELD_ONLY 非空时只跑白名单字段，其余全部跳过
    only = getattr(config, "FIELD_ONLY", None)
    if only:
        pick = [x for x in only if x in ids]
        if not pick:
            log(f"FIELD_ONLY={only} 与可用字段无交集，回退到全字段")
            pick = priority + rest
    else:
        pick = priority + rest
    pick = fields_with_advice(pick)

    global NEXT_WINDOWS
    if NEXT_WINDOWS:
        windows = NEXT_WINDOWS            # 上轮建议的窗口组，只用一次
        NEXT_WINDOWS = None
    else:
        windows = WINDOW_SETS[round_no % len(WINDOW_SETS)]
    tasks = [dict(t, source="rules") for t in gen.gen_rules(pick, windows=windows)]
    tasks = order_by_family(tasks)
    tasks = quota_by_family(tasks)

    if config.LLM_API_KEY:
        desc = {f["id"]: f.get("description", "") for f in fields_meta}
        # LLM 不受 FIELD_ONLY 约束：规则生成走已验证的窄口径，
        # AI 生成走宽口径，且把基本面字段排在前面（跨类别才是它的价值所在）
        pv_set = gen.CONST_OK
        llm_pick = ([x for x in ids if x not in pv_set]
                    + [x for x in ids if x in pv_set])
        hint = (ADVICE or {}).get("focus")
        try:
            cand = gen.gen_llm(llm_pick[:36], n=30, field_desc=desc, hint=hint)
        except Exception as e:
            log(f"LLM 生成失败，本轮只用规则模板：{e}")
            cand = []
        tasks += [dict(t, source="llm") for t in cand]

    return tasks


def run_round(store: Store, fields_meta: list, round_no: int) -> int:
    global ADVICE, NEXT_WINDOWS
    t_start = datetime.now()
    allowed = {f["id"] for f in fields_meta}
    raw = build_candidates(fields_meta, round_no)

    fresh, seen = [], set()
    for t in raw:
        expr = t["expression"]
        if expr in seen:
            continue
        ok, _ = gen.validate(expr, allowed)
        if not ok:
            continue
        seen.add(expr)
        if store.seen(expr):
            continue
        fresh.append(t)

    if not fresh:
        return 0

    todo = fresh[:ROUND_BATCH]
    okc = 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        futs = {ex.submit(miner.run_one, store, t): t for t in todo}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception as e:
                log(f"  任务异常：{e}")
                continue
            if r.get("fatal"):
                raise AuthError(r["fatal"])
            if r.get("ok") is False and r.get("error"):
                for op in operators.extract_denied_ops(r["error"]):
                    gen.deny_operator(op, r["error"])
                    log(f"  算子权限：{op} 被平台拒绝，已加入黑名单（之后不再生成）")
            if r.get("ok"):
                okc += 1
                m = r["metrics"]
                log(f"  Sharpe={m['sharpe']} Fit={m['fitness']} TO={m['turnover']} | {r['expression'][:60]}")

    # 每轮复盘：AI 根据本轮数据调整下一轮的选样逻辑
    try:
        adv = round_review.review_round(t_start)
        if adv:
            ADVICE = adv
            NEXT_WINDOWS = adv.get("windows") or None
            log(f"轮次复盘：{adv.get('summary', '')[:50]}")
            log(f"下轮重点：{adv.get('focus', '')[:60]}")
    except Exception as e:
        log(f"轮次复盘失败：{e}")
    return okc


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=0, help="0 = 无限")
    args = ap.parse_args()

    log("=" * 60)
    log(f"无人值守启动 · 每日上限 {config.DAILY_SIM_CAP} 条 · 并发 {config.MAX_WORKERS}")
    log(f"每轮 {ROUND_BATCH} 条，轮间隔 {ROUND_GAP_MIN} 分钟，连续 {MAX_DRY_ROUNDS} 轮空转即收工")
    log(f"LLM：{'已配置 ' + config.LLM_MODEL if config.LLM_API_KEY else '未配置（只用规则模板，候选池有限）'}")

    fields_meta = miner.load_fields()
    if not fields_meta:
        log("没有字段缓存，先跑：python miner.py fields --dataset pv1")
        return

    load_advice()
    log(f"窗口轮换表共 {len(WINDOW_SETS)} 组，按轮次循环：{WINDOW_SETS}")

    store = Store()
    dry = 0
    n = 0
    auth_fail = 0
    while True:
        n += 1
        if args.rounds and n > args.rounds:
            log("轮次跑满，收工")
            break

        used = today_count(store)
        if used >= config.DAILY_SIM_CAP:
            maybe_daily_review()          # 睡前把今天的账盘一遍
            wait = 60 * 60
            log(f"今日已跑 {used} 条，到达上限 {config.DAILY_SIM_CAP}，睡 1 小时后复查")
            time.sleep(wait)
            continue

        budget = min(ROUND_BATCH, config.DAILY_SIM_CAP - used)
        log(f"—— 第 {n} 轮开始，今日已用 {used}/{config.DAILY_SIM_CAP}，本轮额度 {budget}")

        try:
            got = run_round(store, fields_meta, n)
        except AuthError as e:
            auth_fail += 1
            if auth_fail >= 3:
                log(f"连续 {auth_fail} 次认证/网络失败，收工：{str(e)[:120]}")
                break
            wait_min = 5 * auth_fail
            log(f"认证/网络异常（第 {auth_fail}/3 次，{wait_min} 分钟后重试）：{str(e)[:100]}")
            time.sleep(wait_min * 60)
            continue

        auth_fail = 0

        log(f"—— 第 {n} 轮结束，成功 {got} 条")
        maybe_daily_review()
        if got == 0:
            dry += 1
            if dry >= MAX_DRY_ROUNDS:
                maybe_daily_review()
                log(f"连续 {MAX_DRY_ROUNDS} 轮没有新候选，候选池已耗尽，收工。")
                log("想继续跑：补数据集的字段，或配 LLM_API_KEY 让它自己造。")
                break
            time.sleep(60 * 2)
            continue

        dry = 0
        time.sleep(ROUND_GAP_MIN * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("手动停止。已跑的结果都在 data/alphas.db 里，重跑会自动跳过。")
