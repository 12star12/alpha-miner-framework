# -*- coding: utf-8 -*-
"""
两个生成器：

L0  gen_rules()  纯规则：模板 × 字段 × 窗口。零成本、零依赖、稳定的产量底座。
L1  gen_llm()    免费 LLM 生成。重点是「带经济假设」，不是瞎拼算子。

两者都过 validate()：语法 / 字段 / 算子三重校验，不合格的直接丢弃，
不浪费一次模拟额度（这一步能省掉 30%-60% 的无效回测）。
"""
import json
import re
import time

import requests

import config
import expr_guard
import operators

# ---------------------------------------------------------------- 白名单
# 第一阶段只放稳的算子。加新算子前先去平台文档确认签名。
OPERATORS = {
    "rank", "zscore", "scale", "winsorize", "normalize",
    "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta",
    "ts_sum", "ts_min", "ts_max", "ts_arg_max", "ts_arg_min",
    "ts_corr", "ts_covariance", "ts_decay_linear", "ts_delay",
    "ts_product", "ts_scale", "ts_backfill",
    "group_rank", "group_zscore", "group_neutralize", "group_mean",
    "signed_power", "log", "abs", "sign", "max", "min",
}
GROUPS = {"market", "sector", "industry", "subindustry", "country", "exchange"}
CONST_OK = {"close", "open", "high", "low", "volume", "vwap", "returns", "cap", "adv20"}
# 算子关键字参数名：winsorize(x, std=4) 这类写法里的 std 不是字段，校验时放行
KWARGS = {"std", "range"}

# ---------------------------------------------------------------- L0 模板
# 每个模板都是「一类经济学假设」的可执行形式，命名要能直接讲给面试官听。
#
# 这里只放 7 个教科书级示例，作用是示范「一个模板长什么样」。
# 真正的产出取决于你自己往里加什么 —— 加的不是更多窗口，而是「信号怎么被调制」
# 的新结构（量比调制、波动归一、区间位置……），否则 40 条候选其实只有一个想法。
TEMPLATES = [
    ("reversal-1d",      "-ts_delta({f}, {d})"),
    ("reversal-vs-mean", "-rank({f} / ts_mean({f}, {d}))"),
    ("momentum",         "rank(ts_delta({f}, {d}))"),
    ("low-volatility",   "-rank(ts_std_dev({f}, {d}))"),
    ("ts-rank",          "-ts_rank({f}, {d})"),
    ("sector-neutral",   "group_rank(ts_zscore({f}, {d}), subindustry)"),
    ("winsorized",       "winsorize(-ts_delta({f}, {d}), std=4)"),
]

WINDOWS = [5, 20, 60]


def fill(tpl: str, field: str, d: int) -> str:
    return (tpl.replace("{f}", field)
               .replace("{d2}", str(max(d // 4, 2)))
               .replace("{d}", str(d)))


def gen_rules(fields: list, windows=None) -> list:
    """规则笛卡尔积。fields 是字段 id 列表。"""
    windows = windows or WINDOWS
    out = []
    for f in fields:
        for name, tpl in TEMPLATES:
            for d in windows:
                if "{d}" not in tpl and d != windows[0]:
                    continue
                out.append({
                    "expression": fill(tpl, f, d),
                    "template": f"{name} | d={d}",
                    "field": f,
                })
    return out


# ---------------------------------------------------------------- 校验
def validate(expr: str, allowed_fields: set) -> tuple:
    """返回 (是否合法, 原因)。省额度的关键一步。"""
    if not expr or len(expr) > 400:
        return False, "长度异常"
    if expr.count("(") != expr.count(")"):
        return False, "括号不配对"
    if "{" in expr or "}" in expr:
        return False, "模板占位未替换"

    # 标识符
    idents = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
    for tok in idents:
        if tok in config.FIELD_BLACKLIST:
            return False, f"工具字段 {tok}"
        if tok in OPERATORS or tok in GROUPS or tok in CONST_OK or tok in KWARGS:
            continue
        if tok in allowed_fields:
            continue
        return False, f"未知标识符 {tok}"

    # 函数调用必须白名单
    for fn in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr):
        if fn not in OPERATORS:
            return False, f"未授权算子 {fn}"
        if fn in _denied_operators():
            return False, f"算子无权限(已实证) {fn}"

    # 自反构造：A - B 中 A 与 B 只差一个负号 → 两条腿互为反向，净信息量为零
    # 例：ts_rank(x,30) - ts_rank(-x,30)、rank(-x) - rank(x)
    # 因 ts_rank(-x) = 1 - ts_rank(x)，这类差值退化为原信号的线性变换
    s = expr.strip()
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "-" and depth == 0 and i > 0:
            left = s[:i].replace(" ", "").replace("-", "")
            right = s[i + 1:].replace(" ", "").replace("-", "")
            if left and left == right:
                return False, "自反构造(信息量为零)"
            break

    # ts_ 类算子缺窗口参数：ts_mean(x) 这种，平台会直接 400
    NEED_WIN = ("ts_mean", "ts_std_dev", "ts_rank", "ts_zscore", "ts_delta",
                "ts_sum", "ts_min", "ts_max", "ts_delay", "ts_decay_linear",
                "ts_arg_max", "ts_arg_min", "ts_product", "ts_scale")
    for m in re.finditer(r"\b(ts_[a-z_]+)\s*\(([^(),]*)\)", expr):
        if m.group(1) in NEED_WIN and "," not in m.group(2):
            return False, f"{m.group(1)} 缺窗口参数"

    # 参数签名：平台报错实证过的三类，本地直接拦，不花额度
    sig_ok, sig_why = expr_guard.check_signature(expr)
    if not sig_ok:
        return False, sig_why

    # 退化结构三态判定：只拒可严格证明的，其余放行并留痕
    rep = expr_guard.inspect(expr, allowed_fields)
    if rep["decision"] == "REJECT":
        return False, rep["reason"]
    if rep["decision"] == "WARN":
        expr_guard.log_warning(expr, rep)

    return True, ""


# ---------------------------------------------------------------- L1 LLM
_SYSTEM_TMPL = """你是量化研究员，在 WorldQuant BRAIN 平台上用 FASTEXPR 语言写 alpha 表达式。

硬性要求：
1. 只能使用我给出的字段名，一个字母都不许改、不许编造。
2. 只能使用这些算子（已自动剔除你账号无权限的算子）：__OPS__
3. 所有 ts_ 开头的算子必须带整数窗口参数，例如 ts_mean(x, 20)，不能写成 ts_mean(x)，
   也不能把字段当窗口写 ts_mean(x, close)。ts_corr / ts_covariance 需要 3 个参数：
   ts_corr(x, y, 20)。
4. 一元算子只接受 1 个参数：log、abs、sign、rank、zscore、normalize、winsorize。
   不要写 log(x, 10)、winsorize(x, 0.05) 这种两参数形式；二元算子 max/min/signed_power
   必须正好 2 个参数。
5. group_rank / group_zscore / group_neutralize / group_mean 的第二个参数是分组名，
   必须写成裸标识符 sector / industry / subindustry，不能加引号，也不能写数字。
6. 禁止自反构造：不要写 rank(-x) - rank(x) 或 ts_rank(-x,n) - ts_rank(x,n)，
   这类表达式的两条腿互为反向，净信息量为零。
7. 禁止常数结构：不要写 A / A、A - A、0 * A，它们不含任何截面信息。
8. 禁止使用 adjfactor、split、sharesout、dividend 这些复权/股本工具字段，它们不是信号。
9. 每个表达式必须配一句中文「经济假设」：为什么这个信号能预测收益，要用市场行为解释，
   不要写「因为回测表现好」这种废话。
10. 表达式之间要有差异：不要同一个式子换个窗口就交三遍。

基本面字段的正经用法示例（照这个思路，不要照抄）：
  - 应计质量：净资产收益率减去经营现金流收益率
  - 资本开支强度：capex 相对 assets 的同比变化
  - 盈利质量：ebit 相对 sales 的稳定性
  - 杠杆变化：debt 相对 assets 的同比变化（取负）

只输出 JSON 数组，不要任何解释文字，格式：
[{"expression": "...", "hypothesis": "...", "field": "..."}]"""


# ---------------------------------------------------------------- 算子权限
_DENIED_CACHE = None


def _denied_operators() -> set:
    """本次进程内缓存的禁用算子集合。"""
    global _DENIED_CACHE
    if _DENIED_CACHE is None:
        _DENIED_CACHE = operators.active_denied()
    return _DENIED_CACHE


def refresh_denied() -> set:
    global _DENIED_CACHE
    _DENIED_CACHE = operators.active_denied()
    return _DENIED_CACHE


def deny_operator(op: str, error: str = "") -> None:
    """运行时拉黑：写入文件 + 更新内存缓存，本进程立即生效。"""
    operators.deny(op, error)
    _denied_operators().add(op)


def build_system() -> str:
    """算子清单运行时生成，自动剔除无权限算子。"""
    ops = "、".join(sorted(OPERATORS - _denied_operators()))
    return _SYSTEM_TMPL.replace("__OPS__", ops)


SYSTEM = _SYSTEM_TMPL  # 兼容旧引用；实际调用请用 build_system()


def gen_llm(fields: list, n: int = 30, field_desc: dict = None, retries: int = 3,
            hint: str = None) -> list:
    """调用免费 LLM 生成候选。失败返回空列表，不阻断主流程。
    hint：轮次复盘给出的研究重点提示，注入到用户消息里引导生成方向。"""
    if not config.LLM_API_KEY:
        return []

    sample = fields[:40]
    lines = []
    for f in sample:
        desc = (field_desc or {}).get(f, "")
        lines.append(f"- {f}" + (f"  // {desc}" if desc else ""))
    user = (
        f"可用字段（共 {len(sample)} 个）：\n" + "\n".join(lines) +
        f"\n\n请生成 {n} 条 alpha 表达式，覆盖不同的经济逻辑（反转、动量、波动、量价、行业中性）。"
    )
    if hint:
        user += f"\n\n本轮研究重点（上一轮数据给出的方向）：{hint}"

    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": build_system()},
            {"role": "user", "content": user},
        ],
        "temperature": 1.0,
        "max_tokens": 4000,
    }
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=120)
            if r.status_code != 200:
                print(f"  LLM 返回 {r.status_code}: {r.text[:200]}")
                time.sleep(3)
                continue
            text = r.json()["choices"][0]["message"]["content"]
            return _parse_json_array(text)
        except Exception as e:
            print(f"  LLM 调用异常：{e}")
            time.sleep(3)
    return []


def _parse_json_array(text: str) -> list:
    """LLM 经常裹 ```json，做个宽容解析。"""
    text = text.strip()
    if "```" in text:
        parts = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
        if parts:
            text = parts[0].strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        expr = str(item.get("expression", "")).strip()
        if not expr:
            continue
        out.append({
            "expression": expr,
            "hypothesis": str(item.get("hypothesis", ""))[:200],
            "field": str(item.get("field", "")),
        })
    return out
