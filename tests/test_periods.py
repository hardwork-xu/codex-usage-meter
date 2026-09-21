"""Calendar and pricing coverage checks using synthetic event-day counters."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
        self.assertEqual(result["models"][0]["tokens"]["total"], 500)
        self.assertEqual(result["models"][0]["turnCount"], 2)

    def test_all_turns_are_aggregated_before_display_limit(self):
        result = self.summarize([turn({"2026-09-15": 100}, ident=f"fixture-{n}") for n in range(251)])["today"]
        self.assertEqual(result["turnCount"], 251)
        self.assertEqual(result["tokens"]["total"], 25100)
        self.assertEqual(result["models"][0]["turnCount"], 251)
        self.assertEqual(result["models"][0]["tokens"]["total"], 25100)

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

    def assertModelsConservePeriod(self, period):
        groups = period["models"]
        if period["tokens"] is not None:
            for key, total in period["tokens"].items():
                self.assertEqual(sum(group["tokens"][key] for group in groups), total, key)
        self.assertEqual(sum(group["turnCount"] for group in groups), period["turnCount"])
        self.assertEqual(sum(group["unpricedTokens"] for group in groups), period["unpricedTokens"])
        for key in ("amount", "credits", "usd"):
            known = [Decimal(group[key]) for group in groups if group[key] is not None]
            if known:
                self.assertEqual(sum(known), Decimal(period[key]), key)
            elif groups:
                self.assertIsNone(period[key], key)

    def test_models_are_sorted_with_labels_and_conserve_money_and_tokens(self):
        result = self.summarize([
            turn({"2026-09-15": 1000000}, ident="luna", model="gpt-5.6-luna"),
            turn({"2026-09-15": 12000000}, ident="astra"),
        ])
        for name in ("today", "subscription"):
            period = result[name]
            self.assertEqual([group["model"] for group in period["models"]], ["gpt-6-astra", "gpt-5.6-luna"])
            self.assertEqual([group["label"] for group in period["models"]], ["GPT-6 Astra", "GPT-5.6 Luna"])
            self.assertEqual([group["tokens"]["total"] for group in period["models"]], [12000000, 1000000])
            self.assertEqual(period["models"][0]["amount"], "120.000000")
            self.assertEqual(period["models"][1]["amount"], "0.200000")
            self.assertModelsConservePeriod(period)

    def test_models_use_period_dates_and_exclude_undated_usage(self):
        result = self.summarize([
            turn({"2026-09-08": 9000, "2026-09-14": 200, "2026-09-15": 100}, undated=50),
            turn({"2026-09-15": 150}, ident="luna", model="gpt-5.6-luna"),
        ])
        self.assertEqual([group["model"] for group in result["today"]["models"]], ["gpt-5.6-luna", "gpt-6-astra"])
        self.assertEqual(result["subscription"]["models"][0]["tokens"]["total"], 300)
        self.assertEqual(result["unassignedTokens"], 50)
        for period in (result["today"], result["subscription"]):
            self.assertTrue(all(group["partial"] for group in period["models"]))
            self.assertModelsConservePeriod(period)

    def test_unknown_mixed_and_unsupported_model_ids_are_not_guessed(self):
        unknown = turn({"2026-09-15": 100}, ident="unknown", model=None)
        mixed = turn({"2026-09-15": 200}, ident="mixed", model=None)
        mixed["pricingMetadataStatus"] = "mixed"
        unsupported = turn({"2026-09-15": 50}, ident="future", model="future-fixture-model")
        period = self.summarize([unknown, mixed, unsupported])["today"]
        group, future = period["models"]
        self.assertIsNone(group["model"])
        self.assertEqual(group["label"], "未确认模型")
        self.assertEqual(group["tokens"]["total"], 300)
        self.assertEqual(group["turnCount"], 2)
        self.assertEqual(future["model"], "future-fixture-model")
        self.assertEqual(future["label"], "future-fixture-model")
        self.assertTrue(all(item["amount"] is None for item in period["models"]))
        self.assertModelsConservePeriod(period)

    def test_missing_or_mixed_tier_does_not_erase_a_known_model(self):
        normal = turn({"2026-09-15": 1000})
        mixed_tier = turn({"2026-09-15": 500}, ident="mixed-tier")
        mixed_tier["pricingMetadataStatus"] = "mixed"
        period = self.summarize([normal, mixed_tier])["today"]
        self.assertEqual(len(period["models"]), 1)
        group = period["models"][0]
        self.assertEqual(group["model"], "gpt-6-astra")
        self.assertEqual(group["amount"], "0.010000")
        self.assertEqual(group["unpricedTokens"], 500)
        self.assertTrue(group["partial"])
        self.assertModelsConservePeriod(period)

    def test_spark_and_all_six_counter_fields_are_preserved(self):
        astra = turn({"2026-09-15": 120})
        astra["tokens"] = {"total": 120, "input": 100, "output": 20,
                           "cachedInput": 40, "cacheWriteInput": 2, "reasoningOutput": 10}
        astra["dailyUsage"]["2026-09-15"] = dict(astra["tokens"])
        spark = turn({"2026-09-15": 500}, ident="spark", model="gpt-5.3-codex-spark")
        period = self.summarize([astra, spark])["today"]
        self.assertEqual(period["models"][0]["model"], "gpt-5.3-codex-spark")
        self.assertEqual(period["models"][0]["unpricedTokens"], 500)
        self.assertTrue(all(group["amount"] is None for group in period["models"]))
        self.assertEqual(period["models"][1]["tokens"], astra["tokens"])
        self.assertModelsConservePeriod(period)

    def test_custom_pricing_and_partial_reads_are_preserved_per_model(self):
        self.settings.update(pricingMode="custom", ratePerMillion="2")
        result = self.summarize([turn({"2026-09-15": 1000}, model=None)], reading_incomplete=True)["today"]
        group = result["models"][0]
        self.assertIsNone(group["model"])
        self.assertEqual(group["amount"], "0.002000")
        self.assertIsNone(group["credits"])
        self.assertIsNone(group["usd"])
        self.assertEqual(group["unpricedTokens"], 0)
        self.assertTrue(group["partial"])
        self.assertModelsConservePeriod(result)

    def test_empty_and_unconfigured_periods_have_no_model_groups(self):
        result = self.summarize([])
        self.assertEqual(result["today"]["models"], [])
        self.assertEqual(result["subscription"]["models"], [])
        self.settings["subscriptionRenewalDay"] = None
        result = self.summarize([turn({"2026-09-15": 100})])
        self.assertEqual(result["subscription"]["models"], [])

    def test_invalid_model_metadata_is_not_exposed_as_a_label(self):
        value = turn({"2026-09-15": 100}, model="synthetic text with spaces")
        group = self.summarize([value])["today"]["models"][0]
        self.assertIsNone(group["model"])
        self.assertEqual(group["label"], "未确认模型")


if __name__ == "__main__":
    unittest.main()
