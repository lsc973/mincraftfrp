#!/usr/bin/env python3
"""验证中继收到停止信号后能优雅退出。

这条路径必须在**子进程**里测 —— 信号是发给进程的，在同一个进程里没法模拟。
所以它不在 tests/ 里，而是单独一个脚本。

Linux/macOS：直接发 SIGTERM（systemd、docker stop 用的就是它）。

Windows：``os.kill`` 是强杀，收不到信号。但 Windows 有 ``CTRL_BREAK_EVENT``
（对应 SIGBREAK），走的是**同一段处理代码**，所以在这里测通，就能对 Linux
上的 SIGTERM 有相当把握。

    python tools/signal_test.py                 # 测源码
    python tools/signal_test.py --exe dist/lanlink-relay.exe   # 测打包出来的二进制
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENV = dict(os.environ)
ENV["PYTHONIOENCODING"] = "utf-8"

IS_WINDOWS = sys.platform.startswith("win")
PORT = "53201"

problems: list = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'[通过]' if ok else '[失败]'} {label}" + (f" ——— {detail}" if detail else ""))
    if not ok:
        problems.append(label)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="验证中继的优雅退出")
    parser.add_argument("--exe", help="要测的可执行文件；不填就测源码")
    parser.add_argument("--port", default=PORT)
    args = parser.parse_args()

    if args.exe:
        command = [str(Path(args.exe).resolve()), "--port", args.port, "--log-level", "INFO"]
        label = f"二进制 {Path(args.exe).name}"
    else:
        command = [sys.executable, "-m", "lanlink", "relay", "--port", args.port, "--log-level", "INFO"]
        label = "源码 python -m lanlink relay"

    print("=" * 66)
    print(f"  中继优雅退出测试 —— {label}")
    print("=" * 66)

    # CREATE_NEW_PROCESS_GROUP 是 Windows 上必须的：控制台事件只能发给进程组，
    # 而且带上它子进程才不会被父进程的 Ctrl+C 误伤。其他平台忽略这个参数。
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=ENV, **kwargs,
    )
    time.sleep(4.0)

    if not check("中继已启动并存活", proc.poll() is None, f"poll={proc.poll()}"):
        print(proc.stdout.read()[-500:])
        proc.kill()
        return 1

    # 确认它真的在服务，而不是只是没崩
    probe = subprocess.run(
        [sys.executable, "-m", "lanlink", "list", "--relay", f"127.0.0.1:{args.port}"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, env=ENV,
    )
    check("中继能正常响应查询", "上的房间" in probe.stdout)

    signal_name = "CTRL_BREAK_EVENT" if IS_WINDOWS else "SIGTERM"
    print(f"\n  发送 {signal_name}（systemd / docker stop 用的就是这条路径）…")
    started = time.monotonic()
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
    except Exception as exc:
        check(f"发送 {signal_name}", False, str(exc))
        proc.kill()
        return 1

    try:
        proc.wait(timeout=15)
        elapsed = time.monotonic() - started
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
        elapsed = time.monotonic() - started
        proc.kill()
        proc.wait(timeout=10)

    out = proc.stdout.read()
    check("收到信号后主动退出（没被强杀）", exited, f"{elapsed:.2f}s")
    check("退出码为 0", proc.returncode == 0, f"实际 {proc.returncode}")
    check("响应及时（< 3 秒）", elapsed < 3.0, f"{elapsed:.2f}s")
    graceful = "正在关闭" in out or "已停止" in out
    check("走的是优雅路径（打印了关闭提示）", graceful)

    print("\n  --- 中继输出 ---")
    for line in out.splitlines()[-8:]:
        print("    " + line)

    print()
    print("=" * 66)
    if problems:
        print(f"  {len(problems)} 项没过：" + "、".join(problems))
    else:
        print("  优雅退出验证通过")
    print("=" * 66)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
