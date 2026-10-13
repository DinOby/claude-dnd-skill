"""--bind: the display listens on 127.0.0.1 + the Tailscale address, never 0.0.0.0.

Fail-closed is the point: every way Tailscale can be missing must stop the
display with a message, not quietly open it on another network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.app_isolation import DISPLAY, SKILL, isolate_process

if str(DISPLAY) not in sys.path:
    sys.path.insert(0, str(DISPLAY))
import bind_address as ba   # noqa: E402

TS_V4 = "100.64.0.1"
TS_V6 = "fd7a:115c:a1e0::c201:f4f3"


def fake_status(state="Running", ips=(TS_V4, TS_V6), stdout=None):
    """A stand-in for subprocess.run that answers `tailscale status --json`."""
    out = stdout if stdout is not None else json.dumps({"BackendState": state, "Self": {"TailscaleIPs": list(ips)}})
    return lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")


class PopBindArgTests(unittest.TestCase):
    def test_both_spellings_are_removed_from_argv(self):
        argv = ["app.py", "--bind", "tailscale", "--tls"]
        self.assertEqual(ba.pop_bind_arg(argv), "tailscale")
        self.assertEqual(argv, ["app.py", "--tls"])
        argv = ["app.py", f"--bind={TS_V4}"]
        self.assertEqual(ba.pop_bind_arg(argv), TS_V4)
        self.assertEqual(argv, ["app.py"])

    def test_absent_flag_changes_nothing(self):
        argv = ["app.py", "--lan"]
        self.assertIsNone(ba.pop_bind_arg(argv))
        self.assertEqual(argv, ["app.py", "--lan"])

    def test_missing_value_is_an_error(self):
        for argv in (["app.py", "--bind"], ["app.py", "--bind", "--tls"], ["app.py", "--bind="]):
            with self.assertRaises(ba.BindError, msg=argv):
                ba.pop_bind_arg(list(argv))


class TailscaleStatusTests(unittest.TestCase):
    def setUp(self):
        fake_cli = Path(isolate_process()) / "tailscale-fake"
        fake_cli.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"DND_TAILSCALE_CLI": str(fake_cli)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_running_reports_its_addresses(self):
        self.assertEqual(ba.tailscale_ips(run=fake_status()), [TS_V4, TS_V6])

    def test_every_not_running_state_refuses(self):
        for state in ("Stopped", "NeedsLogin", "Starting", None):
            with self.assertRaises(ba.BindError, msg=state) as cm:
                ba.tailscale_ips(run=fake_status(state=state))
            self.assertIn("not running", str(cm.exception))

    def test_unreadable_answer_or_no_address_refuses(self):
        with self.assertRaises(ba.BindError):
            ba.tailscale_ips(run=fake_status(stdout="not json"))
        with self.assertRaises(ba.BindError):
            ba.tailscale_ips(run=fake_status(ips=()))

    def test_missing_cli_refuses(self):
        with mock.patch.dict(os.environ, {"DND_TAILSCALE_CLI": str(Path(isolate_process()) / "nope.exe")}):
            with self.assertRaises(ba.BindError) as cm:
                ba.tailscale_ips(run=fake_status())
        self.assertIn("not installed", str(cm.exception))


class ResolveBindTests(unittest.TestCase):
    ips = staticmethod(lambda: [TS_V4, TS_V6])

    def test_keyword_picks_the_ipv4_address(self):
        self.assertEqual(ba.resolve_bind("tailscale", ips=self.ips), TS_V4)
        self.assertEqual(ba.resolve_bind("Tailscale", ips=lambda: [TS_V6]), TS_V6)

    def test_explicit_address_must_be_ours_right_now(self):
        self.assertEqual(ba.resolve_bind(TS_V4, ips=self.ips), TS_V4)
        with self.assertRaises(ba.BindError):
            ba.resolve_bind("100.64.0.7", ips=self.ips)

    def test_tailscale_down_stops_both_forms(self):
        def down():
            raise ba.BindError("Tailscale is not running (state: Stopped).")
        for value in ("tailscale", TS_V4):
            with self.assertRaises(ba.BindError, msg=value):
                ba.resolve_bind(value, ips=down)

    def test_never_all_networks_or_other_networks(self):
        for value in ("0.0.0.0", "::", "192.168.2.91", "10.0.0.5", "127.0.0.1", "localhost", "evil.example", ""):
            with self.assertRaises(ba.BindError, msg=value):
                ba.resolve_bind(value, ips=self.ips)

    def test_listens_on_loopback_and_tailscale_only(self):
        self.assertEqual(ba.listen_addresses(TS_V4), ["127.0.0.1", "::1", TS_V4])
        hosts = ba.allowed_hosts(TS_V4)
        self.assertEqual(hosts, {"localhost", "127.0.0.1", "::1", TS_V4})
        self.assertNotIn("0.0.0.0", hosts)


def _wsgi(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


class MakeServersTests(unittest.TestCase):
    def test_binds_every_address(self):
        servers = ba.make_servers(["127.0.0.1"], 0, _wsgi)
        try:
            self.assertEqual(servers[0].server_address[0], "127.0.0.1")
        finally:
            for s in servers:
                s.server_close()

    def test_all_or_nothing(self):
        created = []
        real = __import__("werkzeug.serving", fromlist=["make_server"]).make_server

        def spy(*a, **kw):
            s = real(*a, **kw)
            created.append(s)
            return s
        with mock.patch("werkzeug.serving.make_server", side_effect=spy):
            with self.assertRaises(ba.BindError) as cm:
                ba.make_servers(["127.0.0.1", "192.0.2.1"], 0, _wsgi)   # TEST-NET: never ours
        self.assertIn("192.0.2.1", str(cm.exception))
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].socket.fileno(), -1)   # the loopback server was closed again

    def test_optional_address_is_skipped(self):
        servers = ba.make_servers(["127.0.0.1", "192.0.2.1"], 0, _wsgi, optional=("192.0.2.1",))
        try:
            self.assertEqual([s.server_address[0] for s in servers], ["127.0.0.1"])
        finally:
            for s in servers:
                s.server_close()


class StartupTests(unittest.TestCase):
    """The real app script, started the way start-display.sh does."""

    def run_app(self, *args, cli="missing"):
        home = isolate_process()
        env = dict(os.environ, DND_CAMPAIGN_ROOT=str(home), DND_RUNTIME_DIR=str(Path(home) / ".runtime"),
                   DND_TAILSCALE_CLI=str(Path(home) / cli), PYTHONUTF8="1")
        return subprocess.run([sys.executable, str(DISPLAY / "dnd-display-app.py"), *args],
                              capture_output=True, text=True, timeout=60, env=env)

    def test_no_tailscale_means_no_display(self):
        res = self.run_app("--bind", "tailscale")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("display not started", res.stderr)
        self.assertIn("Tailscale is not installed", res.stderr)
        self.assertNotIn("0.0.0.0:5001", res.stdout)

    def test_bind_and_lan_together_refuse(self):
        res = self.run_app("--lan", "--bind", TS_V4)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("exclude each other", res.stderr)


class AutorunPermissionTests(unittest.TestCase):
    def test_autorun_never_grants_blanket_bash(self):
        text = (SKILL / "SKILL-commands.md").read_text(encoding="utf-8")
        section = text.split("## `/dm:dnd autorun on`", 1)[1].split("\n---", 1)[0]
        self.assertNotIn('merge `"Bash"`', section)
        self.assertNotIn("add it automatically", section)
        self.assertIn("Never touch the permission settings", section)

    def test_session_setup_starts_on_tailscale_never_lan(self):
        text = (SKILL / "SKILL-commands.md").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.lstrip().startswith("- `Display (")]
        self.assertEqual(len(lines), 4, lines)   # local + Tailscale, in /new and /load
        tailscale = [ln for ln in lines if "Display (Tailscale)" in ln]
        self.assertEqual(len(tailscale), 2)
        for ln in tailscale:
            self.assertIn("start-display.sh --bind tailscale", ln)
            self.assertIn("Never fall back to `--lan`", ln)
        for ln in lines:
            self.assertNotIn("start-display.sh --lan", ln)


if __name__ == "__main__":
    unittest.main()
