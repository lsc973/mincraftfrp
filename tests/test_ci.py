"""GitHub Actions 工作流和发布打包脚本。

工作流里引用的路径写错了、或者打包脚本坏了，**只有推到 GitHub 上跑一遍
才会暴露** —— 一轮好几分钟，还得到网页上翻日志。所以在本地就拦住。

PyYAML 不是这个项目的依赖（项目本身零第三方依赖），装不上就跳过 YAML
那部分检查，别让跑测试变成必须联网装包。
"""

import subprocess
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

    def test_also_takes_a_relay(self):
        """家里连不进来（CGNAT / 光猫防火墙关不掉）时，中继是唯一走得通的路。

        工作流里没有这个输入的话，那些人就只能本机打包，自动打包对他们没用。
        """
        inputs = self.workflow[True if "on" not in self.workflow else "on"]["workflow_dispatch"]["inputs"]
        self.assertIn("server_relay", inputs)
        self.assertIn("relay_token", inputs)

        script = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--server-relay", script)

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


class TestDeployScript(unittest.TestCase):
    """VPS 一键部署脚本。

    这个脚本只能跑在 Linux 上，而开发机是 Windows —— 所以**它没法在本地
    完整跑一遍**。能测的部分尽量测掉：语法、参数解析、以及几个已知会出事的点。

    没测到的部分（真的在 Ubuntu 上装服务）只能靠第一次部署时现场发现。
    """

    SCRIPT = PROJECT_ROOT / "tools" / "deploy_relay.sh"

    def setUp(self):
        self.text = self.SCRIPT.read_text(encoding="utf-8")

    def test_exists_and_is_bash_syntax_valid(self):
        self.assertTrue(self.SCRIPT.exists(), f"找不到 {self.SCRIPT}")
        rc, _, err = self._run(["-n", str(self.SCRIPT)], timeout=60)
        self.assertEqual(rc, 0, f"bash 语法错误：{err}")

    def _parse_only(self):
        """把参数解析那一段单独拎出来，好在 Windows 上测。

        脚本开头就是 `[ "$(uname -s)" = "Linux" ] || die`，整脚本在这台
        机器上跑不下去。所以只截取到解析完为止。
        """
        cut = self.text.index("----- 前置检查")
        head = self.text[:cut].replace("set -euo pipefail", "")
        return head + '\necho "PORT=[$PORT] TOKEN=[$TOKEN] FORCE=[$FORCE_TOKEN]"\n'

    @classmethod
    def _bash(cls):
        """找到真正的 bash，找不到返回 None。

        坑一：Windows 上 `bash` 可能解析到 System32 里的那个 —— 那是 WSL
        的入口。WSL 没装的话它只打一句"未安装 Linux 发行版"（还是 UTF-16），
        然后所有断言都莫名其妙地失败。**这个坑我踩过**：一开始以为是被测
        脚本坏了，查了半天。

        坑二：Git Bash 的 bash 和 WSL 的 bash 同名，PATH 顺序决定拿到哪个。
        所以这里显式挑，不靠 PATH。
        """
        import os
        import shutil

        candidates = [
            shutil.which("bash"),
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ]
        for path in candidates:
            if path and os.path.exists(path) and "System32" not in path:
                return path
        return None

    def _run(self, args, timeout=20):
        """跑一个命令，返回 (退出码, stdout, stderr)。

        **不用 subprocess.run** —— Python 3.8 上它有个坑：进程"没产生任何
        输出"时 `capture_output=True` 会在内部抛 IndexError（读线程留下的
        数组是空的）。而参数出错时脚本正好是"秒退且无输出"，必踩。
        Popen + communicate 没这个问题。
        """
        bash = self._bash()
        if bash is None:
            self.skipTest("这台机器上没有可用的 bash（Git Bash），跳过")
        proc = subprocess.Popen([bash, *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise AssertionError(f"脚本卡死了（{timeout} 秒没结束）：{args}")
        # 中文在 Windows 上可能是 GBK 出来的，两种都试
        def decode(raw):
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "replace")
        return proc.returncode, decode(out), decode(err)

    def _run_parse(self, *args):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parse.sh"
            path.write_text(self._parse_only(), encoding="utf-8")
            return self._run([str(path), *args])

    def test_defaults(self):
        _, out, _ = self._run_parse("--token", "abc")
        self.assertIn("PORT=[9000]", out)
        self.assertIn("TOKEN=[abc]", out)

    def test_equals_form(self):
        _, out, _ = self._run_parse("--token=xyz", "--port=8080")
        self.assertIn("PORT=[8080]", out)
        self.assertIn("TOKEN=[xyz]", out)

    def test_missing_value_does_not_hang(self):
        """这条是真踩过的：`--token` 写在最后会让 shift 失败，而失败之后
        $# 不变，于是 while 原地转圈 —— 脚本卡死还不报错。

        所以这里必须能跑完（超时会被 subprocess 抛出来，测试就红）。
        """
        for flag in ("--token", "--port", "--repo"):
            rc, out, err = self._run_parse(flag)
            self.assertNotEqual(rc, 0, f"{flag} 缺值应该报错")
            self.assertIn("要跟一个值", out + err)

    def test_unknown_flag_is_rejected(self):
        rc, out, err = self._run_parse("--nope")
        self.assertNotEqual(rc, 0)
        self.assertIn("不认识的参数", out + err)

    def test_refuses_to_run_off_linux(self):
        """在 Windows/macOS 上直接跑应该干净地拒绝，而不是半路出错。"""
        rc, out, err = self._run([str(self.SCRIPT), "--token", "x"], timeout=60)
        self.assertNotEqual(rc, 0)
        self.assertIn("Linux", out + err)

    # ---- 几个改对了才算数的地方 ----

    def test_python_path_is_discovered_not_hardcoded(self):
        """写死 /usr/bin/python3 的话，装在别处的机器上服务起不来，
        而 systemd 报的是 "No such file or directory"，很难看出是解释器的事。
        """
        self.assertIn('PYTHON=$(command -v python3)', self.text)
        self.assertIn("ExecStart=$PYTHON ", self.text)
        self.assertNotIn("ExecStart=/usr/bin/python3", self.text)

    def test_token_goes_through_a_file_not_the_command_line(self):
        """命令行参数会出现在 ps 输出里，同一个机器上谁都能看到。"""
        self.assertIn("--token-file", self.text)
        exec_line = [l for l in self.text.splitlines() if l.startswith("ExecStart=")]
        self.assertTrue(exec_line)
        self.assertNotIn("--token ", exec_line[0])

    def test_warns_about_the_cloud_security_list(self):
        """脚本改不了云控制台那层。不提醒的话，用户会以为装好了，
        然后对着"连不上"查半天防火墙。
        """
        self.assertIn("安全列表", self.text)
        self.assertIn("安全组", self.text)

    def test_keeps_an_existing_token_by_default(self):
        """换了口令而没重新打包的话，对面手上那个 exe 里的口令就对不上，
        他会一直连不上而两边都看不出原因。所以默认不动。
        """
        self.assertIn("force-token", self.text)
        self.assertIn("FORCE_TOKEN", self.text)
