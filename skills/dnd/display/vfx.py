"""
vfx.py — short action overlays (attack, spell, theft, …) for the display.

Architecture mirrors audio.py: the server spots trigger phrases in DM
narration and broadcasts {"vfx": {...}} over SSE; the browser (static/js/
vfx.js) decides how the effect looks. Two config files keep those concerns
apart, and both are user-overridable via config_loader:

  vfx-triggers.json — which phrases fire which effect, per language, plus
                      cooldown_ms and max_per_chunk
  vfx-iconset.json  — how each effect looks (icon, animation, duration, tint)

The DM can also fire an effect explicitly (send.py --vfx attack:Flerb), which
is more reliable than keyword spotting and bypasses the cooldown.

SSE payload:  {"vfx": {"effect": "attack", "actor": "Flerb" | null,
                       "source": "keyword" | "explicit"}}
"""

import os
import re
import sys
import threading
import time
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from config_loader import ConfigFile, check_types
from triggers import TriggerMatcher

EFFECT_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")
ICON_RE = re.compile(r"^[\w\-][\w.\-]{0,80}\.(png|svg|webp|gif|jpe?g)$", re.IGNORECASE)
ACTOR_MAX = 60

BUNDLED_ICON_DIR = os.path.join(_HERE, "icons")


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_triggers(cfg: dict) -> "list[str]":
    problems = check_types(cfg, {"version": int, "cooldown_ms": int,
                                 "max_per_chunk": int, "triggers": dict})
    if problems:
        return problems
    if cfg["cooldown_ms"] < 0:
        problems.append("'cooldown_ms' must be >= 0")
    if cfg["max_per_chunk"] < 1:
        problems.append("'max_per_chunk' must be >= 1")
    for lang, pack in cfg["triggers"].items():
        if not isinstance(pack, dict):
            problems.append(f"triggers.{lang} should be an object")
            continue
        for effect, phrases in pack.items():
            if not EFFECT_RE.match(effect):
                problems.append(f"triggers.{lang}: invalid effect name '{effect}'")
            if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
                problems.append(f"triggers.{lang}.{effect} should be a list of strings")
    return problems


def _validate_iconset(cfg: dict) -> "list[str]":
    problems = check_types(cfg, {"version": int, "effects": dict})
    if problems:
        return problems
    for effect, spec in cfg["effects"].items():
        where = f"effects.{effect}"
        if not EFFECT_RE.match(effect):
            problems.append(f"invalid effect name '{effect}'")
        if not isinstance(spec, dict):
            problems.append(f"{where} should be an object")
            continue
        problems += [f"{where}: {p}" for p in
                     check_types(spec, {"icon": str, "animation": str, "duration_ms": int})]
        if isinstance(spec.get("icon"), str) and not ICON_RE.match(spec["icon"]):
            problems.append(f"{where}: icon must be a plain image file name")
        if isinstance(spec.get("duration_ms"), int) and not 100 <= spec["duration_ms"] <= 10000:
            problems.append(f"{where}: duration_ms must be 100–10000")
    fallback = cfg.get("fallback")
    if fallback is not None and fallback not in cfg["effects"]:
        problems.append(f"fallback '{fallback}' is not a defined effect")
    return problems


# ── State ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_broadcast_fn: Optional[Callable] = None
_triggers_cfg: ConfigFile
_iconset_cfg: ConfigFile
_user_icon_dir = ""
_languages: "Optional[list[str]]" = None   # None → every language in the config
_matcher: Optional[TriggerMatcher] = None
_matcher_key: tuple = ()
_last_emit = float("-inf")


def _default_user_icon_dir() -> str:
    scripts = os.path.join(_HERE, os.pardir, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from paths import user_assets_dir
        return str(user_assets_dir() / "vfx")
    except Exception:
        root = os.environ.get("DND_CAMPAIGN_ROOT", "").strip() or "~/.claude/dnd"
        return os.path.join(os.path.expanduser(root), "assets", "vfx")


def configure(default_dir: Optional[str] = None, user_dir: Optional[str] = None,
              user_icon_dir: Optional[str] = None) -> None:
    """(Re)bind config and icon locations and reset runtime state.

    Called once on import with the real locations; tests call it with
    temporary directories.
    """
    global _triggers_cfg, _iconset_cfg, _user_icon_dir, _matcher, _matcher_key, _last_emit
    with _lock:
        _triggers_cfg = ConfigFile("vfx-triggers.json", validator=_validate_triggers,
                                   default_dir=default_dir, user_dir=user_dir)
        _iconset_cfg = ConfigFile("vfx-iconset.json", validator=_validate_iconset,
                                  default_dir=default_dir, user_dir=user_dir)
        _user_icon_dir = user_icon_dir or _default_user_icon_dir()
        _matcher, _matcher_key = None, ()
        _last_emit = float("-inf")


configure()


def set_broadcast(fn: Optional[Callable]) -> None:
    """Wire the SSE broadcast function (dnd-display-app.py does this on start)."""
    global _broadcast_fn
    _broadcast_fn = fn


def set_languages(langs: "Optional[list[str]]") -> None:
    """Active trigger languages in priority order; None = all configured."""
    global _languages
    with _lock:
        _languages = [l.strip() for l in langs if l.strip()] if langs is not None else None


def available_languages() -> "list[str]":
    return sorted(_triggers_cfg.get().get("triggers", {}))


def _current() -> "tuple[dict, TriggerMatcher]":
    """Config + matcher, rebuilding the matcher after config/language changes.

    Caller holds _lock.
    """
    global _matcher, _matcher_key
    cfg = _triggers_cfg.get()
    key = (_triggers_cfg.generation, tuple(_languages) if _languages is not None else None)
    if _matcher is None or key != _matcher_key:
        packs = cfg.get("triggers", {})
        langs = _languages if _languages is not None else list(packs)
        _matcher = TriggerMatcher(packs, languages=langs)
        _matcher_key = key
    return cfg, _matcher


# ── Events ────────────────────────────────────────────────────────────────────

def _emit(effect: str, actor: Optional[str], source: str) -> dict:
    payload = {"effect": effect, "actor": actor, "source": source}
    if _broadcast_fn:
        _broadcast_fn({"vfx": payload})
    return payload


def on_text(text: str, now: Optional[float] = None) -> "list[str]":
    """Scan DM narration; broadcast up to max_per_chunk effects.

    Returns the effects fired (empty inside the cooldown window or without a
    wired broadcast function).
    """
    if not _broadcast_fn or not text:
        return []
    global _last_emit
    with _lock:
        cfg, matcher = _current()
        if not cfg:
            return []
        now = time.monotonic() if now is None else now
        if (now - _last_emit) * 1000 < cfg.get("cooldown_ms", 0):
            return []
        effects = matcher.all_matches(text)[: cfg.get("max_per_chunk", 1)]
        if effects:
            _last_emit = now
    for effect in effects:
        _emit(effect, None, "keyword")
    return effects


def parse_spec(spec: str) -> "tuple[Optional[str], Optional[str]]":
    """'attack' / 'attack:Flerb' → (effect, actor); (None, None) if invalid."""
    effect, _, actor = (spec or "").partition(":")
    effect = effect.strip().lower()
    if not EFFECT_RE.match(effect):
        return None, None
    actor = actor.strip()[:ACTOR_MAX] or None
    return effect, actor


def trigger(effect: str, actor: Optional[str] = None,
            now: Optional[float] = None) -> Optional[dict]:
    """Fire an effect explicitly (DM flag). Bypasses and restarts the cooldown,
    so narration sent right after doesn't double up with a keyword overlay.

    Unknown-but-well-formed effect names are passed through; the browser
    shows the iconset's fallback. Returns the payload, or None if invalid.
    """
    global _last_emit
    effect = (effect or "").strip().lower()
    if not EFFECT_RE.match(effect):
        return None
    actor = (actor or "").strip()[:ACTOR_MAX] or None
    with _lock:
        _last_emit = time.monotonic() if now is None else now
    return _emit(effect, actor, "explicit")


# ── Iconset / icon files ──────────────────────────────────────────────────────

def get_iconset() -> dict:
    return _iconset_cfg.get()


def icon_path(filename: str) -> "Optional[tuple[str, str]]":
    """(directory, filename) for an overlay icon: user dir first, then bundled."""
    if not ICON_RE.match(filename or ""):
        return None
    for directory in (_user_icon_dir, BUNDLED_ICON_DIR):
        if directory and os.path.isfile(os.path.join(directory, filename)):
            return directory, filename
    return None
