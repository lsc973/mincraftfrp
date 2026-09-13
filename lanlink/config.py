"""lanlink 的配置文件。

放在用户目录下，需要持久化的设置都存这儿。目前有两段：

``ddns``
    免费动态域名的服务商/域名/令牌
``server``
    默认服务器地址 —— 也就是"对面要连哪儿"

单独拎成一个模块，是因为好几处都要读写同一个文件。各写一份的话迟早会
互相覆盖：A 模块读出来、改一个键、写回去，正好把 B 模块刚存的东西冲掉。

位置::

    Windows:  %APPDATA%\\lanlink\\config.json
    其他:     $XDG_CONFIG_HOME/lanlink/config.json 或 ~/.config/lanlink/config.json

``LANLINK_CONFIG_DIR`` 可以覆盖整个目录（测试要用）。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "config_dir",
    "config_path",
    "read",
    "write",
    "get_section",
    "set_section",
    "delete_section",
]


def config_dir() -> Path:
    override = os.environ.get("LANLINK_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "lanlink"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lanlink"


def config_path() -> Path:
    return config_dir() / "config.json"


def read() -> dict:
    """读出整个配置。文件不在、读不动、内容不是 JSON 对象 —— 都返回空字典。

    配置坏了不该让程序起不来，最差就当没配过。
    """
    try:
        raw = config_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write(data: dict) -> Path:
    """整体写回。调用方一般该用 set_section，别直接用它。"""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict(path)
    return path


def _restrict(path: Path) -> None:
    """尽量把权限收到只有自己能读写。

    文件里可能有 DDNS 令牌这类等同于写权限的东西。Windows 上 chmod 基本
    没用（靠用户目录本身的 ACL），所以失败就算了。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_section(name: str) -> Optional[dict]:
    section = read().get(name)
    return section if isinstance(section, dict) else None


def set_section(name: str, value: Optional[dict]) -> Path:
    """写入一段配置。``value`` 为 None 表示删掉这一段。

    只动这一段，其他段原样保留 —— 这正是要把配置读写集中到这里的理由。
    """
    data = read()
    if value is None:
        data.pop(name, None)
    else:
        data[name] = value
    return write(data)


def delete_section(name: str) -> bool:
    """删掉一段。本来就没有则返回 False。"""
    if name not in read():
        return False
    set_section(name, None)
    return True
