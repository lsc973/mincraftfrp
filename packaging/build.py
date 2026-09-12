"""一键打包成 exe。

用法::

    python packaging/build.py              # 图形版（默认）
    python packaging/build.py --mode cli   # 命令行版
    python packaging/build.py --mode both  # 两个都打
    python packaging/build.py --clean      # 先清干净再打

产物在 ``dist/`` 下。

关于体积：PyInstaller 会把整个 Python 运行时和 tkinter 塞进去，所以图形版
大概 10~15 MB。这是单文件模式的正常代价 —— 换来的是对方不用装 Python。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST = PROJECT_ROOT / "dist"
BUILD = PROJECT_ROOT / "build"

#: 打包时故意不带的模块。能把体积压下来一截，而且这些我们确实一个都没用。
_EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "PyQt5", "PySide2",
    "pytest", "setuptools", "pip", "unittest", "pydoc", "doctest",
    "email", "html", "http.server", "xmlrpc", "sqlite3", "distutils",
]

MODES = {
    "gui": {
        "name": "lanlink",
        "entry": "packaging/launcher_gui.py",
        "windowed": True,
        "desc": "图形界面版（双击即用）",
    },
    "cli": {
        "name": "lanlink-cli",
        "entry": "packaging/launcher_cli.py",
        "windowed": False,
        "desc": "命令行版（host / join / list / relay / doctor）",
    },
    "relay": {
        "name": "lanlink-relay",
        "entry": "packaging/launcher_relay.py",
        "windowed": False,
        "desc": "中继服务器专用（部署到 Linux 用，不带参数直接跑中继）",
    },
}

#: 图形版只能在有桌面的平台上构建 —— PyInstaller 不支持交叉编译，
#: 所以 Linux 服务器上的中继二进制必须在 Linux 机器上打。
GUI_CAPABLE = sys.platform in ("win32", "darwin")


def check_pyinstaller() -> str:
    try:
        import PyInstaller  # noqa: F401

        return PyInstaller.__version__
    except ImportError:
        print("没装 PyInstaller。先装：", file=sys.stderr)
        print("    pip install pyinstaller", file=sys.stderr)
        raise SystemExit(1)


def build_one(mode: str, *, clean_first: bool = False) -> Path:
    spec = MODES[mode]
    entry = PROJECT_ROOT / spec["entry"]
    if not entry.exists():
        raise SystemExit(f"入口脚本不存在：{entry}")

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", spec["name"],
        "--paths", str(PROJECT_ROOT),
        "--distpath", str(DIST),
        "--workpath", str(BUILD / mode),
        "--specpath", str(BUILD),
    ]
    command.append("--windowed" if spec["windowed"] else "--console")
    for module in _EXCLUDES:
        command += ["--exclude-module", module]
    if clean_first:
        command.append("--clean")
    command.append(str(entry))

    print(f"\n{'=' * 62}")
    print(f"  打包 {spec['desc']}")
    print(f"  入口 {spec['entry']}")
    print(f"{'=' * 62}")

    started = time.monotonic()
    result = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    if result.returncode != 0:
        raise SystemExit(f"打包失败（退出码 {result.returncode}）")

    suffix = ".exe" if sys.platform.startswith("win") else ""
    output = DIST / f"{spec['name']}{suffix}"
    if not output.exists():
        raise SystemExit(f"打包命令成功了，但没找到产物：{output}")

    size_mb = output.stat().st_size / 1024 / 1024
    print(f"\n完成：{output}  （{size_mb:.1f} MB，耗时 {time.monotonic() - started:.0f} 秒）")
    return output


def cleanup_project() -> None:
    """清掉 PyInstaller 可能落在项目根目录的中间产物。

    已经用 --workpath/--specpath 把构建目录指到 build/ 下了，
    正常情况下根目录是干净的；这里只是兜个底。
    注意 dist/ 是产物，不能删。
    """
    for junk in (PROJECT_ROOT / "__pycache__", PROJECT_ROOT / "build"):
        if junk.exists():
            shutil.rmtree(junk, ignore_errors=True)
    for stale in PROJECT_ROOT.glob("*.spec"):
        try:
            stale.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="把 lanlink 打包成 exe")
    parser.add_argument(
        "--mode", choices=["gui", "cli", "relay", "both", "all"], default="gui",
        help="打包哪个（both = gui+cli，all = 全部）",
    )
    parser.add_argument("--clean", action="store_true", help="打包前清掉缓存")
    args = parser.parse_args()

    version = check_pyinstaller()
    print(f"Python {sys.version.split()[0]} / PyInstaller {version}")

    if args.mode == "both":
        modes = ["gui", "cli"]
    elif args.mode == "all":
        modes = ["gui", "cli", "relay"]
    else:
        modes = [args.mode]

    if "gui" in modes and not GUI_CAPABLE:
        print(
            f"跳过图形版：{sys.platform} 上打不出来 —— PyInstaller 不能交叉编译，"
            "而且服务器上一般也没装 tkinter。",
            file=sys.stderr,
        )
        print("  中继要部署到 Linux 的话，在那台机器上用 --mode relay 构建。", file=sys.stderr)
        modes = [m for m in modes if m != "gui"]
        if not modes:
            return 1
    outputs = []
    for mode in modes:
        outputs.append(build_one(mode, clean_first=args.clean))

    cleanup_project()

    print(f"\n{'=' * 62}")
    print("  打包完成")
    print(f"{'=' * 62}")
    for output in outputs:
        print(f"  {output}   {output.stat().st_size / 1024 / 1024:.1f} MB")
    print()
    suffix = ".exe" if sys.platform.startswith("win") else ""
    print("  用法：")
    if "gui" in modes:
        print("    图形版       双击就能用")
    if "cli" in modes:
        print(f"    命令行版     {DIST / ('lanlink-cli' + suffix)} --help")
    if "relay" in modes:
        print(f"    中继         {DIST / ('lanlink-relay' + suffix)} --port 9000")
    print()
    print("  提醒：二进制里已经包含 Python 运行时，目标机器不需要装 Python。")
    if sys.platform.startswith("win"):
        print("        Windows 上首次监听时防火墙会弹窗，要点“允许”。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
