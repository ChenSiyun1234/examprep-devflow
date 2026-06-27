"""Merge phase nodes. The merge is NEVER executed in this scaffold."""

from __future__ import annotations

from devflow.state import DevflowState, APPROVED, REJECTED


def _derive_halt(state: DevflowState):
    """Infer a human-readable safe-stop reason from the state, if the run did not complete."""
    if state.get("codex_advisory_status") == "timeout":
        return "codex advisory timed out"
    if state.get("human_approval") == REJECTED:
        return "advisory implementation rejected"
    if state.get("fix_approval") == REJECTED:
        return "blocking-fix rejected"
    if state.get("codex_review_status") == "timeout":
        return "codex review timed out"
    if state.get("merge_approval") == REJECTED:
        return "merge rejected"
    if state.get("review_summary") is not None and not state.get("merge_readiness_ready"):
        return "not merge-ready"
    return None


def merge_readiness(state: DevflowState) -> dict:
    """Assess readiness. A (re-)review must have actually COMPLETED — never merge-ready while the
    re-review is only 'requested'. If there were blocking comments, require a completed clean
    re-review (rereview_done and not rereview_blocking); otherwise just require a ready review."""
    review_ok = state.get("codex_review_status") == "ready"
    had_blocking = bool(state.get("blocking_comments"))
    if had_blocking:
        ready = review_ok and bool(state.get("rereview_done")) and not state.get("rereview_blocking")
    else:
        ready = review_ok
    return {
        "merge_readiness_ready": ready,
        "event_log": [f"[merge_readiness] ready={ready} (review_ok={review_ok}, "
                      f"had_blocking={had_blocking}, rereview_done={bool(state.get('rereview_done'))}, "
                      f"rereview_blocking={bool(state.get('rereview_blocking'))})."],
    }


def claude_execute_merge(state: DevflowState) -> dict:
    """No merge capability exists in devflow yet. This is a pure no-op in every mode
    (including real_github): merge execution is deferred to a later, explicitly-approved PR."""
    return {
        "status": "running",
        "event_log": ["[claude_execute_merge] merge NOT executed — no merge capability in this "
                      "PR (deferred to a future human-approved merge PR)."],
    }


def post_merge_report(state: DevflowState) -> dict:
    """Terminal node: build a readable final report covering whatever path was taken
    (completed, or safely stopped at a rejected gate / timeout)."""
    merged = state.get("merge_approval") == APPROVED and bool(state.get("merge_readiness_ready"))
    halt = state.get("halt_reason") or _derive_halt(state)
    # outstanding (unresolved) blocking: a clean re-review resolves the earlier findings, so report
    # 0 outstanding instead of the raw historical count (which would look inconsistent with would-merge)
    raw_blocking = len(state.get("blocking_comments", []))
    rs = state.get("review_summary") or {}
    outstanding = rs["outstanding_blocking"] if "outstanding_blocking" in rs else raw_blocking
    lines = [
        "================ devflow dry-run report ================",
        f"task_type        : {state.get('task_type')}",
        f"thread_id        : {state.get('thread_id')}",
        f"repo             : {state.get('repo')}",
        f"branch_name      : {state.get('branch_name')}",
        f"issue            : #{state.get('issue_number')} {state.get('issue_url')}",
        f"pr               : #{state.get('pr_number')} {state.get('pr_url')}",
        f"advisory_status  : {state.get('codex_advisory_status')}",
        f"review_status    : {state.get('codex_review_status')}",
        f"advisory_approval: {state.get('human_approval')}",
        f"fix_approval     : {state.get('fix_approval')}",
        f"merge_approval   : {state.get('merge_approval')}",
        f"blocking         : {outstanding} outstanding ({raw_blocking} found"
        f"{', resolved by re-review' if state.get('rereview_done') and not outstanding else ''})",
        f"non_blocking     : {len(state.get('non_blocking_comments', []))}",
        f"checks_run       : {state.get('checks_run', [])}",
        f"checks_not_run   : {state.get('checks_not_run', [])}",
        f"files_changed    : {state.get('files_changed', [])}",
        f"errors           : {state.get('errors', [])}",
        f"merge performed  : NO (dry-run scaffold; merges are never executed)",
        f"outcome          : {'stopped: ' + halt if halt else ('would-merge' if merged else 'completed (no merge)')}",
        "-------------------------------------------------------",
        "event log:",
    ]
    lines += [f"  {i+1:2d}. {e}" for i, e in enumerate(state.get("event_log", []))]
    lines.append("=======================================================")
    report = "\n".join(lines)
    return {"final_report": report, "status": "done",
            "event_log": ["[post_merge_report] final report generated."]}
