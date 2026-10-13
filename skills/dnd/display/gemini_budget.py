#!/usr/bin/env python3
"""gemini_budget.py — a hard monthly cap on what Gemini may cost (images + narration).

Google's own budgets only send warnings; they never stop a request. This
module is the stop: every paid Gemini call asks first and is refused once the
month's spending plus the call's estimate would pass the limit.

    budget = Budget()
    budget.check(budget.image_cost(model))        # BudgetExceeded when it would pass the cap
    ... call Gemini ...
    budget.charge("image", budget.image_cost(model), detail=key)

Prices and the limit live in config/gemini-budget.json (user override in
<data-root>/config/gemini-budget.json, e.g. {"monthly_limit_eur": 5}). USD
prices are counted 1:1 as EUR, which over-counts slightly. The ledger is
<runtime>/gemini-ledger.json, one total per calendar month (local time), so a
new month starts at 0 by itself. The display process (narration) and
scripts/assets.py (images) share it through a lock file.

Status for the DM:   python3 gemini_budget.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from config_loader import ConfigFile   # noqa: E402
from runtime_paths import rt           # noqa: E402

LOG_KEEP = 200            # newest ledger lines kept for "what did the money go to"
LOCK_STALE_SECONDS = 30   # a lock older than this belongs to a crashed process


class BudgetExceeded(Exception):
    """The call would pass this month's Gemini limit; the message says so in plain words."""


def _validate(cfg: dict) -> "list[str]":
    problems = []
    limit = cfg.get("monthly_limit_eur")
    if not isinstance(limit, (int, float)) or limit < 0:
        problems.append("monthly_limit_eur must be a number ≥ 0")
    prices = cfg.get("image_price_usd")
    if not isinstance(prices, dict) or not isinstance(prices.get("default"), (int, float)):
        problems.append("image_price_usd needs a numeric 'default'")
    tts = cfg.get("tts_price_usd_per_1m_tokens")
    if not isinstance(tts, dict) or not all(isinstance(tts.get(k), (int, float)) for k in ("input", "output")):
        problems.append("tts_price_usd_per_1m_tokens needs numeric 'input' and 'output'")
    return problems


def budget_config() -> ConfigFile:
    return ConfigFile("gemini-budget.json", validator=_validate)


class _FileLock:
    """Cross-process lock via an exclusively created file (works on Windows and POSIX)."""

    def __init__(self, path: str, timeout: float = 5.0):
        self.path, self.timeout = path, timeout

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_SECONDS:
                        os.unlink(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"budget ledger is locked ({self.path})")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Budget:
    def __init__(self, config: Optional[ConfigFile] = None, ledger_path: Optional[str] = None,
                 now: Callable[[], datetime] = datetime.now):
        self.config = config or budget_config()
        self.ledger_path = ledger_path or rt("gemini-ledger.json")
        self._now = now

    # ── prices ────────────────────────────────────────────────────────────────

    def limit_eur(self) -> float:
        return float(self.config.get().get("monthly_limit_eur", 0))

    def image_cost(self, model: Optional[str]) -> float:
        prices = self.config.get()["image_price_usd"]
        return float(prices.get(model or "", prices["default"]))

    def tts_cost(self, input_tokens: int, output_tokens: int) -> float:
        p = self.config.get()["tts_price_usd_per_1m_tokens"]
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000

    def tts_estimate(self, text: str) -> float:
        """Cost of narrating `text`, guessed from its length (before the call)."""
        cfg = self.config.get()
        seconds = len(text) / float(cfg.get("tts_chars_per_second", 12))
        out_tokens = seconds * float(cfg.get("tts_audio_tokens_per_second", 25))
        return self.tts_cost(len(text) // 4 + 1, int(out_tokens) + 1)

    def tts_cost_from_audio(self, text: str, pcm_bytes: int) -> float:
        """Fallback when the response has no token counts: 16-bit mono 24 kHz audio."""
        seconds = pcm_bytes / 2 / 24000
        out_tokens = seconds * float(self.config.get().get("tts_audio_tokens_per_second", 25))
        return self.tts_cost(len(text) // 4 + 1, int(out_tokens) + 1)

    # ── ledger ────────────────────────────────────────────────────────────────

    def _month(self) -> str:
        return self._now().strftime("%Y-%m")

    def _read(self) -> dict:
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("months", {})
                data.setdefault("log", [])
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # An unreadable ledger must not reset spending to 0 — refuse instead.
            raise BudgetExceeded("Das Gemini-Kostenbuch ist beschädigt — keine bezahlten Aufrufe, "
                                 f"bis {self.ledger_path} repariert oder gelöscht ist.")
        return {"months": {}, "log": []}

    def _write(self, data: dict) -> None:
        tmp = self.ledger_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.ledger_path)

    def _lock(self) -> _FileLock:
        return _FileLock(self.ledger_path + ".lock")

    def status(self) -> dict:
        month = self._month()
        with self._lock():
            m = self._read()["months"].get(month, {})
        spent, limit = round(float(m.get("eur", 0.0)), 4), self.limit_eur()
        return {"month": month, "spent_eur": spent, "limit_eur": limit,
                "remaining_eur": round(max(0.0, limit - spent), 4),
                "images": int(m.get("images", 0)), "tts_calls": int(m.get("tts_calls", 0)),
                "tts_seconds": round(float(m.get("tts_seconds", 0.0)), 1),
                "exhausted": spent >= limit}

    def check(self, cost_eur: float) -> None:
        """Raise BudgetExceeded if this call would pass the month's limit."""
        s = self.status()
        if s["spent_eur"] + cost_eur > s["limit_eur"]:
            raise BudgetExceeded(
                f"Gemini-Monatsbudget erreicht: {s['spent_eur']:.2f} € von {s['limit_eur']:.2f} € "
                f"verbraucht ({s['month']}); dieser Aufruf würde etwa {cost_eur:.2f} € kosten.")

    def charge(self, kind: str, cost_eur: float, detail: str = "", seconds: float = 0.0) -> dict:
        """Book one paid call. kind: "image" or "tts"."""
        month = self._month()
        with self._lock():
            data = self._read()
            m = data["months"].setdefault(month, {"eur": 0.0, "images": 0, "tts_calls": 0, "tts_seconds": 0.0})
            m["eur"] = round(float(m.get("eur", 0.0)) + float(cost_eur), 6)
            if kind == "image":
                m["images"] = int(m.get("images", 0)) + 1
            elif kind == "tts":
                m["tts_calls"] = int(m.get("tts_calls", 0)) + 1
                m["tts_seconds"] = round(float(m.get("tts_seconds", 0.0)) + seconds, 2)
            data["log"].append({"t": self._now().isoformat(timespec="seconds"), "kind": kind,
                                "eur": round(float(cost_eur), 6), "detail": str(detail)[:80]})
            data["log"] = data["log"][-LOG_KEEP:]
            self._write(data)
        return self.status()


def _cli() -> int:
    s = Budget().status()
    print(f"Gemini {s['month']}: {s['spent_eur']:.2f} € von {s['limit_eur']:.2f} € "
          f"(übrig {s['remaining_eur']:.2f} €) · {s['images']} Bilder · "
          f"{s['tts_calls']} Vorlese-Blöcke, {s['tts_seconds'] / 60:.1f} Min.")
    if s["exhausted"]:
        print("Budget erreicht — keine neuen Bilder; Vorlesen nutzt die Browser-Stimme.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(_cli())
