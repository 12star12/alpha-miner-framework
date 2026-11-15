# -*- coding: utf-8 -*-
"""
tests/test_guard.py —— 表达式守卫的回归测试（不联网、不消耗额度）

跑法：
    python tests/test_guard.py       # 不依赖 pytest
    pytest tests/test_guard.py       # 也可以用 pytest

这些用例的意义：守卫把「能证明是空的」和「只是可疑」分开处理，
一旦有人把 WARN 改成 REJECT，下面第 4 条会立刻失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from expr_guard import check_signature, inspect  # noqa: E402

FIELDS = {"close", "debt", "eps", "volume", "assets"}


def test_provably_degenerate_is_rejected():
    """恒为常数的构造：必须直接拒。"""
    assert inspect("close - close", FIELDS)["decision"] == "REJECT"
    assert inspect("0 * rank(debt)", FIELDS)["decision"] == "REJECT"
    assert inspect("42", FIELDS)["decision"] == "REJECT"       # 不含任何字段


def test_self_ratio_is_only_warned():
    """A/A 默认只 WARN：某些上下文里它是有意义的归一化，不该一刀切。"""
    rep = inspect("debt / debt", FIELDS)
    assert rep["decision"] == "WARN", rep


def test_opposite_ranks_are_redundant_not_degenerate():
    """ts_rank(x) - ts_rank(-x)：f 单调时只是等价冗余，不是常数，必须只 WARN。"""
    rep = inspect("ts_rank(close, 30) - ts_rank(-close, 30)", FIELDS)
    assert rep["decision"] == "WARN", rep


def test_normal_expression_passes():
    assert inspect("ts_rank(close, 20)", FIELDS)["decision"] == "PASS"
    assert inspect("-ts_delta(close, 5) / ts_std_dev(returns, 20)",
                   FIELDS | {"returns"})["decision"] == "PASS"


def test_signature_rules():
    """算子签名：错误的写法要在本地就被拦掉，不要拿到平台上去试。"""
    assert check_signature("log(close, 2)")[0] is False        # 一元算子给了两个参数
    assert check_signature("ts_rank(close)")[0] is False       # 缺 lookback
    assert check_signature("ts_rank(close, 20)")[0] is True
    assert check_signature("group_rank(close, 5)")[0] is False  # 分组参数不能是数字
    assert check_signature('group_rank(close, "industry")')[0] is False  # 不要引号


def test_winsorize_keyword_argument_is_legal():
    """winsorize 是二参数算子，第二参数是关键字，别被一元规则误杀。"""
    assert check_signature("winsorize(close, std=4)")[0] is True
    assert check_signature("winsorize(close)")[0] is True
    assert check_signature("winsorize(close, 4)")[0] is False


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}  {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
