"""Typed workflow state for the devflow orchestrator.

The state is a ``TypedDict`` so it works directly as a LangGraph state schema. List-valued
fields are annotated with an ``operator.add`` reducer so concurrent / sequential node updates
*append* rather than overwrite — LangGraph honours these reducers natively, and the pure-stdlib
fallback runner in :mod:`devflow.graph` reads the same annotations to merge identically.

Nothing here performs side effects; it only describes the shape of the data that flows through
the graph.
"""

from __future__ import annotations

import operator
from typing import Any, Optional, TypedDict

# `typing.Annotated` only exists on Python 3.9+. The repo's CI still runs the test matrix on 3.8
# with no third-party deps, so importing this module must not require Annotated. When it's absent
# we fall back to a plain ``list`` annotation; the fallback runner uses the explicit append-key set
# below, and the (optional, 3.9+) LangGraph backend reads the Annotated reducers when available.
try:
    from typing import Annotated
    _LIST_ADD = Annotated[list, operator.add]
except ImportError:  # Python 3.8
    _LIST_ADD = list

# ---- approval gate identifiers (one per human-in-the-loop interrupt) ----
GATE_ADVISORY = "advisory_implementation"   # approve implementing the Codex advisory
GATE_FIX = "blocking_fix"                    # approve fixing blocking review comments
GATE_MERGE = "merge"                         # approve the final merge
APPROVAL_GATES = (GATE_ADVISORY, GATE_FIX, GATE_MERGE)

# decision values
APPROVED = "approved"
REJECTED = "rejected"
PENDING = "pending"


class DevflowState(TypedDict, total=False):
    """Everything the workflow needs to carry between nodes.

    Fields with an ``_LIST_ADD`` reducer accumulate across nodes;
    scalar fields are last-write-wins.
    """

    # --- request / identity ---
    task_type: str
    task_text: str
    thread_id: str
    repo: str
    agent_profile: str
    dashboard_start_mode: str
    branch_name: Optional[str]

    # --- GitHub artifacts (populated as simulated values in dry-run) ---
    issue_number: Optional[int]
    issue_url: Optional[str]
    pr_number: Optional[int]
    pr_url: Optional[str]

    # --- Codex interaction status ---
    codex_advisory_status: str   # "" | "requested" | "ready" | "timeout"
    codex_review_status: str     # "" | "requested" | "ready" | "timeout"
    codex_review_baseline_at: str  # latest Codex review timestamp BEFORE the request (fresh-vs-stale poll)
    advisory_packet: Optional[dict]
    review_summary: Optional[dict]

    # --- review findings ---
    blocking_comments: _LIST_ADD
    non_blocking_comments: _LIST_ADD
    deferred_followups: _LIST_ADD

    # --- human decisions ---
    human_approval: Optional[str]   # APPROVED | REJECTED | PENDING
    merge_approval: Optional[str]

    # --- checks / changes ---
    checks_run: _LIST_ADD
    checks_not_run: _LIST_ADD
    files_changed: _LIST_ADD

    # --- diagnostics ---
    errors: _LIST_ADD
    event_log: _LIST_ADD

    # --- control / dry-run plumbing (not GitHub data) ---
    dry_run: bool
    real_github: bool        # False (default) = dry-run writes; True = real gh mutations (opt-in)
    max_polls: int           # bounded wait: max attempts when polling GitHub for Codex responses
    poll_seconds: int        # bounded wait: sleep between polls
    # approvals supplied up front (gate -> APPROVED/REJECTED). Used by the fallback runner and
    # as default resume values; with real LangGraph these come from interrupt()/Command(resume=).
    approvals: dict
    # gate at which to deliberately PAUSE (raise an interrupt) instead of auto-deciding.
    pause_at: Optional[str]
    status: str                     # "running" | "paused" | "stopped" | "done"
    halt_reason: Optional[str]
    final_report: Optional[str]

    # --- control channels that nodes/routers read (declared so the real LangGraph StateGraph
    #     does not drop them; LangGraph filters node updates to known channels) ---
    fix_approval: Optional[str]         # decision at the blocking-fix gate
    merge_readiness_ready: bool         # merge_readiness verdict
    rereview_done: bool                 # a re-review completed after fixes
    rereview_blocking: bool             # re-review still found blocking issues
    _simulate: dict                     # dry-run simulation hooks {advisory, review}
    _force_fallback: bool               # force DevflowInterrupt even if langgraph is installed
    paused_at_gate: Optional[str]
    paused_at_node: Optional[str]
    interrupt_payload: dict
    _sleep_fn: object                   # injectable sleep for bounded polling (tests)
    issue_simulated: bool               # the issue id is a dry-run fake (refuse live resume)
    pr_simulated: bool                  # the PR id is a dry-run fake (refuse live resume)
    branch_pushed: bool                 # the branch was actually pushed (gate real PR creation)


def new_state(task_type: str, thread_id: str, repo: str = "ZeKaiNie/universal-examprep-skill",
              approvals: Optional[dict] = None, pause_at: Optional[str] = None,
              dry_run: bool = True, real_github: bool = False,
              max_polls: int = 6, poll_seconds: int = 30) -> DevflowState:
    """Build a fresh initial state.

    ``real_github=False`` (default) keeps all GitHub writes as logged dry-run no-ops. Setting it True
    (CLI ``--real-github``) lets the create/comment nodes perform real, guarded gh mutations — but
    NEVER merge/delete/force-push, and never past the human-approval gate without approval.
    """
    return DevflowState(
        task_type=task_type,
        thread_id=thread_id,
        repo=repo,
        real_github=bool(real_github),
        max_polls=int(max_polls),
        poll_seconds=int(poll_seconds),
        branch_name=None,
        issue_number=None, issue_url=None, pr_number=None, pr_url=None,
        codex_advisory_status="", codex_review_status="",
        advisory_packet=None, review_summary=None,
        blocking_comments=[], non_blocking_comments=[], deferred_followups=[],
        human_approval=PENDING, merge_approval=PENDING,
        checks_run=[], checks_not_run=[], files_changed=[],
        errors=[], event_log=[],
        dry_run=True,                      # hard-wired: this scaffold never leaves dry-run
        approvals=dict(approvals or {}),
        pause_at=pause_at,
        status="running", halt_reason=None, final_report=None,
    )


# Keys whose values accumulate (append) across node updates. Declared explicitly (rather than
# derived via get_type_hints, which needs Annotated) so this works on Python 3.8 too. Keep in sync
# with the _LIST_ADD-annotated fields above.
_APPEND_KEYS = frozenset({
    "blocking_comments", "non_blocking_comments", "deferred_followups",
    "checks_run", "checks_not_run", "files_changed", "errors", "event_log",
})


def append_keys() -> set:
    return set(_APPEND_KEYS)
