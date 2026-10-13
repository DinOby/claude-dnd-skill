"""The monthly Gemini cap: ledger, prices, image pipeline stop, narration fallback (402)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.app_isolation import DISPLAY, IsolatedApp, isolate_process

if str(DISPLAY) not in sys.path:
    sys.path.insert(0, str(DISPLAY))
import asset_pipeline as ap          # noqa: E402
import gemini_budget as gb           # noqa: E402
import image_providers as ip         # noqa: E402
from asset_queue import PendingQueue  # noqa: E402
from asset_store import AssetStore    # noqa: E402
from config_loader import ConfigFile  # noqa: E402

OCT = datetime(2026, 10, 13, 20, 0)
NOV = datetime(2026, 11, 1, 0, 5)


class _BudgetCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.user_cfg = self.base / "config"
        self.user_cfg.mkdir()
        self.ledger = self.base / "gemini-ledger.json"
        self.now = OCT
        self.budget = self.make_budget()

    def make_budget(self, **override):
        if override:
            (self.user_cfg / "gemini-budget.json").write_text(json.dumps(override), encoding="utf-8")
        cfg = ConfigFile("gemini-budget.json", validator=gb._validate,
                         default_dir=str(DISPLAY / "config"), user_dir=str(self.user_cfg))
        return gb.Budget(config=cfg, ledger_path=str(self.ledger), now=lambda: self.now)


class LedgerTests(_BudgetCase):
    def test_bundled_config_is_valid_and_capped_at_ten_euro(self):
        cfg = self.budget.config.get()
        self.assertEqual(self.budget.config.warnings, [])
        self.assertEqual(cfg["monthly_limit_eur"], 10)

    def test_prices(self):
        self.assertAlmostEqual(self.budget.image_cost("gemini-3.1-flash-lite-image"), 0.0336)
        self.assertAlmostEqual(self.budget.image_cost("some-new-model"), 0.15)   # unknown → expensive default
        self.assertAlmostEqual(self.budget.tts_cost(0, 1_000_000), 10.0)
        self.assertAlmostEqual(self.budget.tts_cost(1_000_000, 0), 0.5)
        # 1200 chars ≈ 100 s of speech ≈ 2500 audio tokens ≈ 2.5 ct
        self.assertAlmostEqual(self.budget.tts_estimate("x" * 1200), 0.025, places=3)
        # 10 s of 24 kHz 16-bit audio = 480000 bytes = 250 tokens
        self.assertAlmostEqual(self.budget.tts_cost_from_audio("abc", 480000), (1 * 0.5 + 251 * 10) / 1e6)

    def test_fresh_month_starts_at_zero(self):
        s = self.budget.status()
        self.assertEqual((s["month"], s["spent_eur"], s["limit_eur"], s["exhausted"]), ("2026-10", 0.0, 10.0, False))

    def test_charges_add_up_and_refuse_before_the_limit(self):
        for _ in range(3):
            self.budget.charge("image", 3.0, detail="token:x")
        self.budget.charge("tts", 0.5, seconds=60)
        s = self.budget.status()
        self.assertEqual((s["spent_eur"], s["images"], s["tts_calls"], s["tts_seconds"]), (9.5, 3, 1, 60.0))
        self.budget.check(0.5)                 # exactly to the limit is fine
        with self.assertRaises(gb.BudgetExceeded) as cm:
            self.budget.check(0.51)
        self.assertIn("9.50 € von 10.00 €", str(cm.exception))

    def test_new_month_resets_old_month_kept(self):
        self.budget.charge("image", 10.0)
        self.assertTrue(self.budget.status()["exhausted"])
        self.now = NOV
        self.assertEqual(self.budget.status()["spent_eur"], 0.0)
        self.budget.check(1.0)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(data["months"]["2026-10"]["eur"], 10.0)

    def test_user_override_changes_the_limit(self):
        budget = self.make_budget(monthly_limit_eur=5)
        budget.charge("image", 4.9)
        with self.assertRaises(gb.BudgetExceeded):
            budget.check(0.2)

    def test_broken_ledger_refuses_instead_of_resetting(self):
        self.ledger.write_text("{not json", encoding="utf-8")
        with self.assertRaises(gb.BudgetExceeded) as cm:
            self.budget.check(0.01)
        self.assertIn("beschädigt", str(cm.exception))

    def test_stale_lock_from_a_crash_is_cleared(self):
        lock = Path(str(self.ledger) + ".lock")
        lock.write_text("", encoding="utf-8")
        old = lock.stat().st_mtime - gb.LOCK_STALE_SECONDS - 5
        os.utime(lock, (old, old))
        self.budget.charge("tts", 0.01)
        self.assertFalse(lock.exists())

    def test_log_is_bounded(self):
        for i in range(gb.LOG_KEEP + 20):
            self.budget.charge("tts", 0.0001, detail=str(i))
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(data["log"]), gb.LOG_KEEP)
        self.assertEqual(data["log"][-1]["detail"], str(gb.LOG_KEEP + 19))


class _Paid:
    """Provider double that costs money like Gemini."""
    name, paid, model = "paidfake", True, "gemini-3.1-flash-lite-image"

    def __init__(self, data=b"\x89PNG fake"):
        self.calls, self.data = [], data

    def available(self):
        return True, "ok"

    def generate(self, req):
        self.calls.append(req)
        return ip.ImageResult(data=self.data, mime="image/png", meta={"model": self.model})


class PipelineBudgetTests(_BudgetCase):
    def setUp(self):
        super().setUp()
        self.store = AssetStore(global_root=str(self.base / "assets"))
        self.queue = PendingQueue(str(self.base / "assets" / "pending-assets.json"))
        cfg = ConfigFile("image-provider.json", validator=ap.validate_provider_config,
                         default_dir=str(DISPLAY / "config"), user_dir=str(self.user_cfg))
        self.prov = _Paid()
        # the bundled default provider is gemini; the loader hands out the paid double instead
        self.p = ap.Pipeline(self.store, self.queue, config=cfg, loader=lambda name, c: self.prov,
                             budget=self.budget)
        self.queue.add_many([{"key": ap.parse_key(n), "kind": "item", "name": n, "category": "weapon"}
                             for n in ("Axt", "Bogen", "Dolch")])
        self.log = []

    def test_each_image_is_booked(self):
        summary = self.p.generate(log=self.log.append)
        self.assertEqual(len(summary["done"]), 3)
        s = self.budget.status()
        self.assertEqual(s["images"], 3)
        self.assertAlmostEqual(s["spent_eur"], 3 * 0.0336)

    def test_full_budget_stops_before_calling_gemini(self):
        self.budget.charge("image", 10.0 - 0.05)        # room for exactly one more image
        summary = self.p.generate(log=self.log.append)
        self.assertEqual(len(self.prov.calls), 1)
        self.assertEqual(len(summary["done"]), 1)
        self.assertEqual(len(summary["blocked"]), 2)
        pending = [e for e in self.queue.entries("pending")]
        self.assertEqual(len(pending), 2)                 # stays on the wait-list, no failed attempt
        self.assertTrue(all(not e.get("attempts") for e in pending))
        self.assertTrue(any("Monatsbudget erreicht" in line for line in self.log))

    def test_unusable_image_is_still_paid(self):
        self.queue = PendingQueue(str(self.base / "assets" / "pending-sprites.json"))
        self.p.queue = self.queue
        self.queue.add_many([{"key": "sprite:fass", "kind": "sprite", "name": "Fass"}])
        with mock.patch.object(ap, "pillow_available", return_value=True), \
             mock.patch.object(ap, "chroma_key", side_effect=ValueError("bad")):
            summary = self.p.generate(log=self.log.append)
        self.assertEqual(summary["failed"], ["sprite:fass"])
        self.assertEqual(self.budget.status()["images"], 1)

    def test_seed_catalogue_respects_the_budget_too(self):
        import asset_seed
        self.budget.charge("image", 10.0)
        summary = self.p.seed(asset_seed.SeedCatalog(), limit=5, log=self.log.append)
        self.assertEqual(self.prov.calls, [])
        self.assertEqual(len(summary["blocked"]), 1)
        self.assertEqual(summary["done"], [])

    def test_every_provider_call_goes_through_the_guard(self):
        src = (DISPLAY / "asset_pipeline.py").read_text(encoding="utf-8")
        self.assertEqual(src.count(".generate(ImageRequest"), 1)      # only inside _request
        self.assertEqual(src.count("self._request("), src.count("self._paid_guard(prov, key)"))

    def test_free_providers_are_not_booked(self):
        self.prov.paid = False
        self.p.generate(log=self.log.append)
        self.assertFalse(self.ledger.exists())


class NarrationBudgetTests(IsolatedApp):
    app_module_name = "_budget_app_under_test"

    def setUp(self):
        home = isolate_process()
        self.ledger = Path(home) / f"ledger-{self._testMethodName}.json"
        self.app._budget = gb.Budget(ledger_path=str(self.ledger), now=lambda: OCT)
        key = mock.patch.object(self.app._tts, "key_source", return_value="env:TEST")
        synth = mock.patch.object(self.app._tts, "synthesize_with_usage",
                                  return_value=(b"\x00\x00" * 24000,   # 1 s of audio
                                                {"promptTokenCount": 40, "candidatesTokenCount": 100}))
        key.start()
        self.addCleanup(key.stop)
        self.synth = synth.start()
        self.addCleanup(synth.stop)

    def post_tts(self, text="Die Tür knarrt."):
        resp = self.client.post("/tts", json={"text": text})
        self.addCleanup(resp.close)
        return resp

    def test_narration_is_booked_by_its_tokens(self):
        resp = self.post_tts()
        self.assertEqual(resp.status_code, 200)
        s = self.app._budget.status()
        self.assertEqual(s["tts_calls"], 1)
        self.assertAlmostEqual(s["spent_eur"], (40 * 0.5 + 100 * 10) / 1e6, places=4)   # status rounds to 0.01 ct
        self.assertAlmostEqual(s["tts_seconds"], 1.0)

    def test_used_up_budget_answers_402_without_calling_gemini(self):
        self.app._budget.charge("image", 10.0)
        resp = self.post_tts()
        self.assertEqual(resp.status_code, 402)
        body = resp.get_json()
        self.assertEqual(body["error"], "budget")
        self.assertTrue(body["budget"]["exhausted"])
        self.synth.assert_not_called()

    def test_status_route(self):
        self.app._budget.charge("image", 1.25)
        resp = self.client.get("/gemini-budget")
        self.addCleanup(resp.close)
        self.assertEqual(resp.get_json()["spent_eur"], 1.25)


class PageWiringTests(unittest.TestCase):
    def test_page_loads_the_module_and_falls_back_on_402(self):
        html = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/static/js/gemini-budget.js"></script>', html)
        self.assertIn("resp.status === 402 && window.DndBudget", html)
        self.assertIn("DndBudget.stop()", html)
        js = (DISPLAY / "static" / "js" / "gemini-budget.js").read_text(encoding="utf-8")
        self.assertIn("speechSynthesis.speak", js)
        self.assertIn("'de-DE'", js)


if __name__ == "__main__":
    unittest.main()
