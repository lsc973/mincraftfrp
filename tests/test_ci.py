"""GitHub Actions 工作流和发布打包脚本。

工作流里引用的路径写错了、或者打包脚本坏了，**只有推到 GitHub 上跑一遍
才会暴露** —— 一轮好几分钟，还得到网页上翻日志。所以在本地就拦住。

PyYAML 不是这个项目的依赖（项目本身零第三方依赖），装不上就跳过 YAML
那部分检查，别让跑测试变成必须联网装包。
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.make_release import build_instructions, make_zip, safe_filename  # noqa: E402

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "build.yml"


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("没装 PyYAML（项目本身不依赖它），跳过 YAML 检查")
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_file_exists(self):
        self.assertTrue(WORKFLOW.exists(), f"找不到 {WORKFLOW}")

    def test_runs_on_windows(self):
        """PyInstaller 不能交叉编译，Linux 上打不出 exe。"""
        self.assertEqual(self.workflow["jobs"]["build"]["runs-on"], "windows-latest")

    def test_can_be_started_by_hand(self):
        triggers = self.workflow.get("on") or self.workflow.get(True)
        self.assertIn("workflow_dispatch", triggers,
                      "没有 workflow_dispatch 的话只能靠推标签出包")

    def test_takes_a_server_address(self):
        """填了地址对面才只要房间号和口令 —— 这是这个工作流的主要用途。"""
        inputs = self.workflow[True if "on" not in self.workflow else "on"]["workflow_dispatch"]["inputs"]
        self.assertIn("server", inputs)

    def test_referenced_scripts_exist(self):
        """工作流里写到的每个路径都得真的在仓库里。

        写错一个字母，就得推上去跑几分钟才发现，然后翻日志找。
        """
        script = WORKFLOW.read_text(encoding="utf-8")
        for name in ("packaging/build.py", "tools/exe_smoke_test.py",
                     "tools/make_release.py"):
            self.assertIn(name, script, f"工作流里没提到 {name}？")
            self.assertTrue((PROJECT_ROOT / name).exists(),
                            f"工作流引用了 {name}，但仓库里没有")

    def test_graphical_smoke_test_does_not_block_the_build(self):
        """CI 机器没有交互式桌面，"窗口标题"那类检查天生不稳。

        所以图形版验证允许失败（但要在日志里看得见），不能因为它挂掉
        就不出包 —— 否则每次都得手动重跑。
        """
        steps = self.workflow["jobs"]["build"]["steps"]
        gui_steps = [s for s in steps
                     if "exe_smoke_test" in str(s.get("run", ""))
                     and "--skip-gui" not in str(s.get("run", ""))]
        self.assertTrue(gui_steps, "找不到图形版验证那一步")
        self.assertTrue(all(s.get("continue-on-error") for s in gui_steps),
                        "图形版验证不该卡住整个构建")

    def test_uploads_an_artifact(self):
        uses = [s.get("uses", "") for s in self.workflow["jobs"]["build"]["steps"]]
        self.assertTrue(any("upload-artifact" in u for u in uses),
                        "没有上传产物的话跑完什么也拿不到")


class TestInstructions(unittest.TestCase):
    """给对面那份说明。

    说明错了对面就卡在第一步，而你还得再解释一遍 —— 这正是要避免的。
    """

    def test_with_builtin_address_says_room_number_only(self):
        text = build_instructions(server=("me.dynv6.net", 50001), label="给小明的")
        self.assertIn("房间号", text)
        self.assertIn("房间密码", text)
        self.assertIn("不需要知道任何地址", text)
        self.assertNotIn("对方地址", text)

    def test_without_a_server_explains_where_to_put_the_address(self):
        text = build_instructions(server=None)
        self.assertIn("对方地址", text)

    def test_always_tells_them_the_game_address(self):
        for server in (None, ("a.b", 1)):
            text = build_instructions(server=server)
            self.assertIn("127.0.0.1:25565", text)

    def test_warns_about_smartscreen(self):
        """没签名的 exe 首次运行会被 Windows 拦，不说明的话对面会以为有毒。"""
        self.assertIn("更多信息", build_instructions(server=("a.b", 1)))

    def test_mentions_the_label(self):
        self.assertIn("给小明的", build_instructions(server=("a.b", 1), label="给小明的"))


class TestSafeFilename(unittest.TestCase):
    def test_keeps_chinese(self):
        self.assertEqual(safe_filename("给小明的"), "给小明的")

    def test_strips_path_separators(self):
        """标签里混进路径分隔符的话，zip 可能被写到别的目录去。"""
        self.assertNotIn("/", safe_filename("a/b"))
        self.assertNotIn("\\", safe_filename("a\\b"))

    def test_strips_windows_forbidden_characters(self):
        # 注意别写成 for ch in ': * ? " < > |' —— 那样会把空格也当成禁用字符
        for ch in list(':*?"<>|'):
            self.assertNotIn(ch, safe_filename(f"a{ch}b"))

    def test_keeps_spaces(self):
        self.assertEqual(safe_filename("给 小明 的"), "给 小明 的")

    def test_does_not_end_with_a_dot_or_space(self):
        """Windows 上以点或空格结尾的文件名会出问题。"""
        for bad in ("..", "名字.", "名字 "):
            cleaned = safe_filename(bad)
            self.assertFalse(cleaned.endswith((".", " ")), f"{bad!r} -> {cleaned!r}")

    def test_empty_falls_back(self):
        for bad in ("", "   ", "...", '\\/:*?"<>|'):
            self.assertEqual(safe_filename(bad), "lanlink")


class TestMakeZip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.dist = self.dir / "dist"
        self.dist.mkdir()
        # 假的 exe，内容无所谓 —— 这里只验证打包逻辑
        (self.dist / "lanlink.exe").write_bytes(b"fake-gui-exe")
        (self.dist / "lanlink-cli.exe").write_bytes(b"fake-cli-exe")

    def tearDown(self):
        self._tmp.cleanup()

    def test_puts_the_exe_and_the_instructions_in(self):
        target = make_zip(self.dist, self.dir / "release", label="给小明的",
                          instructions="说明书正文")
        names = zipfile.ZipFile(target).namelist()
        self.assertIn("lanlink.exe", names)
        self.assertIn("使用说明.txt", names)
        self.assertEqual(
            zipfile.ZipFile(target).read("使用说明.txt").decode("utf-8"), "说明书正文")

    def test_names_the_file_after_the_label(self):
        target = make_zip(self.dist, self.dir / "release", label="给小明的")
        self.assertIn("给小明的", target.name)

    def test_without_a_label(self):
        target = make_zip(self.dist, self.dir / "release")
        self.assertEqual(target.name, "lanlink.zip")

    def test_cli_is_opt_in(self):
        plain = make_zip(self.dist, self.dir / "out1")
        self.assertNotIn("lanlink-cli.exe", zipfile.ZipFile(plain).namelist())

        both = make_zip(self.dist, self.dir / "out2", with_cli=True)
        self.assertIn("lanlink-cli.exe", zipfile.ZipFile(both).namelist())

    def test_creates_the_output_directory(self):
        target = make_zip(self.dist, self.dir / "深" / "更深")
        self.assertTrue(target.exists())

    def test_missing_exe_explains_what_to_do(self):
        (self.dist / "lanlink.exe").unlink()
        with self.assertRaises(FileNotFoundError) as ctx:
            make_zip(self.dist, self.dir / "release")
        self.assertIn("build.py", str(ctx.exception))



class TestWorksWithoutTkinter(unittest.TestCase):
    """没装 tkinter 的机器上，测试要干净地跳过而不是报一堆加载失败。

    tkinter 是**可选**的：命令行版和中继在没有它的机器上照样跑（比如专门
    跑中继的 Linux 服务器，或者 CI 里那个 Python 恰好没带 tcl/tk）。
    那种情况下 unittest 该说"跳过"，不该甩出满屏 collection error ——
    后者会让人以为是代码坏了，去查半天。
    """

    #: 在子进程里把 tkinter 屏蔽掉，模拟一台没装的机器。
    #:
    #: 必须实现 ``find_spec`` —— 老的 ``find_module`` 在 Python 3.12 起
    #: 导入系统就不调了，写了也不生效。那种"静默不生效"最坑：测试照样绿，
    #: 但其实什么都没屏蔽，真去开了几分钟窗口。
    RUNNER = """
import sys, unittest
from importlib.abc import MetaPathFinder

class Blocker(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("simulated: no tkinter on this machine")
        return None

sys.meta_path.insert(0, Blocker())
sys.path.insert(0, "tests")
sys.path.insert(0, ".")

suite = unittest.TestSuite()
loader = unittest.TestLoader()
for name in ("test_gui", "test_gui_layout", "test_gui_tunnel", "test_packaging"):
    suite.addTests(loader.loadTestsFromName(name))
result = unittest.TextTestRunner(verbosity=0).run(suite)
print("RESULT", len(result.errors), len(result.failures), result.testsRun)
sys.exit(1 if (result.errors or result.failures) else 0)
"""

    def test_gui_modules_skip_instead_of_erroring(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-c", self.RUNNER],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        combined = proc.stdout + (proc.stderr or "")
        self.assertNotIn("ModuleNotFoundError", combined)
        self.assertIn("RESULT 0 0", combined,
                      "没有 tkinter 时不该有错误或失败：\n" + combined[-1500:])
        self.assertEqual(proc.returncode, 0, combined[-800:])

    def test_gui_helpers_import_survives_a_missing_tkinter(self):
        """导不进 tkinter 时 gui_helpers 本身也不能崩 —— 它提供跳过机制。"""
        text = (PROJECT_ROOT / "tests" / "gui_helpers.py").read_text(encoding="utf-8")
        self.assertIn("HAS_TK", text)
        self.assertIn("def require_tk", text)

if __name__ == "__main__":
    unittest.main()
