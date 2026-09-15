"""Real local pipe tests on each OS; children are synthetic and use no account."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rpc_transport


class TransportTests(unittest.TestCase):
    def test_fragmented_utf8_and_multiple_messages_use_real_pipes(self):
        child = '''import json, sys
request = json.loads(sys.stdin.buffer.readline())
records = [{"id":request["id"],"result":{"name":"中文 标题"}}, {"id":2,"result":{}}]
data = b"invalid\\n" + b"".join((json.dumps(x,ensure_ascii=False)+"\\n").encode("utf-8") for x in records)
for offset in range(0,len(data),3):
    sys.stdout.buffer.write(data[offset:offset+3]);sys.stdout.buffer.flush()
'''
        with rpc_transport.JsonRpcProcess([sys.executable, "-u", "-c", child], timeout=5) as rpc:
            rpc.send({"id": 1, "method": "synthetic"})
            replies = list(rpc.responses())
        self.assertEqual(replies, [{"id": 1, "result": {"name": "中文 标题"}}, {"id": 2, "result": {}}])
        self.assertIsNotNone(rpc.proc.poll())

    def test_unterminated_line_cannot_defeat_deadline(self):
        child = 'import sys,time;sys.stdout.buffer.write(b\'{"id":\');sys.stdout.buffer.flush();time.sleep(10)'
        start = time.monotonic()
        with rpc_transport.JsonRpcProcess([sys.executable, "-u", "-c", child], timeout=0.5) as rpc:
            with self.assertRaises(TimeoutError):
                list(rpc.responses())
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNotNone(rpc.proc.poll())

    def test_child_not_reading_stdin_cannot_block_send_forever(self):
        start = time.monotonic()
        with rpc_transport.JsonRpcProcess([sys.executable, "-u", "-c", "import time;time.sleep(10)"], timeout=0.5) as rpc:
            with self.assertRaises(TimeoutError):
                for _ in range(256):
                    rpc.send({"padding": "x" * 8000})
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNotNone(rpc.proc.poll())

    def test_oversized_record_terminates_reading_without_returning_content(self):
        child = 'import sys;sys.stdout.buffer.write(b"x"*65+b"\\n");sys.stdout.buffer.flush()'
        with mock.patch.object(rpc_transport, "MAX_MESSAGE_BYTES", 64):
            with rpc_transport.JsonRpcProcess([sys.executable, "-u", "-c", child], timeout=5) as rpc:
                self.assertEqual(list(rpc.responses()), [])

    def test_total_output_is_bounded(self):
        child = 'import sys;sys.stdout.buffer.write(b\'{"id":1}\\n\'*100);sys.stdout.buffer.flush()'
        with mock.patch.object(rpc_transport, "MAX_OUTPUT_BYTES", 20):
            with rpc_transport.JsonRpcProcess([sys.executable, "-u", "-c", child], timeout=5) as rpc:
                self.assertEqual(len(list(rpc.responses())), 2)

    def test_only_well_formed_command_prefixes_are_accepted(self):
        for invalid in (None, [], [None], [""], ["bad\x00path"], ["x"] * 33):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                rpc_transport.command_prefix(invalid)
        self.assertEqual(rpc_transport.command_prefix(["node.exe", "目录/codex.js"]), ["node.exe", "目录/codex.js"])


if __name__ == "__main__":
    unittest.main()
