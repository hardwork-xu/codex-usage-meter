#!/usr/bin/env python3
"""User-run installer using Codex's personal marketplace scaffold and CLI."""
from pathlib import Path
import json
import os
import shutil
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
NAME = "codex-usage-meter"


def main():
    skill = Path.home() / ".codex/skills/.system/plugin-creator/scripts"
    scaffold = skill / "create_basic_plugin.py"
    validator = skill / "validate_plugin.py"
    if not scaffold.is_file() or not validator.is_file():
        raise SystemExit("未找到 Codex 官方 plugin-creator 技能。请在 Codex 中用该技能安装此文件夹。")
    validation_env = dict(os.environ)
    validation_env["PYTHONPATH"] = str(ROOT / "vendor")
    candidates = [sys.executable, str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3")]
    validation_python = None
    for candidate in candidates:
        if Path(candidate).is_file() and subprocess.run([candidate, "-c", "import yaml"], env=validation_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            validation_python = candidate
            break
    if validation_python is None:
        raise SystemExit("官方插件验证器需要 PyYAML；请在 Codex 内加载工作区依赖后安装。本程序没有修改个人插件目录。")
    subprocess.run([validation_python, str(validator), str(ROOT)], env=validation_env, check=True)
    target = Path.home() / "plugins" / NAME
    if target.exists():
        raise SystemExit("个人插件目录已有同名文件夹。为保护现有内容，本安装器不覆盖；请在 Codex 中按插件更新流程处理。")
    # Explicitly running this installer requests personal-marketplace installation.
    subprocess.run([sys.executable, str(scaffold), NAME, "--with-marketplace", "--with-skills", "--with-hooks", "--with-mcp"], check=True)
    shutil.copytree(ROOT, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", ".DS_Store", "__pycache__", "*.pyc", "*.zip"))
    # Bind to the actual installed source without relying on an undocumented MCP macro.
    # Both hook and MCP paths use one dedicated, normal local application data directory.
    folder = str(Path.home() / "Library/Application Support/Codex Usage Meter")
    config = {"mcpServers": {"usage-meter": {"command": sys.executable,
        "args": [str(target / "scripts/meter.py"), "--data-dir", folder, "mcp"], "env": {"METER_DATA_DIR": folder}}}}
    (target / ".mcp.json").write_text(json.dumps(config, indent=2) + "\n")
    hooks_path = target / "hooks/hooks.json"
    hooks = json.loads(hooks_path.read_text())
    for groups in hooks["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = (shlex.quote(sys.executable) + ' "${PLUGIN_ROOT}/scripts/meter.py" --data-dir ' + shlex.quote(folder) + " hook")
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n")
    subprocess.run([validation_python, str(validator), str(target)], env=validation_env, check=True)
    name = subprocess.check_output([sys.executable, str(skill / "read_marketplace_name.py")], text=True).strip()
    from meter import codex_binary
    subprocess.run([codex_binary(), "plugin", "add", NAME + "@" + name], check=True)
    print("\n安装命令已完成。请在 Codex 插件页面审阅并信任用量计 Hooks，然后在新任务中说：打开用量计。")
    print("安装器没有修改 Hooks 信任、沙箱或审批设置。")


if __name__ == "__main__":
    main()
