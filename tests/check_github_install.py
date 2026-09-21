"""Real official-CLI installation check, restricted to disposable GitHub runners.

No login, model request, hook trust change, or local browser service is needed.
The runner owns its normal home directory; this check never rewrites HOME.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("RUNNER_TEMP"):
    raise SystemExit("This installation check runs only in disposable GitHub Actions runners.")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from platform_support import codex_command
from rpc_transport import JsonRpcProcess

MARKET = "codex-usage-meter-community"
NAME = "codex-usage-meter"
prefix = codex_command()


def command(*arguments):
    result = subprocess.run([*prefix, *arguments], capture_output=True, text=True,
                            encoding="utf-8", timeout=120, check=True)
    return json.loads(result.stdout)


added = command("plugin", "marketplace", "add", "hardwork-xu/codex-usage-meter", "--json")
assert added["marketplaceName"] == MARKET, added
command("plugin", "add", NAME + "@" + MARKET, "--json")
expected = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
checked = False
with JsonRpcProcess([*prefix, "app-server", "--stdio"], timeout=30) as rpc:
    rpc.send({"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "usage_meter_public_install_check", "version": expected},
        "capabilities": {"experimentalApi": True}}})
    for response in rpc.responses():
        if response.get("id") == 1:
            assert "error" not in response, response
            rpc.send({"method": "initialized"})
            rpc.send({"id": 2, "method": "plugin/read", "params": {
                "marketplacePath": str(Path(added["installedRoot"]) / ".agents/plugins/marketplace.json"),
                "pluginName": NAME}})
        elif response.get("id") == 2:
            assert "error" not in response, response
            plugin = response["result"]["plugin"]
            summary = plugin["summary"]
            assert summary["installed"] and summary["enabled"], summary
            assert summary["localVersion"] == expected, summary
            # App Server serializes HookEventName as camelCase; hooks.json uses
            # the user-facing PascalCase event names.
            assert {h["eventName"] for h in plugin["hooks"]} == {
                "sessionStart", "userPromptSubmit", "stop", "subagentStop", "interrupt"}, plugin["hooks"]
            skills = [skill for skill in plugin["skills"] if skill["name"] == NAME + ":usage-meter"]
            assert len(skills) == 1, plugin["skills"]
            skill = skills[0]
            installed = Path(skill["path"]).parents[2]
            subprocess.run([sys.executable, "-X", "utf8", str(installed / "scripts/run.py"), "--help"],
                           check=True, timeout=10, stdout=subprocess.DEVNULL)
            checked = True
            break
assert checked, "Codex did not return installed plugin details"
print(f"Official CLI installed {NAME} {expected} from GitHub; skill, entry script and five Hooks verified.")
