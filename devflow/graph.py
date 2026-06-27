"""Build and run the devflow workflow graph.

Two interchangeable backends share the exact same node functions and routing logic:

* **LangGraph backend** (used automatically if ``langgraph`` is importable): a real
  ``StateGraph`` compiled with a ``MemorySaver`` checkpointer and native ``interrupt()`` pauses.
* **Fallback backend** (pure stdlib, the default in this repo since langgraph is an optional dev
  dependency): a tiny deterministic runner that walks the same node/edge map, merges state with
  the schema's reducers, and pauses by catching :class:`DevflowInterrupt`.

Everything is dry-run; see module docstrings in :mod:`devflow`.
"""

from __future__ import annotations

import operator
from typing import Callable, Optional

from devflow import nodes as N
from devflow._compat import HAS_LANGGRAPH, DevflowInterrupt
from devflow.state import (
    DevflowState, APPROVED, GATE_ADVISORY, GATE_FIX, GATE_MERGE, append_keys,
)

END = "__end__"

# ---- node registry (name -> function) ----
NODE_FUNCS: dict[str, Callable] = {
    "check_environment": N.check_environment,
    "create_advisory_issue": N.create_advisory_issue,
    "request_codex_advisory": N.request_codex_advisory,
    "wait_for_codex_advisory": N.wait_for_codex_advisory,
    "summarize_advisory": N.summarize_advisory,
    # node name must differ from the state key "human_approval" (LangGraph forbids node==channel)
    "human_approval_gate": N.human_approval,
    "apply_approved_changes": N.apply_approved_changes,
    "run_checks": N.run_checks,
    "commit_push_branch": N.commit_push_branch,
    "create_draft_pr": N.create_draft_pr,
    "request_codex_review": N.request_codex_review,
    "wait_for_codex_review": N.wait_for_codex_review,
    "summarize_review": N.summarize_review,
    "human_fix_approval": N.human_fix_approval,
    "fix_blocking_comments": N.fix_blocking_comments,
    "request_codex_rereview": N.request_codex_rereview,
    "merge_readiness": N.merge_readiness,
    "human_merge_approval": N.human_merge_approval,
    "claude_execute_merge": N.claude_execute_merge,
    "post_merge_report": N.post_merge_report,
}

ENTRY = "check_environment"

# ---- static (unconditional) edges ----
LINEAR_NEXT: dict[str, str] = {
    "check_environment": "create_advisory_issue",
    "create_advisory_issue": "request_codex_advisory",
    "request_codex_advisory": "wait_for_codex_advisory",
    "summarize_advisory": "human_approval_gate",
    "apply_approved_changes": "run_checks",
    "run_checks": "commit_push_branch",
    "commit_push_branch": "create_draft_pr",
    "create_draft_pr": "request_codex_review",
    "request_codex_review": "wait_for_codex_review",
    "fix_blocking_comments": "request_codex_rereview",
    "request_codex_rereview": "merge_readiness",
    "claude_execute_merge": "post_merge_report",
    "post_merge_report": END,
}


# ---- conditional routers (state -> next node name) ----
def route_after_advisory_wait(s: DevflowState) -> str:
    return "summarize_advisory" if s.get("codex_advisory_status") == "ready" else "post_merge_report"


def route_after_human_approval(s: DevflowState) -> str:
    return "apply_approved_changes" if s.get("human_approval") == APPROVED else "post_merge_report"


def route_after_review_wait(s: DevflowState) -> str:
    return "summarize_review" if s.get("codex_review_status") == "ready" else "post_merge_report"


def route_after_summarize_review(s: DevflowState) -> str:
    # skip the fix gate entirely when there are no blocking comments
    return "human_fix_approval" if s.get("blocking_comments") else "merge_readiness"


def route_after_fix_approval(s: DevflowState) -> str:
    return "fix_blocking_comments" if s.get("fix_approval") == APPROVED else "post_merge_report"


def route_after_merge_readiness(s: DevflowState) -> str:
    return "human_merge_approval" if s.get("merge_readiness_ready") else "post_merge_report"


def route_after_merge_approval(s: DevflowState) -> str:
    return "claude_execute_merge" if s.get("merge_approval") == APPROVED else "post_merge_report"


ROUTERS: dict[str, Callable[[DevflowState], str]] = {
    "wait_for_codex_advisory": route_after_advisory_wait,
    "human_approval_gate": route_after_human_approval,
    "wait_for_codex_review": route_after_review_wait,
    "summarize_review": route_after_summarize_review,
    "human_fix_approval": route_after_fix_approval,
    "merge_readiness": route_after_merge_readiness,
    "human_merge_approval": route_after_merge_approval,
}

GATE_TO_NODE = {
    GATE_ADVISORY: "human_approval_gate",
    GATE_FIX: "human_fix_approval",
    GATE_MERGE: "human_merge_approval",
}

_APPEND_KEYS = append_keys()


def _merge(state: dict, update: Optional[dict]) -> None:
    """Apply a node's partial update to ``state`` using the schema reducers (append vs overwrite)."""
    if not update:
        return
    for k, v in update.items():
        if k in _APPEND_KEYS and isinstance(state.get(k), list) and isinstance(v, list):
            state[k] = operator.add(state[k], v)
        else:
            state[k] = v


# ======================================================================================
# Fallback backend (pure stdlib)
# ======================================================================================
class FallbackApp:
    """Deterministic stdlib runner with the same nodes/edges as the LangGraph build."""

    backend = "fallback"
    MAX_STEPS = 200  # hard safety bound; this DAG visits far fewer nodes (no infinite loops)

    def invoke(self, state: dict, start_node: Optional[str] = None) -> dict:
        # Force the stdlib interrupt path even if langgraph is installed: this runner only catches
        # DevflowInterrupt, so approval nodes must NOT call langgraph's native interrupt().
        state["_force_fallback"] = True
        if start_node:
            # Resuming: a seeded approval is the resume decision, so clear any forced pause_at —
            # otherwise the gate would re-pause forever instead of consuming the decision.
            state["pause_at"] = None
        cur = start_node or ENTRY
        steps = 0
        while cur != END:
            steps += 1
            if steps > self.MAX_STEPS:
                _merge(state, {"errors": ["exceeded MAX_STEPS"], "status": "stopped"})
                break
            func = NODE_FUNCS[cur]
            try:
                update = func(state)
            except DevflowInterrupt as it:
                state["status"] = "paused"
                state["paused_at_gate"] = it.gate
                state["paused_at_node"] = cur
                state["interrupt_payload"] = it.payload
                _merge(state, {"event_log": [f"[interrupt] paused at gate '{it.gate}' "
                                             f"(resume by seeding approvals['{it.gate}'])."]})
                return state
            _merge(state, update)
            cur = ROUTERS[cur](state) if cur in ROUTERS else LINEAR_NEXT[cur]
        return state


# ======================================================================================
# LangGraph backend (used only if langgraph is installed)
# ======================================================================================
def _build_state_graph():  # pragma: no cover - needs langgraph installed
    """Construct the devflow ``StateGraph`` (nodes + edges) WITHOUT compiling it.

    Shared by the CLI's compiled app and the Studio entry point. Importing this module never calls
    this — langgraph is imported lazily here, so the stdlib fallback path has no dependency on it.
    """
    from langgraph.graph import StateGraph, START, END as LG_END

    g = StateGraph(DevflowState)
    for name, func in NODE_FUNCS.items():
        g.add_node(name, func)
    g.add_edge(START, ENTRY)
    for src, dst in LINEAR_NEXT.items():
        g.add_edge(src, LG_END if dst == END else dst)
    for src, router in ROUTERS.items():
        # map router outputs to themselves; END handled via post_merge_report -> LG_END edge
        targets = sorted({router_target for router_target in _router_targets(src)})
        g.add_conditional_edges(src, router, {t: t for t in targets})
    return g


def _build_langgraph_app():  # pragma: no cover - exercised only when langgraph is installed
    """Compile the StateGraph for the CLI's own use (with an in-process MemorySaver)."""
    from langgraph.checkpoint.memory import MemorySaver

    return _build_state_graph().compile(checkpointer=MemorySaver())


def _router_targets(src: str) -> list[str]:
    # the set of nodes a router can return (kept explicit for the conditional-edge map)
    return {
        "wait_for_codex_advisory": ["summarize_advisory", "post_merge_report"],
        "human_approval_gate": ["apply_approved_changes", "post_merge_report"],
        "wait_for_codex_review": ["summarize_review", "post_merge_report"],
        "summarize_review": ["human_fix_approval", "merge_readiness"],
        "human_fix_approval": ["fix_blocking_comments", "post_merge_report"],
        "merge_readiness": ["human_merge_approval", "post_merge_report"],
        "human_merge_approval": ["claude_execute_merge", "post_merge_report"],
    }[src]


def build_graph(prefer_fallback: bool = False):
    """Return a runnable workflow app.

    Uses the real LangGraph backend when available unless ``prefer_fallback`` is set; otherwise
    the stdlib fallback. Both expose ``.invoke(state)`` and a ``.backend`` attribute.
    """
    if HAS_LANGGRAPH and not prefer_fallback:  # pragma: no cover
        app = _build_langgraph_app()
        try:
            app.backend = "langgraph"
        except (AttributeError, TypeError):  # compiled graph may disallow ad-hoc attributes
            pass
        return app
    return FallbackApp()


def make_graph():  # pragma: no cover - invoked by langgraph-cli / LangGraph Studio
    """LangGraph Studio / ``langgraph dev`` entry point (declared in ``langgraph.json``).

    Returns the **uncompiled** ``StateGraph`` so the LangGraph platform supplies its own
    persistence — that is what lets Studio drive threads and surface the human-approval gates as
    interrupts. The CLI is unaffected: it keeps using :func:`build_graph` / the stdlib fallback.

    Importing :mod:`devflow.graph` never builds a graph or imports langgraph; only *calling* this
    function does. (A module-level ``graph = build_graph(...)`` was deliberately NOT used: without
    langgraph it would be the stdlib ``FallbackApp`` — not Studio-loadable — and it would build at
    import time. A factory is the idiomatic, safe choice.)
    """
    return _build_state_graph()
