# -*- coding: utf-8 -*-
"""
run.py —— 一个窗口搞定：启动 + 监控

行为：
  - 检测到已有 daemon 在跑 → 只显示面板（不重复启动）
  - 没有 → 启动一个，然后显示面板
  - 关闭这个窗口 → 一切停止

用法：
    python run.py
 或直接双击 GO.bat
"""
import os
import re
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path

import config
import watch

LOG = config.DATA_DIR / "auto.log"
W = 66


def other_python_pids():
    """列出除自己以外的 python.exe 进程（用来看 daemon 在不在）"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=8,
        ).stdout
    except Exception:
        return []
    pids = re.findall(r'"python\.exe","(\d+)"', out)
    me = os.getpid()
    return [p for p in pids if p != str(me)]


def start_daemon():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")
    p = subprocess.Popen(
        [sys.executable, "-u", "daemon.py"],
        stdout=f, stderr=subprocess.STDOUT,
        cwd=str(config.ROOT),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return p


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    existing = other_python_pids()
    proc = None
    if existing:
        print(f"检测到 daemon 已在运行 (PID {existing[0]})，只做监控。")
        time.sleep(1.2)
    else:
        print("没有运行中的 daemon，正在启动…")
        proc = start_daemon()
        print(f"已启动 (PID {proc.pid})，等待首条结果…")
        time.sleep(2)

    try:
        while True:
            if sys.stdout.isatty():
                os.system("cls")
            alive = proc is not None and proc.poll() is None
            watch.render(alive=alive)
            print()
            if proc is None:
                print(" daemon 由其他窗口启动；关那个窗口才会停。")
            elif alive:
                print(f" 本窗口管理着 daemon (PID {proc.pid})，运行中")
            else:
                tail = ""
                if LOG.exists():
                    tail = LOG.read_text(encoding="utf-8", errors="ignore")[-600:]
                if "收工" in tail:
                    print(" daemon 已按计划收工（额度满 / 候选池空 / 连续认证失败）。")
                    print(" 想再跑：关掉本窗口，重新双击 GO.bat。")
                else:
                    print(" daemon 异常退出，10 秒后自动重启…")
                    time.sleep(10)
                    proc = start_daemon()
                    print(f" 已重启 (PID {proc.pid})")
            print(" Ctrl+C 退出面板（daemon 不受影响）")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n\n面板已退出。")
        if proc is not None and proc.poll() is None:
            print(f"daemon (PID {proc.pid}) 仍在后台运行。要停止它：")
            print(f"    taskkill /F /PID {proc.pid}")
        else:
            print("daemon 仍在后台运行，不受影响。")


if __name__ == "__main__":
    main()
