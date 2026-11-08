import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytz

import agent


class SignalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 8, 12, 0, tzinfo=pytz.utc)
        self.manager = agent.SignalManager(
            file_path=str(Path(self.temp_dir.name) / 'signals.json'),
            archive_path=str(Path(self.temp_dir.name) / 'archive.jsonl'),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_signal(self, direction='LONG', created_at=None):
        created_at = created_at or self.now - timedelta(minutes=30)
        is_long = direction == 'LONG'
        return {
            'signal_id': 'test-signal',
            'key': f'BITCOIN_{direction}',
            'name': 'BITCOIN',
            'direction': direction,
            'entry': 100.0,
            'stop_loss': 98.5 if is_long else 101.5,
            'take_profit': 102.5 if is_long else 97.5,
            'risk_distance': 1.5,
            'timestamp': created_at.isoformat(),
            'last_evaluated_at': created_at.isoformat(),
            'status': 'open',
            'confidence': 0.6,
            'features': {},
            'mae_percent': 0.0,
            'mfe_percent': 0.0,
        }

    def evaluate_with_bar(self, signal, *, low, high, close, max_age_hours=48, now=None):
        bar_time = self.now - timedelta(minutes=15)
        data = {
            'opens': [100.0],
            'highs': [high],
            'lows': [low],
            'prices': [close],
            'timestamps': [bar_time.timestamp()],
        }
        self.manager.signals = {signal['signal_id']: signal}
        with patch.object(agent, 'get_price_data', return_value=(data, 'test')), \
                patch.object(agent, 'should_update_weights', return_value=False), \
                patch.object(agent, 'should_update_threshold', return_value=False), \
                patch.object(agent, 'should_record_stats', return_value=False), \
                patch.object(agent, 'log_trade_outcome'):
            self.manager.evaluate_open_signals(
                object(), max_age_hours=max_age_hours, now=now or self.now
            )
        return signal

    def test_long_stop_is_filled_at_stop_even_when_close_gaps_below(self):
        signal = self.make_signal()
        closed = self.evaluate_with_bar(signal, low=98.0, high=100.5, close=90.0)
        self.assertEqual(closed['outcome'], 'loss')
        self.assertEqual(closed['closed_price'], 98.5)
        self.assertAlmostEqual(closed['r_multiple'], -1.0)
        archive = [
            json.loads(line)
            for line in Path(self.manager.archive_path).read_text().splitlines()
        ]
        self.assertEqual(archive[-1]['event'], 'closed')
        self.assertEqual(archive[-1]['signal_id'], signal['signal_id'])

    def test_first_hit_in_chronological_order_closes_the_position(self):
        signal = self.make_signal()
        self.manager.signals = {signal['signal_id']: signal}
        data = {
            'opens': [100.0, 100.0],
            'highs': [100.5, 103.0],
            'lows': [98.0, 99.0],
            'prices': [100.0, 102.0],
            'timestamps': [
                (self.now - timedelta(minutes=30)).timestamp(),
                (self.now - timedelta(minutes=15)).timestamp(),
            ],
        }
        with patch.object(agent, 'get_price_data', return_value=(data, 'test')), \
                patch.object(agent, 'should_update_weights', return_value=False), \
                patch.object(agent, 'should_update_threshold', return_value=False), \
                patch.object(agent, 'should_record_stats', return_value=False), \
                patch.object(agent, 'log_trade_outcome'):
            self.manager.evaluate_open_signals(object(), now=self.now)
        self.assertEqual(signal['outcome'], 'loss')
        self.assertEqual(signal['exit_bar_timestamp'], datetime.fromtimestamp(
            data['timestamps'][0], tz=pytz.utc
        ).isoformat())

    def test_long_take_profit_is_filled_at_target(self):
        signal = self.make_signal()
        closed = self.evaluate_with_bar(signal, low=99.5, high=103.0, close=103.0)
        self.assertEqual(closed['outcome'], 'win')
        self.assertEqual(closed['closed_price'], 102.5)
        self.assertAlmostEqual(closed['r_multiple'], 2.5 / 1.5)

    def test_net_r_subtracts_estimated_round_trip_spread(self):
        signal = self.make_signal()
        signal['spread_abs_estimate'] = 0.15
        closed = self.evaluate_with_bar(signal, low=99.5, high=103.0, close=103.0)
        self.assertAlmostEqual(closed['gross_r_multiple'], 2.5 / 1.5)
        self.assertAlmostEqual(closed['spread_cost_r'], 0.1)
        self.assertAlmostEqual(closed['r_multiple'], 2.5 / 1.5 - 0.1)

    def test_short_stop_and_target_are_evaluated_in_correct_direction(self):
        stop = self.make_signal('SHORT')
        closed_stop = self.evaluate_with_bar(stop, low=99.5, high=102.0, close=102.0)
        self.assertEqual(closed_stop['outcome'], 'loss')
        self.assertAlmostEqual(closed_stop['r_multiple'], -1.0)

        target = self.make_signal('SHORT')
        closed_target = self.evaluate_with_bar(target, low=97.0, high=100.5, close=97.0)
        self.assertEqual(closed_target['outcome'], 'win')
        self.assertAlmostEqual(closed_target['r_multiple'], 2.5 / 1.5)

    def test_stop_wins_when_one_candle_touches_both_levels(self):
        signal = self.make_signal()
        closed = self.evaluate_with_bar(signal, low=98.0, high=103.0, close=100.0)
        self.assertEqual(closed['outcome'], 'loss')
        self.assertAlmostEqual(closed['r_multiple'], -1.0)

    def test_position_stays_open_when_no_barrier_is_touched(self):
        signal = self.make_signal()
        opened = self.evaluate_with_bar(signal, low=99.0, high=101.0, close=100.5)
        self.assertEqual(opened['status'], 'open')
        self.assertNotIn('r_multiple', opened)

    def test_timeout_marks_to_market_using_latest_close(self):
        signal = self.make_signal(created_at=self.now - timedelta(hours=3))
        signal['last_evaluated_at'] = (self.now - timedelta(hours=2)).isoformat()
        closed = self.evaluate_with_bar(
            signal, low=99.0, high=101.0, close=101.0,
            max_age_hours=1,
        )
        self.assertEqual(closed['outcome'], 'timeout')
        self.assertEqual(closed['closed_price'], 101.0)
        self.assertAlmostEqual(closed['r_multiple'], 1.0 / 1.5)

    def test_signal_ids_preserve_repeated_signals_and_archive_events(self):
        signal = {
            'name': 'BITCOIN', 'direction': 'LONG', 'entry': 100,
            'stop_loss': 98.5, 'take_profit': 102.5, 'confidence': 0.6,
            'features': {},
        }
        with patch.object(agent, 'append_feature_store'), \
                patch.object(agent, 'log_signal_per_mode'):
            self.assertTrue(self.manager.store_signal(signal))
            first_id = next(iter(self.manager.signals))
            self.manager.signals[first_id]['status'] = 'closed'
            self.manager.signals[first_id]['closed_at'] = (
                self.now - timedelta(hours=3)
            ).isoformat()
            self.assertTrue(self.manager.store_signal(signal))

        self.assertEqual(len(self.manager.signals), 2)
        archive = [
            json.loads(line)
            for line in Path(self.manager.archive_path).read_text().splitlines()
        ]
        self.assertEqual([event['event'] for event in archive], ['opened', 'opened'])
        self.assertNotEqual(archive[0]['signal_id'], archive[1]['signal_id'])

    def test_active_position_blocks_signal_without_mutating_history(self):
        signal = self.make_signal()
        self.manager.signals = {signal['signal_id']: signal}
        candidate = {'name': 'BITCOIN', 'direction': 'SHORT'}
        self.assertFalse(self.manager.should_send_signal(candidate, now=self.now))
        self.assertEqual(len(self.manager.signals), 1)

    def test_market_cooldown_expires_after_two_hours(self):
        signal = self.make_signal()
        signal['status'] = 'closed'
        signal['closed_at'] = (self.now - timedelta(hours=1)).isoformat()
        self.manager.signals = {signal['signal_id']: signal}
        candidate = {'name': 'BITCOIN', 'direction': 'SHORT'}
        self.assertFalse(self.manager.should_send_signal(candidate, now=self.now))
        self.assertTrue(self.manager.should_send_signal(
            candidate, now=self.now + timedelta(hours=2)
        ))

    def test_cluster_limit_accounts_for_already_open_positions(self):
        open_signal = self.make_signal()
        self.manager.signals = {open_signal['signal_id']: open_signal}
        candidates = [
            {'name': 'BITCOIN'},
            {'name': 'ETHEREUM'},
        ]
        existing = agent._open_signal_cluster_counts(self.manager.signals)
        accepted = agent.apply_cluster_limits(
            candidates, max_per_cluster=2, existing_counts=existing
        )
        self.assertEqual([signal['name'] for signal in accepted], ['BITCOIN'])

    def test_weekend_cycle_still_evaluates_open_positions(self):
        with patch.object(agent, 'AGENT_MODE', agent.RunMode.SHADOW), \
                patch.object(agent, 'SignalManager') as manager_cls, \
        patch.object(agent, 'TimeframeWeights') as tf_weights_cls, \
        patch.object(agent, 'FeatureWeights') as feature_weights_cls, \
        patch.object(agent, 'AdaptiveThreshold') as threshold_cls, \
        patch.object(agent, 'PerformanceStats') as stats_cls, \
        patch.object(agent, 'CircuitBreaker') as breaker_cls, \
                patch.object(agent, 'NearMissTracker') as near_miss_cls, \
                patch.object(agent, 'initialize_shadow_learning_state'), \
                patch.object(agent, 'maintain_runtime_logs'), \
                patch.object(agent, 'evaluate_near_misses'), \
                patch.object(agent, 'is_weekend', return_value=True):
            manager_cls.return_value.signals = {'open': {'status': 'open'}}
            near_miss_cls.return_value.dirty = False
            agent.main()
            manager_cls.return_value.evaluate_open_signals.assert_called_once()
            near_miss_cls.return_value.save.assert_called_once()
            manager_cls.assert_called_once_with(
                file_path=agent.SHADOW_SIGNALS_FILE,
                archive_path=agent.SHADOW_SIGNAL_ARCHIVE_FILE,
            )
            tf_weights_cls.assert_called_once_with(agent.SHADOW_TIMEFRAME_WEIGHTS_FILE)
            feature_weights_cls.assert_called_once_with(agent.SHADOW_FEATURE_WEIGHTS_FILE)
            threshold_cls.assert_called_once_with(agent.SHADOW_ADAPTIVE_THRESHOLD_FILE)
            stats_cls.assert_called_once_with(agent.SHADOW_PERFORMANCE_STATS_FILE)
            breaker_cls.assert_called_once_with(agent.SHADOW_CIRCUIT_BREAKER_FILE)


if __name__ == '__main__':
    unittest.main()
