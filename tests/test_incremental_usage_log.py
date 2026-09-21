"""Incremental reader checks using only temporary synthetic JSONL files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_log

THREAD = "incremental-fixture"
META = {"type": "session_meta", "payload": {"id": THREAD}}


def line(record):
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def event(kind, **payload):
    return {"timestamp": 1700000000, "type": "event_msg", "payload": {"type": kind, **payload}}


def tokens(total):
    counters = {"total_tokens": total, "input_tokens": total, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    return event("token_count", info={"total_token_usage": counters, "last_token_usage": counters})


def body(total=100, turn="turn-1"):
    return b"".join(map(line, (META, event("task_started", turn_id=turn), tokens(total))))


class ReadTrace:
    def __init__(self, stream, calls):
        self.stream, self.calls = stream, calls

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, size=-1):
        position = self.stream.tell()
        result = self.stream.read(size)
        self.calls.append(("read", position, len(result)))
        return result

    def readline(self, size=-1):
        position = self.stream.tell()
        result = self.stream.readline(size)
        self.calls.append(("readline", position, len(result)))
        return result


class IncrementalUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="incremental-usage-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "usage.jsonl"

    def reader(self):
        # Small limits exercise real chunk boundaries without large fixtures.
        with mock.patch.object(usage_log, "MAX_LINE_BYTES", 1024):
            return usage_log.UsageLogReader(THREAD, byte_budget=4096)

    def append(self, value):
        with self.path.open("ab") as stream:
            stream.write(value)

    def traced_read(self, reader):
        calls = []
        real_fdopen = os.fdopen
        with mock.patch.object(usage_log.os, "fdopen", side_effect=lambda *a, **kw: ReadTrace(real_fdopen(*a, **kw), calls)):
            result = reader.read(self.path)
        self.assertLessEqual(sum(item[2] for item in calls), reader._byte_budget)
        for turn in result["turns"]:
            if turn["tokens"] is not None:
                allocated = {key: turn["undatedTokens"][key] + sum(bucket[key] for bucket in turn["dailyUsage"].values())
                             for key in turn["tokens"]}
                self.assertEqual(allocated, turn["tokens"])
        return result, calls

    def finish(self, reader, maximum=100):
        for _ in range(maximum):
            result, _ = self.traced_read(reader)
            if result["reading"]["complete"]:
                return result
        self.fail("Synthetic backfill failed to make bounded progress")

    def test_backfill_progress_is_partial_then_complete_without_recounting(self):
        noise = line({"type": "response_item", "payload": {"text": "x" * 700}})
        self.path.write_bytes(body() + noise * 30 + line(tokens(200)) + line(event("task_complete", turn_id="turn-1")))
        reader = self.reader()
        first, _ = self.traced_read(reader)
        self.assertFalse(first["reading"]["complete"])
        self.assertEqual(first["turns"][0]["tokens"]["total"], 100)
        self.assertTrue(first["turns"][0]["readingIncomplete"])
        self.assertEqual(first["turns"][0]["quality"], "partial")
        final = self.finish(reader)
        self.assertEqual(final["reading"], {"complete": True, "bytesRead": self.path.stat().st_size, "fileBytes": self.path.stat().st_size})
        self.assertEqual(final["turns"][0]["tokens"]["total"], 200)
        self.assertEqual(final["turns"][0]["quality"], "complete")
        self.assertNotIn("readingIncomplete", final["turns"][0])
        self.assertEqual(final["warnings"], [])
        self.assertEqual(first["turns"][0]["tokens"]["total"], 100)
        again, calls = self.traced_read(reader)
        self.assertEqual(again, final)
        self.assertEqual(calls, [])

    def test_append_reuses_offset_and_deduplicates_cumulative_snapshots(self):
        self.path.write_bytes(body())
        reader = self.reader()
        first = self.finish(reader)
        offset = first["reading"]["bytesRead"]
        self.append(line(tokens(100)) + line(tokens(150)))
        result, calls = self.traced_read(reader)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 150)
        self.assertTrue(all(position >= offset for kind, position, _ in calls if kind == "readline"))

    def test_partial_tail_and_newline_less_valid_json_recover_without_permanent_gap(self):
        self.path.write_bytes(body())
        reader = self.reader()
        self.finish(reader)
        raw = line(tokens(150))
        self.append(raw[:40])
        partial, _ = self.traced_read(reader)
        self.assertEqual(partial["turns"][0]["tokens"]["total"], 100)
        self.assertTrue(any("尚未写完" in warning for warning in partial["warnings"]))
        unchanged, calls = self.traced_read(reader)
        self.assertEqual(unchanged, partial)
        self.assertEqual(calls, [])
        self.append(raw[40:-1])
        valid_but_uncommitted, _ = self.traced_read(reader)
        self.assertFalse(valid_but_uncommitted["reading"]["complete"])
        self.assertEqual(valid_but_uncommitted["turns"][0]["tokens"]["total"], 100)
        self.append(b"\n")
        recovered = self.finish(reader)
        self.assertEqual(recovered["turns"][0]["tokens"]["total"], 150)
        self.assertEqual(recovered["turns"][0]["quality"], "complete")
        self.assertEqual(recovered["warnings"], [])

    def test_empty_new_file_can_later_receive_matching_identity(self):
        self.path.touch()
        reader = self.reader()
        with self.assertRaises(usage_log.UsageLogError):
            reader.read(self.path)
        self.append(body())
        self.assertEqual(self.finish(reader)["turns"][0]["tokens"]["total"], 100)

    def test_oversized_line_spans_calls_and_does_not_swallow_next_record(self):
        self.path.write_bytes(line(META) + b'x' * 20_000 + b'\n' + body(30)[len(line(META)):])
        reader = self.reader()
        offsets = []
        for _ in range(20):
            result, _ = self.traced_read(reader)
            offsets.append(result["reading"]["bytesRead"])
            if result["reading"]["complete"]:
                break
        self.assertTrue(result["reading"]["complete"])
        self.assertEqual(offsets, sorted(set(offsets)))
        self.assertEqual(result["turns"][0]["tokens"]["total"], 30)
        self.assertEqual(sum("过大的单条" in warning for warning in result["warnings"]), 1)

    def test_186_mib_file_is_backfilled_with_hard_per_call_budget(self):
        # Sparse oversized ignored record: reproduces the old whole-file rejection
        # while avoiding a large allocated fixture or any real transcript.
        with self.path.open("wb") as stream:
            stream.write(line(META))
            stream.seek(186 * 1024 * 1024)
            stream.write(b"\n" + body(77)[len(line(META)):])
        reader = usage_log.UsageLogReader(THREAD)
        # This checks the byte cap, not disk speed. A slow CI disk may hit the
        # separate soft time budget first; its cutoff is covered with an
        # advancing clock in test_read_budget.py.
        with mock.patch.object(usage_log.time, "monotonic", return_value=0.0):
            first, _ = self.traced_read(reader)
            self.assertFalse(first["reading"]["complete"])
            self.assertGreater(first["reading"]["fileBytes"], 186 * 1024 * 1024)
            result = self.finish(reader, maximum=30)
        self.assertEqual(result["turns"][0]["tokens"]["total"], 77)

    def test_truncate_same_size_rewrite_and_regrow_reset_baseline(self):
        self.path.write_bytes(body(100))
        reader = self.reader()
        self.finish(reader)
        old_stamp = self.path.stat().st_mtime_ns
        self.path.write_bytes(body(200))
        os.utime(self.path, ns=(old_stamp + 2_000_000_000, old_stamp + 2_000_000_000))
        self.assertEqual(self.finish(reader)["turns"][0]["tokens"]["total"], 200)
        self.path.write_bytes(body(5))
        self.assertEqual(self.finish(reader)["turns"][0]["tokens"]["total"], 5)
        # Same inode and larger final size after truncate/rewrite: a cursor digest
        # change must prevent treating a replacement total as a cumulative delta.
        self.path.write_bytes(body(300) + line({"type": "response_item", "payload": "padding" * 100}))
        self.assertEqual(self.finish(reader)["turns"][0]["tokens"]["total"], 300)

    def test_atomic_replacement_and_path_change_revalidate_task(self):
        self.path.write_bytes(body(100))
        reader = self.reader()
        self.finish(reader)
        replacement = self.path.with_name("replacement.jsonl")
        replacement.write_bytes(body(200))
        os.replace(replacement, self.path)
        self.assertEqual(self.finish(reader)["turns"][0]["tokens"]["total"], 200)
        other = self.path.with_name("other.jsonl")
        other.write_bytes(body(300))
        self.assertEqual(reader.read(other)["turns"][0]["tokens"]["total"], 300)
        other.write_bytes(line({"type": "session_meta", "payload": {"id": "foreign"}}))
        with self.assertRaises(usage_log.UsageLogError):
            reader.read(other)

    def test_concurrent_same_size_rewrite_rejects_mixed_snapshot(self):
        padding = line({"type": "response_item", "payload": "x" * 700}) * 40
        def content(first, cumulative):
            return (body(first) + line(event("task_complete", turn_id="turn-1")) + padding +
                    line(event("task_started", turn_id="turn-2")) + line(tokens(cumulative)) +
                    line(event("task_complete", turn_id="turn-2")))
        original, rewritten = content(100, 300), content(200, 400)
        self.assertEqual(len(original), len(rewritten))
        self.path.write_bytes(original)
        stamp = self.path.stat().st_mtime_ns
        real_handle = usage_log._Reader.handle
        changed = False
        def handle(parser, record):
            nonlocal changed
            real_handle(parser, record)
            if not changed and record.get("payload", {}).get("type") == "token_count":
                changed = True
                self.path.write_bytes(rewritten)
                os.utime(self.path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
        reader = usage_log.UsageLogReader(THREAD)
        with mock.patch.object(usage_log._Reader, "handle", handle):
            with self.assertRaisesRegex(usage_log.UsageLogError, "发生变化"):
                reader.read(self.path)
        self.assertEqual([turn["tokens"]["total"] for turn in self.finish(reader)["turns"]], [200, 200])

    def test_late_foreign_identity_poison_survives_append_and_timestamp_touch(self):
        noise = line({"type": "response_item", "payload": "x" * 700})
        self.path.write_bytes(body() + noise * 10 + line({"type": "session_meta", "payload": {"id": "foreign"}}))
        reader = self.reader()
        first = reader.read(self.path)
        self.assertEqual(first["turns"][0]["tokens"]["total"], 100)
        with self.assertRaises(usage_log.UsageLogError):
            self.finish(reader)
        self.assertEqual(reader._reader.turns, {})
        for change in (lambda: None, lambda: self.append(line(tokens(999))),
                       lambda: os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 2_000_000_000))):
            change()
            with self.assertRaises(usage_log.UsageLogError):
                reader.read(self.path)
        replacement = self.path.with_name("valid.jsonl")
        replacement.write_bytes(body(12))
        os.replace(replacement, self.path)
        self.assertEqual(self.finish(reader)["turns"][0]["tokens"]["total"], 12)

    def test_results_are_independent_and_no_message_or_partial_body_is_cached(self):
        secret = "PROMPT-THAT-MUST-NOT-BE-CACHED-847d42"
        self.path.write_bytes(body() + line({"type": "response_item", "payload": secret}) +
                              ('{"type":"response_item","payload":"' + secret).encode())
        reader = self.reader()
        first = reader.read(self.path)
        first["turns"][0]["tokens"]["total"] = 9999
        first["warnings"].append("injected")
        first["reading"]["bytesRead"] = 0
        next_result = reader.read(self.path)
        self.assertEqual(next_result["turns"][0]["tokens"]["total"], 100)
        self.assertNotIn("injected", next_result["warnings"])
        self.assertNotIn(secret, json.dumps(next_result))
        def stored_strings(value, seen=None):
            seen = set() if seen is None else seen
            if id(value) in seen:
                return []
            seen.add(id(value))
            if isinstance(value, str):
                return [value]
            if isinstance(value, bytes):
                return [value.decode("utf-8", errors="replace")]
            if isinstance(value, dict):
                return sum((stored_strings(item, seen) for item in value.values()), [])
            if isinstance(value, (tuple, list)):
                return sum((stored_strings(item, seen) for item in value), [])
            if type(value).__module__ == usage_log.__name__:
                return stored_strings(vars(value), seen)
            return []
        self.assertFalse(any(secret in value for value in stored_strings(reader)))

    def test_parallel_calls_do_not_double_count_or_share_mutable_results(self):
        self.path.write_bytes(body() + line(tokens(150)))
        reader = self.reader()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: reader.read(self.path), range(12)))
        self.assertTrue(all(result["turns"][0]["tokens"]["total"] == 150 for result in results))
        results[0]["turns"][0]["tokens"]["total"] = 1
        self.assertEqual(results[1]["turns"][0]["tokens"]["total"], 150)

    def test_compatibility_function_reports_incomplete_backfill(self):
        self.path.write_bytes(body() + line({"type": "response_item", "payload": "x" * 700}) * 20)
        with mock.patch.object(usage_log, "MAX_RECORDS_PER_READ", 5):
            result = usage_log.read_usage_log(self.path, THREAD)
        self.assertFalse(result["reading"]["complete"])
        self.assertTrue(result["warnings"])

    def test_daily_buckets_follow_token_events_across_local_midnight(self):
        before = datetime(2026, 9, 14, 23, 59, 59).timestamp()
        after = datetime(2026, 9, 15, 0, 0, 1).timestamp()
        first = {"total_tokens": 500, "input_tokens": 400, "cached_input_tokens": 200,
                 "cache_write_input_tokens": 20, "output_tokens": 100, "reasoning_output_tokens": 30}
        second = {"total_tokens": 1000, "input_tokens": 800, "cached_input_tokens": 500,
                  "cache_write_input_tokens": 50, "output_tokens": 200, "reasoning_output_tokens": 80}
        def usage(counts, timestamp):
            return {**event("token_count", info={"total_token_usage": counts, "last_token_usage": counts}), "timestamp": timestamp}
        records = [META, {**event("task_started", turn_id="turn-1"), "timestamp": before - 100},
                   usage(first, before), usage(first, after), usage(second, after),
                   {**event("task_complete", turn_id="turn-1"), "timestamp": after + 100}]
        self.path.write_bytes(b"".join(map(line, records)))
        result = self.finish(self.reader())
        turn = result["turns"][0]
        self.assertEqual(turn["dailyUsage"], {
            "2026-09-14": {"total": 500, "input": 400, "cachedInput": 200, "cacheWriteInput": 20, "output": 100, "reasoningOutput": 30},
            "2026-09-15": {"total": 500, "input": 400, "cachedInput": 300, "cacheWriteInput": 30, "output": 100, "reasoningOutput": 50},
        })
        self.assertTrue(all(value == 0 for value in turn["undatedTokens"].values()))

    def test_missing_invalid_timestamps_are_counted_only_as_undated(self):
        dated = {**tokens(100), "timestamp": datetime(2026, 9, 14, 12, 0).timestamp()}
        missing = tokens(150)
        missing.pop("timestamp")
        self.path.write_bytes(b"".join(map(line, [META, event("task_started", turn_id="turn-1"), dated,
                                                   missing, {**tokens(200), "timestamp": True},
                                                   {**tokens(200), "timestamp": "invalid"}])))
        turn = self.finish(self.reader())["turns"][0]
        self.assertEqual(turn["dailyUsage"]["2026-09-14"]["total"], 100)
        self.assertEqual(turn["undatedTokens"]["total"], 100)
        self.assertEqual(turn["tokens"]["total"], 200)
        self.assertEqual(set(turn["dailyUsage"]), {"2026-09-14"})

    def test_reset_fallback_is_assigned_to_its_event_day_without_negative_buckets(self):
        day1 = datetime(2026, 9, 14, 12, 0).timestamp()
        day2 = datetime(2026, 9, 15, 12, 0).timestamp()
        self.path.write_bytes(b"".join(map(line, [META, event("task_started", turn_id="turn-1"),
                                                   {**tokens(100), "timestamp": day1},
                                                   {**tokens(20), "timestamp": day2},
                                                   {**tokens(50), "timestamp": day2}])))
        turn = self.finish(self.reader())["turns"][0]
        self.assertEqual(turn["quality"], "partial")
        self.assertEqual({day: bucket["total"] for day, bucket in turn["dailyUsage"].items()},
                         {"2026-09-14": 100, "2026-09-15": 50})
        self.assertEqual(turn["tokens"]["total"], 150)

    def test_midnight_partial_tail_allocates_only_after_newline_and_exports_copies(self):
        day1 = datetime(2026, 9, 14, 23, 59).timestamp()
        day2 = datetime(2026, 9, 15, 0, 1).timestamp()
        self.path.write_bytes(line(META) + line(event("task_started", turn_id="turn-1")) + line({**tokens(100), "timestamp": day1}))
        reader = self.reader()
        initial = self.finish(reader)
        pending = line({**tokens(200), "timestamp": day2})
        self.append(pending[:-1])
        incomplete = reader.read(self.path)
        self.assertEqual(set(incomplete["turns"][0]["dailyUsage"]), {"2026-09-14"})
        self.assertTrue(incomplete["turns"][0]["readingIncomplete"])
        self.append(b"\n")
        final = self.finish(reader)
        self.assertEqual({day: bucket["total"] for day, bucket in final["turns"][0]["dailyUsage"].items()},
                         {"2026-09-14": 100, "2026-09-15": 100})
        self.assertEqual(final["turns"][0]["quality"], "complete")
        initial["turns"][0]["dailyUsage"]["2026-09-14"]["total"] = 9999
        initial["turns"][0]["undatedTokens"]["total"] = 9999
        self.assertEqual(reader.read(self.path), final)

    def test_identity_is_false_until_verified_header_and_metadata_is_sanitized(self):
        noise = line({"type": "response_item", "payload": "x" * 700}) * 20
        metadata = {"type": "session_meta", "payload": {"id": THREAD, "cwd": "private-cwd-sentinel",
                    "instructions": "private-instructions-sentinel", "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": "parent-fixture", "agent_path": "/root/parser", "other": "private-source-sentinel"}}}}}
        self.path.write_bytes(noise + line(metadata) + body()[len(line(META)):])
        reader = self.reader()
        initial = reader.read(self.path)
        self.assertFalse(initial["identityVerified"])
        self.assertFalse(initial["reading"]["complete"])
        self.assertEqual(initial["conversationMetadata"], {})
        final = self.finish(reader)
        self.assertTrue(final["identityVerified"])
        self.assertEqual(final["conversationMetadata"], {"sourceType": "subagent", "parentThreadId": "parent-fixture", "agentLabel": "parser"})
        self.assertNotIn("private-", json.dumps(final))
        final["conversationMetadata"]["agentLabel"] = "mutated"
        self.assertEqual(reader.read(self.path)["conversationMetadata"]["agentLabel"], "parser")

    def test_foreign_metadata_never_reaches_source_metadata_helper(self):
        self.path.write_bytes(line({"type": "session_meta", "payload": {"id": "foreign", "source": "subagent"}}))
        with mock.patch.object(usage_log, "source_metadata") as sanitize:
            with self.assertRaises(usage_log.UsageLogError):
                self.reader().read(self.path)
            sanitize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
