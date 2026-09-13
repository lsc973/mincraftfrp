"""验证打包出来的 exe 真的能用。

打包成功不等于能跑 —— PyInstaller 经常打出一个双击就闪退的东西。这个脚本
把 exe 当成黑盒完整跑一遍：

* 命令行版：doctor / 开房 / 加入 / 收发中文 / 中继列表
* 图形版：进程要活着、窗口标题要对、日志里不能有异常

用法::

    python tools/exe_smoke_test.py            # 默认查 dist/ 下的两个 exe
    python tools/exe_smoke_test.py --dist 别的目录
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: exe 在管道场景下会走系统 ANSI 代码页。统一成 UTF-8，不然中文全是乱码，
#: 往里喂 UTF-8 还会解出代理字符让程序崩掉。（打包用的 launcher 也会做这件事，
#: 这里再设一遍是为了覆盖 exe 里可能在 launcher 之前就读了环境变量的情况。）
ENV = dict(os.environ)
ENV["PYTHONIOENCODING"] = "utf-8"

_TEMP = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")


def kill_leftovers() -> None:
    """清掉残留的 exe 进程。

    不清的话下一轮 PyInstaller 打包会因为 dist/*.exe 被占用而
    PermissionError，日志文件也会被锁住。
    """
    for image in ("lanlink.exe", "lanlink-cli.exe"):
        subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True, check=False)
    time.sleep(1.5)


class Report:
    def __init__(self) -> None:
        self.problems: list = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'[通过]' if ok else '[失败]'} {label}" + (f" —— {detail}" if detail else ""))
        if not ok:
            self.problems.append(label)
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 66)
        print(f"  {title}")
        print("=" * 66)


def run_cli(cli: Path, report: Report) -> None:
    report.section("1. 命令行版：环境自检")
    proc = subprocess.run(
        [str(cli), "doctor"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180, env=ENV,
    )
    # doctor 的退出码是有含义的：0 = 全过，1 = 有项目没通过（比如宽带在
    # 运营商大内网里）。不能断言必须为 0 —— 那取决于跑测试的机器是什么网络。
    report.check("doctor 正常结束（0=全过 / 1=有项目没通过）",
                 proc.returncode in (0, 1), f"rc={proc.returncode}")
    report.check("doctor 输出中文正常", "环境自检" in proc.stdout and "本机 IP" in proc.stdout)
    report.check("doctor 无异常", "Traceback" not in (proc.stderr or ""))
    for line in proc.stdout.splitlines():
        if line.startswith(("[通过]", "[失败]")):
            print("    " + line)

    report.section("2. 命令行版：开房 + 加入 + 收发中文")
    host = subprocess.Popen(
        [str(cli), "host", "--room", "exe测试房", "--no-advertise", "--port", "53101"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=ENV,
    )
    time.sleep(4.0)
    report.check("主机进程存活", host.poll() is None, f"poll={host.poll()}")

    joined = subprocess.run(
        [str(cli), "join", "--addr", "127.0.0.1:53101", "--name", "exe客户端"],
        input="你好呀\n/quit\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90, env=ENV,
    )
    report.check("客户端正常退出", joined.returncode == 0, f"rc={joined.returncode}")
    report.check("客户端加入成功", "已加入" in joined.stdout)
    report.check("客户端没崩", "Traceback" not in (joined.stdout + (joined.stderr or "")))
    if joined.returncode != 0:
        print("    --- 客户端 stderr ---")
        print("    " + (joined.stderr or "").strip()[:400].replace("\n", "\n    "))

    host.stdin.write("/quit\n")
    host.stdin.flush()
    try:
        host.wait(timeout=20)
    except subprocess.TimeoutExpired:
        host.kill()
    host_out = host.stdout.read()
    report.check("主机看到客户端进入", "exe客户端" in host_out and "进来了" in host_out)
    report.check("主机收到中文消息", "你好呀" in host_out)
    report.check("主机没崩", "Traceback" not in host_out)
    print("    --- 主机输出片段 ---")
    for line in [l for l in host_out.splitlines() if l.strip()][-5:]:
        print("    " + line)

    report.section("3. 命令行版：免费域名的 HTTPS")
    # 这一段专门防"源码能跑、exe 崩"：ssl 和根证书在 PyInstaller onefile 里
    # 最容易缺。缺了的话报的是 ModuleNotFoundError / DLL load failed，
    # 跟网络不通是两码事 —— 所以这里要区分开，不能一律当成"网络问题"放过。
    ddns_env = dict(ENV)
    ddns_env["LANLINK_CONFIG_DIR"] = str(_TEMP / "lanlink-smoke-ddns")
    setup = subprocess.run(
        [str(cli), "ddns", "setup", "--provider", "dynv6",
         "--hostname", "smoketest.dynv6.net", "--token", "definitely-not-a-real-token"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env=ddns_env,
    )
    out = setup.stdout + (setup.stderr or "")
    markers = ("No module named", "DLL load failed", "ImportError")
    packaging_broken = any(m in out for m in markers)
    # 出问题时把真正那一行挑出来，别甩最后一行（那是"配置已经存下来了"之类
    # 的收尾话，对排查毫无帮助）
    detail = ""
    if packaging_broken:
        for line in out.splitlines():
            if any(m in line for m in markers):
                detail = line.strip()[:200]
                break
    report.check("exe 里 ssl / 证书没缺", not packaging_broken, detail)
    if "令牌不对" in out:
        report.check("HTTPS 真的走通了（第三方 API 正常回话）", True)
    elif packaging_broken:
        pass   # 上面那条已经记下来了
    else:
        # 断网、被墙、服务商抽风 —— 跟打包无关，不该让 exe 验证失败
        print("    [跳过] 这次没连上 dynv6，无法确认 HTTPS 往返（不影响打包结论）")
    shutil.rmtree(_TEMP / "lanlink-smoke-ddns", ignore_errors=True)

    report.section("4. 命令行版：中继")
    relay = subprocess.Popen(
        [str(cli), "relay", "--port", "53102", "--token", "tk"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=ENV,
    )
    time.sleep(3.0)
    listed = subprocess.run(
        [str(cli), "list", "--relay", "127.0.0.1:53102"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90, env=ENV,
    )
    report.check("能连上 exe 起的中继", "上的房间" in listed.stdout,
                 listed.stdout.strip().splitlines()[0] if listed.stdout.strip() else "")
    relay.terminate()
    try:
        relay.wait(timeout=10)
    except subprocess.TimeoutExpired:
        relay.kill()


def run_gui(gui: Path, report: Report) -> None:
    report.section("5. 图形版：启动检查")

    log_path = _TEMP / "lanlink.log"
    if log_path.exists():
        try:
            log_path.unlink()
        except OSError:
            pass

    proc = subprocess.Popen([str(gui)], env=ENV)
    time.sleep(8.0)
    alive = proc.poll() is None
    report.check("进程存活（没在启动时闪退）", alive, f"退出码={proc.poll()}")

    if alive:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process lanlink -ErrorAction SilentlyContinue | "
             "Where-Object { $_.MainWindowTitle -ne '' } | "
             "Select-Object -ExpandProperty MainWindowTitle"],
            capture_output=True, timeout=60,
        )
        title = ps.stdout.decode("gbk", errors="replace").strip() or \
            ps.stdout.decode("utf-8", errors="replace").strip()
        report.check("窗口已创建", bool(title), repr(title))
        report.check("窗口标题正确", "lanlink" in title)

        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        report.check("写出了日志文件", log_path.exists(), str(log_path))
        report.check("日志里没有异常", "Traceback" not in log_text and "ERROR" not in log_text)

        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="验证打包出来的 exe")
    parser.add_argument("--dist", default=str(PROJECT_ROOT / "dist"), help="exe 所在目录")
    parser.add_argument("--skip-gui", action="store_true", help="只测命令行版")
    args = parser.parse_args()

    if not sys.platform.startswith("win"):
        print("这个脚本只用于验证 Windows 的 exe", file=sys.stderr)
        return 2

    dist = Path(args.dist)
    cli = dist / "lanlink-cli.exe"
    gui = dist / "lanlink.exe"

    wanted = [cli] + ([] if args.skip_gui else [gui])
    missing = [p for p in wanted if not p.exists()]
    if missing:
        print(f"找不到 exe：{missing}\n先跑 python packaging/build.py --mode both", file=sys.stderr)
        return 2

    kill_leftovers()
    report = Report()
    try:
        run_cli(cli, report)
        if not args.skip_gui and gui.exists():
            run_gui(gui, report)
    finally:
        kill_leftovers()

    print()
    print("=" * 66)
    if report.problems:
        print(f"  有 {len(report.problems)} 项没过：")
        for item in report.problems:
            print("    · " + item)
    else:
        print("  exe 验证全部通过")
    print("=" * 66)
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
