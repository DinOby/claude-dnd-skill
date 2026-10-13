"""auto_assets.py — generate images on their own as soon as content needs one.

The display puts a figure, item or map without an image on the wait-list
(pending-assets.json). Until now someone had to run `assets.py generate`
afterwards; the table saw initials meanwhile. With `auto_generate.enabled` in
config/image-provider.json the display starts that run itself, in the
background, a few seconds after something new was queued:

- The short delay lets the DM give a new entry a proper description first
  (`assets.py prompt KEY "…" --add`, ideally in the same call as --token-add).
- Several additions in a row lead to one run, never to parallel runs.
- The run is the normal `assets.py generate --limit N`: the monthly Gemini cap
  (gemini_budget.py), the attempt limit and the asset lock all still apply.
- After a run that only failed (no credit, provider down) it waits
  `backoff_minutes` before trying again, so a dead account is not hammered.

Nothing here costs money by itself; it only decides *when* the existing
generator runs. Switched off: `"auto_generate": {"enabled": false}` in
<data-root>/config/image-provider.json.
"""

import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

DEFAULTS = {"enabled": True, "delay_seconds": 20, "max_per_run": 8, "backoff_minutes": 15}
_ASSETS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "assets.py")


def settings(cfg: Optional[dict]) -> dict:
    """The auto_generate block of the provider config, filled up with defaults."""
    out = dict(DEFAULTS)
    block = (cfg or {}).get("auto_generate")
    if isinstance(block, dict):
        for key, default in DEFAULTS.items():
            value = block.get(key)
            if isinstance(value, type(default)) and not (isinstance(value, bool) and not isinstance(default, bool)):
                out[key] = value
    out["delay_seconds"] = max(0, min(out["delay_seconds"], 600))
    out["max_per_run"] = max(1, min(out["max_per_run"], 50))
    out["backoff_minutes"] = max(0, min(out["backoff_minutes"], 24 * 60))
    return out


def run_generate(limit: int) -> "tuple[int, int]":
    """One `assets.py generate --limit N` run; returns (done, failed). Never raises."""
    try:
        res = subprocess.run([sys.executable, _ASSETS_SCRIPT, "generate", "--limit", str(limit)],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"auto images: run failed ({exc})", file=sys.stderr)
        return 0, 1
    done = failed = 0
    for line in (res.stdout or "").splitlines():
        if line.startswith("Done:"):
            try:
                parts = dict(p.strip().split(": ", 1) for p in line.split(",")[:2])
                done, failed = int(parts.get("Done", 0)), int(parts.get("failed", 0))
            except ValueError:
                pass
    return done, failed


class AutoGenerator:
    """Coalesces 'something new was queued' into background runs of the generator."""

    def __init__(self, get_settings: Callable[[], dict], pending: Callable[[], int],
                 runner: Callable[[int], "tuple[int, int]"] = run_generate,
                 clock: Callable[[], float] = time.monotonic):
        self._get_settings = get_settings
        self._pending = pending
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._again = False
        self._quiet_until = 0.0
        self.runs = 0

    def poke(self) -> bool:
        """New entries were queued: schedule a run. Returns True if one is (now) scheduled."""
        cfg = self._get_settings()
        if not cfg["enabled"]:
            return False
        with self._lock:
            if self._running:
                self._again = True
                return True
            if self._timer is not None:
                return True
            delay = max(cfg["delay_seconds"], self._quiet_until - self._clock())
            self._timer = threading.Timer(delay, self.run_now)
            self._timer.daemon = True
            self._timer.start()
            return True

    def run_now(self) -> None:
        """Work through the wait-list (also called directly by tests)."""
        with self._lock:
            self._timer = None
            if self._running:
                self._again = True
                return
            self._running = True
        try:
            while True:
                cfg = self._get_settings()
                if not cfg["enabled"] or self._clock() < self._quiet_until or self._pending() <= 0:
                    break
                self.runs += 1
                done, failed = self._runner(cfg["max_per_run"])
                if not done:
                    if failed:
                        self._quiet_until = self._clock() + cfg["backoff_minutes"] * 60
                    break
                with self._lock:
                    again, self._again = self._again, False
                if not again and self._pending() <= 0:
                    break
        finally:
            with self._lock:
                self._running = False
                again, self._again = self._again, False
            if again:
                self.poke()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
