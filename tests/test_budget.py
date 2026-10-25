"""Unit tests for the pure cost-accounting module (no AstrBot required)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import budget  # noqa: E402

CN = timezone(timedelta(hours=8))


def _row(
    *,
    provider_id="deepseek/deepseek-flash",
    provider_model="deepseek-flash",
    cached=0,
    other=0,
    output=0,
    end_ts=0.0,
    created="2026-10-22 02:00:00",
    umo="test:GroupMessage:1",
):
    return {
        "provider_id": provider_id,
        "provider_model": provider_model,
        "umo": umo,
        "token_input_other": other,
        "token_input_cached": cached,
        "token_output": output,
        "start_time": end_ts,
        "end_time": end_ts,
        "created_at": created,
    }


def _sample_config():
    return {
        "provider_sources": {
            "deepseek": {
                "id": "deepseek",
                "type": "openai_chat_completion",
                "key": ["sk-1f4f90352cc14cd0bbcbed5769bdd42d"],
            },
            "mimo_tts": {
                "id": "mimo_tts",
                "provider_type": "text_to_speech",
                "key": ["sk-tts-secret"],
            },
        },
        "provider": [
            {
                "id": "deepseek/deepseek-flash",
                "provider_source_id": "deepseek",
                "model": "deepseek-flash",
            },
            {
                "id": "mimo_tts",
                "type": "mimo_tts_api",
                "provider_type": "text_to_speech",
                "api_key": "sk-tts-secret",
            },
        ],
        "provider_settings": {
            "websearch_provider": "deepseek",
            "websearch_deepseek_key": ["sk-search-abcdef123456"],
        },
    }


class MaskKeyTest(unittest.TestCase):
    def test_mask_keeps_prefix_and_suffix(self):
        self.assertEqual(
            budget.mask_key("sk-1f4f90352cc14cd0bbcbed5769bdd42d"),
            "sk-***d42d",
        )

    def test_mask_short_and_empty(self):
        self.assertEqual(budget.mask_key("abc"), "***")
        self.assertEqual(budget.mask_key(""), "")


class PeakConfigTest(unittest.TestCase):
    def setUp(self):
        self.peak = budget.parse_peak_config(
            ["09:00-12:00", "14:00-18:00"], [1, 2, 3, 4, 5], ["2026-10-01"]
        )

    def test_peak_inside_window(self):
        self.assertTrue(self.peak.is_peak(datetime(2026, 10, 22, 10, 0, tzinfo=CN)))

    def test_midday_is_off_peak(self):
        self.assertFalse(self.peak.is_peak(datetime(2026, 10, 22, 12, 30, tzinfo=CN)))

    def test_weekend_is_off_peak(self):
        self.assertFalse(self.peak.is_peak(datetime(2026, 10, 24, 10, 0, tzinfo=CN)))

    def test_holiday_is_off_peak(self):
        self.assertFalse(self.peak.is_peak(datetime(2026, 10, 1, 10, 0, tzinfo=CN)))

    def test_empty_windows_mean_no_peak(self):
        no_peak = budget.parse_peak_config([], [1, 2, 3, 4, 5], [])
        self.assertFalse(no_peak.is_peak(datetime(2026, 10, 22, 10, 0, tzinfo=CN)))


class PricingTest(unittest.TestCase):
    def test_lookup_exact_and_suffix(self):
        table = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        exact, exact_ok = budget.lookup_price(table, "deepseek-flash")
        suffix, suffix_ok = budget.lookup_price(table, "deepseek-flash-1017")
        self.assertTrue(exact_ok)
        self.assertTrue(suffix_ok)
        self.assertEqual(exact["off_peak"], suffix["off_peak"])

    def test_unknown_model_falls_back_to_default(self):
        table = budget.normalize_pricing({"my-model": {"input": 1, "output": 2}})
        price_pair, ok = budget.lookup_price(table, "some-other-model")
        self.assertFalse(ok)
        self.assertEqual(price_pair["peak"], table["default"]["peak"])


class EstimateCostTest(unittest.TestCase):
    def test_peak_and_off_peak(self):
        table = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        peak = budget.estimate_cost("deepseek-flash", 1_000_000, 0, 1_000_000, table, True)
        off = budget.estimate_cost("deepseek-flash", 1_000_000, 0, 1_000_000, table, False)
        self.assertAlmostEqual(peak, 10.0, places=6)
        self.assertAlmostEqual(off, 5.0, places=6)


class DayRangeTest(unittest.TestCase):
    def test_midnight_reset(self):
        start, end = budget.local_day_range(datetime(2026, 10, 22, 15, 0, tzinfo=CN), CN, 0)
        self.assertEqual(start, datetime(2026, 10, 22, 0, 0, tzinfo=CN))
        self.assertEqual(end, datetime(2026, 10, 23, 0, 0, tzinfo=CN))

    def test_five_am_reset_before_reset_hour(self):
        start, _end = budget.local_day_range(datetime(2026, 10, 22, 3, 0, tzinfo=CN), CN, 5)
        self.assertEqual(start, datetime(2026, 10, 21, 5, 0, tzinfo=CN))


class ScopeFromUmoTest(unittest.TestCase):
    def test_group_and_private(self):
        self.assertEqual(budget.scope_from_umo("default_1:GroupMessage:abc"), "group")
        self.assertEqual(budget.scope_from_umo("default_1:FriendMessage:abc"), "private")
        self.assertEqual(budget.scope_from_umo(""), "group")


class KeyMapTest(unittest.TestCase):
    def test_chat_and_websearch_keys(self):
        chat_map, websearch = budget.build_key_map(_sample_config())
        ref = chat_map["deepseek/deepseek-flash"]
        raw = "sk-1f4f90352cc14cd0bbcbed5769bdd42d"
        self.assertEqual(ref.key_id, raw)
        self.assertEqual(ref.masked, "sk-***" + raw[-4:])
        self.assertEqual(ref.kind, "chat")
        # TTS providers must not appear as chat keys.
        self.assertNotIn("mimo_tts", chat_map)
        self.assertIsNotNone(websearch)
        self.assertEqual(websearch.key_id, "websearch:deepseek")

    def test_list_style_sources(self):
        config = _sample_config()
        config["provider_sources"] = [
            {"id": "deepseek", "key": ["sk-abc123456789"]},
        ]
        chat_map, _ = budget.build_key_map(config)
        self.assertEqual(chat_map["deepseek/deepseek-flash"].key_id, "sk-abc123456789")


class BudgetOverrideTest(unittest.TestCase):
    def test_source_id_override(self):
        ref = budget.KeyRef(
            key_id="sk-1", masked="sk-***1", label="x", source_ids=("deepseek", "p"), kind="chat"
        )
        self.assertEqual(budget.resolve_budget(ref, 2.0, {"deepseek": 5}), 5.0)
        self.assertEqual(budget.resolve_budget(ref, 2.0, {}), 2.0)


class ComputeReportTest(unittest.TestCase):
    def test_cost_with_cache_and_peak_split(self):
        chat_map, websearch = budget.build_key_map(_sample_config())
        pricing = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        peak = budget.parse_peak_config(["09:00-12:00"], [1, 2, 3, 4, 5], [])
        # 10:00 on Thursday (peak): 1M cached + 1M uncached + 1M output
        peak_ts = datetime(2026, 10, 22, 10, 0, tzinfo=CN).timestamp()
        # 20:00 same day (off-peak)
        off_ts = datetime(2026, 10, 22, 20, 0, tzinfo=CN).timestamp()
        rows = [
            _row(cached=1_000_000, other=1_000_000, output=1_000_000, end_ts=peak_ts),
            _row(cached=0, other=1_000_000, output=0, end_ts=off_ts),
        ]
        report = budget.compute_report(
            rows,
            chat_map,
            websearch,
            3,
            pricing=pricing,
            tz=CN,
            peak_config=peak,
            default_budget=2.0,
            overrides={},
            websearch_price_per_1000=0.0,
            date_label="2026-10-22",
        )
        chat_group = next(g for g in report.groups if g.kind == "chat")
        # peak: 0.04 + 2 + 8 = 10.04 ; off-peak: 1.0
        self.assertAlmostEqual(chat_group.spent, 11.04, places=6)
        self.assertEqual(chat_group.usage.count, 2)
        self.assertAlmostEqual(chat_group.usage.peak_cost, 10.04, places=6)
        self.assertTrue(chat_group.over_budget)
        search_group = report.websearch_group()
        self.assertIsNotNone(search_group)
        self.assertEqual(search_group.usage.count, 3)
        self.assertEqual(len(report.blockers()), 1)

    def test_shared_budget_combines_chat_and_websearch(self):
        chat_map, websearch = budget.build_key_map(_sample_config())
        pricing = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        peak = budget.parse_peak_config(["09:00-12:00"], [1, 2, 3, 4, 5], [])
        off_ts = datetime(2026, 10, 22, 20, 0, tzinfo=CN).timestamp()
        rows = [_row(cached=0, other=1_000_000, output=0, end_ts=off_ts)]
        report = budget.compute_report(
            rows,
            chat_map,
            websearch,
            2,
            pricing=pricing,
            tz=CN,
            peak_config=peak,
            default_budget=2.0,
            overrides={},
            websearch_price_per_1000=500.0,
            date_label="2026-10-22",
            budget_mode="shared",
        )
        # 1M off-peak input = ¥1.00, two searches = ¥1.00, total exactly ¥2.00.
        self.assertTrue(report.shared)
        self.assertAlmostEqual(report.shared_spent, 2.0, places=6)
        self.assertTrue(report.shared_over)
        self.assertTrue(report.websearch_over_budget())
        self.assertTrue(report.blockers())
        text = budget.format_today(report)
        self.assertIn("共用", text)

    def test_session_type_buckets(self):
        pricing = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        peak = budget.parse_peak_config(["09:00-12:00"], [1, 2, 3, 4, 5], [])
        off_ts = datetime(2026, 10, 22, 20, 0, tzinfo=CN).timestamp()
        rows = [
            _row(other=1_000_000, end_ts=off_ts, umo="g:GroupMessage:1"),
            _row(other=1_000_000, end_ts=off_ts, umo="u:FriendMessage:1"),
        ]
        report = budget.compute_session_report(
            rows,
            {"group": 2, "private": 0},
            pricing=pricing,
            tz=CN,
            peak_config=peak,
            group_budget=0.0,
            private_budget=0.0,
            websearch_price_per_1000=500.0,
            date_label="2026-10-22",
            total_budget=1.5,
        )
        group = report.group("scope:group")
        private = report.group("scope:private")
        self.assertIsNotNone(group)
        self.assertIsNotNone(private)
        assert group is not None and private is not None
        # group: ¥1 token + ¥1 search = ¥2 ; private: ¥1 ; total ¥3 > ¥1.5
        self.assertAlmostEqual(group.spent, 2.0, places=6)
        self.assertAlmostEqual(private.spent, 1.0, places=6)
        self.assertTrue(report.total_cap_over)
        blockers = report.blockers("scope:private")
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].label, "今日总花费")
        text = budget.format_today(report)
        self.assertIn("总计：¥3.00 / ¥1.50", text)
        self.assertIn("群聊：¥2.00", text)
        self.assertIn("私聊：¥1.00", text)

    def test_unknown_provider_is_still_reported(self):
        pricing = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        report = budget.compute_report(
            [_row(provider_id="gone/model")],
            {},
            None,
            0,
            pricing=pricing,
            tz=CN,
            peak_config=budget.parse_peak_config([], [], []),
            default_budget=2.0,
            overrides={},
            websearch_price_per_1000=0.0,
            date_label="2026-10-22",
        )
        self.assertIn("未知来源", report.groups[0].label)


class QueryUsageTest(unittest.TestCase):
    def test_query_uses_end_time_and_created_at_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data_v4.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE provider_stats ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT, "
                "provider_model TEXT, umo TEXT, token_input_other INTEGER, "
                "token_input_cached INTEGER, token_output INTEGER, "
                "start_time REAL, end_time REAL, created_at TEXT)"
            )
            inside = datetime(2026, 10, 22, 10, 0, tzinfo=CN).timestamp()
            outside = datetime(2026, 10, 21, 10, 0, tzinfo=CN).timestamp()
            connection.execute(
                "INSERT INTO provider_stats (provider_id, provider_model, umo, "
                "token_input_other, token_input_cached, token_output, "
                "start_time, end_time, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("p", "m", "g:GroupMessage:1", 10, 20, 30, inside, inside, "2026-10-22 02:00:00"),
            )
            connection.execute(
                "INSERT INTO provider_stats (provider_id, provider_model, umo, "
                "token_input_other, token_input_cached, token_output, "
                "start_time, end_time, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("p", "m", "g:GroupMessage:1", 1, 2, 3, outside, outside, "2026-10-21 02:00:00"),
            )
            connection.commit()
            connection.close()

            start = datetime(2026, 10, 22, 0, 0, tzinfo=CN)
            end = datetime(2026, 10, 23, 0, 0, tzinfo=CN)
            rows = budget.query_usage(db_path, start, end)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["token_input_cached"], 20)

    def test_missing_database_raises(self):
        with self.assertRaises(budget.UsageDataUnavailable):
            budget.query_usage("definitely-missing.db", datetime.now(CN), datetime.now(CN))


class AggregateByDayTest(unittest.TestCase):
    def test_groups_by_local_date(self):
        pricing = budget.normalize_pricing(budget.DEFAULT_PRICING_JSON)
        peak = budget.parse_peak_config([], [], [])
        rows = [
            _row(
                cached=0,
                other=1_000_000,
                output=0,
                end_ts=datetime(2026, 10, 21, 20, 0, tzinfo=CN).timestamp(),
            ),
            _row(
                cached=0,
                other=1_000_000,
                output=0,
                end_ts=datetime(2026, 10, 22, 20, 0, tzinfo=CN).timestamp(),
            ),
        ]
        result = budget.aggregate_by_day(rows, pricing=pricing, tz=CN, peak_config=peak)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[date(2026, 10, 22)].cost, 1.0, places=6)


class FormatTest(unittest.TestCase):
    def test_format_today_contains_key_and_budget(self):
        chat_map, websearch = budget.build_key_map(_sample_config())
        report = budget.compute_report(
            [_row(cached=0, other=0, output=0)],
            chat_map,
            websearch,
            0,
            pricing=budget.normalize_pricing(budget.DEFAULT_PRICING_JSON),
            tz=CN,
            peak_config=budget.parse_peak_config([], [], []),
            default_budget=2.0,
            overrides={},
            websearch_price_per_1000=0.0,
            date_label="2026-10-22",
        )
        text = budget.format_today(report)
        self.assertIn("sk-***" + "d42d", text)
        self.assertIn("¥2.00", text)

    def test_format_history_contains_days(self):
        today = date(2026, 10, 22)
        day_costs = {
            today: budget.DayCost(day=today, cost=0.5, count=3),
        }
        text = budget.format_history(day_costs, today, 7)
        self.assertIn("最近 7 天", text)
        self.assertIn("10-22", text)
        self.assertIn("¥0.50", text)


if __name__ == "__main__":
    unittest.main()
