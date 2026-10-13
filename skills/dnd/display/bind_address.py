"""bind_address.py — `--bind`: serve the display on localhost and one Tailscale address.

`--lan` listens on 0.0.0.0, i.e. on every network the machine is in (home
Wi-Fi, a café hotspot, VPNs). `--bind` instead listens on localhost only
(127.0.0.1 and ::1) for the DM's own browser and scripts, plus this machine's
Tailscale address for the remote players.

    python3 dnd-display-app.py --bind tailscale        # look the address up
    python3 dnd-display-app.py --bind 100.64.0.1   # must be ours right now

It fails closed: when Tailscale is not installed, not running, or the address
is not this machine's, the display refuses to start (BindError) — it never
falls back to 0.0.0.0 or to localhost-only without saying so.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from typing import Callable, Optional

LOOPBACK = "127.0.0.1"
LOOPBACK_V6 = "::1"   # Windows tries ::1 first for "localhost"; without it every script call waits ~2 s
# Every Tailscale node address lies in these ranges (CGNAT and Tailscale's ULA).
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_WINDOWS_CLI = r"C:\Program Files\Tailscale\tailscale.exe"


class BindError(RuntimeError):
    """The display must not start; the message says why and what to do."""


def pop_bind_arg(argv: "list[str]") -> Optional[str]:
    """Remove `--bind VALUE` / `--bind=VALUE` from argv; returns VALUE or None."""
    for i, arg in enumerate(argv):
        if arg == "--bind":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise BindError("--bind needs a value: --bind tailscale  or  --bind <your Tailscale IP>")
            value = argv[i + 1]
            del argv[i:i + 2]
            return value.strip()
        if arg.startswith("--bind="):
            del argv[i]
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise BindError("--bind needs a value: --bind tailscale  or  --bind <your Tailscale IP>")
            return value
    return None


def _tailscale_cli() -> Optional[str]:
    override = os.environ.get("DND_TAILSCALE_CLI")
    if override is not None:   # tests point this at a fake (or at nothing)
        return override if os.path.exists(override) else None
    return shutil.which("tailscale") or (_WINDOWS_CLI if os.path.exists(_WINDOWS_CLI) else None)


def tailscale_ips(run: Callable = subprocess.run) -> "list[str]":
    """This machine's Tailscale addresses; BindError unless Tailscale is up."""
    cli = _tailscale_cli()
    if not cli:
        raise BindError("Tailscale is not installed (no `tailscale` command found).")
    try:
        out = run([cli, "status", "--json"], capture_output=True, text=True, timeout=10)
        status = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise BindError(f"Could not ask Tailscale for its status ({e}).") from e
    state = status.get("BackendState")
    if state != "Running":
        raise BindError(f"Tailscale is not running (state: {state or 'unknown'}). "
                        "Start Tailscale and log in, then start the display again.")
    ips = [str(ip) for ip in (status.get("Self") or {}).get("TailscaleIPs") or []]
    if not ips:
        raise BindError("Tailscale is running but reports no address for this machine.")
    return ips


def _in_tailscale(addr) -> bool:
    return any(addr.version == net.version and addr in net for net in TAILSCALE_NETS)


def resolve_bind(value: str, ips: Callable[[], "list[str]"] = tailscale_ips) -> str:
    """The Tailscale address to listen on, checked against the running Tailscale."""
    if value.lower() == "tailscale":
        found = ips()
        v4 = [ip for ip in found if ipaddress.ip_address(ip).version == 4]
        return (v4 or found)[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        raise BindError(f"--bind {value}: not an IP address. Use --bind tailscale or --bind <Tailscale IP>.") from None
    if addr.is_unspecified:
        raise BindError(f"--bind {value} would listen on every network. Use --bind tailscale "
                        "(or --lan if you really want that).")
    if not _in_tailscale(addr):
        raise BindError(f"--bind {value} is not a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48). "
                        "--bind only serves Tailscale; use --lan for other networks.")
    if str(addr) not in {str(ipaddress.ip_address(ip)) for ip in ips()}:
        raise BindError(f"--bind {value} is not this machine's Tailscale address right now "
                        "(Tailscale reports a different one). Use --bind tailscale.")
    return str(addr)


def listen_addresses(bind_addr: str) -> "list[str]":
    """Exactly the addresses the server listens on: loopback (v4, v6), then Tailscale."""
    return [LOOPBACK, LOOPBACK_V6, bind_addr]


def allowed_hosts(bind_addr: str) -> "set[str]":
    """Host names the Host/CORS checks accept in --bind mode."""
    return {"localhost", LOOPBACK, "::1", bind_addr.lower()}


def make_servers(addresses: "list[str]", port: int, app, ssl_context=None,
                 optional: "tuple[str, ...]" = (LOOPBACK_V6,)) -> list:
    """One werkzeug server per address — all bound, or none (BindError).
    An `optional` address (::1 on a machine without IPv6) is skipped instead."""
    from werkzeug.serving import make_server
    servers = []
    for host in addresses:
        try:
            servers.append(make_server(host, port, app, threaded=True, ssl_context=ssl_context))
        except (OSError, SystemExit) as e:   # werkzeug exits on bind errors after printing them
            if host in optional:
                continue
            for s in servers:
                s.server_close()
            why = "address not available or port in use" if isinstance(e, SystemExit) else e
            raise BindError(f"Could not listen on {host}:{port} ({why}).") from None
    return servers
