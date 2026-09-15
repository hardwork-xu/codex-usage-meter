"""Calendar and pricing coverage checks using synthetic event-day counters."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from periods import summarize_periods


def counters(total):
    return {"total": total, "input": total, "cachedInput": 0, "cacheWriteInput": 0,
            "output": 0, "reasoningOutput": 0}


def turn(days, *, ident="turn-fixture", thread="thread-fixture", model="gpt-6-astra", undated=0):
    return {"id": ident, "threadId": thread, "model": model, "serviceTier": None,
            "pricingMetadataStatus": "unknown", "quality": "complete", "status": "completed",
            "startedAt": 1, "tokens": counters(sum(days.values()) + undated),
            "dailyUsage": {day: counters(amount) for day, amount in days.items()},
            "undatedTokens": counters(undated)}


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"pricingMode": "official", "speedMode": "standard", "usdPerCredit": "0.04",
                         "currencyPerUsd": "1", "subscriptionRenewalDay": 9}
        self.now = date(2026, 9, 15)

    def summarize(self, turns, **kwargs):
        return summarize_periods(turns, self.settings, now=kwargs.pop("now", self.now), **kwargs)

    def test_midnight_uses_usage_dates_not_turn_start(self):
        value = turn({"2026-09-14": 1000, "2026-09-15": 2000})
        result = self.summarize([value])
        self.assertEqual(result["today"]["tokens"]["total"], 2000)
        self.assertEqual(result["today"]["amount"], "0.020000")
        self.assertEqual(result["subscription"]["tokens"]["total"], 3000)
        self.assertEqual(result["subscription"]["turnCount"], 1)

    def test_cycle_excludes_previous_day_and_next_renewal(self):
        value = turn({"2026-09-08": 1, "2026-09-09": 10, "2026-10-08": 20, "2026-10-09": 100})
        result = self.summarize([value])["subscription"]
        self.assertEqual((result["startDate"], result["endDate"]), ("2026-09-09", "2026-10-09"))
        self.assertEqual(result["tokens"]["total"], 30)
        before = self.summarize([], now=date(2026, 9, 8))["subscription"]
        self.assertEqual((before["startDate"], before["endDate"]), ("2026-08-09", "2026-09-09"))

    def test_renewal_31_clamps_each_month_and_leap_year(self):
        self.settings["subscriptionRenewalDay"] = 31
        for today, expected in ((date(2026, 2, 27), ("2026-01-31", "2026-02-28")),
                                (date(2026, 2, 28), ("2026-02-28", "2026-03-31")),
                                (date(2028, 2, 29), ("2028-02-29", "2028-03-31")),
                                (date(2026, 1, 1), ("2025-12-31", "2026-01-31"))):
            with self.subTest(today=today):
                value = self.summarize([], now=today)["subscription"]
                self.assertEqual((value["startDate"], value["endDate"]), expected)

    def test_spark_only_never_becomes_zero_cost(self):
        result = self.summarize([turn({"2026-09-15": 500}, model="gpt-5.3-codex-spark")])["today"]
        self.assertEqual(result["tokens"]["total"], 500)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["amount"])
        self.assertIsNone(result["credits"])
        self.assertEqual((result["unpricedTokens"], result["unpricedTurnCount"]), (500, 1))
        self.assertTrue(result["partial"])

    def test_unknown_plus_known_preserves_only_priced_subtotal(self):
        result = self.summarize([turn({"2026-09-15": 1000}),
                                 turn({"2026-09-15": 500}, ident="spark", model="gpt-5.3-codex-spark")])["today"]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["amount"], "0.010000")
        self.assertEqual(result["tokens"]["total"], 1500)
        self.assertEqual(result["unpricedTokens"], 500)

    def test_duplicate_snapshots_count_once_but_thread_ids_are_independent(self):
        first = turn({"2026-09-15": 100})
        latest = turn({"2026-09-15": 200})
        other = turn({"2026-09-15": 300}, thread="another-thread")
        result = self.summarize([first, latest, deepcopy(latest), other])["today"]
        self.assertEqual(result["tokens"]["total"], 500)
        self.assertEqual(result["turnCount"], 2)

    def test_all_turns_are_aggregated_before_display_limit(self):
        result = self.summarize([turn({"2026-09-15": 100}, ident=f"fixture-{n}") for n in range(251)])["today"]
        self.assertEqual(result["turnCount"], 251)
        self.assertEqual(result["tokens"]["total"], 25100)

    def test_unassigned_is_never_inferred_from_start_date(self):
        value = turn({}, undated=1000)
        result = self.summarize([value])
        self.assertEqual(result["unassignedTokens"], 1000)
        for period in (result["today"], result["subscription"]):
            self.assertIsNone(period["tokens"])
            self.assertIsNone(period["amount"])
            self.assertEqual(period["unassignedTokens"], 1000)
            self.assertTrue(period["partial"])

    def test_legacy_tokens_without_daily_buckets_are_unassigned(self):
        value = turn({"2026-09-15": 100})
        value.pop("dailyUsage")
        value.pop("undatedTokens")
        self.assertEqual(self.summarize([value])["unassignedTokens"], 100)

    def test_unknown_counters_and_unfinished_reads_do_not_report_empty_zero(self):
        for values, kwargs in (([], {"reading_incomplete": True}), ([], {"read_error_count": 1}),
                               ([{"id": "unknown", "tokens": None}], {})):
            with self.subTest(kwargs=kwargs):
                result = self.summarize(values, **kwargs)["today"]
                self.assertIsNone(result["amount"])
                self.assertIsNone(result["tokens"])
                self.assertTrue(result["partial"])

    def test_partial_turn_and_loading_preserve_observed_subtotals(self):
        value = turn({"2026-09-15": 1000})
        value["quality"] = "partial"
        result = self.summarize([value], reading_incomplete=True)["today"]
        self.assertEqual(result["amount"], "0.010000")
        self.assertEqual(result["status"], "partial")

    def test_empty_complete_period_is_labeled_observed_empty(self):
        result = self.summarize([])["today"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["amount"], "0.000000")
        self.assertEqual(result["tokens"]["total"], 0)
        self.assertIn("暂无已记录用量", result["note"])

    def test_custom_mode_preserves_null_official_currency_totals(self):
        self.settings.update(pricingMode="custom", ratePerMillion="2")
        result = self.summarize([turn({"2026-09-15": 1000})])["today"]
        self.assertEqual(result["amount"], "0.002000")
        self.assertIsNone(result["credits"])
        self.assertIsNone(result["usd"])

    def test_days_are_summed_before_rounding_one_turn(self):
        self.settings.update(pricingMode="custom", ratePerMillion="0.4")
        value = turn({"2026-09-14": 1, "2026-09-15": 1})
        result = self.summarize([value])["subscription"]
        self.assertEqual(result["amount"], "0.000001")

    def test_no_renewal_day_requires_configuration(self):
        for value in (None, True, 0, 32, "9"):
            with self.subTest(value=value):
                self.settings["subscriptionRenewalDay"] = value
                result = self.summarize([])
                self.assertEqual(result["subscription"]["status"], "needs_configuration")
                self.assertIsNone(result["subscription"]["amount"])
                self.assertIsNone(result["renewalDay"])

    def test_aware_clock_uses_injected_local_date_and_rejects_naive_time(self):
        now = datetime(2026, 9, 15, 0, 5, tzinfo=timezone(timedelta(hours=8)))
        result = self.summarize([turn({"2026-09-15": 100})], now=now)["today"]
        self.assertEqual(result["startDate"], "2026-09-15")
        self.assertEqual(result["tokens"]["total"], 100)
        with self.assertRaises(ValueError):
            self.summarize([], now=now.replace(tzinfo=None))

    def test_incoherent_daily_buckets_cannot_inflate_totals(self):
        value = turn({"2026-09-15": 100})
        value["dailyUsage"]["2026-09-15"] = counters(200)
        result = self.summarize([value])
        self.assertIsNone(result["today"]["tokens"])
        self.assertEqual(result["unassignedTokens"], 100)

    def test_input_is_not_mutated(self):
        values = [turn({"2026-09-14": 10, "2026-09-15": 20})]
        before = deepcopy(values)
        self.summarize(values)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
