"""Synthetic metadata transport checks; never launch Codex or query an account."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import conversations


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
    def run_fetch(self, ids, records, failure=None):
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        def responses():
            yield from records
            if failure:
                raise failure
        rpc.responses.side_effect = responses
        with mock.patch.object(conversations, "JsonRpcProcess", return_value=rpc) as transport:
            result = conversations.fetch_conversation_titles(ids, ["synthetic-codex"])
        sent = [call.args[0] for call in rpc.send.call_args_list]
        self.assertTrue(rpc.__exit__.called)
        return result, sent, transport

    def test_only_public_metadata_requests_and_title_values_escape(self):
        secret = "PRIVATE PREVIEW AND MESSAGE CONTENT"
        result, sent, transport = self.run_fetch(["thread-1", "thread-2"], [
            {"id": 1, "result": {}},
            named(2, "thread-1", "  课程\n笔记  ", preview=secret, turns=[{"message": secret}]),
            named(3, "thread-2", "额度插件", preview=secret),
        ])
        self.assertEqual(result, {"thread-1": "课程 笔记", "thread-2": "额度插件"})
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual([item["method"] for item in sent], ["initialize", "initialized", "thread/read", "thread/read"])
        self.assertEqual([item["params"] for item in sent if item["method"] == "thread/read"], [
            {"threadId": "thread-1", "includeTurns": False},
            {"threadId": "thread-2", "includeTurns": False},
        ])
        self.assertEqual(transport.call_args.args[0], ["synthetic-codex", "app-server", "--stdio"])

    def test_no_name_never_falls_back_to_preview(self):
        result, _, _ = self.run_fetch(["thread-1", "thread-2"], [
            {"id": 1, "result": {}}, named(2, "thread-1", None, preview="DO NOT RETURN"),
            named(3, "thread-2", " \x00 "),
        ])
        self.assertEqual(result, {})

    def test_foreign_thread_response_and_per_id_failure_are_omitted(self):
        result, sent, _ = self.run_fetch(["thread-1", "thread-2", "thread-3"], [
            {"id": 1, "result": {}}, named(2, "other-thread", "foreign title"),
            {"id": 3, "error": {"message": "private server diagnostic"}}, named(4, "thread-3", "third title"),
        ])
        self.assertEqual(result, {"thread-3": "third title"})
        self.assertEqual(sum(item["method"] == "thread/read" for item in sent), 3)

    def test_boolean_ids_and_notifications_are_ignored(self):
        result, sent, _ = self.run_fetch(["thread-1"], [
            {"id": True, "result": {}}, {"method": "notification"},
            {"id": 1, "result": {}}, named(2, "thread-1", "title"),
        ])
        self.assertEqual(result, {"thread-1": "title"})
        self.assertEqual(sum(item["method"] == "initialized" for item in sent), 1)

    def test_initialize_error_sends_no_thread_requests(self):
        result, sent, _ = self.run_fetch(["thread-1"], [{"id": 1, "error": {"message": "private failure"}}])
        self.assertEqual(result, {})
        self.assertEqual([item["method"] for item in sent], ["initialize"])

    def test_deadline_keeps_already_read_names(self):
        result, _, _ = self.run_fetch(["thread-1", "thread-2"], [
            {"id": 1, "result": {}}, named(2, "thread-1", "kept")], TimeoutError("private diagnostic"))
        self.assertEqual(result, {"thread-1": "kept"})

    def test_duplicate_registered_ids_are_read_once(self):
        result, sent, _ = self.run_fetch(["thread-1", "thread-1"], [
            {"id": 1, "result": {}}, named(2, "thread-1", "title")])
        self.assertEqual(result, {"thread-1": "title"})
        self.assertEqual(sum(item["method"] == "thread/read" for item in sent), 1)

    def test_invalid_or_excessive_ids_are_rejected_before_process_start(self):
        for ids in (None, "thread-1", ["bad id"], ["x" * 129], [True], [f"thread-{index}" for index in range(101)]):
            with self.subTest(ids=ids), mock.patch.object(conversations, "JsonRpcProcess") as transport:
                with self.assertRaises(ValueError):
                    conversations.fetch_conversation_titles(ids, "synthetic-codex")
                transport.assert_not_called()

    def test_empty_request_does_not_start_a_process(self):
        with mock.patch.object(conversations, "JsonRpcProcess") as transport:
            self.assertEqual(conversations.fetch_conversation_titles([], "synthetic-codex"), {})
            transport.assert_not_called()

    def test_spawn_failure_does_not_expose_diagnostic(self):
        with mock.patch.object(conversations, "JsonRpcProcess", side_effect=OSError("private executable path")):
            self.assertEqual(conversations.fetch_conversation_titles(["thread-1"], "synthetic-codex"), {})


if __name__ == "__main__":
    unittest.main()
