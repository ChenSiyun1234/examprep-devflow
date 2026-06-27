# -*- coding: utf-8 -*-
"""Tests for LangGraph Studio compatibility (langgraph.json + an exported graph factory).

These run on pure stdlib (no langgraph needed); the one test that actually builds a StateGraph is
skipped unless langgraph is installed.

    python -m unittest tests.test_devflow_studio
"""

import json
import os
import unittest
from unittest import mock

import devflow.graph as G

# tests/ lives at the repo root, so the parent dir is the repo root where langgraph.json lives.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestStudioCompat(unittest.TestCase):

    def test_graph_module_imports_without_langgraph(self):
        # importing the module must not require langgraph or build any graph
        self.assertTrue(hasattr(G, "build_graph"))
        self.assertTrue(callable(G.build_graph))

    def test_studio_target_exists(self):
        self.assertTrue(hasattr(G, "make_graph"), "devflow.graph must export make_graph for Studio")
        self.assertTrue(callable(G.make_graph))

    def test_langgraph_json_exists_and_points_to_valid_target(self):
        path = os.path.join(ROOT, "langgraph.json")
        self.assertTrue(os.path.exists(path), "langgraph.json missing at repo root")
        cfg = json.load(open(path, encoding="utf-8"))
        self.assertIn("dependencies", cfg)
        self.assertIn("graphs", cfg)
        target = cfg["graphs"].get("devflow")
        self.assertTrue(target, "graphs.devflow must be declared")
        mod_path, sep, attr = target.partition(":")
        self.assertTrue(sep and attr, "target must be of the form './path/module.py:attr'")
        # the referenced file exists on disk and the attribute is defined in devflow.graph
        self.assertTrue(os.path.exists(os.path.join(ROOT, mod_path)), f"{mod_path} not found")
        self.assertTrue(hasattr(G, attr), f"devflow.graph has no '{attr}' (langgraph.json target)")

    def test_fallback_cli_and_backend_still_work(self):
        import devflow.cli  # noqa: F401 — must still import without langgraph
        app = G.build_graph(prefer_fallback=True)
        self.assertEqual(getattr(app, "backend", None), "fallback")

    def test_locating_studio_target_performs_no_gh_writes(self):
        # merely referencing/holding the Studio factory must not spawn gh or run the workflow
        from devflow.tools import github_cli
        with mock.patch.object(github_cli.subprocess, "run",
                               side_effect=AssertionError("no subprocess at import/target time")):
            self.assertTrue(callable(G.make_graph))

    @unittest.skipUnless(G.HAS_LANGGRAPH, "langgraph not installed")
    def test_make_graph_builds_compilable_stategraph(self):  # pragma: no cover
        from langgraph.graph import StateGraph
        sg = G.make_graph()
        self.assertIsInstance(sg, StateGraph)
        # all workflow nodes are present and the graph compiles
        for node in G.NODE_FUNCS:
            self.assertIn(node, sg.nodes)
        self.assertIsNotNone(sg.compile())

    @unittest.skipUnless(G.HAS_LANGGRAPH, "langgraph not installed")
    def test_no_node_name_collides_with_a_state_key(self):  # pragma: no cover
        # LangGraph forbids a node name equal to a state channel; this is what broke Studio loading.
        from devflow.state import DevflowState
        keys = set(DevflowState.__annotations__)
        self.assertEqual(set(G.NODE_FUNCS) & keys, set(),
                         "node name(s) collide with state keys -> graph won't build under LangGraph")

    @unittest.skipUnless(G.HAS_LANGGRAPH, "langgraph not installed")
    def test_approval_gate_surfaces_as_interrupt(self):  # pragma: no cover
        # what Studio relies on: an unseeded approval gate pauses the compiled graph as an interrupt
        from langgraph.checkpoint.memory import MemorySaver
        from devflow.state import new_state
        app = G.make_graph().compile(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "studio-test"}}
        app.invoke(new_state("docs-advisory", "studio-test", approvals={}), cfg)
        snap = app.get_state(cfg)
        self.assertIn("human_approval_gate", snap.next)  # paused at the advisory approval gate


if __name__ == "__main__":
    unittest.main()
