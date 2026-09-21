#!/usr/bin/env python3
"""Install through the official scaffold, validator, and Codex CLI.

Never writes hook trust or changes sandbox/approval configuration.
Windows hooks target the native PowerShell agent; WSL uses its own install.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePath
import re
import shlex
import shutil
import subprocess
import sys

if sys.version_info < (3, 10):
    raise SystemExit("用量计需要 Python 3.10 或更新版本；尚未修改个人插件目录。")

from platform_support import codex_command, default_data_dir

ROOT = Path(__file__).resolve().parent.parent
NAME = "codex-usage-meter"


def utf8_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def powershell_literal(value: str) -> str:
    """Keep whitespace, Unicode, dollar signs, and apostrophes as literal data."""
    return "'" + value.replace("'", "''") + "'"


def windows_hook_command(python: str, data_dir: PurePath) -> str:
    return (
        "& " + powershell_literal(python)
        + " -X utf8 (Join-Path -Path $env:PLUGIN_ROOT -ChildPath 'scripts/meter.py')"
        + " --data-dir " + powershell_literal(str(data_dir))
        + " hook; exit $LASTEXITCODE"
    )


def posix_hook_command(python: str, data_dir: PurePath) -> str:
    return (
        shlex.quote(python)
        + ' -X utf8 "${PLUGIN_ROOT}/scripts/meter.py" --data-dir '
        + shlex.quote(str(data_dir)) + " hook"
    )


def build_mcp_config(target: PurePath, python: str, data_dir: PurePath) -> dict:
    folder = str(data_dir)
    return {"mcpServers": {"usage-meter": {
        "command": python,
        "args": ["-X", "utf8", str(target / "scripts" / "meter.py"),
                 "--data-dir", folder, "mcp"],
        "env": {"METER_DATA_DIR": folder, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }}}


def build_hook_config(source: dict, python: str, data_dir: PurePath, *, windows: bool) -> dict:
    hooks = copy.deepcopy(source)
    for groups in hooks["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                if handler.get("type") != "command":
                    continue
                if windows:
                    handler["commandWindows"] = windows_hook_command(python, data_dir)
                else:
                    handler["command"] = posix_hook_command(python, data_dir)
    return hooks


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_plugin_source(source: Path, target: Path) -> None:
    """Copy distributable components only, never the surrounding checkout data."""
    files = []
    for name in (".codex-plugin", ".mcp.json", "hooks", "scripts", "skills", "web", "vendor",
                 "README.md", "LICENSE", "docs", "PUBLIC_RELEASE_CHECKS.md"):
        component = source / name
        if component.is_symlink():
            raise RuntimeError("插件源码中包含符号链接，未复制")
        if not component.exists():
            continue
        candidates = [component, *component.rglob("*")] if component.is_dir() else [component]
        for path in candidates:
            if path.is_symlink():
                raise RuntimeError("插件源码中包含符号链接，未复制")
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if path.is_file():
                files.append(path)
            elif not path.is_dir():
                raise RuntimeError("插件源码中包含非普通文件，未复制")
    for path in files:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def official_skill_scripts() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    scripts = codex_home / "skills" / ".system" / "plugin-creator" / "scripts"
    required = ("create_basic_plugin.py", "validate_plugin.py", "read_marketplace_name.py")
    if not all((scripts / name).is_file() for name in required):
        raise SystemExit(
            "未找到完整的 Codex 官方 plugin-creator 技能。请在 Codex 中使用 $plugin-creator "
            "安装此源码文件夹；尚未修改个人插件目录。"
        )
    return scripts


def validation_interpreter(env: dict[str, str]) -> str:
    candidates = [sys.executable]
    if sys.platform == "darwin":
        candidates.append(str(Path.home() / ".cache" / "codex-runtimes" /
                              "codex-primary-runtime" / "dependencies" / "python" / "bin" / "python3"))
    for candidate in dict.fromkeys(candidates):
        if Path(candidate).is_file() and subprocess.run(
            [candidate, "-X", "utf8", "-c", "import yaml"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0:
            return candidate
    raise SystemExit(
        "官方插件验证器无法加载 PyYAML。请确认下载了含 vendor 文件夹的完整源码包，"
        "或在 Codex 中加载工作区依赖；尚未修改个人插件目录。"
    )


def main() -> None:
    skill = official_skill_scripts()
    target = Path.home() / "plugins" / NAME
    if target.exists():
        raise SystemExit(
            "个人插件目录已有同名文件夹。为保护现有内容，本安装器不覆盖；"
            "请在 Codex 中按官方插件更新流程处理。"
        )
    env = utf8_environment()
    validation_env = dict(env, PYTHONPATH=str(ROOT / "vendor"))
    validation_python = validation_interpreter(validation_env)
    validator = skill / "validate_plugin.py"
    # Resolve the CLI before creating any personal-marketplace entry.
    try:
        cli = codex_command()
    except (FileNotFoundError, RuntimeError) as exc:
        raise SystemExit("未找到可运行的 Codex CLI；请先确认 codex --version 可用。" + str(exc)) from exc
    if not cli:
        raise SystemExit("未找到可运行的 Codex CLI；尚未修改个人插件目录。")
    subprocess.run([*cli, "--version"], env=env, check=True)
    subprocess.run([validation_python, "-X", "utf8", str(validator), str(ROOT)],
                   env=validation_env, check=True)

    # Only the official helper creates/updates the default personal marketplace.
    subprocess.run([
        sys.executable, "-X", "utf8", str(skill / "create_basic_plugin.py"), NAME,
        "--path", str(target.parent), "--with-marketplace", "--with-skills", "--with-hooks", "--with-mcp",
    ], env=env, check=True)
    copy_plugin_source(ROOT, target)

    # Materialize paths only in the installed copy. Public .mcp.json stays empty.
    data_dir = default_data_dir()
    write_json(target / ".mcp.json", build_mcp_config(target, sys.executable, data_dir))
    hooks_path = target / "hooks" / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    write_json(hooks_path, build_hook_config(hooks, sys.executable, data_dir, windows=os.name == "nt"))
    subprocess.run([validation_python, "-X", "utf8", str(validator), str(target)],
                   env=validation_env, check=True)
    marketplace = subprocess.check_output(
        [sys.executable, "-X", "utf8", str(skill / "read_marketplace_name.py")],
        env=env, text=True, encoding="utf-8",
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", marketplace):
        raise SystemExit("官方辅助程序未返回有效的个人市场名称；已停止，未执行插件添加命令。")
    subprocess.run([*cli, "plugin", "add", NAME + "@" + marketplace], env=env, check=True)
    print("\n安装命令已完成。请在 Codex 中通过 /hooks 审阅并信任用量计的五项 Hooks。")
    print("仅在待审阅列表全部属于该插件时，才按界面提示使用 t；然后在新任务中说：打开用量计。")
    print("安装器没有修改 Hooks 信任、沙箱、审批或 PowerShell 执行策略。")


if __name__ == "__main__":
    main()
