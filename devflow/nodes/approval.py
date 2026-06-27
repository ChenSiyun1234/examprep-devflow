"""Human-in-the-loop approval nodes.

Each gate calls :func:`devflow._compat.request_human_decision` exactly ONCE per invocation
(one interrupt per node — never inside a loop). Under real LangGraph this is a native
``interrupt()`` pause; in the stdlib fallback it reads a pre-seeded decision or raises
``DevflowInterrupt`` to pause the run.
"""

from __future__ import annotations

from devflow._compat import request_human_decision
from devflow.state import DevflowState, GATE_ADVISORY, GATE_FIX, GATE_MERGE


def human_approval(state: DevflowState) -> dict:
    """Gate 1: approve implementing the Codex advisory."""
    decision = request_human_decision(state, GATE_ADVISORY, {
        "gate": GATE_ADVISORY,
        "question": "Approve implementing the advisory below?",
        "advisory": (state.get("advisory_packet") or {}).get("summary"),
    })
    return {"human_approval": decision,
            "event_log": [f"[human_approval] advisory implementation: {decision}"]}


def human_fix_approval(state: DevflowState) -> dict:
    """Gate 2: approve fixing the blocking review comments."""
    decision = request_human_decision(state, GATE_FIX, {
        "gate": GATE_FIX,
        "question": "Approve fixing the blocking review comments below?",
        "blocking_comments": state.get("blocking_comments", []),
    })
    # recorded in a dedicated field so routing is unambiguous; human_approval keeps gate-1 value
    return {"fix_approval": decision,
            "event_log": [f"[human_fix_approval] blocking-fix: {decision}"]}


def human_merge_approval(state: DevflowState) -> dict:
    """Gate 3: approve the final merge."""
    decision = request_human_decision(state, GATE_MERGE, {
        "gate": GATE_MERGE,
        "question": "Approve merging this PR?",
        "pr_url": state.get("pr_url"),
        "review_summary": state.get("review_summary"),
    })
    return {"merge_approval": decision,
            "event_log": [f"[human_merge_approval] merge: {decision}"]}
