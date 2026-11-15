# -*- coding: utf-8 -*-
"""
expr_guard.py —— 退化结构的三种判定（Phase 1 · 第二步）

设计原则（v4.2 约定）：
  能严格证明的直接拒；不能证明的高亮留痕，交给人判断。

  STRICT_REJECT 只保留三条数学上可证明的结构：
    S1  A - A           两侧操作数完全相同 → 恒为 0
    S2  0 * A / A * 0   恒为 0
    S4  不含任何字段     纯常数表达式

  其余一律 WARN（放行 + 留痕，不拦截）：
    S3  A / A           非零点恒为 ±1，但零值/缺失/NaN 语义未经平台核验 → 只警告
    W1  f(x) 与 f(-x)   单调算子下是原信号的单调变换（信息保留，仅冗余），
                        偶函数下才恒零，无法静态区分 → 只警告
    W2  A / (A ± c)     近似自比，有真实截面差异
    W4  深层嵌套        仅提示过拟合与可读性风险

明确不做：不在本模块做任何"等价性去重"。骨架相似 ≠ 等价。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# 可严格证明的判定开关：S3 默认关闭（平台语义核验后才允许打开）
STRICT_SELF_RATIO = False

WARN_LOG = Path(__file__).resolve().parent / "data" / "warnings.jsonl"

_CODE_DESC = {
    "S1": "两侧操作数完全相同，恒为 0",
    "S2": "乘以 0，恒为 0",
    "S3": "自比结构，非零点恒为常数（零值/缺失语义未核验）",
    "S4": "不含任何字段，纯常数表达式",
    "W1": "同时出现 f(x) 与 f(-x)，可能是原信号的单调变换（冗余）",
    "W2": "近似自比 A/(A±c)，需确认截面差异是否来自真实信号",
    "W4": "嵌套层级过深，过拟合与可读性风险",
}


# ---------------------------------------------------------------- 扫描工具

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _binary_minus_positions(s: str) -> list:
    """找出所有二元减号的位置（跳过一元负号，如 -ts_rank(...) 开头）。"""
    out = []
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "-":
            j = i - 1
            while j >= 0 and s[j] == " ":
                j -= 1
            if j >= 0 and (s[j].isalnum() or s[j] in ")_"):
                out.append(i)
    return out


def _left_operand(s: str, i: int) -> str:
    j = i - 1
    depth = 0
    buf = []
    while j >= 0:
        ch = s[j]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch in "+-*/,":
            break
        buf.append(ch)
        j -= 1
    return _norm("".join(reversed(buf)))


def _right_operand(s: str, i: int) -> str:
    depth = 0
    buf = []
    for ch in s[i + 1:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch in "+-*/,":
            break
        buf.append(ch)
    return _norm("".join(buf))


def _slash_pairs(s: str) -> list:
    """所有除法的 (左操作数, 右操作数)，已去空格。"""
    pairs = []
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "/":
            l = _left_operand(s, i)
            r = _right_operand(s, i)
            if l and r:
                pairs.append((l, r))
    return pairs


def _max_depth(s: str) -> int:
    d = mx = 0
    for ch in s:
        if ch == "(":
            d += 1
            mx = max(mx, d)
        elif ch == ")":
            d -= 1
    return mx


def _strip_parens(x: str) -> str:
    """去掉整体包裹的成对括号，便于比较操作数。"""
    while len(x) > 1 and x[0] == "(" and x[-1] == ")":
        depth, ok = 0, True
        for i, ch in enumerate(x):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(x) - 1:
                    ok = False
                    break
        if not ok:
            break
        x = x[1:-1]
    return x


# ---------------------------------------------------------------- 主判定

def inspect(expr: str, allowed_fields) -> dict:
    """返回 {decision, code, reason, warnings}。decision ∈ PASS / WARN / REJECT。"""
    if not expr:
        return {"decision": "PASS", "code": None, "reason": "", "warnings": []}

    s = _norm(expr)
    warnings = []

    # ---- S1 两侧操作数完全相同 → 恒 0
    for pos in _binary_minus_positions(s):
        left = _left_operand(s, pos)
        right = _right_operand(s, pos)
        if left and left == right:
            return _reject("S1", f"{left} - {left}")
        if left and _norm(left.replace("-", "")) == _norm(right.replace("-", "")) and left != right:
            warnings.append(("W1", f"{left} 与 {right} 互为反向"))

    # ---- S2 乘 0
    if re.search(r"(^|[(,+\-*/])0\*", s) or re.search(r"\*0($|[),+\-*/])", s):
        return _reject("S2", "乘以 0")

    # ---- 除法：自比 / 近似自比
    for l, r in _slash_pairs(s):
        ln = _norm(_strip_parens(l).lstrip("-"))
        rn = _norm(_strip_parens(r).lstrip("-"))
        if ln and ln == rn:
            if STRICT_SELF_RATIO:
                return _reject("S3", f"{l} / {r}")
            warnings.append(("S3", f"{l} / {r} 为自比结构"))
            continue
        if ln and rn.startswith(ln) and len(rn) > len(ln) and rn[len(ln)] in "+-":
            warnings.append(("W2", f"{l} / {r} 近似自比"))

    # ---- S4 不含字段
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s))
    if not (tokens & set(allowed_fields)):
        return _reject("S4", "不含任何字段，纯常数表达式")

    # ---- W4 深嵌套
    if _max_depth(s) >= 4:
        warnings.append(("W4", f"嵌套 {_max_depth(s)} 层"))

    # ---- 去重后返回
    seen, uniq = set(), []
    for code, detail in warnings:
        if code in seen:
            continue
        seen.add(code)
        uniq.append({"code": code, "reason": f"{_CODE_DESC[code]}：{detail}"})

    if uniq:
        return {"decision": "WARN", "code": uniq[0]["code"],
                "reason": uniq[0]["reason"], "warnings": uniq}
    return {"decision": "PASS", "code": None, "reason": "", "warnings": []}


def _reject(code: str, detail: str) -> dict:
    return {"decision": "REJECT", "code": code,
            "reason": f"[{code}] {_CODE_DESC[code]}：{detail}", "warnings": []}


# ---------------------------------------------------------------- 留痕

def log_warning(expr: str, rep: dict, field: str = "", template: str = "") -> None:
    """WARN 落盘，best-effort：任何异常都不影响主流程。"""
    try:
        WARN_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "expr": expr[:300],
            "decision": rep.get("decision"),
            "code": rep.get("code"),
            "reason": rep.get("reason", "")[:200],
            "field": field or "",
            "template": template or "",
        }
        with WARN_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_warnings(date: str = None) -> dict:
    """统计 warnings.jsonl：按 code 计数 + 取样例。date 为 'YYYY-MM-DD' 时只读当天。"""
    from collections import Counter
    counts, samples, total = Counter(), [], 0
    try:
        if not WARN_LOG.exists():
            return {"counts": counts, "samples": [], "total": 0}
        for line in WARN_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if date and not str(rec.get("ts", "")).startswith(date):
                continue
            total += 1
            counts[rec.get("code") or "?"] += 1
            if len(samples) < 5:
                samples.append(rec)
    except Exception:
        pass
    return {"counts": counts, "samples": samples, "total": total}

# ---------------------------------------------------------------- 参数签名校验
# 依据：一次真实运行的失败样本里，近八成集中在「一元算子给多参」「带引号字符串」
# 「ts_* 的 lookback 不是整数」这三类模式上。它们 100% 可在本地判定，
# 不需要上平台花额度才知道错。
_UNARY = {"log", "abs", "sign", "rank", "zscore", "normalize", "winsorize"}
_BINARY = {"max", "min", "signed_power"}
_TS_LOOKBACK = {
    "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta", "ts_sum",
    "ts_min", "ts_max", "ts_arg_max", "ts_arg_min", "ts_corr", "ts_covariance",
    "ts_decay_linear", "ts_delay", "ts_product", "ts_scale", "ts_backfill",
}


def _split_args(inner: str) -> list:
    """按顶层逗号切分参数，忽略嵌套括号内的逗号。"""
    args, depth, cur = [], 0, []
    for ch in inner:
        if ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail or args:
        args.append(tail)
    return [a for a in args if a != ""] if args else []


def _iter_calls(expr: str):
    """产出 (算子名, 括号内文本)，支持嵌套。"""
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr):
        name = m.group(1)
        i = m.end() - 1
        depth = 0
        for j in range(i, len(expr)):
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
                if depth == 0:
                    yield name, expr[i + 1:j]
                    break


def check_signature(expr: str) -> tuple:
    """参数签名校验。返回 (是否通过, 原因)。

    只拒绝「平台已经用报错证明过」的三类，不涉及任何研究判断：
      S5 一元/二元算子参数个数错
      S6 出现引号字符串（group 名必须用裸标识符）
      S7 ts_ 算子的 lookback 不是整数
    """
    if re.search(r"['\"]", expr):
        return False, "[S6] 不能用引号字符串；group 名请直接用标识符，如 sector"

    for name, inner in _iter_calls(expr):
        args = _split_args(inner)
        if name in _UNARY and len(args) != 1:
            # 例外：winsorize 是二参数算子，第二参数是关键字而非位置参数
            # winsorize(x, std=4) / winsorize(x, range="0,1") 都合法
            if not (name == "winsorize" and len(args) == 2
                    and re.match(r"^\s*(std|range)\s*=", args[1])):
                return False, f"[S5] {name} 只接受 1 个参数，实际给了 {len(args)} 个"
        if name in _BINARY and len(args) != 2:
            return False, f"[S5] {name} 只接受 2 个参数，实际给了 {len(args)} 个"
        # S8：group_* 的第二个参数必须是分组标识符，不能是数字/表达式
        if name.startswith("group_") and len(args) == 2 and re.fullmatch(r"\d+", args[1].strip()):
            return False, f"[S8] {name} 的第二个参数是分组名，不能是数字 {args[1].strip()}"
        if name in _TS_LOOKBACK:
            if len(args) < 2:
                return False, f"[S7] {name} 缺 lookback 参数"
            last = args[-1].strip()
            if not re.fullmatch(r"\d+", last):
                return False, f"[S7] {name} 的 lookback 必须是整数，实际是 {last[:24]}"
    return True, ""
