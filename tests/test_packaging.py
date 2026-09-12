"""打包相关的回归测试。

这三样东西出错的后果都是"发出去之后才发现"：
* 中继入口的参数透传 —— 写错了用户 `-v` 就用不了
* pyproject 的 packages —— 漏了子包，装完 import 就炸
* systemd 单元文件 —— 少了 Restart 段服务就不会自愈

所以拿测试钉住，别等打包完才发现。
"""

import ast
import configparser
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_launcher(name: str):
    """把 packaging/ 下的 launcher 当模块加载进来（它不是包的一部分）。"""
    path = ROOT / "packaging" / f"launcher_{name}.py"
    spec = importlib.util.spec_from_file_location(f"_launcher_{name}", path)
    module = importlib.util.module_from_spec(spec)
    # 这些 launcher 顶层会 import lanlink.*，那个能正常导入；
    # 只有跑到 __main__ 才会启服务，所以这里加载是安全的。
    spec.loader.exec_module(module)
    return module


class TestRelayLauncherArgv(unittest.TestCase):
    """lanlink-relay 的参数透传。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_launcher("relay")

    def build(self, argv):
        return self.mod._build_argv(argv)

    def test_empty_becomes_relay(self):
        self.assertEqual(self.build([]), ["relay"])

    def test_plain_flags_get_relay_prepended(self):
        self.assertEqual(self.build(["--port", "9000"]), ["relay", "--port", "9000"])

    def test_global_flag_stays_in_front(self):
        """-v 是顶层标志，必须在子命令前面 —— 不然 argparse 会说无法识别。

        这是真踩过的坑：直接无脑补 "relay" 会得到 relay -v，然后报错。
        """
        self.assertEqual(
            self.build(["-v", "--port", "9000"]), ["-v", "relay", "--port", "9000"]
        )
        self.assertEqual(
            self.build(["--verbose", "--port", "9000"]),
            ["--verbose", "relay", "--port", "9000"],
        )

    def test_multiple_global_flags(self):
        self.assertEqual(self.build(["-v", "--port", "1"]), ["-v", "relay", "--port", "1"])

    def test_explicit_subcommand_untouched(self):
        self.assertEqual(self.build(["relay", "--port", "1"]), ["relay", "--port", "1"])
        self.assertEqual(self.build(["doctor"]), ["doctor"])
        self.assertEqual(self.build(["list"]), ["list"])

    def test_help_gets_relay_help(self):
        self.assertEqual(self.build(["--help"]), ["relay", "--help"])
        self.assertEqual(self.build(["-h"]), ["relay", "-h"])

    def test_unknown_subcommand_is_prepended(self):
        """不是已知子命令就当参数处理，别把用户的输入吃掉。"""
        self.assertEqual(self.build(["foo"]), ["relay", "foo"])

    def test_all_known_subcommands_recognized(self):
        for name in ("host", "list", "join", "relay", "doctor", "gui"):
            with self.subTest(name=name):
                self.assertEqual(self.build([name]), [name])


class TestLaunchersImportable(unittest.TestCase):
    def test_cli_launcher_has_main_callable(self):
        mod = _load_launcher("cli")
        self.assertTrue(callable(mod.main))

    def test_gui_launcher_has_main_callable(self):
        mod = _load_launcher("gui")
        self.assertTrue(callable(mod.main))


class TestPyproject(unittest.TestCase):
    """pyproject 的配置正确性。

    没有 tomllib（Python < 3.11）就退回文本断言 —— 够用了。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_gui_subpackage_is_listed(self):
        """漏了 lanlink.gui 的话 pip 装完 `lanlink gui` 会 ImportError，
        而从源码目录跑却完全正常 —— 特别容易漏过去。"""
        self.assertIn('"lanlink.gui"', self.text)

    def test_console_script_points_at_real_function(self):
        self.assertIn("lanlink.cli:main", self.text)
        from lanlink.cli import main

        self.assertTrue(callable(main))

    def test_declares_no_runtime_dependencies(self):
        """说了零依赖就得是零依赖 —— 加依赖会让 exe 体积和安装门槛都涨。"""
        self.assertIn("dependencies = []", self.text)

    def test_requires_python_matches_tested_range(self):
        self.assertIn('requires-python = ">=3.8"', self.text)

    def test_version_matches_package(self):
        import re

        import lanlink

        match = re.search(r'^version = "([^"]+)"', self.text, re.MULTILINE)
        self.assertIsNotNone(match, "pyproject 里找不到 version")
        self.assertEqual(
            match.group(1), lanlink.__version__,
            "pyproject 的版本和 lanlink.__version__ 对不上",
        )


class TestSystemdUnit(unittest.TestCase):
    """systemd 单元文件的必要配置。

    不做完整校验（那是 systemd-analyze 的事），只盯住几个漏了会出事的点。
    """

    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "packaging" / "systemd" / "lanlink-relay.service"
        cls.text = cls.path.read_text(encoding="utf-8")
        # systemd 单元文件是 INI 风格，但重复键和多值行会让 configparser 犯难，
        # 所以只做文本级检查 + 段存在性检查。
        cls.parser = configparser.ConfigParser(strict=False, allow_no_value=True)
        cls.parser.read_string(cls.text)

    def test_file_exists(self):
        self.assertTrue(self.path.exists())

    def test_has_required_sections(self):
        for section in ("Unit", "Service", "Install"):
            with self.subTest(section=section):
                self.assertIn(section, self.parser.sections())

    def test_exec_start_uses_token_file_not_plain_token(self):
        """口令走命令行会被 ps 看到，生产环境必须用文件。"""
        exec_start = self.parser.get("Service", "ExecStart")
        self.assertIn("--token-file", exec_start)
        self.assertNotIn("--token ", exec_start + " ")

    def test_restarts_on_failure(self):
        self.assertIn("Restart=always", self.text)
        self.assertIn("RestartSec=", self.text)

    def test_config_error_does_not_loop(self):
        """端口占用这类错误重试没用，得让它停下来等人工处理。"""
        self.assertIn("RestartPreventExitStatus=2", self.text)

    def test_raises_fd_limit(self):
        """每客户端一个连接，默认 1024 很快就不够。"""
        self.assertIn("LimitNOFILE=", self.text)

    def test_graceful_stop(self):
        self.assertIn("KillSignal=SIGTERM", self.text)
        self.assertIn("TimeoutStopSec=", self.text)

    def test_runs_as_non_root(self):
        self.assertIn("User=lanlink", self.text)
        self.assertNotIn("User=root", self.text)

    def test_hardening_present(self):
        for directive in ("NoNewPrivileges=true", "ProtectSystem=strict", "PrivateTmp=true"):
            with self.subTest(directive=directive):
                self.assertIn(directive, self.text)

    def test_wanted_by_multi_user(self):
        self.assertIn("WantedBy=multi-user.target", self.text)


class TestBuildScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
        import ast as _ast

        _ast.parse(cls.text)

    def test_build_modes_defined(self):
        for mode in ('"gui"', '"cli"', '"relay"'):
            with self.subTest(mode=mode):
                self.assertIn(mode, self.text)

    def test_gui_guarded_by_platform(self):
        """PyInstaller 不能交叉编译，Linux 上必须跳过图形版而不是报错退出。"""
        self.assertIn("GUI_CAPABLE", self.text)

    def test_excludes_heavy_modules_to_keep_size_down(self):
        self.assertIn("_EXCLUDES", self.text)
        for module in ("numpy", "PyQt5", "matplotlib"):
            self.assertIn(f'"{module}"', self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
