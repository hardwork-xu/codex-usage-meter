"""Registry growth and fair title batches using only temporary synthetic data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter


class RegistryScaleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="registry-scale-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.registry = self.folder / "registry.json"
        self.calls = []
        self.omitted = set()
        self.fail_next = False
        self.clock = 1000
        command = mock.patch.object(meter, "codex_command", return_value=["synthetic-codex"])
        command.start()
        self.addCleanup(command.stop)
        fetch = mock.patch.object(meter, "fetch_conversation_titles", side_effect=self.fake_titles)
        fetch.start()
        self.addCleanup(fetch.stop)
        child = mock.patch.object(meter.subprocess, "Popen", side_effect=AssertionError("No real child processes in synthetic tests"))
        self.child = child.start()
        self.addCleanup(child.stop)

    def tearDown(self):
        self.child.assert_not_called()

    def seed(self, count):
        records = {f"synthetic-thread-{index:05d}": {
            "path": str(self.folder / f"synthetic-{index:05d}.jsonl"), "registeredAt": index + 1,
        } for index in range(count)}
        if records:
            records[next(iter(records))]["alias"] = "Synthetic preserved alias"
        meter.write_json(self.registry, records)
        return records

    def read_registry(self):
        return json.loads(self.registry.read_text(encoding="utf-8"))

    def write_log(self, ident):
        path = self.folder / "synthetic-new.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": ident}}) + "\n", encoding="utf-8")
        return path

    def fake_titles(self, ids, command):
        batch = tuple(ids)
        self.calls.append(batch)
        if len(batch) > 100:
            raise ValueError("Synthetic official RPC batch limit")
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Synthetic title read failure")
        return {ident: "Synthetic title " + ident for ident in batch if ident not in self.omitted}

    def refresh(self, subject, advance=61):
        with mock.patch.object(meter.time, "time", return_value=self.clock):
            subject.refresh_titles()
        self.clock += advance

    def assert_valid_batches(self, records):
        self.assertTrue(self.calls)
        for batch in self.calls:
            self.assertTrue(1 <= len(batch) <= 100)
            self.assertEqual(len(batch), len(set(batch)))
            self.assertTrue(set(batch) <= set(records))

    def test_registration_beyond_100_preserves_existing_registry(self):
        for count in (100, 250):
            with self.subTest(existing=count):
                previous = self.seed(count)
                path = self.write_log("synthetic-new-thread")
                result = meter.register(self.folder, path, "synthetic-new-thread")
                records = self.read_registry()
                self.assertEqual(len(records), count + 1)
                self.assertEqual({key: records[key] for key in previous}, previous)
                self.assertEqual(records["synthetic-new-thread"]["path"], str(path))
                self.assertEqual(result, {"threadId": "synthetic-new-thread", "turnCount": 0})
        self.assertEqual(self.calls, [])

    def test_unverified_identity_still_cannot_register_above_100(self):
        self.seed(250)
        before = self.registry.read_bytes()
        with mock.patch.object(meter, "read_usage_log", return_value={"identityVerified": False, "turns": []}):
            with self.assertRaises(ValueError):
                meter.register(self.folder, self.folder / "synthetic-new.jsonl", "synthetic-new-thread")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_foreign_exact_log_still_cannot_register_above_100(self):
        self.seed(250)
        before = self.registry.read_bytes()
        path = self.write_log("synthetic-foreign-thread")
        with self.assertRaises(ValueError):
            meter.register(self.folder, path, "synthetic-new-thread")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_empty_titles_do_not_prevent_round_robin_coverage(self):
        records = self.seed(250)
        self.omitted = set(records)
        subject = meter.Meter(self.folder)
        for _ in range(4):
            self.refresh(subject)
        self.assert_valid_batches(records)
        self.assertEqual(set().union(*map(set, self.calls[:3])), set(records))
        self.assertFalse(set(self.calls[0]) & set(self.calls[1]))
        self.assertEqual(subject.titles, {})

    def test_unnamed_tasks_do_not_starve_already_named_tasks(self):
        records = self.seed(250)
        ids = sorted(records)
        meter.write_json(self.folder / "titles.json", {ident: "Synthetic cached title" for ident in ids[:200]})
        self.omitted = set(ids[200:])
        subject = meter.Meter(self.folder)
        for _ in range(3):
            self.refresh(subject)
        self.assert_valid_batches(records)
        self.assertEqual(set().union(*map(set, self.calls)), set(records))
        self.assertEqual(subject.titles[ids[0]], "Synthetic title " + ids[0])
        self.assertNotIn(ids[-1], subject.titles)

    def test_failed_batch_advances_and_later_returns_for_retry(self):
        records = self.seed(250)
        subject = meter.Meter(self.folder)
        self.fail_next = True
        for _ in range(4):
            self.refresh(subject)
        self.assert_valid_batches(records)
        self.assertFalse(set(self.calls[0]) & set(self.calls[1]))
        self.assertEqual(set().union(*map(set, self.calls[:3])), set(records))
        self.assertTrue(set(self.calls[0]) & set(self.calls[3]))
        self.assertTrue(set(self.calls[3]) <= set(subject.titles))

    def test_registry_growth_and_removal_do_not_leave_cursor_out_of_range(self):
        self.seed(150)
        subject = meter.Meter(self.folder)
        self.refresh(subject)
        records = self.seed(251)
        for _ in range(3):
            self.refresh(subject)
        self.assert_valid_batches(records)
        self.assertEqual(set().union(*map(set, self.calls)), set(records))
        remaining = {key: records[key] for key in sorted(records)[-25:]}
        meter.write_json(self.registry, remaining)
        self.calls.clear()
        for _ in range(2):
            self.refresh(subject)
        self.assert_valid_batches(remaining)
        self.assertEqual(set().union(*map(set, self.calls)), set(remaining))

    def test_title_refresh_keeps_existing_time_throttle(self):
        records = self.seed(250)
        subject = meter.Meter(self.folder)
        self.refresh(subject, advance=1)
        self.refresh(subject, advance=60)
        self.assertEqual(len(self.calls), 1)
        self.refresh(subject)
        self.assertEqual(len(self.calls), 2)
        self.assert_valid_batches(records)
        self.assertFalse(set(self.calls[0]) & set(self.calls[1]))


if __name__ == "__main__":
    unittest.main()
