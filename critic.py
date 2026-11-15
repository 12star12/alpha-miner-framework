# -*- coding: utf-8 -*-
"""
因子批评者。挂在流水线尾巴上的否决关卡。

它的核心观点：一条因子"过线"没有任何意义，要问的是
  —— 它的收益从哪来？是真的选股，还是押对了一个风格？

不依赖 LLM 也能跑：风格检测靠「做实验」而不是靠「问模型」。
  · 市值中性化后腰斩  → 小盘风格变装
  · 行业中性化后腰斩  → 行业轮动
  · 换成 TOP500 就死  → 容量不足

配了 LLM_API_KEY 之后，额外追问两件事：
  · 这个信号的经济机制是什么（说人话，不许提指标名）
  · 如果它明天失效，最可能怎么死

用法：
  python critic.py                  # 审最近一轮全部过线因子
  python critic.py --style fund     # 用基本面提问模板（需 LLM key）
  python critic.py --limit 5
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

import config
from brain import BrainClient, Store, log, AuthError

# ---------------------------------------------------------------- 提问模板
QUESTIONS = {
    "pv": """你是量化研究的老手，任务是**否决**下面这个因子。你的默认立场是"它是假的"，
除非它能扛住你的追问。

请回答三个问题，每个问题不超过两句话：
1. 这个信号背后的经济机制是什么？必须说人话——说清楚"谁在什么情况下被迫买/卖"，
   不许出现 "alpha"、"IC"、"因子" 这类词。
2. 它最像哪个已知风格？（小盘 / 波动 / 动量 / 反转 / 流动性 / 行业轮动）
3. 如果它一年后失效，最可能的原因是什么？

最后给一行结论：RETAIN 或 REJECT，并给一句话理由。""",

    "fund": """你是基本面量化研究的老手，任务是**否决**下面这个因子。默认立场是"它是假的"。

请回答三个问题，每个问题不超过两句话：
1. 这个信号对应哪一组会计事实？必须指名科目（比如应计利润、经营现金流、应收账款、
   存货、商誉），并说清它为什么会导致未来收益差异。
2. 它最像哪个已知风格？（价值 / 成长 / 质量 / 规模 / 盈利动量）
3. 如果它一年后失效，最可能的原因是什么？考虑会计准则变化、行业结构变化、
   以及"财报粉饰被市场识别"这三种情形。

最后给一行结论：RETAIN 或 REJECT，并给一句话理由。""",
}


def llm_judge(expr, metrics, style="pv"):
    """有 key 才跑。没有就返回 None，不阻断流程。"""
    if not config.LLM_API_KEY:
        return None
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": QUESTIONS.get(style, QUESTIONS["pv"])},
            {"role": "user", "content": (
                f"表达式：{expr}\n"
                f"Sharpe={metrics.get('sharpe')} Fitness={metrics.get('fitness')} "
                f"换手={metrics.get('turnover')} 年化={metrics.get('returns')}"
            )},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    try:
        r = requests.post(
            config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=90,
        )
        if r.status_code != 200:
            return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def sim(client, expr, universe=None, neut=None):
    st = dict(config.SIM_SETTINGS)
    if universe:
        st["universe"] = universe
    if neut:
        st["neutralization"] = neut
    res = client.simulate(expr, settings=st)
    if "error" in res:
        return None
    return res["metrics"]


def audit(client, expr, base_sharpe):
    """核心：用三次对照实验判断收益来源。"""
    # 市值 10 档内中性化
    size_expr = f'group_neutralize(({expr}), bucket(rank(cap), range="0,1,0.1"))'
    m_size = sim(client, size_expr)
    # 行业中性化
    m_ind = sim(client, expr, neut="INDUSTRY")
    # 大盘股
    m_big = sim(client, expr, universe="TOP500")

    d_size = None if not m_size else (m_size["sharpe"] - base_sharpe) / abs(base_sharpe)
    d_ind = None if not m_ind else (m_ind["sharpe"] - base_sharpe) / abs(base_sharpe)
    d_big = None if not m_big else (m_big["sharpe"] - base_sharpe) / abs(base_sharpe)

    verdict, reasons = "RETAIN", []

    if d_size is not None and d_size < -0.35:
        reasons.append(f"市值中性化后 Sharpe 从 {base_sharpe:.2f} 掉到 {m_size['sharpe']:.2f}"
                       f"（{d_size*100:.0f}%），主要是小盘暴露")
        verdict = "REJECT"
    if d_ind is not None and d_ind < -0.35:
        reasons.append(f"行业中性化后掉 {d_ind*100:.0f}%，含大量行业轮动")
        verdict = "REJECT"
    if d_big is not None and d_big < -0.30:
        reasons.append(f"TOP500 只剩 {m_big['sharpe']:.2f}，容量不足")
        if verdict == "RETAIN":
            verdict = "WEAK"

    return {
        "expr": expr, "base": base_sharpe,
        "size": m_size, "ind": m_ind, "big": m_big,
        "d_size": d_size, "d_ind": d_ind, "d_big": d_big,
        "verdict": verdict, "reasons": reasons,
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--style", choices=["pv", "fund"], default="pv")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    store = Store()
    flt = config.FILTER
    rows = store.conn.execute(
        "SELECT expression, sharpe, fitness, turnover, returns FROM alphas "
        "WHERE sharpe >= ? AND fitness >= ? AND turnover BETWEEN ? AND ? "
        "ORDER BY fitness DESC LIMIT ?",
        (flt["sharpe_min"], flt["fitness_min"], flt["turnover_min"], flt["turnover_max"], args.limit),
    ).fetchall()

    if not rows:
        print("当前没有过线因子，没什么可审的。")
        return

    # 骨架去重：同一个骨架只审一条，别浪费额度
    def skel(e):
        s = re.sub(r"\b(close|open|high|low|vwap|volume|returns|cap|adv20)\b", "F", e)
        return re.sub(r"\s+", "", re.sub(r"\b\d+(\.\d+)?\b", "D", s))

    uniq, seen = [], set()
    for r in rows:
        k = skel(r[0])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    log(f"过线 {len(rows)} 条，去重骨架后 {len(uniq)} 条待审")
    client = BrainClient()

    out = [f"# 因子审查报告 · {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}", ""]
    out.append(f"审查对象：过线 {len(rows)} 条，按骨架去重后 {len(uniq)} 条")
    out.append("")

    for i, r in enumerate(uniq, 1):
        expr, s, f, t, ret = r
        log(f"[{i}/{len(uniq)}] 审查 {expr[:60]}")
        res = audit(client, expr, s)

        out.append(f"## {i}. `{expr}`")
        out.append("")
        out.append(f"- 原版：Sharpe {s:.2f} / Fitness {f:.2f} / 换手 {t:.2f} / 年化 {ret:.1%}")
        for k, label in (("size", "市值中性"), ("ind", "行业中性"), ("big", "TOP500")):
            m = res[k]
            if not m or m.get("sharpe") is None:
                out.append(f"- {label}：跑失败")
            else:
                out.append(f"- {label}：Sharpe {m['sharpe']:.2f}")
        out.append("")
        out.append(f"**判定：{res['verdict']}**")
        for why in res["reasons"]:
            out.append(f"- {why}")

        judge = llm_judge(expr, {"sharpe": s, "fitness": f, "turnover": t, "returns": ret}, args.style)
        if judge:
            out.append("")
            out.append("**LLM 追问：**")
            out.append("")
            out.append("> " + judge.replace("\n", "\n> "))
        elif not config.LLM_API_KEY:
            out.append("- （未配 LLM key，跳过机制追问）")
        out.append("")

    path = config.DATA_DIR / "critic.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    print(f"\n已写入 {path}")


if __name__ == "__main__":
    try:
        main()
    except AuthError as e:
        log(f"认证失败：{e}")
        sys.exit(2)
