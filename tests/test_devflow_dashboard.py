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
from devflow.tools.packet_writer import PacketError
import devflow.tools.review_orchestrator_runner as orch_runner
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

    def test_list_runs_skips_watcher_seen_file(self):
        with _quiet():
            service.create_run("real-run", "docs-advisory", "owner/x", pause_at="advisory")
        # the watcher writes codex_seen.json into the SAME CKPT_DIR — it must not appear as a run
        with open(os.path.join(_cli.CKPT_DIR, "codex_seen.json"), "w", encoding="utf-8") as f:
            json.dump({"owner/repo": {"1": {"key": "abc"}}}, f)
        self.assertEqual([r["thread_id"] for r in service.list_runs()], ["real-run"])

    def test_create_run_refuses_duplicate_thread_id(self):
        with _quiet():
            service.create_run("dup-1", "docs-advisory", "owner/x", pause_at="advisory")
        with self.assertRaises(ValueError):
            service.create_run("dup-1", "docs-advisory", "owner/x", pause_at="advisory")


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

    def test_decide_refuses_real_github_checkpoint(self):
        # a live (--real-github) checkpoint must not be resumed as dry-run (would clobber provenance)
        with _quiet():
            service.create_run("rg-1", "docs-advisory", "owner/x", pause_at="advisory")
        st = service.get_run("rg-1")
        st["real_github"] = True
        _cli._save_ckpt(st)
        with self.assertRaises(ValueError):
            service.decide_gate("rg-1", "advisory", "approved")


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

    def test_stdout_producers_share_one_lock(self):
        # ALL stdout producers (create_run/decide_gate AND the watcher) must hold the SAME lock, so a
        # concurrent dry-run print can't bleed into the watcher's redirect_stdout buffer.
        self.assertTrue(hasattr(service, "_STDOUT_LOCK"))
        entered = []
        real = service._STDOUT_LOCK

        class Tracking:
            def __enter__(s):
                entered.append(1)
                return real.__enter__()

            def __exit__(s, *a):
                return real.__exit__(*a)

        seen = os.path.join(self.tmp, "seen.json")
        with mock.patch.object(service, "_STDOUT_LOCK", Tracking()), \
             mock.patch.object(_cli, "check_gh_available",
                               return_value={"available": True, "authenticated": True}), \
             mock.patch.object(_cli, "ReadOnlyGitHub", side_effect=lambda repo: _FakeReadOnlyGitHub(repo)), \
             mock.patch.object(_cli, "_codex_seen_path", return_value=seen), _quiet():
            service.create_run("lock-1", "docs-advisory", "owner/x", pause_at="advisory")
            service.run_watcher("owner/repo")
        self.assertGreaterEqual(len(entered), 2)   # both create_run AND run_watcher took the shared lock


class _FakeOrchGH:
    """Read-only stand-in for ReadOnlyGitHub used by the orchestration runner — only read methods."""
    def __init__(self, repo):
        self.repo = repo
        self.calls = []

    def resolve_repo(self):
        self.calls.append("resolve_repo")
        return "o/r"

    def get_repo_info(self):
        self.calls.append("get_repo_info")
        return {"default_branch": "main"}

    def list_prs(self, state="open", limit=50):
        self.calls.append(("list_prs", state, limit))
        return [{"number": 5, "title": "feat: x", "state": "OPEN", "head_ref": "feat/x",
                 "base_ref": "main", "branch": "feat/x", "head": "aaaaaaa"}]

    def get_pr_meta(self, num):
        self.calls.append(("get_pr_meta", num))
        return {"number": num, "state": "OPEN", "mergeable": "MERGEABLE", "base_ref": "main",
                "head_ref": "feat/x", "head_oid": "aaaaaaa", "is_draft": True, "additions": 10,
                "deletions": 0, "changed_files": 2, "title": "feat: x"}

    def get_pr_codex_signals(self, num):
        self.calls.append(("get_pr_codex_signals", num))
        return {"reviews": [], "inline": [], "comments": []}

    def merged_heads(self, branches):
        self.calls.append(("merged_heads", tuple(sorted(branches or []))))
        return {}


class BuildOrchestrationResultTests(unittest.TestCase):
    def test_structured_result_is_read_only_and_does_not_persist_by_default(self):
        holder = {}

        def mk(repo):
            holder["gh"] = _FakeOrchGH(repo)
            return holder["gh"]

        with mock.patch.object(orch_runner, "ReadOnlyGitHub", side_effect=mk), \
             mock.patch.object(orch_runner.orch, "save_state") as save:
            res = orch_runner.build_orchestration_result("o/r", limit=50)   # persist_state default False
        self.assertEqual(res["marker"], "ORCHESTRATION_PLAN")
        self.assertEqual(res["repo"], "o/r")
        self.assertEqual(res["default_branch"], "main")
        self.assertEqual([p["number"] for p in res["open_prs"]], [5])
        self.assertIn(5, res["plan"]["request_review"])          # unreviewed PR -> request review
        save.assert_not_called()                                  # default: never persists tracking state
        self.assertIn("resolve_repo", holder["gh"].calls)        # only the fake's read methods exist

    def test_persist_state_true_saves_tracking(self):
        with mock.patch.object(orch_runner, "ReadOnlyGitHub", side_effect=lambda r: _FakeOrchGH(r)), \
             mock.patch.object(orch_runner.orch, "save_state") as save:
            orch_runner.build_orchestration_result("o/r", persist_state=True)
        save.assert_called_once()


class OrchestratorServiceTests(unittest.TestCase):
    def test_run_orchestrator_requires_repo(self):
        with self.assertRaises(ValueError):
            service.run_orchestrator("")

    def test_run_orchestrator_clamps_limit_and_never_persists(self):
        with mock.patch.object(service, "build_orchestration_result",
                               return_value={"marker": "NO_ACTION_NEEDED"}) as m:
            service.run_orchestrator("o/r", limit=99999)
            service.run_orchestrator("o/r", limit="not-a-number")
        big = m.call_args_list[0].kwargs
        self.assertLessEqual(big["limit"], service.ORCH_LIMIT_MAX)   # clamped
        self.assertFalse(big["persist_state"])                       # dashboard never persists
        bad = m.call_args_list[1].kwargs
        self.assertEqual(bad["limit"], service.ORCH_LIMIT_DEFAULT)   # invalid -> default


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


class PayloadRenderTests(unittest.TestCase):
    def test_payload_html_renders_full_gate_context(self):
        out = app._payload_html({"question": "ok?", "advisory": "do the thing",
                                 "blocking_comments": [{"path": "a.py", "note": "fix import"}],
                                 "pr_url": "https://example.test/pr/1",
                                 "review_summary": {"blocking": 1}})
        self.assertIn("do the thing", out)
        self.assertIn("a.py: fix import", out)
        self.assertIn("https://example.test/pr/1", out)
        self.assertIn("blocking", out)

    def test_payload_html_escapes_untrusted_text(self):
        out = app._payload_html({"advisory": "<b>x</b>"})
        self.assertIn("&lt;b&gt;", out)
        self.assertNotIn("<b>x</b>", out)


class PackagingTests(unittest.TestCase):
    def test_dashboard_package_and_templates_declared(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "pyproject.toml"), encoding="utf-8") as f:
            toml = f.read()
        self.assertIn("devflow.dashboard", toml)          # package shipped
        self.assertIn("templates/*.html", toml)           # templates shipped as package data


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

    def test_ipv6_host_binds_ipv6_family(self):
        import socket as _socket
        try:
            httpd = app.run_server("::1", 0)
        except OSError:
            self.skipTest("IPv6 not available on this host")
        try:
            self.assertEqual(httpd.address_family, _socket.AF_INET6)
            self.assertIn("::1", httpd.allowed_hosts)
        finally:
            httpd.server_close()

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

    def test_main_brackets_ipv6_url(self):
        fake = mock.Mock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        out = io.StringIO()
        with mock.patch.object(app, "run_server", return_value=fake), mock.patch("sys.stdout", out):
            app.main(["--host", "::1", "--port", "0"])
        self.assertIn("[::1]", out.getvalue())               # IPv6 literal bracketed in the printed URI


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

    def _post(self, path, fields, extra_headers=None):
        """POST a form, optionally with extra headers (e.g. Sec-Fetch-Site / Origin)."""
        body = urllib.parse.urlencode(fields).encode()
        c = self._conn()
        c.putrequest("POST", path)                    # default: sends Host: 127.0.0.1:<port>
        c.putheader("Content-Type", "application/x-www-form-urlencoded")
        c.putheader("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        c.send(body)
        return c.getresponse()

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

    def test_export_over_http_writes_packet_and_surfaces_result(self):
        with _quiet():
            service.create_run("http-exp", "docs-advisory", "owner/x", pause_at="advisory")
        c = self._conn()
        with _quiet():
            c.request("POST", "/export",
                      body=urllib.parse.urlencode({"thread_id": "http-exp", "decision": "approved"}),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = c.getresponse()
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)                   # renders the result, not a bare redirect
        self.assertIn("exported", body.lower())
        slugs = os.listdir(self.packets)                     # PACKETS_DIR redirected by DashboardBase
        self.assertTrue(slugs)
        md = os.path.join(self.packets, slugs[0], "implementation-packet.md")
        self.assertTrue(os.path.isfile(md))
        self.assertIn("implementation-packet.md", body)      # path surfaced in the UI

    def test_export_failure_is_surfaced(self):
        with _quiet():
            service.create_run("exp-fail", "docs-advisory", "owner/x", pause_at="advisory")
        with mock.patch.object(service, "export_packet", side_effect=PacketError("symlink refused")):
            c = self._conn()
            c.request("POST", "/export",
                      body=urllib.parse.urlencode({"thread_id": "exp-fail", "decision": "approved"}),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = c.getresponse()
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("Export failed", body)

    def test_responses_deny_framing(self):
        c = self._conn()
        c.request("GET", "/")
        resp = c.getresponse()
        resp.read()
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors", resp.getheader("Content-Security-Policy") or "")

    def test_export_offers_explicit_decision_buttons(self):
        with _quiet():
            service.create_run("exp-btn", "docs-advisory", "owner/x", pause_at="advisory")
        c = self._conn()
        c.request("GET", "/run/exp-btn")
        resp = c.getresponse()
        body = resp.read().decode("utf-8")
        self.assertIn("Export approved packet", body)
        self.assertIn("Export rejected packet", body)        # explicit decision, not a silent "approved"

    def test_real_github_run_is_export_only(self):
        with _quiet():
            service.create_run("rg-detail", "docs-advisory", "owner/x", pause_at="advisory")
        st = service.get_run("rg-detail")
        st["real_github"] = True
        _cli._save_ckpt(st)
        c = self._conn()
        c.request("GET", "/run/rg-detail")
        resp = c.getresponse()
        body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("real-github", body)
        self.assertNotIn("action='/decide'", body)            # no Approve/Reject form
        self.assertIn("Export approved packet", body)          # export still offered

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

    def test_csrf_cross_site_post_rejected(self):
        resp = self._post("/new", {"thread_id": "csrf-x", "pause_at": "advisory"},
                          {"Sec-Fetch-Site": "cross-site"})
        resp.read()
        self.assertEqual(resp.status, 403)
        self.assertIsNone(service.get_run("csrf-x"))         # cross-site POST had no effect

    def test_csrf_same_origin_post_allowed(self):
        with _quiet():
            resp = self._post("/new", {"thread_id": "csrf-ok", "pause_at": "advisory"},
                              {"Sec-Fetch-Site": "same-origin"})
            resp.read()
        self.assertEqual(resp.status, 303)
        self.assertIsNotNone(service.get_run("csrf-ok"))

    def test_csrf_foreign_origin_rejected(self):
        resp = self._post("/new", {"thread_id": "csrf-o", "pause_at": "advisory"},
                          {"Origin": "http://evil.example.com"})
        resp.read()
        self.assertEqual(resp.status, 403)
        self.assertIsNone(service.get_run("csrf-o"))

    def test_completed_run_redirects_to_runs_page_not_404(self):
        with _quiet():
            resp = self._post("/new", {"thread_id": "f2-done", "task_type": "docs-advisory",
                                       "repo": "o/x", "pause_at": ""})
            resp.read()
        self.assertEqual(resp.status, 303)
        self.assertTrue(resp.getheader("Location").startswith("/?done="))
        self.assertIsNone(service.get_run("f2-done"))        # completed -> checkpoint cleared

    def test_orchestrator_page_loads(self):
        c = self._conn()
        c.request("GET", "/orchestrator")
        resp = c.getresponse()
        body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("Review Queue", body)
        self.assertIn("read-only", body.lower())

    def test_orchestrator_missing_repo_shows_validation(self):
        resp = self._post("/orchestrator", {"repo": "", "limit": "50"})
        body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("repo is required", body)

    def test_orchestrator_renders_plan_sections(self):
        canned = {
            "marker": "ORCHESTRATION_PLAN", "repo": "o/r", "default_branch": "main",
            "state_path": "/tmp/s.json", "rate_limited": True,
            "errors": [{"pr": 9, "error": "boom"}],
            "open_prs": [{"number": 5, "title": "feat: x", "branch": "feat/x", "base_ref": "main"},
                         {"number": 6, "title": "fix: y", "branch": "fix/y", "base_ref": "main"}],
            "plan": {"ranking": [{"pr": 5, "priority": 10, "rounds": 0, "clean": False, "state": "OPEN"}],
                     "request_review": [5], "findings_to_fix": [6], "mergeable_now": [5],
                     "force_mergeable": [], "ready_then_merge": [], "needs_conflict": [],
                     "needs_retarget": [], "retarget_to": {}, "mergeable_unknown": [],
                     "in_flight": [5], "rate_limited": True}}
        with mock.patch.object(service, "run_orchestrator", return_value=canned):
            resp = self._post("/orchestrator", {"repo": "o/r", "limit": "50"})
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        for section in ("Request review", "Findings to fix", "Mergeable now"):
            self.assertIn(section, body)
        self.assertIn("@codex review", body)                  # copyable request text
        self.assertIn("back off", body)                       # rate_limited surfaced
        self.assertIn("read errors", body.lower())            # errors surfaced
        self.assertIn("merge preflight", body.lower())        # no merge button, preflight note instead
        # read-only: the result section has NO write forms; only the compute-plan form exists on the page
        self.assertEqual(body.count("<form"), 1)
        self.assertNotIn("action='/decide'", body)
        self.assertNotIn("Merge</button>", body)
        self.assertNotIn("Approve</button>", body)

    def test_orchestrator_escapes_html(self):
        canned = {
            "marker": "ORCHESTRATION_PLAN", "repo": "o/r", "default_branch": "main",
            "state_path": "/tmp/s.json", "rate_limited": False,
            "errors": [{"pr": 9, "error": "<i>boom</i>"}],
            "open_prs": [{"number": 5, "title": "<script>pwn</script>", "branch": "<b>brnch</b>",
                          "base_ref": "main"}],
            "plan": {"ranking": [{"pr": 5, "priority": 1, "rounds": 0, "clean": False, "state": "OPEN"}],
                     "request_review": [5], "findings_to_fix": [], "mergeable_now": [],
                     "force_mergeable": [], "ready_then_merge": [], "needs_conflict": [],
                     "needs_retarget": [], "retarget_to": {}, "mergeable_unknown": [],
                     "in_flight": [], "rate_limited": False}}
        with mock.patch.object(service, "run_orchestrator", return_value=canned):
            resp = self._post("/orchestrator", {"repo": "o/r"})
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertNotIn("<script>pwn</script>", body)        # PR title escaped
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<b>brnch</b>", body)                # branch escaped
        self.assertNotIn("<i>boom</i>", body)                 # error escaped


if __name__ == "__main__":
    unittest.main()
