"""把打好的 exe 收拾成一份可以直接发给对面的压缩包。

    python tools/make_release.py --label "给小明的"

产物是 ``release/lanlink-给小明的.zip``，里面除了 exe 还有一份 ``使用说明.txt``。

**为什么要带说明**：对面拿到的是一个 exe，他不知道该点哪儿、该填什么。
一段话就能省掉来回问。说明的内容会根据 exe 里有没有编服务器地址自动变 ——
编了就说"只要填房间号和口令"，没编就得告诉他怎么填地址。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lanlink.link import format_addr  # noqa: E402
from lanlink import server as servercfg  # noqa: E402

__all__ = ["build_instructions", "make_zip", "safe_filename"]


def safe_filename(text: str) -> str:
    """把标签变成能当文件名的东西。

    Windows 文件名不能含 ``\\ / : * ? " < > |``，也不能以点或空格结尾
    （``lanlink-...zip`` 这种在资源管理器里会出问题）。中文和中间的空格没问题。
    """
    cleaned = "".join(ch for ch in (text or "") if ch not in '\\/:*?"<>|')
    cleaned = cleaned.strip().strip(".").strip()
    return cleaned or "lanlink"


def build_instructions(*, server=None, label: str = "") -> str:
    """给对面的使用说明。"""
    lines = ["lanlink 使用说明", "=" * 40, ""]
    if label:
        lines += [f"这份是：{label}", ""]

    if server is not None:
        lines += [
            "你不需要知道任何地址，照下面做就行：",
            "",
            "1. 双击 lanlink.exe",
            "2. 左边点「端口转发隧道」",
            "3. 选「服务在对面（在本机开个端口接过去）」",
            "4. 填两个空：",
            "     房间号   ← 开房的人发给你的一串字符，原样填",
            "     房间密码 ← 同上",
            "   （「本地监听」保持默认的 25565 就行，被占用了才需要改）",
            "5. 点「启动隧道」，看到「运行中」就成了",
            "",
            "然后打开游戏，在「多人游戏 → 直接连接」里填：",
            "",
            "     127.0.0.1:25565",
            "",
            "就等于连到了对方的服务器。",
            "",
        ]
    else:
        lines += [
            "1. 双击 lanlink.exe",
            "2. 左边点「端口转发隧道」",
            "3. 选「服务在对面（在本机开个端口接过去）」",
            "4. 填：",
            "     对方地址 ← 开房的人发给你的（形如 1.2.3.4:50001）",
            "     房间密码 ← 同上",
            "   （「本地监听」保持默认的 25565 就行）",
            "5. 点「启动隧道」，看到「运行中」就成了",
            "",
            "然后打开游戏，在「多人游戏 → 直接连接」里填：",
            "",
            "     127.0.0.1:25565",
            "",
        ]

    lines += [
        "-" * 40,
        "连不上怎么办",
        "-" * 40,
        "",
        "· 提示「房间号不对」—— 找开房的人核对一下，可能是抄错了",
        "· 提示「口令不对」—— 同上",
        "· 一直转圈连不上 —— 把窗口里那几行日志截图发给开房的人，",
        "  问题多半在他那边（防火墙或网络）",
        "",
        "· 第一次运行如果弹「Windows 已保护你的电脑」：",
        "  点「更多信息」→「仍要运行」。这个程序没有买代码签名证书，",
        "  所以 Windows 会拦一下，不是有毒。",
        "",
    ]
    return "\n".join(lines)


def make_zip(dist: Path, out_dir: Path, *, label: str = "",
             with_cli: bool = False, instructions: str = "") -> Path:
    """把 exe 和说明压成一个 zip，返回它的路径。"""
    gui = dist / "lanlink.exe"
    if not gui.exists():
        raise FileNotFoundError(f"找不到 {gui} —— 先跑 python packaging/build.py --mode gui")

    wanted = [gui]
    if with_cli:
        cli = dist / "lanlink-cli.exe"
        if not cli.exists():
            raise FileNotFoundError(f"找不到 {cli}（--with-cli 需要它）")
        wanted.append(cli)

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{safe_filename(label)}" if label else ""
    target = out_dir / f"lanlink{suffix}.zip"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in wanted:
            zf.write(path, path.name)
        zf.writestr("使用说明.txt", instructions or build_instructions())
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="把 exe 收拾成可以直接发给对面的压缩包")
    parser.add_argument("--dist", default=str(PROJECT_ROOT / "dist"), help="exe 所在目录")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "release"), help="zip 放哪儿")
    parser.add_argument("--label", default="", help="这份是给谁的，会写进文件名和说明里")
    parser.add_argument("--with-cli", action="store_true", help="把命令行版也塞进去")
    args = parser.parse_args()

    baked = servercfg.baked()
    try:
        target = make_zip(
            Path(args.dist), Path(args.out), label=args.label,
            with_cli=args.with_cli,
            instructions=build_instructions(server=baked, label=args.label),
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    size = target.stat().st_size / 1024 / 1024
    print(f"打好了：{target}  （{size:.1f} MB）")
    print()
    if baked is not None:
        print(f"这份 exe 里编了服务器地址：{format_addr(*baked)}")
        print("对面拿到之后只要填房间号和口令。")
    else:
        print("注意：这份 exe **没有**编服务器地址，对面得自己填地址。")
        print("  想让对面只填房间号，打包时加 --server 你的地址:端口：")
        print("    python packaging/build.py --mode gui --server yourname.dynv6.net:50001")
    return 0


if __name__ == "__main__":
    sys.exit(main())
