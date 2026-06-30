# -*- coding: utf-8 -*-
"""The ONE narrow real-GitHub-write the local Dashboard may perform: post the FIXED comment
``@codex review`` to a PR, behind strong gating.

There is deliberately NO generic comment API here — the body is a module constant, never a parameter,
so no caller can post arbitrary text. This module NEVER merges / marks-ready / retargets / requests
reviewers / closes / deletes / pushes / force-pushes, calls no LLM, and handles no secrets. The actual
mutation goes through the existing guarded :class:`GitHubWriter` (write-shape allow-list + secret scan).
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Optional

from devflow.tools.github_cli import ReadOnlyGitHub, GitHubWriter, GhError
from devflow.tools import review_orchestrator as orch

CODEX_REVIEW_BODY = "@codex review"                    # the ONLY body this module is allowed to post
AUDIT_DIR = os.path.join(".devflow", "actions")        # local tool-state, gitignored
AUDIT_FILE = "dashboard-writes.jsonl"


def confirmation_text(pr_number) -> str:
    """The exact phrase the operator must type to confirm a post — PR-specific."""
    return "POST @codex review to #%s" % pr_number


def _audit(audit_dir: Optional[str], record: dict) -> None:
    """Append one JSON line to the LOCAL dashboard-write audit log. No secrets, no GitHub content dump."""
    d = audit_dir or AUDIT_DIR
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, AUDIT_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def post_codex_review_request(repo: str, pr_number, expected_head_sha: str, confirmation: str, *,
                              live: bool = True, audit_dir: Optional[str] = None,
                              state_file: Optional[str] = None) -> dict:
    """Post EXACTLY ``@codex review`` to ``repo`` PR ``pr_number`` — and NOTHING else.

    Gates (each raises ValueError on failure, so nothing is posted): repo non-empty; PR number a positive
    int; ``expected_head_sha`` present; ``confirmation`` exactly equals :func:`confirmation_text`; the PR's
    CURRENT head (read read-only) still equals ``expected_head_sha`` AND the PR is OPEN. Then the FIXED
    body is posted via the guarded GitHubWriter. On success an audit line is written and the local
    orchestrator ``requested_head[pr]`` is stamped; on failure neither requested_head nor a success audit
    is recorded. Raises GhError on a gh failure during the read."""
    repo = (repo or "").strip()
    if not repo:
        raise ValueError("repo is required")
    try:
        n = int(str(pr_number).strip())
    except (TypeError, ValueError):
        raise ValueError("PR number must be a positive integer")
    if n <= 0:
        raise ValueError("PR number must be a positive integer")
    expected = (expected_head_sha or "").strip()
    if not expected:
        raise ValueError("expected_head_sha is required")
    if (confirmation or "").strip() != confirmation_text(n):
        raise ValueError("confirmation does not match — type exactly: %s" % confirmation_text(n))

    # verify the PR head is unchanged (read-only) so we can't post against a stale plan, and that it's open
    meta = ReadOnlyGitHub(repo).get_pr_meta(n)
    head = (meta.get("head_oid") or "").strip()
    if (meta.get("state") or "").upper() != "OPEN":
        raise ValueError("PR #%s is not OPEN (state=%s) — refusing to request review" % (n, meta.get("state")))
    if not head or head != expected:
        raise ValueError("PR #%s head changed (now %s, expected %s) — refresh Review Queue and retry"
                         % (n, (head[:8] or "?"), expected[:8]))

    # post the FIXED body through the guarded writer (real write only when live=True)
    res = GitHubWriter(repo, live=bool(live)).comment_on_pr(n, CODEX_REVIEW_BODY)
    ok = bool(res.get("executed")) and not res.get("error")

    _audit(audit_dir, {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "action": "post_codex_review", "repo": repo, "pr_number": n, "head_sha": head,
        "body": CODEX_REVIEW_BODY, "result": "success" if ok else "failure", "actor": "dashboard",
    })
    if not ok:
        return {"ok": False, "error": res.get("error") or "post failed", "pr_number": n, "head_sha": head}

    # the Dashboard ACTUALLY requested review here (unlike the read-only Review Queue), so stamp the
    # local orchestrator requested_head — ONLY on success, and never touching converged/done state.
    try:
        path = state_file or orch.state_path_for_repo(repo)
        st = orch.load_state(path)
        st["requested_head"][str(n)] = head
        orch.save_state(st, path)
    except Exception:
        pass                                           # best-effort bookkeeping; the post already succeeded
    return {"ok": True, "pr_number": n, "head_sha": head, "body": CODEX_REVIEW_BODY}
