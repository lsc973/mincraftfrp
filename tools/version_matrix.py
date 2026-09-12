"""在多个 Python 版本上跑全量测试，输出兼容性矩阵。

用 uv 管理解释器，不需要往系统里装任何东西::

    uv python install 3.9 3.10 3.11 3.12 3.13
    python tools/version_matrix.py

已经装好的解释器会被自动发现；系统自带的那个（当前正在跑脚本的这个）
也会一并纳入。加 ``--python`` 可以手动指定额外的解释器路径。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_interpreters(extra: list) -> "dict[str, str]":
    """找出所有值得测的解释器，返回 {版本标签: 可执行文件路径}。"""
    found: "dict[str, str]" = {}

    # 当前解释器总是要测的
    found[_version_label(sys.executable)] = sys.executable

    uv = shutil.which("uv")
    if uv:
        try:
            proc = subprocess.run(
                [uv, "python", "list", "--only-installed", "--output-format", "json"],
                capture_output=True, timeout=60, check=False,
                # 必须显式指定 utf-8：uv 输出是 UTF-8，而 subprocess 在中文
                # Windows 上默认按 GBK 解码，会直接抛 UnicodeDecodeError
                encoding="utf-8", errors="replace",
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                # 千万别静默跳过 —— 那样会只测到一个解释器却报告"全部通过"
                detail = (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
                print(
                    f"uv 没能列出已安装的解释器，只能测当前这一个：\n  {detail}\n"
                    "  （uv 需要可写的缓存目录；可以设 UV_CACHE_DIR 指向一个可写位置重试）",
                    file=sys.stderr,
                )
            else:
                for item in json.loads(raw):
                    path = item.get("path")
                    if not path or not os.path.exists(path):
                        continue
                    # freethreaded 构建行为差异较大，先不混进来
                    if "freethreaded" in str(item.get("key", "")):
                        continue
                    found.setdefault(_version_label(path), path)
        except (ValueError, subprocess.SubprocessError, OSError) as exc:
            print(f"uv 解释器枚举出错，只能测当前这一个：{exc}", file=sys.stderr)

    for path in extra:
        if os.path.exists(path):
            found[_version_label(path)] = path
        else:
            print(f"（跳过不存在的解释器：{path}）", file=sys.stderr)

    return found


def _version_label(exe: str) -> str:
    """问一下这个解释器是哪个版本。"""
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, timeout=30, check=False,
            encoding="utf-8", errors="replace",
        ).stdout.strip()
        return out or exe
    except (OSError, subprocess.SubprocessError):
        return exe


def _sort_key(label: str):
    parts = []
    for chunk in label.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return parts + [0] * (3 - len(parts))


def main() -> int:
    parser = argparse.ArgumentParser(description="跨 Python 版本跑测试")
    parser.add_argument("--python", action="append", default=[], help="额外的解释器路径，可重复")
    parser.add_argument("--pattern", default="tests", help="测试目录")
    parser.add_argument("--timeout", type=float, default=900.0, help="单个版本超时（秒）")
    args = parser.parse_args()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    interpreters = find_interpreters(args.python)
    if not interpreters:
        print("没找到任何解释器", file=sys.stderr)
        return 1

    print(f"将在 {len(interpreters)} 个解释器上跑 {args.pattern}/ 下的测试\n")
    results = []
    for label in sorted(interpreters, key=_sort_key):
        exe = interpreters[label]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [exe, "-m", "unittest", "discover", "-s", args.pattern],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, cwd=str(PROJECT_ROOT), timeout=args.timeout, check=False,
            )
        except subprocess.SubprocessError as exc:
            results.append((label, False, f"跑不起来：{exc}"))
            print(f"{label:>9}  错误    {exc}")
            continue

        elapsed = time.monotonic() - start
        lines = (proc.stderr or "").strip().splitlines()
        summary = next((l for l in reversed(lines) if l.startswith("Ran ")), "?")
        ok = proc.returncode == 0
        results.append((label, ok, summary))
        print(f"{label:>9}  {'通过' if ok else '失败':<5} {summary:<30} ({elapsed:.1f}s)")
        if not ok:
            for line in lines[-15:]:
                print("           " + line)

    failed = [r for r in results if not r[1]]
    print()
    if failed:
        print(f"{len(failed)}/{len(results)} 个版本失败：" + "、".join(r[0] for r in failed))
        return 1
    print(f"全部 {len(results)} 个版本通过：" + "、".join(r[0] for r in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
