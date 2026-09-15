"""Synthetic catalog/alias integration tests; no live Codex or account access."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter


A = "shared-prefix-a123456"
B = "shared-prefix-b123456"
C = "shared-prefix-c654321"


def turn(thread_id, turn_id, started, ended=None, status="completed"):
    return {"threadId": thread_id, "id": turn_id, "startedAt": started, "endedAt": ended, "status": status}


class CatalogTests(unittest.TestCase):
    def test_child_uses_verified_parent_name_and_preserves_alias_precedence(self):
        metadata = {A: {"sourceType": "main"}, B: {"sourceType": "subagent", "parentThreadId": A, "agentLabel": "synthetic_worker"}}
        records = {A: {}, B: {}}
        rows = {row["id"]: row for row in meter.conversation_catalog(records, [], {A: "示例主任务"}, metadata)}
        self.assertEqual(rows[B]["title"], "子任务 · synthetic_worker")
        self.assertEqual(rows[B]["parentTitle"], "示例主任务")
        self.assertEqual(rows[B]["parentDisplayId"], "a123456")
        records[B]["alias"] = "自定义子任务"
        rows = {row["id"]: row for row in meter.conversation_catalog(records, [], {}, metadata)}
        self.assertEqual(rows[B]["title"], "自定义子任务")
        metadata[B]["parentThreadId"] = "not-registered"
        rows = {row["id"]: row for row in meter.conversation_catalog(records, [], {"not-registered": "不得使用"}, metadata)}
        self.assertIsNone(rows[B]["parentTitle"])

    def test_turn_numbers_are_per_conversation_and_stable_under_interleaving(self):
        records = {A: {"registeredAt": 1}, B: {"registeredAt": 2}}
        source = [turn(A, "a-second", 30), turn(B, "b-second", 40),
                  turn(B, "b-first", 20), turn(A, "a-first", 10)]
        expected = {"a-first": 1, "a-second": 2, "b-first": 1, "b-second": 2}
        for ordering in (source, list(reversed(source))):
            with self.subTest(ordering=[item["id"] for item in ordering]):
                items = copy.deepcopy(ordering)
                meter.conversation_catalog(records, items, {A: "对话 A", B: "对话 B"})
                self.assertEqual({item["id"]: item["turnNumber"] for item in items}, expected)
                self.assertEqual({item["conversationTitle"] for item in items if item["threadId"] == A}, {"对话 A"})

    def test_equal_start_times_use_id_order_for_stable_numbering(self):
        items = [turn(A, "turn-b", 10), turn(A, "turn-a", 10)]
        meter.conversation_catalog({A: {"registeredAt": 1}}, items, {})
        self.assertEqual({item["id"]: item["turnNumber"] for item in items}, {"turn-a": 1, "turn-b": 2})

    def test_custom_official_and_fallback_title_precedence(self):
        records = {A: {"registeredAt": 1, "alias": "  本地\n备注 "},
                   B: {"registeredAt": 2, "alias": "   "}, C: {"registeredAt": 3}}
        titles = {A: "被备注覆盖的官方名称", B: " 官方名称 ", C: " \x00 ", "unregistered": "忽略"}
        items = [turn(A, "a", 1), turn(B, "b", 2), turn(C, "c", 3)]
        catalog = {row["id"]: row for row in meter.conversation_catalog(records, items, titles)}
        self.assertEqual((catalog[A]["title"], catalog[A]["titleSource"]), ("本地 备注", "custom"))
        self.assertEqual((catalog[B]["title"], catalog[B]["titleSource"]), ("官方名称", "official"))
        self.assertEqual((catalog[C]["title"], catalog[C]["titleSource"]), ("未命名任务 · 654321", "fallback"))
        self.assertNotIn("unregistered", catalog)

    def test_counts_activity_and_order_do_not_merge_other_conversations(self):
        records = {A: {"registeredAt": 1}, B: {"registeredAt": 2}, C: {"registeredAt": 3}}
        items = [turn(A, "a-old", 10, 20), turn(A, "a-run-1", 30, status="running"),
                 turn(A, "a-run-2", 40, status="running"), turn(A, "a-aborted", 25, 28, "interrupted"),
                 turn(B, "b", 100, 150), turn("unregistered", "other", 900, 1000)]
        catalog = meter.conversation_catalog(records, items, {})
        self.assertEqual([row["id"] for row in catalog], [A, B, C])
        indexed = {row["id"]: row for row in catalog}
        self.assertEqual((indexed[A]["turnCount"], indexed[A]["activeTurnCount"], indexed[A]["lastActivityAt"]), (4, 2, 40))
        self.assertEqual((indexed[B]["turnCount"], indexed[B]["activeTurnCount"], indexed[B]["lastActivityAt"]), (1, 0, 150))
        self.assertEqual((indexed[C]["turnCount"], indexed[C]["activeTurnCount"], indexed[C]["lastActivityAt"]), (0, 0, 0))
        self.assertNotIn("turnNumber", items[-1])

    def test_matching_prefixes_and_suffixes_still_get_unique_display_ids(self):
        catalog = meter.conversation_catalog({A: {}, B: {}, C: {}}, [], {})
        labels = {row["id"]: row["displayId"] for row in catalog}
        self.assertEqual(labels, {A: "a123456", B: "b123456", C: "654321"})
        self.assertEqual(len(set(labels.values())), 3)
        self.assertTrue(all(len(label) >= 6 for label in labels.values()))

    def test_fallback_ids_are_stable_under_mapping_order(self):
        records = {B: {"registeredAt": 20}, A: {"registeredAt": 10}, C: {"registeredAt": 30}}
        first = {row["id"]: row["title"] for row in meter.conversation_catalog(records, [], {})}
        reordered = dict(reversed(list(records.items())))
        second = {row["id"]: row["title"] for row in meter.conversation_catalog(reordered, [], {})}
        self.assertEqual(first, {A: "未命名任务 · a123456", B: "未命名任务 · b123456", C: "未命名任务 · 654321"})
        self.assertEqual(second, first)


class RegistryAliasTests(unittest.TestCase):
    def test_unverified_incomplete_prefix_cannot_register(self):
        with mock.patch.object(meter, "read_usage_log", return_value={"identityVerified": False, "turns": []}):
            with self.assertRaises(ValueError):
                meter.register(self.folder, self.folder / "synthetic.jsonl", A)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.registry = self.folder / "registry.json"
        self.initial = {A: {"path": str(self.folder / "a.jsonl"), "registeredAt": 10},
                        B: {"path": str(self.folder / "b.jsonl"), "registeredAt": 20, "alias": "保留 B"}}
        meter.write_json(self.registry, self.initial)

    def read_registry(self):
        return json.loads(self.registry.read_text(encoding="utf-8"))

    def write_log(self, name, thread_id):
        path = self.folder / name
        counters = {"total_tokens": 12, "input_tokens": 10, "output_tokens": 2,
                    "cached_input_tokens": 0, "cache_write_input_tokens": 0, "reasoning_output_tokens": 0}
        records = [
            {"type": "session_meta", "payload": {"id": thread_id}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "test-turn"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": counters, "last_token_usage": counters}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "test-turn"}},
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def test_alias_is_sanitized_persisted_and_only_changes_target_record(self):
        with mock.patch.object(meter, "fetch_conversation_titles") as fetch, mock.patch.object(meter.subprocess, "Popen") as popen:
            result = meter.set_conversation_label(self.folder, {"threadId": A, "title": "  课程\n\x00 笔记  "})
        self.assertEqual(result, {"ok": True})
        records = self.read_registry()
        self.assertEqual(records[A], {**self.initial[A], "alias": "课程 笔记"})
        self.assertEqual(records[B], self.initial[B])
        fetch.assert_not_called()
        popen.assert_not_called()

    def test_alias_is_limited_to_display_length(self):
        meter.set_conversation_label(self.folder, {"threadId": A, "title": "课" * 200})
        self.assertEqual(self.read_registry()[A]["alias"], "课" * 120)

    def test_blank_alias_reset_restores_official_then_fallback_and_is_idempotent(self):
        meter.set_conversation_label(self.folder, {"threadId": A, "title": "自定义备注"})
        for blank in (" \n\t ", ""):
            with self.subTest(blank=blank):
                self.assertEqual(meter.set_conversation_label(self.folder, {"threadId": A, "title": blank}), {"ok": True})
                records = self.read_registry()
                self.assertNotIn("alias", records[A])
                official = {row["id"]: row for row in meter.conversation_catalog(records, [], {A: "官方名称"})}[A]
                self.assertEqual((official["title"], official["titleSource"]), ("官方名称", "official"))
                fallback = {row["id"]: row for row in meter.conversation_catalog(records, [], {})}[A]
                self.assertEqual((fallback["title"], fallback["titleSource"]), ("未命名任务 · a123456", "fallback"))

    def test_invalid_or_unknown_alias_input_does_not_change_registry(self):
        invalids = [None, [], {}, {"threadId": A}, {"threadId": A, "title": "x", "extra": True},
                    {"threadId": None, "title": "x"}, {"threadId": A, "title": None},
                    {"threadId": A, "title": "x" * 241}, {"threadId": A, "title": "\x00\u202e"},
                    {"threadId": "not-registered", "title": "x"}]
        before = self.registry.read_bytes()
        for value in invalids:
            with self.subTest(value=value), self.assertRaises(ValueError):
                meter.set_conversation_label(self.folder, value)
            self.assertEqual(self.registry.read_bytes(), before)

    def test_reregister_updates_path_but_preserves_alias_and_first_registration_time(self):
        meter.set_conversation_label(self.folder, {"threadId": A, "title": "保留 A"})
        replacement = self.write_log("replacement.jsonl", A)
        with mock.patch.object(meter.time, "time", return_value=999):
            result = meter.register(self.folder, replacement, A)
        self.assertEqual(result, {"threadId": A, "turnCount": 1})
        records = self.read_registry()
        self.assertEqual(records[A], {"path": str(replacement), "registeredAt": 10, "alias": "保留 A"})
        self.assertEqual(records[B], self.initial[B])

    def test_new_registration_gets_current_time_once(self):
        path = self.write_log("new.jsonl", C)
        with mock.patch.object(meter.time, "time", return_value=123):
            meter.register(self.folder, path, C)
        with mock.patch.object(meter.time, "time", return_value=456):
            meter.register(self.folder, path, C)
        self.assertEqual(self.read_registry()[C]["registeredAt"], 123)

    def test_foreign_log_registration_cannot_replace_existing_alias_or_path(self):
        path = self.write_log("foreign.jsonl", B)
        before = self.registry.read_bytes()
        with self.assertRaises(ValueError):
            meter.register(self.folder, path, A)
        self.assertEqual(self.registry.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
