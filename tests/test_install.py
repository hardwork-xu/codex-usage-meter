"""Installer tests: temporary fixtures only, no actual personal installation."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install


class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((install.ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.python = r"C:\Program Files\Python 测试\python.exe"
        self.target = PureWindowsPath(r"C:\Users\测试 User\plugins\codex-usage-meter")
        self.data = PureWindowsPath(r"C:\Users\测试 User\AppData\Local\Codex Usage Meter")

    def handlers(self, config):
        return [handler for groups in config["hooks"].values() for group in groups for handler in group["hooks"]]

    def test_windows_mcp_paths_remain_separate_arguments_and_utf8(self):
        server = install.build_mcp_config(self.target, self.python, self.data)["mcpServers"]["usage-meter"]
        self.assertEqual(server["command"], self.python)
        self.assertEqual(server["args"], ["-X", "utf8", str(self.target / "scripts" / "meter.py"), "--data-dir", str(self.data), "mcp"])
        self.assertEqual(server["env"], {"METER_DATA_DIR": str(self.data), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        self.assertNotIn('"', server["command"])

    def test_windows_override_quotes_special_paths_and_preserves_events(self):
        python = "C:\\Program Files\\O'Brien $tools\\python.exe"
        data = PureWindowsPath("C:\\Users\\测试 O'Brien\\AppData\\Local\\Meter & Data")
        original = json.loads(json.dumps(self.template))
        result = install.build_hook_config(self.template, python, data, windows=True)
        self.assertEqual(self.template, original)
        self.assertEqual(set(result["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "Interrupt"})
        for before, after in zip(self.handlers(original), self.handlers(result)):
            command = after["commandWindows"]
            self.assertTrue(command.startswith("& 'C:\\Program Files\\O''Brien $tools\\python.exe' -X utf8 "))
            self.assertIn("(Join-Path -Path $env:PLUGIN_ROOT -ChildPath 'scripts/meter.py')", command)
            self.assertIn("--data-dir 'C:\\Users\\测试 O''Brien\\AppData\\Local\\Meter & Data'", command)
            self.assertTrue(command.endswith("hook; exit $LASTEXITCODE"))
            self.assertEqual(after["command"], before["command"])
            self.assertEqual(after["async"], before["async"])
            self.assertEqual(after["timeout"], before["timeout"])
            self.assertNotIn("ExecutionPolicy", command)

    def test_posix_hook_preserves_spaces_and_windows_override(self):
        python = "/opt/Python Tools/python3"
        data = Path("/tmp/测试 User/Meter's data")
        result = install.build_hook_config(self.template, python, data, windows=False)
        for before, after in zip(self.handlers(self.template), self.handlers(result)):
            arguments = shlex.split(after["command"])
            self.assertEqual(arguments, [python, "-X", "utf8", "${PLUGIN_ROOT}/scripts/meter.py", "--data-dir", str(data), "hook"])
            self.assertEqual(after["commandWindows"], before["commandWindows"])

    def test_source_mcp_stays_portable_and_json_is_utf8(self):
        self.assertEqual(json.loads((install.ROOT / ".mcp.json").read_text(encoding="utf-8")), {"mcpServers": {}})
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "config.json"
            config = install.build_mcp_config(self.target, self.python, self.data)
            install.write_json(output, config)
            raw = output.read_bytes()
            self.assertIn("测试".encode("utf-8"), raw)
            self.assertEqual(json.loads(raw.decode("utf-8")), config)

    @unittest.skipUnless(os.name == "nt", "PowerShell syntax validation runs on Windows CI")
    def test_windows_powershell_parser_accepts_generated_command(self):
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        self.assertIsNotNone(powershell, "Windows CI must provide a PowerShell parser")
        command = install.windows_hook_command("C:\\Program Files\\O'Brien 测试\\python.exe", self.data)
        parser = (
            "$meterTokens=$null; $meterErrors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseInput("
            "$env:METER_TEST_COMMAND,[ref]$meterTokens,[ref]$meterErrors) | Out-Null; "
            "if ($meterErrors.Count) { $meterErrors | Out-String | Write-Error; exit 1 }; exit 0"
        )
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", parser],
                                env=dict(os.environ, METER_TEST_COMMAND=command),
                                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "Native PowerShell hook execution runs on Windows CI")
    def test_windows_hook_executes_with_utf8_stdin_and_unicode_paths(self):
        shells = list(dict.fromkeys(path for name in ("pwsh.exe", "powershell.exe")
                                    if (path := shutil.which(name))))
        self.assertTrue(shells, "Windows CI must provide PowerShell")
        payload = '{"event":"测试","text":"空格 & O\'Brien $data !"}\n'.encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "插件 测试 & O'Brien $tools!"
            scripts = plugin / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "meter.py").write_text(
                "import json, os, pathlib, sys\n"
                "record = {'argv': sys.argv[1:], 'stdinHex': sys.stdin.buffer.read().hex(), "
                "'utf8Mode': sys.flags.utf8_mode}\n"
                "pathlib.Path(os.environ['METER_TEST_RECORD']).write_text("
                "json.dumps(record, ensure_ascii=False), encoding='utf-8')\n"
                "sys.exit(int(os.environ['METER_TEST_EXIT']))\n", encoding="utf-8")
            data = Path(temporary) / "数据 & O'Brien $state!"
            record_path = Path(temporary) / "记录.json"
            command = install.windows_hook_command(sys.executable, data)
            for shell in shells:
                for expected_exit in (0, 23):
                    with self.subTest(shell=Path(shell).name, exit=expected_exit):
                        record_path.unlink(missing_ok=True)
                        env = dict(install.utf8_environment(), PLUGIN_ROOT=str(plugin),
                                   METER_TEST_RECORD=str(record_path), METER_TEST_EXIT=str(expected_exit))
                        result = subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-Command", command],
                            input=payload, capture_output=True, env=env, timeout=20, check=False)
                        self.assertEqual(result.returncode, expected_exit,
                                         result.stderr.decode("utf-8", errors="replace"))
                        record = json.loads(record_path.read_text(encoding="utf-8"))
                        self.assertEqual(record["argv"], ["--data-dir", str(data), "hook"])
                        self.assertEqual(record["stdinHex"], payload.hex())
                        self.assertEqual(record["utf8Mode"], 1)
            self.assertFalse(data.exists(), "The synthetic hook must not create runtime data")


class InstallerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "测试 User"
        self.source = self.base / "source" / install.NAME
        self.skill = self.home / ".codex" / "skills" / ".system" / "plugin-creator" / "scripts"
        self.skill.mkdir(parents=True)
        for name in ("create_basic_plugin.py", "validate_plugin.py", "read_marketplace_name.py"):
            (self.skill / name).write_text("# mocked official helper\n", encoding="utf-8")
        (self.source / "hooks").mkdir(parents=True)
        (self.source / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
        hooks = json.loads((install.ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        install.write_json(self.source / "hooks" / "hooks.json", hooks)
        self.target = self.home / "plugins" / install.NAME
        self.data = self.home / "AppData" / "Local" / "Codex Usage Meter"
        self.calls = []
        self.patches = [
            mock.patch.object(install, "ROOT", self.source),
            mock.patch.object(install.Path, "home", return_value=self.home),
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}),
            mock.patch.object(install, "validation_interpreter", return_value=sys.executable),
            mock.patch.object(install, "codex_command", return_value=["node.exe", "official-codex.js"]),
            mock.patch.object(install, "default_data_dir", return_value=self.data),
            mock.patch.object(install.subprocess, "run", side_effect=self.fake_run),
            mock.patch.object(install.subprocess, "check_output", return_value="personal\n"),
        ]
        self.mocks = [patch.start() for patch in self.patches]
        for patch in reversed(self.patches):
            self.addCleanup(patch.stop)

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if any(str(argument).endswith("create_basic_plugin.py") for argument in command):
            self.target.mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0)

    def test_official_scaffold_validator_cli_and_manual_trust_only(self):
        original_source = (self.source / ".mcp.json").read_bytes()
        messages = io.StringIO()
        with redirect_stdout(messages):
            install.main()
        commands = [command for command, _ in self.calls]
        scaffold = next(command for command in commands if any(str(part).endswith("create_basic_plugin.py") for part in command))
        self.assertIn("--with-marketplace", scaffold)
        self.assertEqual(scaffold[scaffold.index("--path") + 1], str(self.target.parent))
        self.assertNotIn("--force", scaffold)
        validators = [command for command in commands if any(str(part).endswith("validate_plugin.py") for part in command)]
        self.assertEqual([command[-1] for command in validators], [str(self.source), str(self.target)])
        self.assertEqual(commands[0], ["node.exe", "official-codex.js", "--version"])
        self.assertEqual(commands[-1], ["node.exe", "official-codex.js", "plugin", "add", "codex-usage-meter@personal"])
        self.assertTrue(all(kwargs["env"]["PYTHONUTF8"] == "1" for _, kwargs in self.calls))
        self.assertEqual((self.source / ".mcp.json").read_bytes(), original_source)
        installed = json.loads((self.target / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["mcpServers"]["usage-meter"]["args"][-3:], ["--data-dir", str(self.data), "mcp"])
        # The mocked helper writes no marketplace: installer must not create one itself.
        self.assertFalse((self.home / ".agents").exists())
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        self.assertIn("/hooks", messages.getvalue())
        self.assertIn("新任务", messages.getvalue())
        self.assertNotIn("trust", [str(part).lower() for command in commands for part in command])

    def test_existing_installation_is_not_overwritten(self):
        self.target.mkdir(parents=True)
        marker = self.target / "user-file.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "不覆盖"):
            install.main()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.calls, [])

    def test_missing_cli_stops_before_personal_files_are_created(self):
        self.mocks[4].side_effect = FileNotFoundError("Codex not found")
        with self.assertRaisesRegex(SystemExit, "Codex CLI"):
            install.main()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.calls, [])

    def test_invalid_marketplace_name_does_not_execute_plugin_add(self):
        self.mocks[7].return_value = "personal; unexpected-command\n"
        with self.assertRaisesRegex(SystemExit, "有效的个人市场名称"):
            install.main()
        self.assertFalse(any("add" in command for command, _ in self.calls))

    def test_missing_official_helper_fails_before_install(self):
        (self.skill / "read_marketplace_name.py").unlink()
        with self.assertRaisesRegex(SystemExit, "plugin-creator"):
            install.main()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.calls, [])


class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_selects_existing_python_without_download_or_policy_changes(self):
        launcher = (install.ROOT / "install-windows.cmd").read_text(encoding="ascii")
        self.assertIn("DisableDelayedExpansion", launcher)
        self.assertIn("where py", launcher)
        self.assertIn("where python", launcher)
        self.assertIn("sys.version_info >= (3, 10)", launcher)
        self.assertIn('"%~dp0scripts\\install.py"', launcher)
        for forbidden in ("ExecutionPolicy", "Invoke-WebRequest", "curl ", "Start-Process", "runas"):
            self.assertNotIn(forbidden.lower(), launcher.lower())

    @unittest.skipUnless(os.name == "nt", "Native batch launcher execution runs on Windows CI")
    def test_launcher_dispatches_synthetic_installer_and_preserves_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "安装 测试 & O'Brien!"
            scripts = source / "scripts"
            scripts.mkdir(parents=True)
            launcher = source / "install-windows.cmd"
            shutil.copyfile(install.ROOT / "install-windows.cmd", launcher)
            (scripts / "install.py").write_text(
                "import json, os, pathlib, sys\n"
                "record = {'script': str(pathlib.Path(__file__).resolve()), "
                "'utf8Mode': sys.flags.utf8_mode, 'version': list(sys.version_info[:2])}\n"
                "pathlib.Path(os.environ['METER_TEST_RECORD']).write_text("
                "json.dumps(record, ensure_ascii=False), encoding='utf-8')\n"
                "sys.exit(int(os.environ['METER_TEST_EXIT']))\n", encoding="utf-8")
            record_path = Path(temporary) / "启动记录.json"
            comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
            self.assertIsNotNone(comspec, "Windows CI must provide cmd.exe")
            # cmd /s removes the outer quote pair; the remaining pair keeps the path one command.
            command = f'"{comspec}" /d /s /c ""{launcher}""'
            for expected_exit in (0, 23):
                with self.subTest(exit=expected_exit):
                    record_path.unlink(missing_ok=True)
                    env = dict(install.utf8_environment(), CI="1", METER_TEST_RECORD=str(record_path),
                               METER_TEST_EXIT=str(expected_exit))
                    result = subprocess.run(command, executable=comspec, cwd=temporary,
                                            env=env, capture_output=True, timeout=30, check=False)
                    self.assertEqual(result.returncode, expected_exit,
                                     result.stderr.decode("utf-8", errors="replace"))
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    self.assertEqual(record["script"], str((scripts / "install.py").resolve()))
                    self.assertEqual(record["utf8Mode"], 1)
                    self.assertGreaterEqual(tuple(record["version"]), (3, 10))


if __name__ == "__main__":
    unittest.main()
