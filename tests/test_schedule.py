#!/usr/bin/env python3
"""
Local tests for the timed-charge schedule feature (no network, no deps).

The aiohttp / pymyenergi / sigen imports are stubbed out so these run on
any machine with plain Python 3:

    python3 tests/test_schedule.py
"""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime as real_datetime, timezone
from pathlib import Path

# --- Stub the network libraries before importing battery_sitter ------------

aiohttp_stub = types.ModuleType('aiohttp')


class _ClientResponseError(Exception):
    pass


class _ClientSession:
    def __init__(self, *args, **kwargs):
        self.closed = False


aiohttp_stub.ClientResponseError = _ClientResponseError
aiohttp_stub.ClientSession = _ClientSession
sys.modules['aiohttp'] = aiohttp_stub

for name, attrs in (
    ('pymyenergi', {}),
    ('pymyenergi.connection', {'Connection': type('Connection', (), {})}),
    ('pymyenergi.zappi', {'Zappi': type('Zappi', (), {})}),
    ('sigen', {'Sigen': type('Sigen', (), {}), 'BROWSER_HEADERS': {}}),
):
    mod = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(mod, attr, value)
    sys.modules[name] = mod

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import battery_sitter  # noqa: E402
from battery_sitter import BatterySitter  # noqa: E402


# --- Controllable clock ----------------------------------------------------

class FakeDateTime:
    """Replaces battery_sitter.datetime so tests control 'now'."""
    current = real_datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls.current.astimezone(tz)
        return cls.current.replace(tzinfo=None)

    fromisoformat = real_datetime.fromisoformat


def at(hh, mm, ss=0, day=6):
    FakeDateTime.current = real_datetime(
        2026, 9, day, hh, mm, ss, tzinfo=timezone.utc)


SUNDAY_ENTRY = [{
    "start": "2026-09-06T13:00:00Z",
    "duration_minutes": 60,
    "action": "charge",
    "power_kw": 10.0,
    "note": "free electricity hour",
}]


class ScheduleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.schedule_path = os.path.join(self.tmpdir.name, 'schedule.json')
        self._real_datetime = battery_sitter.datetime
        battery_sitter.datetime = FakeDateTime
        at(12, 0)

    def tearDown(self):
        battery_sitter.datetime = self._real_datetime
        self.tmpdir.cleanup()

    def write_schedule(self, entries):
        with open(self.schedule_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f)

    def make_sitter(self, entries=None):
        if entries is not None:
            self.write_schedule(entries)
        sitter = BatterySitter(
            'u', 'p', 's', 'su', 'sp',
            charging_power=1, charging_duration_minutes=30,
            poll_interval=30, schedule_file=self.schedule_path,
        )
        sitter.charge_calls = []

        async def fake_charge(enable, duration_minutes=30,
                              power_kw=1.0, mode="0"):
            sitter.charge_calls.append((enable, duration_minutes, power_kw))
            if enable:
                sitter.manual_charge_enabled_at = FakeDateTime.now()
                sitter.manual_charge_last_duration = duration_minutes

        sitter.set_instant_manual_charge = fake_charge
        return sitter

    @staticmethod
    def handle(sitter, zappi_charging=False, soc=50.0, power=0.0):
        return asyncio.run(sitter._handle_schedule_window(
            zappi_charging, soc, power))

    # --- Parsing ---

    def test_parses_valid_entry(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)
        entries = sitter._load_schedule()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(
            e['start'], real_datetime(2026, 9, 6, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(
            e['end'], real_datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(e['action'], 'charge')
        self.assertEqual(e['power_kw'], 10.0)

    def test_naive_timestamp_is_utc_and_defaults(self):
        sitter = self.make_sitter([
            {"start": "2026-09-06T13:00:00", "duration_minutes": 30},
        ])
        e = sitter._load_schedule()[0]
        self.assertEqual(e['start'].tzinfo, timezone.utc)
        self.assertEqual(e['action'], 'charge')
        self.assertIsNone(e['power_kw'])

    def test_bad_entries_skipped_good_ones_kept(self):
        sitter = self.make_sitter([
            {"start": "not-a-date", "duration_minutes": 60},
            {"start": "2026-09-06T13:00:00Z", "duration_minutes": -5},
            {"start": "2026-09-06T13:00:00Z", "duration_minutes": 60,
             "action": "dance"},
            {"start": "2026-09-06T13:00:00Z", "duration_minutes": 60,
             "power_kw": 0},
            {"duration_minutes": 60},
            {"start": "2026-09-06T13:00:00Z", "duration_minutes": 60},
        ])
        with self.assertLogs('battery_sitter', level='ERROR'):
            entries = sitter._load_schedule()
        self.assertEqual(len(entries), 1)

    def test_unreadable_and_non_list_files(self):
        sitter = self.make_sitter()
        with open(self.schedule_path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        with self.assertLogs('battery_sitter', level='ERROR'):
            self.assertEqual(sitter._load_schedule(), [])
        # Force a reload despite same-second mtime
        sitter._schedule_mtime = None
        self.write_schedule({"start": "x"})  # dict, not a list
        with self.assertLogs('battery_sitter', level='ERROR'):
            self.assertEqual(sitter._load_schedule(), [])

    def test_missing_file_is_inactive_not_fatal(self):
        sitter = self.make_sitter()  # no file written
        self.assertEqual(sitter._load_schedule(), [])
        self.assertFalse(self.handle(sitter))
        self.assertEqual(sitter.charge_calls, [])

    def test_reload_on_mtime_change(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)
        self.assertEqual(len(sitter._load_schedule()), 1)
        self.write_schedule(SUNDAY_ENTRY + [
            {"start": "2026-09-07T13:00:00Z", "duration_minutes": 30},
        ])
        os.utime(self.schedule_path, (1, 1))  # guarantee mtime change
        self.assertEqual(len(sitter._load_schedule()), 2)

    # --- Window matching ---

    def test_window_boundaries(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)
        at(12, 59, 59)
        self.assertIsNone(sitter._active_schedule_entry())
        at(13, 0, 0)
        self.assertIsNotNone(sitter._active_schedule_entry())
        at(13, 59, 59)
        self.assertIsNotNone(sitter._active_schedule_entry())
        at(14, 0, 0)
        self.assertIsNone(sitter._active_schedule_entry())

    # --- Charge window behaviour ---

    def test_charge_window_lifecycle(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)

        # Before the window: schedule stays out of the way
        at(12, 30)
        self.assertFalse(self.handle(sitter))
        self.assertEqual(sitter.charge_calls, [])

        # Window opens: one enable at the entry's power
        at(13, 0, 30)
        self.assertTrue(self.handle(sitter))
        self.assertEqual(sitter.charge_calls, [(True, 30, 10.0)])
        self.assertTrue(sitter.schedule_charge_active)
        self.assertTrue(sitter.manual_charge_enabled)

        # Mid-window, not expiring: no extra API calls
        at(13, 10)
        self.assertTrue(self.handle(sitter))
        self.assertEqual(len(sitter.charge_calls), 1)

        # 29 min elapsed (margin = 2 polls = 1 min): refresh fires
        at(13, 29, 30)
        self.assertTrue(self.handle(sitter))
        self.assertEqual(sitter.charge_calls[-1], (True, 30, 10.0))
        self.assertEqual(len(sitter.charge_calls), 2)

        # Near the window's end a refresh is capped at the remaining time,
        # so Sigenergy stops on schedule even if this process dies
        at(13, 58, 30)
        self.assertTrue(self.handle(sitter))
        enable, duration, power = sitter.charge_calls[-1]
        self.assertTrue(enable)
        self.assertLessEqual(duration, 2)

        # Window over, EV idle: charge disabled, control handed back
        at(14, 0, 30)
        self.assertFalse(self.handle(sitter))
        self.assertEqual(sitter.charge_calls[-1][0], False)
        self.assertFalse(sitter.schedule_charge_active)
        self.assertFalse(sitter.manual_charge_enabled)

    def test_charge_window_uses_config_power_by_default(self):
        entry = [dict(SUNDAY_ENTRY[0])]
        del entry[0]['power_kw']
        sitter = self.make_sitter(entry)
        at(13, 5)
        self.assertTrue(self.handle(sitter))
        self.assertEqual(sitter.charge_calls, [(True, 30, 1)])

    def test_window_end_hands_over_to_ev_protection(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)
        at(13, 30)
        self.assertTrue(self.handle(sitter, zappi_charging=True))
        self.assertTrue(sitter.is_charging)

        # Window ends while the EV is still charging: keep the pin, let the
        # normal EV-protection machine maintain it - no disable call.
        at(14, 0, 30)
        self.assertFalse(self.handle(sitter, zappi_charging=True))
        self.assertTrue(sitter.manual_charge_enabled)
        self.assertTrue(sitter.is_charging)
        self.assertFalse(any(c[0] is False for c in sitter.charge_calls))

    def test_zappi_state_synced_during_window(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)
        at(13, 30)
        sitter.ev_stop_first_seen = FakeDateTime.now()
        self.assertTrue(self.handle(sitter, zappi_charging=True))
        self.assertTrue(sitter.is_charging)
        self.assertIsNone(sitter.ev_stop_first_seen)
        self.assertTrue(self.handle(sitter, zappi_charging=False))
        self.assertFalse(sitter.is_charging)

    def test_stall_detection_during_charge_window(self):
        sitter = self.make_sitter(SUNDAY_ENTRY)
        at(13, 5)
        self.assertTrue(self.handle(sitter, power=0.0))  # window opens
        self.assertEqual(len(sitter.charge_calls), 1)

        # Two stalled polls: counted, no action yet
        self.assertTrue(self.handle(sitter, soc=50.0, power=0.0))
        self.assertTrue(self.handle(sitter, soc=50.0, power=0.0))
        self.assertEqual(len(sitter.charge_calls), 1)

        # Third stalled poll: warning + re-sent enable
        with self.assertLogs('battery_sitter', level='WARNING'):
            self.assertTrue(self.handle(sitter, soc=50.0, power=0.0))
        self.assertEqual(sitter.charge_calls[-1][0], True)
        self.assertEqual(len(sitter.charge_calls), 2)

        # Sixth stalled poll: offline error, no further calls
        self.assertTrue(self.handle(sitter, soc=50.0, power=0.0))
        self.assertTrue(self.handle(sitter, soc=50.0, power=0.0))
        with self.assertLogs('battery_sitter', level='ERROR'):
            self.assertTrue(self.handle(sitter, soc=50.0, power=0.0))
        self.assertEqual(len(sitter.charge_calls), 2)

        # Charging resumes: counter resets, no more complaints
        self.assertTrue(self.handle(sitter, soc=51.0, power=9.8))
        self.assertEqual(sitter.window_stall_polls, 0)

        # A full battery at 0kW is healthy, never a stall
        sitter2 = self.make_sitter(SUNDAY_ENTRY)
        self.assertTrue(self.handle(sitter2, soc=100.0, power=0.0))
        for _ in range(7):
            self.assertTrue(self.handle(sitter2, soc=100.0, power=0.0))
        self.assertEqual(len(sitter2.charge_calls), 1)

    # --- Block window behaviour ---

    def test_block_window_suppresses_manual_charge(self):
        sitter = self.make_sitter([{
            "start": "2026-09-06T13:00:00Z",
            "duration_minutes": 60,
            "action": "block",
        }])
        # We're mid-boost when the block window opens
        sitter.manual_charge_enabled = True
        at(13, 0, 30)
        self.assertTrue(self.handle(sitter, zappi_charging=True))
        self.assertTrue(sitter.schedule_block_active)
        self.assertFalse(sitter.manual_charge_enabled)
        self.assertEqual(sitter.charge_calls, [(False, 0, 0)])

        # Stays suppressed with no extra API calls
        at(13, 30)
        self.assertTrue(self.handle(sitter, zappi_charging=True))
        self.assertEqual(len(sitter.charge_calls), 1)

        # Window over: back to normal
        at(14, 0, 30)
        self.assertFalse(self.handle(sitter))
        self.assertFalse(sitter.schedule_block_active)


if __name__ == '__main__':
    unittest.main(verbosity=2)
