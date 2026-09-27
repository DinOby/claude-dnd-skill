"""config_loader.py — layered JSON config for the display companion.

Two layers per config file:

  1. Bundled default — display/config/<name>, shipped with the plugin and
     replaced on every `/plugin update`.
  2. User override   — <data-root>/config/<name> (see scripts/paths.py
     user_config_dir()), which survives updates.

The override is deep-merged over the default: objects merge key by key,
lists and scalars replace, and an explicit `null` removes the key. So a user
file only needs the keys it changes:

    {"triggers": {"de": {"steal": ["stiehlt", "klaut", "mopst"]}}}

Failure policy — the display must keep running on a bad hand-edit:
  * missing override       → default alone, silently
  * broken override JSON   → default alone, warning
  * merged result invalid  → default alone, warning (validator decides)
  * missing/broken default → {} with a warning (a packaging bug; tests
                             validate every shipped default)

Files are re-read when their mtime or size changes, so edits apply without
restarting the server. Callers that precompile data from a config (regexes,
lookup tables) compare `ConfigFile.generation` to know when to rebuild.

Files are read as UTF-8 with an optional BOM — Windows Notepad writes one.
"""

import copy
import json
import os
import sys
import threading
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(_HERE, "config")

# A validator receives the merged config and returns human-readable problems;
# an empty list means valid.
Validator = Callable[[dict], "list[str]"]


def _default_user_dir() -> str:
    """<data-root>/config — resolved via scripts/paths.py like runtime_paths."""
    scripts = os.path.join(_HERE, os.pardir, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from paths import user_config_dir
        return str(user_config_dir())
    except Exception:
        root = os.environ.get("DND_CAMPAIGN_ROOT", "").strip() or "~/.claude/dnd"
        return os.path.join(os.path.expanduser(root), "config")


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: `override` merged over `base` (inputs untouched).

    Objects merge recursively, lists and scalars replace, None deletes.
    """
    out = copy.deepcopy(base)
    for key, val in override.items():
        if val is None:
            out.pop(key, None)
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def check_types(data: dict, spec: dict) -> "list[str]":
    """Validator helper: required top-level keys and their types.

    spec maps key → type or tuple of types, e.g. {"version": int,
    "triggers": dict}. bool is not accepted where int is asked for.
    """
    problems = []
    for key, want in spec.items():
        if key not in data:
            problems.append(f"missing key '{key}'")
            continue
        val = data[key]
        wants = want if isinstance(want, tuple) else (want,)
        if isinstance(val, bool) and bool not in wants:
            ok = False
        else:
            ok = isinstance(val, wants)
        if not ok:
            names = "/".join(t.__name__ for t in wants)
            problems.append(f"'{key}' should be {names}, got {type(val).__name__}")
    return problems


def _safe_name(name: str) -> str:
    """Reject absolute paths and parent escapes; allow sub-dirs like a/b.json."""
    norm = os.path.normpath(name)
    if os.path.isabs(norm) or norm.startswith(os.pardir) or not norm.endswith(".json"):
        raise ValueError(f"invalid config name: {name!r}")
    return norm


def _signature(path: str) -> "Optional[tuple]":
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_json(path: str) -> "tuple[Optional[dict], Optional[str]]":
    """(data, error). data is None when the file is absent or unusable."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:   # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None, f"{path}: {e}"
    if not isinstance(data, dict):
        return None, f"{path}: top level must be a JSON object"
    return data, None


class ConfigFile:
    """One layered config file, reloaded on change.

        triggers = ConfigFile("vfx-triggers.json", validator=_validate)
        cfg = triggers.get()          # merged dict (a copy — safe to mutate)
        if triggers.generation != seen: rebuild(...)
    """

    def __init__(self, name: str, *, validator: "Optional[Validator]" = None,
                 default_dir: "Optional[str]" = None,
                 user_dir: "Optional[str]" = None):
        self.name = _safe_name(name)
        self._validator = validator
        self.default_path = os.path.join(default_dir or DEFAULT_DIR, self.name)
        self.user_path = os.path.join(user_dir or _default_user_dir(), self.name)
        self._lock = threading.Lock()
        self._sig: "Optional[tuple]" = ("unloaded",)
        self._data: dict = {}
        self.generation = 0          # bumped on every (re)load
        self.warnings: "list[str]" = []
        self.override_active = False  # True when the user file is in effect

    def get(self) -> dict:
        """Merged config, re-read first if either file changed on disk."""
        sig = (_signature(self.default_path), _signature(self.user_path))
        with self._lock:
            if sig != self._sig:
                self._load()
                self._sig = sig
            return copy.deepcopy(self._data)

    def _load(self) -> None:
        warnings: "list[str]" = []

        default, err = _read_json(self.default_path)
        if err:
            warnings.append(err)
        elif default is None:
            warnings.append(f"{self.default_path}: bundled default missing")
        default = default or {}
        if self._validator and default:
            warnings += [f"{self.default_path}: {p}" for p in self._validator(default)]

        merged, override_active = default, False
        override, err = _read_json(self.user_path)
        if err:
            warnings.append(f"{err} — override ignored")
        elif override is not None:
            candidate = deep_merge(default, override)
            problems = self._validator(candidate) if self._validator else []
            if problems:
                warnings += [f"{self.user_path}: {p} — override ignored" for p in problems]
            else:
                merged, override_active = candidate, True

        self._data = merged
        self.override_active = override_active
        self.generation += 1
        if warnings != self.warnings:
            for w in warnings:
                print(f"config_loader: {w}", file=sys.stderr)
        self.warnings = warnings


def load_config(name: str, *, validator: "Optional[Validator]" = None,
                default_dir: "Optional[str]" = None,
                user_dir: "Optional[str]" = None) -> dict:
    """One-shot read for scripts that run once (no reload tracking needed)."""
    return ConfigFile(name, validator=validator, default_dir=default_dir,
                      user_dir=user_dir).get()
