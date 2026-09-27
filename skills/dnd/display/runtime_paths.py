"""runtime_paths.py — resolve the writable runtime-state directory for the
display companion (tokens, queues, device approvals, TLS certs, logs).

These files are deliberately kept OUT of the code directory: a plugin's code
dir is refreshed on `/plugin update` (which would wipe device approvals + certs)
and may be read-only. They live under the data root instead — see
scripts/paths.py `runtime_dir()`. Every display script imports `rt()` from here
so writers and readers always agree on the location.

Process-management files (app.pid, app.log, .cert-server.pid, .scheme) are NOT
runtime state — they are recreated on every launch and stay in the code dir.
"""
import os
import sys
import pathlib

_HERE = os.path.dirname(os.path.abspath(__file__))

# Prefer the canonical resolver in scripts/paths.py; fall back to an identical
# computation if that import fails, so writers/readers never diverge.
sys.path.insert(0, os.path.join(_HERE, os.pardir, "scripts"))
try:
    from paths import runtime_dir as _runtime_dir
except Exception:
    _runtime_dir = None


def runtime_base() -> str:
    """The runtime directory, resolved on every call.

    Resolving late means DND_RUNTIME_DIR / DND_CAMPAIGN_ROOT always take
    effect, however early this module was first imported (tests point them
    at a temp dir; a value frozen at first import would leak into the real
    data root).
    """
    if _runtime_dir is not None:
        try:
            return str(_runtime_dir())
        except Exception:
            pass
    raw = os.environ.get("DND_RUNTIME_DIR", "").strip()
    if raw:
        base = pathlib.Path(raw).expanduser()
    else:
        data_root = os.environ.get("DND_CAMPAIGN_ROOT", "").strip() or "~/.claude/dnd"
        base = pathlib.Path(data_root).expanduser() / ".runtime"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(base)


def rt(name: str) -> str:
    """Absolute path to a runtime-state file by name (e.g. rt('.token'))."""
    return os.path.join(runtime_base(), name)
