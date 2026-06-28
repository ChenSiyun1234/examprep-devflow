"""Implementation + review phase nodes.

In real mode (``state['real_github']``) the create-PR / comment nodes go through the guarded
``GitHubWriter``; the review-wait node bounded-polls real PR comments/reviews. Everything else
(applying edits, running checks, committing/pushing) remains a dry-run no-op in this PR — there is
NO branch push, NO merge, NO force-push capability here.
"""

from __future__ import annotations

from devflow.state import DevflowState
from devflow.tools.github_cli import GitHubWriter, ReadOnlyGitHub, bounded_poll, GhError


def _writer(state: DevflowState) -> GitHubWriter:
    return GitHubWriter(state["repo"], live=bool(state.get("real_github")))


def apply_approved_changes(state: DevflowState) -> dict:
    """Simulate applying the approved changes. NO files are edited in this scaffold."""
    return {
        "files_changed": ["devflow/<scaffold files>"],
        "event_log": ["[apply_approved_changes] dry-run: would apply approved edits — "
                      "no files actually modified."],
    }


def run_checks(state: DevflowState) -> dict:
    """Dry-run: does NOT execute any checks, so it must not claim any passed."""
    return {
        "checks_not_run": [
            "unit tests (dry-run: not executed)",
            "lint (dry-run: not executed)",
        ],
        "event_log": ["[run_checks] dry-run: checks NOT executed (recorded as not-run)."],
    }


def commit_push_branch(state: DevflowState) -> dict:
    """Pure no-op: this PR has NO branch-push capability (and never force-pushes)."""
    return {"event_log": [f"[commit_push_branch] dry-run: would commit & push "
                          f"'{state.get('branch_name')}' — NOT performed (no push capability)."]}


def create_draft_pr(state: DevflowState) -> dict:
    # In real mode, refuse to open a PR for a branch we never actually pushed: commit_push_branch is
    # a no-op in this PR, so branch_pushed is never set. Opening a PR for a deterministically-named
    # branch that happens to already exist on the remote could submit stale/unrelated contents.
    if state.get("real_github") and not state.get("branch_pushed"):
        return {"errors": ["create_draft_pr: branch was never pushed (no branch provenance) — "
                           "refusing to open a real PR"],
                "event_log": ["[create_draft_pr] stopped: no branch provenance; not opening a real "
                              "PR for an unowned branch."]}
    res = _writer(state).create_draft_pr(
        title=f"[devflow] {state['task_type']}",
        body="Automated draft PR from devflow.",
        base="main",
        head=state.get("branch_name") or "devflow/scaffold",
    )
    upd = {"event_log": [f"[create_draft_pr] {res.get('log', '')}"]}
    if res.get("error"):
        return {**upd, "errors": [f"create_draft_pr: {res['error']}"]}
    upd["pr_number"] = res.get("number")
    upd["pr_url"] = res.get("url")
    upd["pr_simulated"] = bool(res.get("simulated"))   # provenance: refuse live resume on fakes
    return upd


def request_codex_review(state: DevflowState) -> dict:
    pr = state.get("pr_number")
    if not pr:   # PR creation failed/unparsed — stop safely instead of commenting on #0
        return {"codex_review_status": "timeout",
                "errors": ["request_codex_review: no PR number (creation failed) — "
                           "refusing to comment on #0"],
                "event_log": ["[request_codex_review] stopped: no PR number; not commenting on #0."]}
    # baseline: the latest Codex review's IDENTITY (dedupe_key) BEFORE this request, so the poll can tell
    # a FRESH review (a different dedupe_key) from a STALE one merely eclipsed by a later usage-limit
    # notice. dedupe_key (not the second-resolution timestamp) distinguishes a same-second fresh review.
    baseline_key = ""
    if state.get("real_github"):
        try:
            prev = ReadOnlyGitHub(state["repo"]).find_latest_codex_review(pr)
            baseline_key = (prev or {}).get("dedupe_key") or ""
        except GhError:
            baseline_key = ""
    res = _writer(state).comment_on_pr(pr, "@codex review this PR.")
    if res.get("error"):
        # comment failed → request never sent; terminal safe-stop so the wait node doesn't poll
        # for (or consume a pre-existing) review that never answered this request.
        return {"codex_review_status": "timeout",
                "errors": [f"request_codex_review: {res['error']}"],
                "event_log": [f"[request_codex_review] FAILED to post '@codex review' "
                              f"({res['error']}); stopping instead of polling."]}
    return {"codex_review_status": "requested", "codex_review_baseline_at": baseline_key,
            "event_log": [f"[request_codex_review] {res.get('log', '')}"]}


def wait_for_codex_review(state: DevflowState) -> dict:
    """Real mode: bounded poll of PR comments/reviews for a Codex review. Dry-run: simulate.
    ``state['_simulate']['review']`` in {'blocking','clean','timeout'} drives the dry-run branch."""
    if state.get("codex_review_status") == "timeout":   # request already failed/safe-stopped
        return {"event_log": ["[wait_for_codex_review] skipped: review request did not succeed; "
                              "not polling."]}
    if not state.get("pr_number"):   # nothing to poll — don't hit PR #0
        return {"codex_review_status": "timeout",
                "errors": ["wait_for_codex_review: no PR number to poll"],
                "event_log": ["[wait_for_codex_review] stopped: no PR number."]}
    if state.get("real_github"):
        repo = state["repo"]
        pr = state.get("pr_number")
        kwargs = {"sleep_fn": state["_sleep_fn"]} if state.get("_sleep_fn") else {}
        baseline_key = state.get("codex_review_baseline_at") or ""
        try:
            def _poll_real_review():
                rev = ReadOnlyGitHub(repo).find_latest_codex_review(pr)
                # accept ONLY a review with a DIFFERENT identity (dedupe_key) than the baseline captured
                # before THIS request — i.e. a FRESH response (a new review/comment, even in the same
                # second). A genuinely new review counts even if a later usage-limit notice eclipses it;
                # but a STALE older review left as the latest signal because the new request got only a
                # quota notice (same dedupe_key as baseline) keeps polling, so we never proceed on stale
                # feedback.
                if rev and rev.get("has_review", True) and (rev.get("dedupe_key") or "") != baseline_key:
                    return rev
                return None
            poll = bounded_poll(
                _poll_real_review,
                max_attempts=int(state.get("max_polls", 6)),
                sleep_seconds=int(state.get("poll_seconds", 30)),
                **kwargs,
            )
        except GhError as e:
            return {"codex_review_status": "timeout", "errors": [f"wait_for_codex_review: {e}"],
                    "event_log": [f"[wait_for_codex_review] gh error: {e}"]}
        if poll["found"]:
            rev = poll["result"]
            if rev.get("blocking"):
                # a blocking review with no parsed bullets still must trip the fix gate, so
                # synthesize one placeholder entry (the router only checks blocking_comments).
                blocking = [{"note": i} for i in (rev.get("items") or [])] or \
                           [{"note": "blocking review (no itemized comments)"}]
            else:
                blocking = []
            return {"codex_review_status": "ready", "review_summary": rev,
                    "blocking_comments": blocking,
                    "event_log": [f"[wait_for_codex_review] Codex review found after "
                                  f"{poll['attempts']} poll(s); blocking={rev.get('blocking')}."]}
        return {"codex_review_status": "timeout",
                "errors": [f"no Codex review after {poll['attempts']} poll(s)"],
                "event_log": [f"[wait_for_codex_review] TIMEOUT after {poll['attempts']} "
                              f"bounded poll(s)."]}

    sim = (state.get("_simulate") or {}).get("review", "blocking")
    if sim == "timeout":
        return {"codex_review_status": "timeout",
                "errors": ["codex review did not arrive within the (simulated) bound"],
                "event_log": ["[wait_for_codex_review] dry-run: simulated TIMEOUT."]}
    blocking = [] if sim == "clean" else [
        {"path": "devflow/graph.py", "note": "simulated: handle empty advisory packet"},
    ]
    non_blocking = [{"path": "docs/devflow-langgraph.md", "note": "simulated: add a diagram"}]
    deferred = [{"note": "simulated: real GitHub backend in a later PR"}]
    return {
        "codex_review_status": "ready",
        "blocking_comments": blocking,
        "non_blocking_comments": non_blocking,
        "deferred_followups": deferred,
        "event_log": [f"[wait_for_codex_review] dry-run: simulated review "
                      f"({len(blocking)} blocking, {len(non_blocking)} non-blocking)."],
    }


def summarize_review(state: DevflowState) -> dict:
    nb = len(state.get("non_blocking_comments", []))
    b = len(state.get("blocking_comments", []))
    summary = {"blocking": b, "non_blocking": nb,
               "deferred": len(state.get("deferred_followups", []))}
    return {"review_summary": {**(state.get("review_summary") or {}), **summary},
            "event_log": [f"[summarize_review] {b} blocking / {nb} non-blocking comments."]}


def fix_blocking_comments(state: DevflowState) -> dict:
    """Simulate addressing blocking comments (no real edits)."""
    fixed = [c.get("path", c.get("note", "?")) for c in state.get("blocking_comments", [])]
    return {
        "files_changed": [f"fix:{p}" for p in fixed],
        "event_log": [f"[fix_blocking_comments] dry-run: would address {len(fixed)} blocking "
                      f"comment(s) — no files modified."],
    }


def request_codex_rereview(state: DevflowState) -> dict:
    pr = state.get("pr_number")
    if not pr:   # PR creation failed/unparsed — stop safely instead of commenting on #0
        return {"errors": ["request_codex_rereview: no PR number — refusing to comment on #0"],
                "event_log": ["[request_codex_rereview] stopped: no PR number."]}
    res = _writer(state).comment_on_pr(pr, "@codex re-review after fixes.")
    upd = {"event_log": [f"[request_codex_rereview] {res.get('log', '')}"]}
    if res.get("error"):
        upd["errors"] = [f"request_codex_rereview: {res['error']}"]
    if state.get("real_github"):
        # a real re-review result must be polled (a later PR); leave it 'requested' so
        # merge_readiness stays safe and never treats this as a completed clean re-review.
        upd["codex_review_status"] = "rereview_requested"
    else:
        # dry-run: simulate a clean re-review (fixes accepted). Mark earlier findings RESOLVED
        # (outstanding_blocking=0) so the report isn't inconsistent (blocking>0 yet would-merge).
        summary = {**(state.get("review_summary") or {}), "outstanding_blocking": 0,
                   "resolved_by_rereview": True}
        upd.update({"codex_review_status": "ready", "rereview_done": True,
                    "rereview_blocking": False, "review_summary": summary})
    return upd
