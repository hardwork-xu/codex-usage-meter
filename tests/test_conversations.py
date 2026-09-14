"""Synthetic metadata transport checks; never launch Codex or query an account."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import conversations


class _Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def encoded(*records):
    return b"".join((json.dumps(record, ensure_ascii=False) + "\n").encode() for record in records)


def named(request_id, thread_id, name, **extra):
    return {"id": request_id, "result": {"thread": {"id": thread_id, "name": name, **extra}}}


class TitleTests(unittest.TestCase):
    def test_title_normalization_removes_controls_and_bounds_length(self):
        self.assertEqual(conversations.normalize_title(" \t 课程\n\x00 笔记\u202e \r "), "课程 笔记")
        self.assertEqual(conversations.normalize_title("标题" * 100), ("标题" * 60))
        self.assertIsNone(conversations.normalize_title(" \x00\n\u200b "))
        for invalid in (None, 123, [], {}, True):
            with self.subTest(invalid=invalid):
                self.assertIsNone(conversations.normalize_title(invalid))

    def test_suffixes_are_short_and_unique_despite_matching_uuid_prefixes(self):
        ids = ["019a0000-0000-0000-0000-000000abc123", "019a0000-0000-0000-0000-000000def456"]
        self.assertEqual(conversations.display_ids(ids), {ids[0]: "abc123", ids[1]: "def456"})

    def test_suffix_collisions_expand_only_conflicting_labels(self):
        result = conversations.display_ids(["a123456", "b123456", "other654321", "a123456"])
        self.assertEqual(result, {"a123456": "a123456", "b123456": "b123456", "other654321": "654321"})
        self.assertEqual(len(result.values()), len(set(result.values())))

    def test_full_id_that_is_another_ids_suffix_remains_distinct(self):
        result = conversations.display_ids(["abc123", "longabc123", "x"])
        self.assertEqual(result, {"abc123": "abc123", "longabc123": "gabc123", "x": "x"})
        self.assertEqual(conversations.display_ids([]), {})


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.writer = _Writer()
        self.stdout = mock.Mock()
        self.stdout.fileno.return_value = 123
        self.proc = mock.Mock(stdin=self.writer, stdout=self.stdout)
        self.proc.poll.return_value = None
        self.proc.wait.return_value = 0
        self.selector = mock.Mock()
        self.selector.select.return_value = [(SimpleNamespace(fileobj=self.stdout), 1)]

    def run_fetch(self, ids, chunks, *, clock=None):
        with mock.patch.object(conversations.subprocess, "Popen", return_value=self.proc) as popen, \
             mock.patch.object(conversations.selectors, "DefaultSelector", return_value=self.selector), \
             mock.patch.object(conversations.os, "set_blocking") as blocking, \
             mock.patch.object(conversations.os, "read", side_effect=[*chunks, b""]), \
             mock.patch.object(conversations.time, "monotonic", side_effect=clock, return_value=100.0):
            result = conversations.fetch_conversation_titles(ids, "/synthetic/Codex binary")
        sent = [json.loads(line) for line in self.writer.data.splitlines()]
        return result, sent, popen, blocking

    def test_only_public_metadata_requests_and_title_values_escape(self):
        secret = "PRIVATE PREVIEW AND MESSAGE CONTENT"
        result, sent, popen, blocking = self.run_fetch(["thread-1", "thread-2"], [encoded(
            {"id": 1, "result": {}},
            named(2, "thread-1", "  课程\n笔记  ", preview=secret, turns=[{"message": secret}]),
            named(3, "thread-2", "额度插件", preview=secret),
        )])
        self.assertEqual(result, {"thread-1": "课程 笔记", "thread-2": "额度插件"})
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual([item["method"] for item in sent], ["initialize", "initialized", "thread/read", "thread/read"])
        self.assertEqual([item["params"] for item in sent if item["method"] == "thread/read"], [
            {"threadId": "thread-1", "includeTurns": False},
            {"threadId": "thread-2", "includeTurns": False},
        ])
        self.assertNotIn("experimentalApi", json.dumps(sent))
        self.assertEqual(popen.call_args.args[0], ["/synthetic/Codex binary", "app-server", "--stdio"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", popen.call_args.kwargs)
        blocking.assert_called_once_with(123, False)
        self.assertTrue(self.writer.closed)
        self.stdout.close.assert_called_once()
        self.selector.close.assert_called_once()
        self.proc.terminate.assert_called_once()

    def test_no_name_never_falls_back_to_preview_or_title_alias(self):
        result, _, _, _ = self.run_fetch(["thread-1", "thread-2"], [encoded(
            {"id": 1, "result": {}},
            named(2, "thread-1", None, preview="DO NOT RETURN", title="not the documented field"),
            named(3, "thread-2", " \x00 "),
        )])
        self.assertEqual(result, {})

    def test_foreign_thread_response_and_per_id_failure_are_omitted(self):
        result, sent, _, _ = self.run_fetch(["thread-1", "thread-2", "thread-3"], [encoded(
            {"id": 1, "result": {}}, named(2, "other-thread", "foreign title"),
            {"id": 3, "error": {"message": "private server diagnostic"}}, named(4, "thread-3", "third title"),
        )])
        self.assertEqual(result, {"thread-3": "third title"})
        self.assertEqual(sum(item["method"] == "thread/read" for item in sent), 3)

    def test_fragmented_utf8_json_and_notifications_do_not_corrupt_titles(self):
        data = encoded({"id": 1, "result": {}}, {"method": "warning", "params": {"message": "ignored"}}, named(2, "thread-1", "中文标题"))
        pieces = [data[index:index + 3] for index in range(0, len(data), 3)]
        result, _, _, _ = self.run_fetch(["thread-1"], pieces)
        self.assertEqual(result, {"thread-1": "中文标题"})

    def test_malformed_lines_and_boolean_ids_do_not_complete_handshake(self):
        data = b"not-json\n" + encoded({"id": True, "result": {}}, {"id": 1, "result": {}}, named(2, "thread-1", "title"))
        result, sent, _, _ = self.run_fetch(["thread-1"], [data])
        self.assertEqual(result, {"thread-1": "title"})
        self.assertEqual(sum(item["method"] == "initialized" for item in sent), 1)

    def test_initialize_error_sends_no_thread_requests_and_cleans_up(self):
        result, sent, _, _ = self.run_fetch(["thread-1"], [encoded({"id": 1, "error": {"message": "private failure"}})])
        self.assertEqual(result, {})
        self.assertEqual([item["method"] for item in sent], ["initialize"])
        self.assertTrue(self.writer.closed)
        self.proc.terminate.assert_called_once()

    def test_whole_rpc_deadline_stops_without_blocking_on_partial_line(self):
        self.selector.select.return_value = []
        result, sent, _, _ = self.run_fetch(["thread-1"], [], clock=[100.0, 100.0, 116.0])
        self.assertEqual(result, {})
        self.assertEqual([item["method"] for item in sent], ["initialize"])
        self.proc.terminate.assert_called_once()
        self.selector.close.assert_called_once()

    def test_incomplete_final_json_keeps_previously_read_names(self):
        data = encoded({"id": 1, "result": {}}, named(2, "thread-1", "kept")) + b'{"id":3,"result":'
        result, _, _, _ = self.run_fetch(["thread-1", "thread-2"], [data])
        self.assertEqual(result, {"thread-1": "kept"})

    def test_large_output_or_unterminated_record_is_bounded(self):
        for limit in ("MAX_OUTPUT_BYTES", "MAX_MESSAGE_BYTES"):
            with self.subTest(limit=limit):
                self.setUp()
                with mock.patch.object(conversations, limit, 32):
                    result, _, _, _ = self.run_fetch(["thread-1"], [b"x" * 33])
                self.assertEqual(result, {})
                self.proc.terminate.assert_called_once()

    def test_duplicate_registered_ids_are_read_once(self):
        result, sent, _, _ = self.run_fetch(["thread-1", "thread-1"], [encoded({"id": 1, "result": {}}, named(2, "thread-1", "title"))])
        self.assertEqual(result, {"thread-1": "title"})
        self.assertEqual(sum(item["method"] == "thread/read" for item in sent), 1)

    def test_invalid_or_excessive_ids_are_rejected_before_process_start(self):
        for ids in (None, "thread-1", ["bad id"], ["x" * 129], [True], [f"thread-{index}" for index in range(101)]):
            with self.subTest(ids=ids), mock.patch.object(conversations.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    conversations.fetch_conversation_titles(ids, "/synthetic/codex")
                popen.assert_not_called()

    def test_empty_request_does_not_start_a_process(self):
        with mock.patch.object(conversations.subprocess, "Popen") as popen:
            self.assertEqual(conversations.fetch_conversation_titles([], "/synthetic/codex"), {})
        popen.assert_not_called()

    def test_spawn_failure_is_empty_and_does_not_expose_diagnostic(self):
        with mock.patch.object(conversations.subprocess, "Popen", side_effect=OSError("private executable path")):
            self.assertEqual(conversations.fetch_conversation_titles(["thread-1"], "/synthetic/codex"), {})

    def test_unresponsive_child_is_killed_and_owned_streams_close(self):
        self.proc.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 0.5), 0]
        result, _, _, _ = self.run_fetch(["thread-1"], [])
        self.assertEqual(result, {})
        self.proc.kill.assert_called_once()
        self.assertTrue(self.writer.closed)
        self.stdout.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
