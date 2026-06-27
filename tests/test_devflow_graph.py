# -*- coding: utf-8 -*-
"""Lightweight tests for the devflow dry-run orchestrator.

Run from the repo root:  python -m unittest tests.test_devflow_graph
"""

import unittest
from unittest import mock

from devflow.graph import build_graph
from devflow.state import (
    new_state, APPROVED, REJECTED,
    GATE_ADVISORY, GATE_FIX, GATE_MERGE, APPROVAL_GATES,
)

ALL_APPROVED = {g: APPROVED for g in APPROVAL_GATES}


def run(approvals=None, simulate=None, start=None, state=None):
    st = state or new_state("docs-advisory", "t-1", approvals=approvals if approvals is not None else dict(ALL_APPROVED))
    if simulate:
        st["_simulate"] = simulate
    app = build_graph(prefer_fallback=True)
    return app.invoke(st, start_node=start) if start else app.invoke(st)


class TestDevflowGraph(unittest.TestCase):

    def test_graph_builds(self):
        app = build_graph(prefer_fallback=True)
        self.assertTrue(hasattr(app, "invoke"))
        self.assertEqual(app.backend, "fallback")

    def test_dry_run_reaches_final_report(self):
        s = run()
        self.assertEqual(s["status"], "done")
        self.assertIn("devflow dry-run report", s["final_report"])
        # merge is simulated only — must never be reported as performed
        self.assertIn("merge performed  : NO", s["final_report"])

    def test_state_has_event_log_entries(self):
        s = run()
        self.assertGreater(len(s["event_log"]), 10)
        self.assertTrue(any("check_environment" in e for e in s["event_log"]))

    def test_reject_merge_routes_to_safe_stop(self):
        s = run({**ALL_APPROVED, GATE_MERGE: REJECTED})
        self.assertEqual(s["merge_approval"], REJECTED)
        self.assertEqual(s["status"], "done")
        self.assertIn("stopped: merge rejected", s["final_report"])
        # the merge-execution node must NOT have run
        self.assertFalse(any("claude_execute_merge" in e for e in s["event_log"]))

    def test_reject_advisory_stops_before_implementation(self):
        s = run({**ALL_APPROVED, GATE_ADVISORY: REJECTED})
        self.assertEqual(s["human_approval"], REJECTED)
        self.assertIn("stopped: advisory implementation rejected", s["final_report"])
        self.assertFalse(any("apply_approved_changes" in e for e in s["event_log"]))

    def test_clean_review_skips_fix_gate(self):
        s = run(simulate={"advisory": "ready", "review": "clean"})
        self.assertEqual(s["status"], "done")
        self.assertEqual(len(s.get("blocking_comments", [])), 0)
        self.assertIsNone(s.get("fix_approval"))  # fix gate skipped entirely
        self.assertFalse(any("human_fix_approval" in e for e in s["event_log"]))

    def test_unseeded_gate_pauses_with_interrupt(self):
        # no approvals seeded -> first gate raises a (fallback) interrupt and the run pauses
        s = run(approvals={})
        self.assertEqual(s["status"], "paused")
        self.assertEqual(s["paused_at_gate"], GATE_ADVISORY)
        self.assertIn("gate", s.get("interrupt_payload", {}))

    def test_resume_after_pause_completes(self):
        paused = run(approvals={})
        self.assertEqual(paused["status"], "paused")
        # seed the decision (the "resume payload") and resume from the paused node
        paused["approvals"] = dict(ALL_APPROVED)
        resumed = run(state=paused, start=paused["paused_at_node"])
        self.assertEqual(resumed["status"], "done")
        self.assertIn("devflow dry-run report", resumed["final_report"])

    def test_advisory_timeout_safe_stop(self):
        s = run(simulate={"advisory": "timeout", "review": "blocking"})
        self.assertEqual(s["codex_advisory_status"], "timeout")
        self.assertIn("stopped: codex advisory timed out", s["final_report"])
        self.assertTrue(s.get("errors"))

    def test_no_real_subprocess_executed_in_dry_run(self):
        # If any node tried to shell out to git/gh, this patched subprocess.run would raise.
        def _boom(*a, **k):
            raise AssertionError("dry-run must not execute any subprocess (gh/git)")
        with mock.patch("subprocess.run", side_effect=_boom), \
             mock.patch("subprocess.Popen", side_effect=_boom), \
             mock.patch("subprocess.check_output", side_effect=_boom):
            s = run()
        self.assertEqual(s["status"], "done")

    def test_github_tool_is_dry_run_only(self):
        from devflow.tools.github_cli import DryRunGitHub
        gh = DryRunGitHub("owner/repo")
        merged = gh.merge_pr(123)
        self.assertFalse(merged["merged"])
        self.assertTrue(all(c["executed"] is False for c in gh.calls))


# ---- fixes from Codex review of PR #1 ----
class TestCodexReviewFixesPR1(unittest.TestCase):
    def test_fallback_sets_force_fallback(self):
        s = run(approvals={})  # pauses at advisory
        self.assertTrue(s.get("_force_fallback"))

    def test_pause_at_overrides_seeded_approval(self):
        # all gates seeded approved, but pause_at forces the advisory gate to interrupt
        st = new_state("docs-advisory", "t", approvals=dict(ALL_APPROVED), pause_at=GATE_ADVISORY)
        final = build_graph(prefer_fallback=True).invoke(st)
        self.assertEqual(final["status"], "paused")
        self.assertEqual(final["paused_at_gate"], GATE_ADVISORY)

    def test_merge_readiness_requires_completed_rereview(self):
        from devflow.nodes.merge import merge_readiness
        base = new_state("t", "t")
        base["blocking_comments"] = [{"note": "x"}]
        base["codex_review_status"] = "ready"
        # re-review NOT done -> not merge-ready (was the bug: synthetic fix made it ready)
        self.assertFalse(merge_readiness({**base})["merge_readiness_ready"])
        # re-review done & clean -> ready
        ok = merge_readiness({**base, "rereview_done": True, "rereview_blocking": False})
        self.assertTrue(ok["merge_readiness_ready"])
        # re-review done but still blocking -> not ready
        bad = merge_readiness({**base, "rereview_done": True, "rereview_blocking": True})
        self.assertFalse(bad["merge_readiness_ready"])

    def test_blocking_path_reaches_merge_via_rereview(self):
        # full dry-run blocking path now passes through a completed re-review before merge
        s = run(simulate={"advisory": "ready", "review": "blocking"})
        self.assertEqual(s["status"], "done")
        self.assertTrue(any("request_codex_rereview" in e for e in s["event_log"]))
        self.assertIn("would-merge", s["final_report"])

    def test_checkpoint_path_no_collision(self):
        from devflow.cli import _ckpt_path
        self.assertNotEqual(_ckpt_path("demo/a"), _ckpt_path("demo_a"))

    def test_invoke_passes_thread_config_to_langgraph(self):
        from devflow.cli import _invoke
        captured = {}

        class FakeLG:
            backend = "langgraph"
            def invoke(self, state, config=None):
                captured["config"] = config
                return {"status": "done"}

        _invoke(FakeLG(), {"thread_id": "abc"})
        self.assertEqual(captured["config"], {"configurable": {"thread_id": "abc"}})


# ---- round 2 of Codex review on PR #1 ----
class TestCodexReviewFixesPR1Round2(unittest.TestCase):
    def test_clean_rereview_marks_findings_resolved(self):
        s = run(simulate={"advisory": "ready", "review": "blocking"})
        self.assertEqual(s["status"], "done")
        # report must be internally consistent: 0 outstanding blocking AND would-merge
        self.assertIn("0 outstanding", s["final_report"])
        self.assertIn("would-merge", s["final_report"])
        self.assertEqual((s.get("review_summary") or {}).get("outstanding_blocking"), 0)

    def test_pause_at_cleared_on_resume(self):
        # pause_at forces the gate to pause even though all gates are seeded-approved...
        st = new_state("docs-advisory", "t", approvals=dict(ALL_APPROVED), pause_at=GATE_ADVISORY)
        paused = build_graph(prefer_fallback=True).invoke(st)
        self.assertEqual(paused["status"], "paused")
        # ...and resuming from the paused node proceeds (pause_at cleared, seeded decision wins)
        resumed = build_graph(prefer_fallback=True).invoke(paused, start_node=paused["paused_at_node"])
        self.assertEqual(resumed["status"], "done")


# ---- round 3 of Codex review on PR #1 ----
class TestCodexReviewFixesPR1Round3(unittest.TestCase):
    def test_append_keys_explicit_and_complete(self):
        from devflow.state import append_keys
        self.assertEqual(append_keys(), {
            "blocking_comments", "non_blocking_comments", "deferred_followups",
            "checks_run", "checks_not_run", "files_changed", "errors", "event_log"})

    # NOTE: `--pause-at` with `--langgraph` is now SUPPORTED (native interrupt/resume) — the old
    # "refused" behavior was removed. See tests/test_devflow_langgraph_resume.py for its coverage.

    def test_checkpoint_filename_is_length_bounded(self):
        from devflow.cli import _ckpt_path
        import os
        self.assertLessEqual(len(os.path.basename(_ckpt_path("z" * 500))), 100)

    def test_completed_run_clears_stale_checkpoint(self):
        from devflow import cli
        import os
        tid = "round3-clear-xyz"
        cli._save_ckpt({"thread_id": tid, "status": "paused"})  # stale paused checkpoint
        self.assertTrue(os.path.exists(cli._ckpt_path(tid)))
        args = cli.build_parser().parse_args(["run", "--thread-id", tid])
        cli.cmd_run(args)  # completes (status done) -> should remove the stale checkpoint
        self.assertFalse(os.path.exists(cli._ckpt_path(tid)))


if __name__ == "__main__":
    unittest.main()
