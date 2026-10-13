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

Status for the DM:   python3 gemini_budget.py [status]
Change the limit:    python3 gemini_budget.py set-limit 15 [--this-month]

set-limit is command-line only on purpose: no web route or button calls it,
and it must never go into a permission allowlist, so Claude Code asks the DM
before every change. It refuses a limit below this month's spending and above
max_settable_limit_eur (100 €) — higher only by editing the config by hand.
Without --this-month the limit is written to the user override
<data-root>/config/gemini-budget.json; with it, the ledger holds a limit for
the current month only, and the next month is back on the standing limit.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
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


class LimitRefused(ValueError):
    """set-limit said no; the message says why and what to do."""


def _validate(cfg: dict) -> "list[str]":
    problems = []
    limit = cfg.get("monthly_limit_eur")
    if not isinstance(limit, (int, float)) or limit < 0:
        problems.append("monthly_limit_eur must be a number ≥ 0")
    cap = cfg.get("max_settable_limit_eur", 100)
    if not isinstance(cap, (int, float)) or cap < 0:
        problems.append("max_settable_limit_eur must be a number ≥ 0")
    ratio = cfg.get("warn_ratio", 0.8)
    if not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
        problems.append("warn_ratio must be a number in (0, 1]")
    per_hour = cfg.get("tts_per_device_per_hour", 20)
    if isinstance(per_hour, bool) or not isinstance(per_hour, int) or per_hour < 0:
        problems.append("tts_per_device_per_hour must be a whole number ≥ 0")
    prices = cfg.get("image_price_usd")
    if not isinstance(prices, dict) or not isinstance(prices.get("default"), (int, float)):
        problems.append("image_price_usd needs a numeric 'default'")
    tts = cfg.get("tts_price_usd_per_1m_tokens")
    if not isinstance(tts, dict) or not all(isinstance(tts.get(k), (int, float)) for k in ("input", "output")):
        problems.append("tts_price_usd_per_1m_tokens needs numeric 'input' and 'output'")
    return problems


def budget_config() -> ConfigFile:
    return ConfigFile("gemini-budget.json", validator=_validate)


class HourlyLimiter:
    """At most N events per key in any 60-minute window (in memory, per process).

    Used by the display for paid narration per player device: the key is the
    connection's peer address — a request header (device id) could be changed
    by the player at will, the Tailscale address of their machine cannot."""

    WINDOW = 3600.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._events: "dict[str, list[float]]" = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        """Count one event for `key` if it is still under `limit`; False otherwise."""
        now = self._clock()
        with self._lock:
            recent = [t for t in self._events.get(key, []) if now - t < self.WINDOW]
            if len(recent) >= limit:
                self._events[key] = recent
                return False
            recent.append(now)
            self._events[key] = recent
            return True

    def used(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            return sum(1 for t in self._events.get(key, []) if now - t < self.WINDOW)


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
        """The standing monthly limit from the config (a month may override it)."""
        return float(self.config.get().get("monthly_limit_eur", 0))

    def max_settable_eur(self) -> float:
        return float(self.config.get().get("max_settable_limit_eur", 100))

    def tts_per_device_per_hour(self) -> int:
        return int(self.config.get().get("tts_per_device_per_hour", 20))

    def _month_limit(self, m: dict) -> "tuple[float, str]":
        override = m.get("limit_override_eur")
        if isinstance(override, (int, float)) and not isinstance(override, bool):
            return float(override), "this-month"
        return self.limit_eur(), "config"

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
        spent = round(float(m.get("eur", 0.0)), 4)
        limit, source = self._month_limit(m)
        ratio = float(self.config.get().get("warn_ratio", 0.8))
        return {"month": month, "spent_eur": spent, "limit_eur": limit, "limit_source": source,
                "standard_limit_eur": self.limit_eur(),
                "remaining_eur": round(max(0.0, limit - spent), 4),
                "images": int(m.get("images", 0)), "tts_calls": int(m.get("tts_calls", 0)),
                "tts_seconds": round(float(m.get("tts_seconds", 0.0)), 1),
                "warn_ratio": ratio, "warning": spent >= ratio * limit,
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


    def set_limit(self, amount: float, this_month: bool = False) -> dict:
        """Change the limit — permanently (user config) or for this month only.
        Returns {"old", "new", "scope", "month", "where", "note"}; LimitRefused otherwise."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
            raise LimitRefused("Die Grenze muss eine Zahl sein, z. B. 15 oder 12,50.")
        amount = round(float(amount), 2)
        if amount < 0:
            raise LimitRefused("Die Grenze kann nicht negativ sein.")
        cap = self.max_settable_eur()
        if amount > cap:
            raise LimitRefused(
                f"{_euro(amount)} liegt über der Obergrenze von {_euro(cap)} für diesen Befehl. "
                f"Höher geht es nur, indem du {self.config.user_path} von Hand bearbeitest "
                f'("monthly_limit_eur").')
        month = self._month()
        with self._lock():
            data = self._read()
            m = data["months"].get(month, {})
            spent = round(float(m.get("eur", 0.0)), 4)
            if amount < spent:
                raise LimitRefused(
                    f"{_euro(amount)} liegt unter dem, was im {month} schon verbucht ist "
                    f"({_euro(spent)}). Wähle mindestens {_euro(math.ceil(spent * 100) / 100)}.")
            month_limit, source = self._month_limit(m)
            note = ""
            if this_month:
                old = month_limit
                entry = data["months"].setdefault(month, {"eur": 0.0, "images": 0, "tts_calls": 0,
                                                          "tts_seconds": 0.0})
                entry["limit_override_eur"] = amount
                data["log"].append({"t": self._now().isoformat(timespec="seconds"), "kind": "limit",
                                    "eur": 0.0, "detail": f"{month}: {old:.2f} -> {amount:.2f}"})
                data["log"] = data["log"][-LOG_KEEP:]
                self._write(data)
                where = self.ledger_path
            else:
                old = self.limit_eur()
                self._write_user_limit(amount)
                where = self.config.user_path
                if source == "this-month":
                    note = (f"Für {month} gilt weiterhin die Monatsgrenze von {_euro(month_limit)}; "
                            f"die neue Grenze gilt ab dem nächsten Monat.")
        return {"old": old, "new": amount, "scope": "this-month" if this_month else "permanent",
                "month": month, "where": where, "note": note}

    def _write_user_limit(self, amount: float) -> None:
        """Set monthly_limit_eur in the user override, keeping its other keys."""
        path = self.config.user_path
        current: dict = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    current = json.load(f)
            except (OSError, ValueError) as e:
                raise LimitRefused(f"{path} ist nicht lesbar ({e}) — bitte von Hand reparieren.") from None
            if not isinstance(current, dict):
                raise LimitRefused(f"{path} ist kein JSON-Objekt — bitte von Hand reparieren.")
        current["monthly_limit_eur"] = amount
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)


def _euro(value: float) -> str:
    return f"{value:.2f} €".replace(".", ",")


def _parse_amount(text: str) -> float:
    try:
        value = float(text.strip().replace("€", "").replace(",", "."))
    except ValueError:
        raise LimitRefused(f"'{text}' ist kein Betrag. Beispiel: set-limit 15  oder  set-limit 12,50") from None
    if not math.isfinite(value):
        raise LimitRefused(f"'{text}' ist kein Betrag.")
    return value


def _print_status(s: dict) -> None:
    scope = " (nur dieser Monat)" if s["limit_source"] == "this-month" else ""
    print(f"Gemini {s['month']}: {_euro(s['spent_eur'])} von {_euro(s['limit_eur'])}{scope} "
          f"(übrig {_euro(s['remaining_eur'])}) · {s['images']} Bilder · "
          f"{s['tts_calls']} Vorlese-Blöcke, {s['tts_seconds'] / 60:.1f} Min.")
    if s["exhausted"]:
        print("Budget erreicht — keine neuen Bilder; Vorlesen nutzt die Browser-Stimme.")
    elif s["warning"]:
        print(f"⚠ Achtung: {round(s['warn_ratio'] * 100)} % des Gemini-Budgets sind verbraucht.")


def _cli(argv: "Optional[list[str]]" = None, budget: Optional[Budget] = None) -> int:
    parser = argparse.ArgumentParser(prog="gemini_budget.py",
                                     description="Monatliche Gemini-Grenze: Stand anzeigen oder ändern.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="Stand dieses Monats (Standard)")
    sl = sub.add_parser("set-limit", help="Monatsgrenze ändern (nur hier, nie über den Browser)")
    sl.add_argument("amount", help="neue Grenze in Euro, z. B. 15 oder 12,50")
    sl.add_argument("--this-month", action="store_true",
                    help="nur für den laufenden Monat; am Monatsersten gilt wieder die Standardgrenze")
    args = parser.parse_args(argv)
    budget = budget or Budget()
    try:
        if args.cmd == "set-limit":
            result = budget.set_limit(_parse_amount(args.amount), this_month=args.this_month)
            scope = f"nur für {result['month']}" if result["scope"] == "this-month" else "dauerhaft"
            print(f"Gemini-Grenze geändert ({scope}): {_euro(result['old'])} → {_euro(result['new'])}")
            print(f"Gespeichert in: {result['where']}")
            if result["note"]:
                print(result["note"])
        _print_status(budget.status())
    except (LimitRefused, BudgetExceeded, TimeoutError) as e:
        print(f"Abgelehnt: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    sys.exit(_cli())
