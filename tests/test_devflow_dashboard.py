# -*- coding: utf-8 -*-
"""Tests for the local DevFlow Dashboard (MVP).

Covers the safety-critical invariants from the spec: the app imports, the service lists/creates
runs, a created run is DRY-RUN (no GitHub writes), approve/reject updates state safely, the manual
packet is created, the watcher uses only the read-only path, the server defaults to localhost (and
rejects non-localhost Host headers), and there is no arbitrary-shell-execution endpoint.
"""

import contextlib
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from devflow import cli as _cli
import devflow.dashboard.app as app
import devflow.dashboard.service as service


@contextlib.contextmanager
def _quiet():
    """Swallow the workflow's dry-run stdout chatter so test output stays readable."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


class DashboardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash_test_")
        self.packets = os.path.join(self.tmp, "packets")
        # redirect the checkpoint + packet dirs so tests never touch the real ~temp/devflow_runs
        # or the cwd's .devflow (the HTTP /export route uses the default PACKETS_DIR).
        self._orig_ckpt = _cli.CKPT_DIR
        self._orig_packets = _cli.PACKETS_DIR
        _cli.CKPT_DIR = os.path.join(self.tmp, "runs")
        _cli.PACKETS_DIR = self.packets
        os.makedirs(_cli.CKPT_DIR, exist_ok=True)

    def tearDown(self):
        _cli.CKPT_DIR = self._orig_ckpt
        _cli.PACKETS_DIR = self._orig_packets
        shutil.rmtree(self.tmp, ignore_errors=True)


class ImportAndListTests(DashboardBase):
    def test_app_and_service_import(self):
        self.assertTrue(hasattr(app, "run_server"))
        self.assertTrue(hasattr(app, "main"))
        self.assertTrue(hasattr(service, "create_run"))
        self.assertTrue(hasattr(service, "list_runs"))

    def test_list_runs_reads_checkpoints(self):
        self.assertEqual(service.list_runs(), [])
        with _quiet():
            service.create_run("run-a", "docs-advisory", "owner/x", pause_at="advisory")
        runs = service.list_runs()
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["thread_id"], "run-a")
        self.assertEqual(r["status"], "paused")
        self.assertEqual(r["paused_gate_alias"], "advisory")


class DryRunSafetyTests(DashboardBase):
    def test_create_run_is_dry_run_and_makes_no_real_gh_calls(self):
        # if anything tried a real gh subprocess, this mock would fire — a created run must not.
        with mock.patch("devflow.tools.github_cli.subprocess.run",
                        side_effect=AssertionError("real gh invoked!")) as m, _quiet():
            final = service.create_run("dry-1", "docs-advisory", "owner/x", pause_at="advisory")
        self.assertFalse(final.get("real_github"))
        self.assertEqual(final.get("status"), "paused")
        m.assert_not_called()

    def test_run_to_completion_clears_checkpoint(self):
        with _quiet():
            final = service.create_run("complete-1", "docs-advisory", "owner/x", pause_at=None)
        self.assertNotEqual(final.get("status"), "paused")
        self.assertIsNone(service.get_run("complete-1"))   # checkpoint cleared on completion


class DecideGateTests(DashboardBase):
    def test_approve_advances_and_clears_checkpoint(self):
        with _quiet():
            service.create_run("dec-approve", "docs-advisory", "owner/x", pause_at="advisory")
            final = service.decide_gate("dec-approve", "advisory", "approved")
        self.assertNotEqual(final.get("status"), "paused")
        self.assertFalse(final.get("real_github"))
        self.assertIsNone(service.get_run("dec-approve"))

    def test_reject_stops_safely(self):
        with _quiet():
            service.create_run("dec-reject", "docs-advisory", "owner/x", pause_at="advisory")
            final = service.decide_gate("dec-reject", "advisory", "rejected")
        self.assertNotEqual(final.get("status"), "paused")
        self.assertEqual(final.get("human_approval"), "rejected")

    def test_decide_wrong_gate_refused(self):
        with _quiet():
            service.create_run("dec-wrong", "docs-advisory", "owner/x", pause_at="advisory")
        with self.assertRaises(ValueError):
            service.decide_gate("dec-wrong", "merge", "approved")   # paused at advisory, not merge

    def test_decide_unknown_thread_refused(self):
        with self.assertRaises(ValueError):
            service.decide_gate("nope", "advisory", "approved")

    def test_decide_bad_decision_refused(self):
        with self.assertRaises(ValueError):
            service.decide_gate("x", "advisory", "yolo")

    def test_fix_gate_resume_routes_to_fix_field(self):
        with _quiet():
            service.create_run("dec-fix", "docs-advisory", "owner/x", pause_at="fix")
            final = service.decide_gate("dec-fix", "fix", "approved")
        self.assertEqual(final.get("fix_approval"), "approved")
        self.assertNotEqual(final.get("status"), "paused")

    def test_merge_gate_resume_reject(self):
        with _quiet():
            service.create_run("dec-merge", "docs-advisory", "owner/x", pause_at="merge")
            final = service.decide_gate("dec-merge", "merge", "rejected")
        self.assertEqual(final.get("merge_approval"), "rejected")
        self.assertNotEqual(final.get("status"), "paused")

    def test_corrupt_checkpoint_missing_gate_refused(self):
        # a paused checkpoint with NO recorded gate must FAIL CLOSED (not accept an operator-chosen gate)
        _cli._save_ckpt({"thread_id": "corrupt", "status": "paused", "paused_at_gate": None,
                         "paused_at_node": None, "approvals": {}})
        with self.assertRaises(ValueError):
            service.decide_gate("corrupt", "merge", "approved")


class PacketTests(DashboardBase):
    def test_export_packet_from_paused(self):
        with _quiet():
            service.create_run("exp-1", "docs-advisory", "owner/x", pause_at="advisory")
            res = service.export_packet("exp-1", decision="approved", out_dir=self.packets)
        self.assertTrue(os.path.isfile(res["paths"]["md_path"]))
        self.assertTrue(os.path.isfile(res["paths"]["json_path"]))
        self.assertIn("safety_boundaries", res["packet"])
        self.assertEqual(res["packet"]["approval"]["decision"], "approved")
        self.assertIn("do not commit/push/merge", res["handoff"])

    def test_export_refuses_non_paused(self):
        with _quiet():
            service.create_run("exp-done", "docs-advisory", "owner/x", pause_at=None)
        with self.assertRaises(ValueError):
            service.export_packet("exp-done", out_dir=self.packets)

    def test_export_rejected_clears_implementable_scope(self):
        # a REJECTED export must not carry any implementable scope (defense-in-depth at this layer)
        with _quiet():
            service.create_run("exp-rej", "docs-advisory", "owner/x", pause_at="advisory")
            res = service.export_packet("exp-rej", decision="rejected", out_dir=self.packets)
        pkt = res["packet"]
        self.assertEqual(pkt["approval"]["decision"], "rejected")
        self.assertEqual(pkt["approval"]["approved_scope"], [])
        self.assertEqual(pkt["implementation_instructions"]["tasks"], [])
        self.assertEqual(pkt["implementation_instructions"]["files_likely_touched"], [])
        self.assertTrue(pkt["implementation_instructions"]["out_of_scope"][0].startswith("REJECTED"))
        self.assertIn("nothing to implement", res["handoff"])

    def test_manual_packet_creates_files(self):
        scope = ("# Approved scope\n- Add a helper to module y\n\n"
                 "# Tasks\n- Implement the helper\n- Add tests\n\n"
                 "# Files likely touched\n- devflow/y.py\n")
        res = service.create_manual_packet("man-1", "Add helper", "owner/x", scope,
                                           out_dir=self.packets)
        self.assertEqual(res["marker"], "MANUAL_IMPLEMENTATION_PACKET_CREATED")
        self.assertTrue(os.path.isfile(res["paths"]["md_path"]))
        self.assertTrue(os.path.isfile(res["paths"]["json_path"]))
        self.assertTrue(res["suggested_prompt"])
        with open(res["paths"]["json_path"], encoding="utf-8") as f:
            pkt = json.load(f)
        self.assertEqual(pkt["source"], "manual_human_scope")
        self.assertIn("Do not merge any pull request.", pkt["safety_boundaries"])

    def test_manual_packet_empty_scope_refused(self):
        with self.assertRaises(ValueError):
            service.create_manual_packet("man-empty", "t", "owner/x",
                                         "# Files likely touched\n- a.py\n", out_dir=self.packets)

    def test_manual_packet_quarantines_prohibited_action(self):
        scope = ("# Approved scope\n- Improve docs\n\n"
                 "# Tasks\n- Update README\n- git push origin main\n")
        res = service.create_manual_packet("man-q", "t", "owner/x", scope, out_dir=self.packets)
        oos = res["packet"]["implementation_instructions"]["out_of_scope"]
        self.assertTrue(any("prohibited git/PR action" in x for x in oos))
        self.assertNotIn("git push origin main",
                         res["packet"]["implementation_instructions"]["tasks"])


class _FakeReadOnlyGitHub:
    """Read-only stand-in: exposes ONLY read methods, records calls."""
    def __init__(self, repo):
        self.repo = repo
        self.calls = []

    def resolve_repo(self):
        self.calls.append("resolve_repo")
        return self.repo

    def list_open_prs(self, limit=50):
        self.calls.append("list_open_prs")
        return []


class WatcherTests(DashboardBase):
    def test_watcher_uses_read_only_path_only(self):
        fake = {}

        def _make(repo):
            fake["gh"] = _FakeReadOnlyGitHub(repo)
            return fake["gh"]

        seen = os.path.join(self.tmp, "codex_seen.json")
        with mock.patch.object(_cli, "check_gh_available",
                               return_value={"available": True, "authenticated": True}), \
             mock.patch.object(_cli, "ReadOnlyGitHub", side_effect=_make), \
             mock.patch.object(_cli, "_codex_seen_path", return_value=seen), \
             mock.patch("devflow.tools.github_cli.subprocess.run",
                        side_effect=AssertionError("real gh invoked!")):
            res = service.run_watcher("owner/repo", init=False)
        self.assertEqual(res["marker"], "NO_NEW_CODEX_REVIEWS")
        # only read methods were exercised
        self.assertEqual(set(fake["gh"].calls), {"resolve_repo", "list_open_prs"})

    def test_watcher_init_baseline_emits_no_marker_and_stays_read_only(self):
        fake = {}

        def _make(repo):
            fake["gh"] = _FakeReadOnlyGitHub(repo)
            return fake["gh"]

        seen = os.path.join(self.tmp, "codex_seen.json")
        with mock.patch.object(_cli, "check_gh_available",
                               return_value={"available": True, "authenticated": True}), \
             mock.patch.object(_cli, "ReadOnlyGitHub", side_effect=_make), \
             mock.patch.object(_cli, "_codex_seen_path", return_value=seen), \
             mock.patch("devflow.tools.github_cli.subprocess.run",
                        side_effect=AssertionError("real gh invoked!")):
            res = service.run_watcher("owner/repo", init=True)
        self.assertIsNone(res["marker"])                      # baseline records state, emits no marker
        self.assertEqual(set(fake["gh"].calls), {"resolve_repo", "list_open_prs"})

    def test_watcher_requires_repo(self):
        with self.assertRaises(ValueError):
            service.run_watcher("")


class NoShellExecutionTests(unittest.TestCase):
    def test_dashboard_layer_has_no_shell_execution(self):
        here = os.path.dirname(os.path.abspath(app.__file__))
        for fname in ("app.py", "service.py"):
            with open(os.path.join(here, fname), encoding="utf-8") as f:
                src = f.read()
            # precise dangerous patterns (avoid false matches like 'empty.' for a broad 'pty.')
            for forbidden in ("os.system(", "os.popen(", "subprocess.run(", "subprocess.Popen(",
                              "subprocess.call(", "import pty", "pty.spawn", "eval(", "exec("):
                self.assertNotIn(forbidden, src,
                                 "%s must not contain %r" % (fname, forbidden))

    def test_post_routes_are_a_fixed_safe_set(self):
        # the only state-changing endpoints; nothing accepts an arbitrary command
        import inspect
        src = inspect.getsource(app.Handler.do_POST)
        for route in ('"/new"', '"/manual"', '"/watcher"', '"/decide"', '"/export"'):
            self.assertIn(route, src)


class ServerTests(unittest.TestCase):
    def test_server_defaults_to_localhost(self):
        httpd = app.run_server(app.DEFAULT_HOST, 0)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            self.assertEqual(app.DEFAULT_HOST, "127.0.0.1")
            self.assertIn("127.0.0.1", httpd.allowed_hosts)
            self.assertIn("localhost", httpd.allowed_hosts)
        finally:
            httpd.server_close()

    def test_allowed_hosts_adds_custom_bind_host_and_keeps_loopback(self):
        # pure helper — exercised without binding a public interface
        allowed = app._allowed_hosts("0.0.0.0")
        self.assertIn("0.0.0.0", allowed)
        self.assertIn("127.0.0.1", allowed)
        self.assertIn("localhost", allowed)
        self.assertNotIn("0.0.0.0", app._LOCALHOST_NAMES)   # 0.0.0.0 is a bind addr, not a loopback name

    def test_main_warns_on_non_localhost_bind(self):
        # mock run_server so no public bind happens; serve_forever returns immediately
        fake = mock.Mock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        err = io.StringIO()
        with mock.patch.object(app, "run_server", return_value=fake), \
             mock.patch("sys.stderr", err), contextlib.redirect_stdout(io.StringIO()):
            rc = app.main(["--host", "0.0.0.0", "--port", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("not localhost", err.getvalue().lower())


class HttpIntegrationTests(DashboardBase):
    def setUp(self):
        super().setUp()
        self.httpd = app.run_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _conn(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        return c

    def test_runs_page_ok(self):
        c = self._conn()
        c.request("GET", "/")
        resp = c.getresponse()
        body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("DevFlow Dashboard", body)

    def test_bad_host_header_rejected(self):
        c = self._conn()
        c.putrequest("GET", "/", skip_host=True)
        c.putheader("Host", "evil.example.com")
        c.endheaders()
        resp = c.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)

    def test_create_run_and_decide_over_http(self):
        # POST /new -> 303 redirect to the run detail
        body = urllib.parse.urlencode({"thread_id": "http-1", "task_type": "docs-advisory",
                                       "repo": "owner/x", "pause_at": "advisory"})
        c = self._conn()
        with _quiet():
            c.request("POST", "/new", body=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = c.getresponse()
            resp.read()
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/run/http-1")

        c = self._conn()
        c.request("GET", "/run/http-1")
        resp = c.getresponse()
        detail = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("Paused at gate", detail)
        self.assertIn("Approve", detail)

        # approve -> redirect, run no longer paused
        c = self._conn()
        with _quiet():
            c.request("POST", "/decide",
                      body=urllib.parse.urlencode({"thread_id": "http-1", "gate": "advisory",
                                                   "decision": "approved"}),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = c.getresponse()
            resp.read()
        self.assertEqual(resp.status, 303)
        self.assertIsNone(service.get_run("http-1"))

    def test_bad_host_rejected_on_mutating_post(self):
        # the mutating POST path has its own host check; a non-localhost Host must 403 with NO effect.
        body = urllib.parse.urlencode({"thread_id": "evil-run", "pause_at": "advisory"}).encode()
        c = self._conn()
        c.putrequest("POST", "/new", skip_host=True)
        c.putheader("Host", "evil.example.com")
        c.putheader("Content-Type", "application/x-www-form-urlencoded")
        c.putheader("Content-Length", str(len(body)))
        c.endheaders()
        c.send(body)
        resp = c.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        self.assertIsNone(service.get_run("evil-run"))       # mutating endpoint did nothing

    def test_keepalive_survives_forbidden_post_with_body(self):
        # the 403 path must drain the body so the NEXT request on the same connection isn't desynced.
        c = self._conn()
        body = urllib.parse.urlencode({"thread_id": "x", "decision": "approved"}).encode()
        c.putrequest("POST", "/decide", skip_host=True)
        c.putheader("Host", "evil.example.com")
        c.putheader("Content-Type", "application/x-www-form-urlencoded")
        c.putheader("Content-Length", str(len(body)))
        c.endheaders()
        c.send(body)
        r1 = c.getresponse()
        r1.read()
        self.assertEqual(r1.status, 403)
        # reuse the SAME connection for a legit GET — must parse cleanly (no leftover body bytes)
        c.request("GET", "/")
        r2 = c.getresponse()
        r2.read()
        self.assertEqual(r2.status, 200)

    def test_export_over_http_writes_packet(self):
        with _quiet():
            service.create_run("http-exp", "docs-advisory", "owner/x", pause_at="advisory")
        c = self._conn()
        with _quiet():
            c.request("POST", "/export",
                      body=urllib.parse.urlencode({"thread_id": "http-exp", "decision": "approved"}),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = c.getresponse()
            resp.read()
        self.assertEqual(resp.status, 303)
        slugs = os.listdir(self.packets)                     # PACKETS_DIR redirected by DashboardBase
        self.assertTrue(slugs)
        self.assertTrue(os.path.isfile(os.path.join(self.packets, slugs[0], "implementation-packet.md")))

    def test_manual_packet_html_is_escaped(self):
        c = self._conn()
        with _quiet():
            c.request("POST", "/manual",
                      body=urllib.parse.urlencode({"thread_id": "xss-1",
                                                   "task": "<script>alert(1)</script>",
                                                   "repo": "o/r", "scope_markdown": "# Tasks\n- do x\n"}),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = c.getresponse()
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertNotIn("<script>alert(1)</script>", body)  # not reflected raw
        self.assertIn("&lt;script&gt;", body)                # HTML-escaped


if __name__ == "__main__":
    unittest.main()
