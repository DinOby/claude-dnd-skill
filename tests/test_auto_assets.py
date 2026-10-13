"""Automatic image generation in the background (display/auto_assets.py).

WHY
===
New figures stood on the map as initials until someone ran `assets.py
generate` by hand — the table noticed every time. The display now starts that
run itself a few seconds after something new is queued. It must not run twice
at once, must stay off when switched off, must not hammer a provider that only
fails (empty account), and must keep the monthly cap — which it does by
running the normal generator, never calling a provider itself.

Run from repo root:
    python3 -m unittest tests.test_auto_assets -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "dnd" / "display"))

import auto_assets   # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Harness:
    def __init__(self, pending=3, results=None, **cfg):
        self.pending = pending
        self.results = list(results or [(3, 0)])
        self.calls = []
        self.cfg = auto_assets.settings({"auto_generate": dict({"delay_seconds": 0}, **cfg)})
        self.clock = FakeClock()
        self.gen = auto_assets.AutoGenerator(lambda: self.cfg, lambda: self.pending, self.run, self.clock)

    def run(self, limit):
        self.calls.append(limit)
        done, failed = self.results.pop(0) if self.results else (0, 0)
        self.pending = max(0, self.pending - done)
        return done, failed


class SettingsTests(unittest.TestCase):
    def test_defaults_and_limits(self):
        self.assertEqual(auto_assets.settings({}), auto_assets.DEFAULTS)
        s = auto_assets.settings({"auto_generate": {"enabled": "ja", "delay_seconds": 99999,
                                                    "max_per_run": 0, "backoff_minutes": True}})
        self.assertTrue(s["enabled"])                  # wrong type → default, never a crash
        self.assertEqual((s["delay_seconds"], s["max_per_run"], s["backoff_minutes"]), (600, 1, 15))

    def test_the_shipped_config_switches_it_on(self):
        import json
        cfg = json.loads((ROOT / "skills" / "dnd" / "display" / "config" / "image-provider.json")
                         .read_text(encoding="utf-8"))
        self.assertTrue(auto_assets.settings(cfg)["enabled"])


class GeneratorTests(unittest.TestCase):
    def test_a_poke_schedules_one_run_that_works_off_the_list(self):
        h = Harness(pending=3, results=[(3, 0)])
        self.assertTrue(h.gen.poke())
        h.gen.cancel()                                 # run synchronously instead of the timer
        h.gen.run_now()
        self.assertEqual(h.calls, [8])
        self.assertEqual(h.pending, 0)

    def test_more_than_one_batch_runs_until_the_list_is_empty(self):
        h = Harness(pending=10, results=[(8, 0), (2, 0)])
        h.gen.run_now()
        self.assertEqual(h.calls, [8, 8])
        self.assertEqual(h.pending, 0)

    def test_switched_off_never_runs(self):
        h = Harness(enabled=False)
        self.assertFalse(h.gen.poke())
        h.gen.run_now()
        self.assertEqual(h.calls, [])

    def test_nothing_waiting_means_no_run(self):
        h = Harness(pending=0)
        h.gen.run_now()
        self.assertEqual(h.calls, [])

    def test_several_pokes_give_one_timer(self):
        h = Harness(delay_seconds=60)
        self.assertTrue(h.gen.poke())
        timer = h.gen._timer
        h.gen.poke()
        h.gen.poke()
        self.assertIs(h.gen._timer, timer)
        h.gen.cancel()

    def test_a_poke_during_a_run_leads_to_one_more_run_not_a_parallel_one(self):
        h = Harness(pending=1)

        def run(limit):
            h.calls.append(limit)
            if len(h.calls) == 1:
                h.pending = 1                           # something new arrived meanwhile …
                self.assertTrue(h.gen.poke())           # … and is remembered, not started in parallel
                self.assertIsNone(h.gen._timer)
            else:
                h.pending = 0
            return 1, 0

        h.gen._runner = run
        h.gen.run_now()
        self.assertEqual(len(h.calls), 2)

    def test_after_only_failures_it_waits_before_trying_again(self):
        h = Harness(pending=2, results=[(0, 2)], backoff_minutes=15)
        h.gen.run_now()
        self.assertEqual(len(h.calls), 1)
        h.gen.run_now()                                # straight away: still quiet
        self.assertEqual(len(h.calls), 1)
        h.clock.t += 15 * 60 + 1
        h.results = [(2, 0)]
        h.gen.run_now()
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.pending, 0)

    def test_blocked_by_the_monthly_cap_stops_without_backoff(self):
        h = Harness(pending=2, results=[(0, 0)])       # generator: "blocked", nothing paid
        h.gen.run_now()
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.gen._quiet_until, 0.0)


class OutputParsingTests(unittest.TestCase):
    def test_the_generator_summary_line_is_read(self):
        import subprocess
        from unittest import mock
        fake = subprocess.CompletedProcess([], 0, stdout="✓ token:x → tokens/x.jpg\n"
                                                         "Done: 1, failed: 2, blocked (provider not ready): 0\n")
        with mock.patch.object(auto_assets.subprocess, "run", return_value=fake) as run:
            self.assertEqual(auto_assets.run_generate(8), (1, 2))
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["generate", "--limit", "8"])
        self.assertIs(run.call_args.kwargs["stdin"], auto_assets.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
