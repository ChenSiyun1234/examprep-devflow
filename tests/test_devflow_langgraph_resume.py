# -*- coding: utf-8 -*-
"""Real LangGraph interrupt() / Command(resume=...) tests, plus fallback-resume parity.

The interrupt/resume *mechanics* are exercised in-process with a MemorySaver and gated on langgraph
being installed; the durable two-command CLI flow is additionally gated on the SQLite checkpointer.
The stdlib fallback resume test needs neither. No test performs real GitHub writes.

    python -m unittest tests.test_devflow_langgraph_resume
"""

import uuid
import unittest
from types import SimpleNamespace
from unittest import mock

import devflow.graph as G
from devflow.state import new_state, APPROVED, GATE_FIX, GATE_MERGE


def _sqlite_available():
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401
        return True
    except Exception:
        return False


def _run_mechanics(decision):
    """Drive the compiled langgraph graph to the advisory gate, then Command(resume=decision)."""
    from langgraph.types import Command
    from langgraph.checkpoint.memory import MemorySaver
    app = G.make_graph().compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "m-" + decision + "-" + uuid.uuid4().hex[:6]}}
    seeded = {GATE_FIX: APPROVED, GATE_MERGE: APPROVED}  # only the advisory gate is unseeded
    app.invoke(new_state("docs-advisory", cfg["configurable"]["thread_id"], approvals=seeded), cfg)
    paused_at = app.get_state(cfg).next
    app.invoke(Command(resume=decision), cfg)            # native resume
    snap = app.get_state(cfg)
    return paused_at, snap.next, snap.values


@unittest.skipUnless(G.HAS_LANGGRAPH, "langgraph not installed")
class TestLangGraphInterruptResume(unittest.TestCase):

    def test_pauses_at_approval_gate(self):
        paused_at, _, _ = _run_mechanics("approved")
        self.assertEqual(paused_at, ("human_approval_gate",))

    def test_resume_approved_continues(self):
        _, nxt, values = _run_mechanics("approved")
        self.assertEqual(nxt, ())                                  # finished
        self.assertEqual(values.get("human_approval"), "approved")  # decision recorded (not inferred)
        self.assertIn("would-merge", values.get("final_report") or "")

    def test_resume_rejected_safe_stops(self):
        _, nxt, values = _run_mechanics("rejected")
        self.assertEqual(nxt, ())
        self.assertEqual(values.get("human_approval"), "rejected")
        self.assertIn("stopped", values.get("final_report") or "")
        # safe stop: the implementation node must not have run
        self.assertFalse(any("apply_approved_changes" in e for e in values.get("event_log", [])))

    def test_state_preserved_across_gate(self):
        # the issue created before the gate is still present in state after resume
        _, _, values = _run_mechanics("approved")
        self.assertIsNotNone(values.get("issue_number"))

    def test_no_real_writes_during_interrupt_resume(self):
        from devflow.tools import github_cli
        with mock.patch.object(github_cli.subprocess, "run",
                               side_effect=AssertionError("no subprocess during dry-run interrupt/resume")):
            _, nxt, _ = _run_mechanics("approved")
        self.assertEqual(nxt, ())


class TestFallbackResumeStillWorks(unittest.TestCase):
    """No langgraph needed: the stdlib fallback pause/resume must keep working."""

    def test_fallback_pause_then_resume(self):
        st = new_state("docs-advisory", "fb-" + uuid.uuid4().hex[:6], approvals={})
        paused = G.build_graph(prefer_fallback=True).invoke(st)
        self.assertEqual(paused["status"], "paused")
        paused["approvals"] = {g: APPROVED for g in (GATE_FIX, GATE_MERGE)}
        paused["approvals"][paused["paused_at_gate"]] = APPROVED
        resumed = G.build_graph(prefer_fallback=True).invoke(paused, start_node=paused["paused_at_node"])
        self.assertEqual(resumed["status"], "done")


@unittest.skipUnless(G.HAS_LANGGRAPH and _sqlite_available(),
                     "needs langgraph + langgraph-checkpoint-sqlite")
class TestLangGraphCliDurableFlow(unittest.TestCase):
    """The two-command CLI flow: `run --langgraph --pause-at` then `resume --langgraph`."""

    def _run_args(self, tid, pause_at="advisory"):
        return SimpleNamespace(task="docs-advisory", thread_id=tid, repo="o/r", reject=None,
                               pause_at=pause_at, simulate_advisory=None, simulate_review=None,
                               langgraph=True)

    def _resume_args(self, tid, decision):
        return SimpleNamespace(thread_id=tid, gate="advisory", decision=decision,
                               real_github=False, langgraph=True)

    def test_run_pauses_then_resume_approved_completes(self):
        from devflow import cli
        from devflow.tools import github_cli
        tid = "cli-appr-" + uuid.uuid4().hex[:8]
        with mock.patch.object(github_cli.subprocess, "run",
                               side_effect=AssertionError("no real gh writes in dry-run")):
            self.assertEqual(cli.cmd_run(self._run_args(tid)), 0)        # pauses
            self.assertEqual(cli.cmd_resume(self._resume_args(tid, "approved")), 0)  # completes

    def test_resume_rejected_safe_stops(self):
        from devflow import cli
        from devflow.tools import github_cli
        tid = "cli-rej-" + uuid.uuid4().hex[:8]
        with mock.patch.object(github_cli.subprocess, "run",
                               side_effect=AssertionError("no real gh writes in dry-run")):
            self.assertEqual(cli.cmd_run(self._run_args(tid)), 0)
            self.assertEqual(cli.cmd_resume(self._resume_args(tid, "rejected")), 0)

    def test_resume_unknown_thread_returns_error(self):
        from devflow import cli
        self.assertEqual(cli.cmd_resume(self._resume_args("never-ran-" + uuid.uuid4().hex[:8],
                                                          "approved")), 1)


if __name__ == "__main__":
    unittest.main()
