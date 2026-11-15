# -*- coding: utf-8 -*-
"""
operators.py —— 算子权限黑名单（Phase 1 · 第二步）

背景：attempts 表实证 177 次提交因 `inaccessible or unknown operator "ts_max"/"ts_min"`
被平台拒绝。本地算子白名单是静态的，不知道账号没有哪个算子的权限。

原则：
  · 只拦"已被平台拒绝过"的算子，不做任何先验猜测；
  · 每个算子最多被拒绝一次，之后永久跳过；
  · 可人工解除（权限会变），解除不删历史记录；
  · 文件损坏时降级到内置默认值，绝不阻断生成主流程。

用法：
  python operators.py --list
  python operators.py --allow ts_max
  python operators.py --deny  some_op "错误原文"
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

FILE = Path(__file__).resolve().parent / "data" / "denied_operators.json"

# 文件缺失/损坏时的兜底，来源：attempts 表实证
DEFAULT_DENIED = ("ts_max", "ts_min")

# 平台错误里的算子名
_OP_IN_ERROR = re.compile(r'inaccessible or unknown operator "([^"]+)"')


def _empty() -> dict:
    return {"version": 1, "items": []}


def load() -> dict:
    """读取黑名单。任何异常都降级到默认值，不抛出。"""
    try:
        if not FILE.exists():
            data = _empty()
            for op in DEFAULT_DENIED:
                data["items"].append({"op": op, "denied_at": "", "error": "内置默认（实证）",
                                      "hits": 0, "source": "bootstrap", "active": True})
            return data
        raw = json.loads(FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ValueError("结构异常")
        items = []
        for it in raw["items"]:
            if isinstance(it, dict) and it.get("op"):
                items.append({
                    "op": str(it["op"]),
                    "denied_at": str(it.get("denied_at", "")),
                    "error": str(it.get("error", ""))[:200],
                    "hits": int(it.get("hits", 0) or 0),
                    "source": str(it.get("source", "platform")),
                    "active": bool(it.get("active", True)),
                })
        return {"version": 1, "items": items}
    except Exception as e:
        print(f"  [operators] 黑名单读取失败，降级到默认值：{e}")
        data = _empty()
        for op in DEFAULT_DENIED:
            data["items"].append({"op": op, "denied_at": "", "error": "读取失败兜底",
                                  "hits": 0, "source": "fallback", "active": True})
        return data


def _save(data: dict) -> bool:
    """原子写入：临时文件 + os.replace。失败返回 False，不抛出。"""
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, FILE)
        return True
    except Exception as e:
        print(f"  [operators] 写入失败（内存态仍生效）：{e}")
        return False


def active_denied() -> set:
    """当前生效的禁用算子集合。"""
    return {it["op"] for it in load()["items"] if it["active"]}


def deny(op: str, error: str = "", source: str = "platform") -> bool:
    """拉黑一个算子。已存在则只累加 hits 并刷新时间。"""
    if not op:
        return False
    data = load()
    now = datetime.now().isoformat(timespec="seconds")
    for it in data["items"]:
        if it["op"] == op:
            it["hits"] = int(it.get("hits", 0)) + 1
            it["denied_at"] = now
            it["active"] = True
            if error:
                it["error"] = error[:200]
            return _save(data)
    data["items"].append({"op": op, "denied_at": now, "error": (error or "")[:200],
                          "hits": 1, "source": source, "active": True})
    return _save(data)


def allow(op: str) -> bool:
    """解除黑名单（保留历史记录，只置 active=False）。"""
    data = load()
    hit = False
    for it in data["items"]:
        if it["op"] == op:
            it["active"] = False
            hit = True
    if not hit:
        print(f"  [operators] {op} 不在黑名单中")
        return False
    _save(data)
    print(f"  [operators] 已解除 {op}（历史记录保留）")
    return True


def extract_denied_ops(msg: str) -> list:
    """从平台错误原文里提取被拒算子名。"""
    return _OP_IN_ERROR.findall(msg or "")


def _fmt(data: dict) -> str:
    if not data["items"]:
        return "（黑名单为空）"
    lines = [f"{'算子':14s} {'状态':6s} {'命中':>4s}  拒绝时间            错误原文"]
    for it in data["items"]:
        state = "禁用" if it["active"] else "已解禁"
        lines.append(f"{it['op']:14s} {state:6s} {it['hits']:>4d}  "
                     f"{it['denied_at'] or '(启动默认)':19s} {it['error'][:60]}")
    return "\n".join(lines)


def main(argv=None):
    argv = argv or sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if not argv or argv[0] in ("-l", "--list"):
        print(_fmt(load()))
        return 0
    if argv[0] in ("-a", "--allow") and len(argv) >= 2:
        allow(argv[1])
        return 0
    if argv[0] in ("-d", "--deny") and len(argv) >= 2:
        deny(argv[1], argv[2] if len(argv) > 2 else "手工添加", source="manual")
        print(f"  [operators] 已加入黑名单：{argv[1]}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
