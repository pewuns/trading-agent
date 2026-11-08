import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytz

import agent
import config


class StageTwoTests(unittest.TestCase):
    def test_shadow_mode_learns_with_separate_feature_flags(self):
        shadow = config.FEATURES[config.RunMode.SHADOW]
        self.assertTrue(shadow['update_learned_weights'])
        self.assertTrue(shadow['record_performance_stats'])
        self.assertTrue(shadow['update_adaptive_threshold'])
        self.assertNotEqual(agent.SHADOW_FEATURE_WEIGHTS_FILE, agent.FEATURE_WEIGHTS_FILE)
        self.assertEqual(agent.SHADOW_PERFORMANCE_STATS_FILE, 'shadow_performance_stats.json')
        with patch.object(agent, 'AGENT_MODE', agent.RunMode.SHADOW):
            self.assertEqual(
                agent._near_miss_files_for_mode(),
                (
                    agent.SHADOW_NEAR_MISS_LOG_FILE,
                    agent.SHADOW_NEAR_MISS_PENDING_FILE,
                    agent.SHADOW_DAILY_SCORES_CACHE_FILE,
                ),
            )

    def test_shadow_model_state_is_seeded_once_from_production(self):
        with tempfile.TemporaryDirectory() as directory:
            live_weights = Path(directory) / 'live.json'
            shadow_weights = Path(directory) / 'shadow.json'
            live_weights.write_text('{"weights": {"15m": 0.3}}')
            with patch.object(agent, 'TIMEFRAME_WEIGHTS_FILE', str(live_weights)), \
                    patch.object(agent, 'SHADOW_TIMEFRAME_WEIGHTS_FILE', str(shadow_weights)), \
                    patch.object(agent, 'FEATURE_WEIGHTS_FILE', str(Path(directory) / 'missing-features.json')), \
                    patch.object(agent, 'SHADOW_FEATURE_WEIGHTS_FILE', str(Path(directory) / 'shadow-features.json')), \
                    patch.object(agent, 'ADAPTIVE_THRESHOLD_FILE', str(Path(directory) / 'missing-thresholds.json')), \
                    patch.object(agent, 'SHADOW_ADAPTIVE_THRESHOLD_FILE', str(Path(directory) / 'shadow-thresholds.json')):
                agent.initialize_shadow_learning_state()
                live_weights.write_text('{"weights": {"15m": 0.9}}')
                agent.initialize_shadow_learning_state()
            self.assertEqual(shadow_weights.read_text(), '{"weights": {"15m": 0.3}}')

    def test_near_miss_cleanup_enforces_ttl_and_capacity(self):
        now = datetime.now(pytz.utc)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(agent, 'MAX_NEAR_MISS_PENDING', 2):
            path = Path(directory) / 'pending.json'
            path.write_text(json.dumps({
                'expired': {'created_at': (now - timedelta(hours=49)).isoformat()},
                'oldest': {'created_at': (now - timedelta(hours=3)).isoformat()},
                'middle': {'created_at': (now - timedelta(hours=2)).isoformat()},
                'newest': {'created_at': (now - timedelta(hours=1)).isoformat()},
            }))
            tracker = agent.NearMissTracker(str(path))
            self.assertEqual(set(tracker.data), {'middle', 'newest'})
            self.assertTrue(tracker.dirty)
            self.assertTrue(tracker.save())
            self.assertEqual(set(json.loads(path.read_text())), {'middle', 'newest'})

    def test_near_miss_adds_are_batched_until_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pending.json'
            tracker = agent.NearMissTracker(str(path))
            tracker.add('BITCOIN', 'LONG', 0.55, 100, 1, {})
            tracker.add('BITCOIN', 'SHORT', 0.54, 100, 1, {})
            self.assertFalse(path.exists())
            self.assertTrue(tracker.save())
            self.assertEqual(len(json.loads(path.read_text())), 2)

    def test_near_miss_pending_is_written_compactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pending.json'
            tracker = agent.NearMissTracker(str(path))
            tracker.add('BITCOIN', 'LONG', 0.55, 100, 1, {'trend': 1})
            tracker.save()
            contents = path.read_text()
            self.assertNotIn('\n  ', contents)
            self.assertEqual(len(json.loads(contents)), 1)

    def test_log_rotation_keeps_recent_records_and_aggregates_old_errors(self):
        now = datetime.now(pytz.utc)
        old = (now - timedelta(days=40)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data_fetch_errors.jsonl'
            path.write_text(
                json.dumps({'timestamp': old, 'source': 'yahoo', 'status_code': 429, 'error': 'rate limit'}) + '\n'
                + json.dumps({'timestamp': old, 'source': 'yahoo', 'status_code': 429, 'error': 'another detail'}) + '\n'
                + json.dumps({'timestamp': recent, 'source': 'yahoo', 'status_code': 500, 'error': 'server'}) + '\n'
                + 'malformed record\n'
            )

            self.assertTrue(agent.rotate_jsonl_log(str(path), days_to_keep=30))

            remaining = path.read_text().splitlines()
            self.assertEqual(len(remaining), 2)
            self.assertEqual(json.loads(remaining[0])['status_code'], 500)
            self.assertEqual(remaining[1], 'malformed record')
            summary_path = Path(directory) / 'data_fetch_errors_summary.jsonl'
            summary = [json.loads(line) for line in summary_path.read_text().splitlines()]
            self.assertEqual(summary[0]['total'], 2)
            self.assertEqual(summary[0]['breakdown'], {'yahoo:HTTP 429': 2})

    def test_shadow_scoring_log_rotation_summarizes_model_directions(self):
        now = datetime.now(pytz.utc)
        old = (now - timedelta(days=31)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'shadow_scoring_log.jsonl'
            path.write_text(json.dumps({
                'timestamp': old,
                'production_direction': 'LONG',
                'signal_engine_direction': 'SHORT',
            }) + '\n')

            self.assertTrue(agent.rotate_jsonl_log(str(path), days_to_keep=30))

            summary = json.loads(
                (Path(directory) / 'shadow_scoring_log_summary.jsonl').read_text()
            )
            self.assertEqual(summary['total'], 1)
            self.assertEqual(
                summary['breakdown'],
                {'production=LONG,signal_engine=SHORT': 1},
            )
            self.assertEqual(path.read_text(), '')

    def test_log_maintenance_runs_once_per_utc_day(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'maintenance.json'
            with patch.object(agent, 'LOG_MAINTENANCE_STATE_FILE', str(state_path)), \
                    patch.object(agent, 'rotate_jsonl_log', return_value=True) as rotate:
                agent.maintain_runtime_logs()
                agent.maintain_runtime_logs()
            self.assertEqual(rotate.call_count, 4)
            self.assertIn('last_maintenance_date', json.loads(state_path.read_text()))

    def test_near_miss_evaluation_fetches_once_per_market(self):
        now = datetime.now(pytz.utc)
        item = {
            'name': 'BITCOIN',
            'direction': 'LONG',
            'confidence': 0.55,
            'entry': 100.0,
            'atr': 1.0,
            'features': {},
            'created_at': (now - timedelta(hours=5)).isoformat(),
        }
        tracker = agent.NearMissTracker('/not/a/real/file')
        tracker.data = {'first': dict(item), 'second': dict(item)}
        data = {
            'prices': [100.0],
            'highs': [100.5],
            'lows': [99.5],
        }
        with patch.dict(agent.MARKETS, {'BITCOIN': {'symbol': 'BTC'}}), \
                patch.object(agent, 'get_price_data', return_value=(data, 'test')) as get_data:
            tracker.evaluate(SimpleNamespace(update=lambda *args, **kwargs: None), now=now)
        get_data.assert_called_once_with('BITCOIN', 'BTC', '15m', '5d')

    def test_adaptive_threshold_uses_r_expectancy(self):
        with tempfile.TemporaryDirectory() as directory:
            thresholds = agent.AdaptiveThreshold(str(Path(directory) / 'thresholds.json'))
            for index in range(15):
                r_multiple = 5 / 3 if index < 8 else -1.0
                thresholds.record_outcome('BITCOIN', 0.6, r_multiple)
            self.assertGreater(thresholds._recalibrate(thresholds.data['BITCOIN']['history']), 0)
            self.assertEqual(thresholds.get_threshold('BITCOIN'), 0.6)

    def test_timeout_records_do_not_count_toward_kelly_sample_minimum(self):
        stats = agent.PerformanceStats('/not/a/real/file')
        stats.records = [
            {'outcome': 'win', 'r_multiple': 1.0},
            {'outcome': 'loss', 'r_multiple': -1.0},
            {'outcome': 'timeout', 'r_multiple': 0.5},
        ]
        self.assertIsNone(stats.get_stats(min_samples=3))

    def test_legacy_win_loss_history_is_not_used_as_r_expectancy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'thresholds.json'
            path.write_text(json.dumps({
                'BITCOIN': {
                    'threshold': 0.9,
                    'history': [[0.6, True]] * 20,
                }
            }))
            thresholds = agent.AdaptiveThreshold(str(path))
            self.assertEqual(thresholds.get_threshold('BITCOIN'), agent.AdaptiveThreshold.DEFAULT)
            self.assertEqual(thresholds.data['BITCOIN']['history'], [])

    def test_cost_filters_require_minimum_stop_and_target_distance(self):
        self.assertTrue(agent.passes_spread_cost_filters(4.0, 3.0, 1.0))
        self.assertFalse(agent.passes_spread_cost_filters(3.99, 3.0, 1.0))
        self.assertFalse(agent.passes_spread_cost_filters(4.0, 2.99, 1.0))
        self.assertFalse(agent.passes_spread_cost_filters(4.0, 3.0, None))

    def test_429_retry_after_header_controls_retry_delay(self):
        rate_limited = SimpleNamespace(
            status_code=429,
            text='rate limited',
            headers={'Retry-After': '7'},
        )
        success = SimpleNamespace(status_code=200)
        with patch.object(agent.requests, 'get', side_effect=[rate_limited, success]), \
                patch.object(agent.time, 'sleep') as sleep:
            response, error = agent.http_get_with_retry(
                'https://example.test/data', retries=2
            )
        self.assertIs(response, success)
        self.assertIsNone(error)
        sleep.assert_called_once_with(7.0)

    def test_long_429_retry_after_does_not_retry_early(self):
        rate_limited = SimpleNamespace(
            status_code=429,
            text='rate limited',
            headers={'Retry-After': '1800'},
        )
        with patch.object(agent.requests, 'get', return_value=rate_limited) as get, \
                patch.object(agent.time, 'sleep') as sleep:
            response, error = agent.http_get_with_retry(
                'https://example.test/data', retries=3
            )
        self.assertIsNone(response)
        self.assertIn('HTTP 429', error)
        get.assert_called_once()
        sleep.assert_not_called()

    def test_missing_cryptocompare_key_is_logged_once(self):
        with patch.object(agent, 'CRYPTOCOMPARE_API_KEY', ''), \
                patch.object(agent, '_MISSING_API_KEYS_LOGGED', set()), \
                patch.object(agent, '_get_cryptocompare_cached', return_value=None), \
                patch.object(agent, 'log_data_error') as log_error:
            agent.fetch_cryptocompare_data('BITCOIN')
            agent.fetch_cryptocompare_data('ETHEREUM')
        log_error.assert_called_once()

    def test_twelvedata_missing_key_uses_only_fresh_cache(self):
        now = datetime.now(pytz.utc)
        cached_data = {'prices': [1.0]}
        cache = {
            'EUR/USD': {
                '15m': {
                    'cached_at': now.isoformat(),
                    'data': cached_data,
                }
            }
        }
        with patch.object(agent, 'TWELVEDATA_API_KEY', ''), \
                patch.object(agent, '_MISSING_API_KEYS_LOGGED', set()), \
                patch.object(agent, '_load_twelvedata_cache', return_value=cache), \
                patch.object(agent, 'log_data_error'):
            self.assertEqual(
                agent.fetch_twelvedata_data('EUR/USD', '15m'),
                cached_data,
            )

    def test_fifteen_minute_context_uses_five_day_yahoo_range(self):
        self.assertEqual(agent.TIMEFRAMES['15m']['range'], '5d')
        self.assertEqual(agent.DECISION_TIMEFRAMES, ('1h', '4h', '1d'))

    def test_cryptocompare_cache_rejects_stale_intraday_data(self):
        now = datetime.now(pytz.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crypto-cache.json'
            path.write_text(json.dumps({
                'BITCOIN': {
                    '15m': {
                        'cached_at': (now - timedelta(minutes=31)).isoformat(),
                        'data': {'prices': [100.0]},
                    }
                }
            }))
            with patch.object(agent, 'CRYPTOCOMPARE_CACHE_FILE', str(path)):
                self.assertIsNone(agent._get_cryptocompare_cached('BITCOIN', '15m'))


if __name__ == '__main__':
    unittest.main()
