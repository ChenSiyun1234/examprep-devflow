"""LangGraph compatibility shim.

If ``langgraph`` is installed, the orchestrator uses a real ``StateGraph`` with native
``interrupt()`` human-in-the-loop pauses. If it is not installed (the repo is stdlib-only and
declares langgraph only as an *optional dev dependency*), a tiny pure-stdlib runner provides the
same behaviour so the CLI and tests work out of the box.

This module isolates the detection + the interrupt primitive so node modules don't import
langgraph directly (and to avoid import cycles with :mod:`devflow.graph`).
"""

from __future__ import annotations

try:  # optional dev dependency — see devflow/requirements-dev.txt
    from langgraph.types import interrupt as _lg_interrupt  # type: ignore
    HAS_LANGGRAPH = True
except Exception:  # pragma: no cover - exercised only when langgraph is absent
    _lg_interrupt = None
    HAS_LANGGRAPH = False


class DevflowInterrupt(Exception):
    """Raised by the fallback runner to pause the graph at a human-approval gate.

    Carries the gate name and the payload that would be shown to the human reviewer; the
    runner turns this into a ``status="paused"`` result that can be resumed by re-invoking the
    workflow with the decision seeded in ``state["approvals"][gate]``.
    """

    def __init__(self, gate: str, payload: dict):
        self.gate = gate
        self.payload = payload
        super().__init__(f"devflow interrupt at gate '{gate}'")


def request_human_decision(state: dict, gate: str, payload: dict) -> str:
    """Return a human decision for ``gate`` ("approved"/"rejected").

    Resolution order (one interrupt per node invocation — never a loop):
      0. an explicit ``state["pause_at"] == gate`` ALWAYS pauses here (overrides any seeded approval);
      1. a decision pre-seeded in ``state["approvals"][gate]`` (the resume payload / dry-run policy);
      2. otherwise pause: real ``interrupt()`` under LangGraph, or ``DevflowInterrupt`` in fallback.
    """
    def _pause():
        if HAS_LANGGRAPH and not state.get("_force_fallback"):
            return _lg_interrupt(payload)   # resumed via Command(resume="approved"/"rejected")
        raise DevflowInterrupt(gate, payload)

    if state.get("pause_at") == gate:        # explicit pause beats a seeded approval
        return _pause()
    approvals = state.get("approvals") or {}
    if gate in approvals:
        return approvals[gate]
    return _pause()
