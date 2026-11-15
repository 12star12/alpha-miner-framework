# -*- coding: utf-8 -*-
"""
主流程。

用法：
  python miner.py test                          # 先验一条，确认链路通
  python miner.py datasets                      # 看有哪些数据集
  python miner.py fields --dataset fundamental6 # 拉字段并缓存
  python miner.py run --mode rules --limit 60   # L0 规则批量跑
  python miner.py run --mode llm  --limit 40    # L1 LLM 生成后跑
  python miner.py run --mode rules --dry-run    # 只生成+校验，不消耗模拟额度
  python miner.py stats                         # 看库里的账
"""
import argparse
import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import gen
from brain import BrainClient, Store, log, AuthError

_local = threading.local()
_store_lock = threading.Lock()


def get_client() -> BrainClient:
    """每个线程一个会话：requests.Session 不是线程安全的，别共用。"""
    c = getattr(_local, "client", None)
    if c is None:
        c = BrainClient()
        _local.client = c
    return c


# ---------------------------------------------------------------- 字段
def load_fields(dataset: str = None) -> list:
    if config.FIELDS_CACHE.exists():
        data = json.loads(config.FIELDS_CACHE.read_text(encoding="utf-8"))
        if not dataset or data.get("dataset") == dataset:
            return data["fields"]
    if not dataset:
        log("没有字段缓存，请先跑：python miner.py fields --dataset <name>")
        return []
    client = get_client()
    log(f"拉取 {dataset} 字段…")
    fields = client.list_fields(dataset)
    config.FIELDS_CACHE.write_text(
        json.dumps({"dataset": dataset, "fields": fields}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log(f"缓存 {len(fields)} 个 MATRIX 字段 → {config.FIELDS_CACHE}")
    return fields


# ---------------------------------------------------------------- 单条任务
def run_one(store: Store, task: dict) -> dict:
    expr = task["expression"]
    if store.seen(expr):
        return {"skipped": True}

    t0 = time.time()
    try:
        client = get_client()
        res = client.simulate(expr)
    except AuthError as e:
        return {"fatal": str(e)}
    except Exception as e:
        res = {"error": f"未捕获异常: {e}"}

    dt = time.time() - t0

    if "error" in res:
        store.save_attempt(expr, task.get("template"), task.get("field"), False, res["error"], dt)
        return {"ok": False, "error": res["error"], "seconds": dt}

    store.save_alpha(res, task.get("template"), task.get("field"), source=task.get("source", "rules"))
    store.save_attempt(expr, task.get("template"), task.get("field"), True, None, dt)
    m = res["metrics"]

    # ---- 提交前自相关预检：只对硬门槛全过的候选查（这类本来就少，查得起）。
    # 平台 cutoff 0.7：自相关过线的候选即使指标再好，交了也会被拒。
    sc = None
    if ((m.get("sharpe") or 0) >= config.FILTER["sharpe_min"]
            and (m.get("fitness") or 0) >= config.FILTER["fitness_min"]
            and not res.get("failed_checks")):
        try:
            sc = client.self_correlation(res["alpha_id"])
            store.save_selfcorr(res["alpha_id"], sc)
            mx = sc.get("max_abs")
            who = ""
            if sc.get("matched_id"):
                who = f"（撞已提交 {sc['matched_id']} S={sc.get('matched_sharpe')}）"
            if mx is None:
                log(f"  自相关查询失败（{sc.get('error')}），可稍后用 python selfcorr.py 补查 | S={m['sharpe']} | {expr[:50]}")
            elif mx >= config.FILTER["self_corr_max"]:
                log(f"  ⚠ 自相关 {mx:.3f}{who} ≥ {config.FILTER['self_corr_max']}，指标过线但不可提交 | S={m['sharpe']} | {expr[:50]}")
            else:
                log(f"  ★ 自相关 {mx:.3f} ✓ 可提交候选 | S={m['sharpe']} Fit={m['fitness']} | {expr[:50]}")
        except Exception as e:
            log(f"  自相关查询异常（不影响入库结果）：{e}")

    return {
        "ok": True, "seconds": dt, "metrics": m,
        "expression": expr, "failed": res.get("failed_checks", []),
        "self_corr": sc,
    }


# ---------------------------------------------------------------- 主循环
def cmd_run(args):
    fields_meta = load_fields(args.dataset)
    if not fields_meta:
        return
    allowed = {f["id"] for f in fields_meta}
    desc = {f["id"]: f.get("description", "") for f in fields_meta}

    # 挑字段：先剔掉工具字段（复权因子之类），再让有经济含义的核心字段排前面
    blacklist = config.FIELD_BLACKLIST
    usable = [f for f in fields_meta if f["id"] not in blacklist and f.get("id")]
    usable.sort(key=lambda x: (x.get("coverage") or 0), reverse=True)
    ids = [f["id"] for f in usable]
    priority = [x for x in config.FIELD_PRIORITY if x in ids]
    rest = [x for x in ids if x not in priority]
    pick = (priority + rest)[:args.n_fields]
    log(f"可用字段 {len(usable)} 个，使用 {len(pick)} 个：{', '.join(pick)}")

    tasks = []
    if args.mode in ("rules", "both"):
        tasks += [dict(t, source="rules") for t in gen.gen_rules(pick)]

    if args.mode in ("llm", "both"):
        log("调用免费 LLM 生成候选…")
        cand = gen.gen_llm(pick, n=args.llm_n, field_desc=desc)
        log(f"LLM 返回 {len(cand)} 条")
        tasks += [dict(t, source="llm") for t in cand]

    # 校验 + 去重
    valid, seen_expr = [], set()
    for t in tasks:
        ok, why = gen.validate(t["expression"], allowed)
        if not ok:
            continue
        if t["expression"] in seen_expr:
            continue
        seen_expr.add(t["expression"])
        valid.append(t)

    log(f"生成 {len(tasks)} 条 → 校验通过 {len(valid)} 条")

    if args.dry_run:
        for t in valid[:20]:
            print("  ", t["expression"], "  ←", t.get("template"))
        log("dry-run 结束，未消耗模拟额度")
        return

    store = Store()
    todo = valid[:args.limit]

    # ---- 护栏 1：每日模拟上限。超过就停，不解释。
    today = datetime.now().strftime("%Y-%m-%d")
    ran_today = store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE created_at LIKE ?", (today + "%",)
    ).fetchone()[0]
    if ran_today + len(todo) > config.DAILY_SIM_CAP:
        log(f"今日已回测 {ran_today} 条，本轮 {len(todo)} 条会超过自律上限 "
            f"{config.DAILY_SIM_CAP}。今天到此为止——堆量换不来质量，还容易踩平台红线。")
        return

    # ---- 护栏 2：有效生成率过低，说明生成器在产垃圾，先停下修生成器
    if len(tasks) and len(valid) / len(tasks) < config.MIN_VALID_RATE:
        log(f"有效生成率仅 {len(valid)/len(tasks)*100:.0f}%（低于 {config.MIN_VALID_RATE*100:.0f}%），"
            f"大概率是表达式语法或字段名有问题。先 --dry-run 检查，别拿额度试错。")
        return

    log(f"开始回测 {len(todo)} 条，并发 {config.MAX_WORKERS}（今日累计 {ran_today} 条）")

    done = okc = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        futs = {ex.submit(run_one, store, t): t for t in todo}
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                log(f"任务异常：{e}")
                continue
            if r.get("fatal"):
                log(f"致命错误：{r['fatal']}")
                for f in futs:
                    f.cancel()
                break
            if r.get("ok"):
                okc += 1
                m = r["metrics"]
                log(f"[{done}/{len(todo)}] Sharpe={m['sharpe']} Fit={m['fitness']} "
                    f"TO={m['turnover']} | {r['expression'][:70]}")
            elif not r.get("skipped"):
                if done % 20 == 0:
                    log(f"[{done}/{len(todo)}] 失败率偏高：{r.get('error', '')[:80]}")
            time.sleep(config.SLEEP_BETWEEN / config.MAX_WORKERS)

    cost = time.time() - t0
    s = store.stats()
    log(f"本轮完成：成功 {okc} / {len(todo)}，耗时 {cost/60:.1f} 分钟")
    log(f"库内累计：{s['alphas']} 条 alpha，最佳 Sharpe {s['best_sharpe']}")
    log("下一步：python analyze.py")


# ---------------------------------------------------------------- 其它
def cmd_test(args):
    """先跑一条已知大概率能过的表达式，验证：认证 / 模拟 / 取结果 / 落库 四步。"""
    client = get_client()
    expr = args.expr or "rank(ts_delta(close, 5))"
    log(f"试跑：{expr}")
    res = client.simulate(expr)
    if "error" in res:
        log(f"失败：{res['error']}")
        log("排查顺序：凭证 → Persona 验证 → 表达式语法 → 网络代理")
        return
    store = Store()
    store.save_alpha(res, template="smoke-test", field="close", source="test")
    m = res["metrics"]
    log(f"成功 alpha_id={res['alpha_id']}")
    log(f"Sharpe={m['sharpe']} Fitness={m['fitness']} Turnover={m['turnover']} "
        f"Returns={m['returns']} Drawdown={m['drawdown']}")
    if res.get("failed_checks"):
        log(f"未通过的检查项：{res['failed_checks']}")
    log("链路通了。下一步：python miner.py fields --dataset <数据集名>")


def cmd_datasets(args):
    client = get_client()
    ds = client.list_datasets()
    log(f"共 {len(ds)} 个数据集")
    rows = sorted(ds, key=lambda x: -(x.get("alphaCount") or 0))
    for d in rows[:40]:
        print(f"  {d.get('id'):<24} {str(d.get('name'))[:34]:<36} "
              f"fields={d.get('fieldCount')} alphas={d.get('alphaCount')}")


def cmd_fields(args):
    load_fields(args.dataset)


def cmd_stats(args):
    store = Store()
    s = store.stats()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    cur = store.conn.execute(
        "SELECT expression, sharpe, fitness, turnover FROM alphas "
        "WHERE sharpe IS NOT NULL ORDER BY sharpe DESC LIMIT 10"
    )
    print("\nTop 10 by Sharpe:")
    for row in cur:
        print(f"  {row[1]:>6.2f}  fit={row[2]:>5.2f}  to={row[3]:>5.2f}  {row[0][:80]}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="BRAIN alpha miner")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test", help="冒烟测试").add_argument("--expr", default=None)
    sub.add_parser("datasets", help="列出数据集")
    sub.add_parser("stats", help="查看库内统计")

    pf = sub.add_parser("fields", help="拉取字段缓存")
    pf.add_argument("--dataset", required=True)

    pr = sub.add_parser("run", help="批量回测")
    pr.add_argument("--mode", choices=["rules", "llm", "both"], default="rules")
    pr.add_argument("--dataset", default=None)
    pr.add_argument("--n-fields", type=int, default=12, help="使用前 N 个高覆盖字段")
    pr.add_argument("--llm-n", type=int, default=40, help="LLM 生成条数")
    pr.add_argument("--limit", type=int, default=60, help="本轮最多回测条数")
    pr.add_argument("--dry-run", action="store_true")

    args = p.parse_args()
    try:
        {
            "test": cmd_test, "datasets": cmd_datasets, "fields": cmd_fields,
            "run": cmd_run, "stats": cmd_stats,
        }[args.cmd](args)
    except AuthError as e:
        log(f"认证失败：{e}")
        sys.exit(2)
    except KeyboardInterrupt:
        log("手动中断，已保存的进度不会丢，重跑会自动跳过")


if __name__ == "__main__":
    main()
