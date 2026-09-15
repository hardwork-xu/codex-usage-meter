"""Native-process lifecycle checks for Windows, macOS and Linux.

Every server owns a fresh temporary data directory. Its account/title refresh
methods are replaced before startup, so this suite never runs Codex, reads an
account, or accesses real transcripts. No shell scripts or symlinks are needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error, request


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import meter


SERVER_HELPER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import meter
meter.Meter.refresh = lambda self: None
meter.Meter.refresh_titles = lambda self: None
sys.argv = ["meter.py", "--data-dir", sys.argv[2], "serve"]
meter.main()
"""

MCP_HELPER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import meter
def refuse_background_spawn(*args, **kwargs):
    raise AssertionError("Synthetic MCP check must reuse the owned test service")
meter.subprocess.Popen = refuse_background_spawn
sys.argv = ["meter.py", "--data-dir", sys.argv[2], "mcp"]
meter.main()
"""


class NativeServiceSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="meter-smoke-")
        self.addCleanup(self.temp.cleanup)
        # Cover Windows Unicode and spaces without assuming a particular drive.
        self.data = Path(self.temp.name) / "local data 中文"
        self.data.mkdir()
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        # The production entrypoint must establish UTF-8 itself, independently
        # of developer machines or CI forcing Python's text encoding.
        self.env.pop("PYTHONIOENCODING", None)
        self.env.pop("PYTHONUTF8", None)

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def helper_command(self, code):
        return [sys.executable, "-u", "-c", code, str(SCRIPTS), str(self.data)]

    def start_service(self):
        process = subprocess.Popen(
            self.helper_command(SERVER_HELPER), cwd=self.temp.name, env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self.addCleanup(self.cleanup_process, process)
        lines = queue.Queue(maxsize=1)
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=10)
        except queue.Empty:
            self.fail("Isolated test service did not report its endpoint")
        if not line:
            process.wait(timeout=5)
            self.fail("Isolated test service exited: " + process.stderr.read())
        endpoint = json.loads(line)
        self.assertTrue(meter.local_url(endpoint["url"]))
        self.assertEqual(meter.local_get(endpoint["url"] + "health")["pid"], process.pid)
        return process, endpoint

    def stop_service(self, process):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "meter.py"), "--data-dir", str(self.data), "stop"],
            cwd=self.temp.name, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        process.wait(timeout=5)

    def test_mcp_open_reuses_owned_service_and_restart_keeps_address(self):
        process, first = self.start_service()
        state = meter.local_get(first["url"] + "api/state")
        self.assertEqual(state["turns"], [])
        self.assertEqual(state["quota"]["buckets"], [])
        with mock.patch.object(meter.subprocess, "Popen", side_effect=AssertionError("Must reuse the owned test service")):
            self.assertEqual(meter.ensure_service(self.data), first["url"])

        # A missing token cannot shut down the owned process.
        forbidden = request.Request(first["url"] + "api/shutdown", data=b"", method="POST")
        opener = request.build_opener(request.ProxyHandler({}))
        with self.assertRaises(error.HTTPError) as denied:
            opener.open(forbidden, timeout=2)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.assertEqual(meter.local_get(first["url"] + "health")["pid"], process.pid)

        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "open_usage_meter", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_usage_summary", "arguments": {}}},
        ]
        completed = subprocess.run(
            self.helper_command(MCP_HELPER), cwd=self.temp.name, env=self.env,
            input="".join(json.dumps(message) + "\n" for message in messages),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        self.assertTrue(all("result" in response for response in responses), responses)
        self.assertIn("open_usage_meter", {tool["name"] for tool in responses[1]["result"]["tools"]})
        opened = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(opened["url"], first["url"])
        summary = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(summary["turns"], [])
        self.assertNotIn("csrfToken", summary)

        self.stop_service(process)
        # The endpoint survives a normal stop so the next launch can reuse it.
        saved = json.loads((self.data / "endpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["url"], first["url"])
        restarted, second = self.start_service()
        self.assertEqual(second["url"], first["url"])
        self.stop_service(restarted)

    def test_claimed_saved_port_fails_without_changing_address(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            url = "http://127.0.0.1:" + str(blocker.getsockname()[1]) + "/"
            meter.write_json(self.data / "endpoint.json", {"url": url, "pid": 0})
            result = subprocess.run(
                self.helper_command(SERVER_HELPER), cwd=self.temp.name, env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("未更换地址", result.stderr)
            self.assertEqual(json.loads((self.data / "endpoint.json").read_text(encoding="utf-8"))["url"], url)


if __name__ == "__main__":
    unittest.main()
