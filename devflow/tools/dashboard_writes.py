# -*- coding: utf-8 -*-
"""The ONE narrow real-GitHub-write the local Dashboard may perform: post the FIXED comment
``@codex review`` to a PR, behind strong gating.

There is deliberately NO generic comment API here — the body is a module constant, never a parameter,
so no caller can post arbitrary text. This module NEVER merges / marks-ready / retargets / requests
reviewers / closes / deletes / pushes / force-pushes, calls no LLM, and handles no secrets. The actual
mutation goes through the existing guarded :class:`GitHubWriter` (write-shape allow-list + secret scan).

Hardening (Codex review on PR #15):
* every write ATTEMPT is audited — including REFUSED ones (`result: "refused"`), so the local trail
  covers rejected attempts, not only executed posts;
* the audit write is BEST-EFFORT and never raises: a filesystem failure must not mask a comment that
  was already posted (which would make the operator retry and double-post);
* the ``requested_head`` load/modify/save is serialized with a process lock, so two concurrent
  dashboard posts (``ThreadingHTTPServer``) can't lose each other's bookkeeping;
* the caller passes the CURRENT ``request_review`` candidate set so a stale form can only post to a PR
  the dashboard still recommends requesting review for (least authority).
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Iterable, Optional

from devflow.tools.github_cli import ReadOnlyGitHub, GitHubWriter, GhError
from devflow.tools import review_orchestrator as orch

CODEX_REVIEW_BODY = "@codex review"                    # the ONLY body this module is allowed to post
AUDIT_DIR = os.path.join(".devflow", "actions")        # local tool-state, gitignored
AUDIT_FILE = "dashboard-writes.jsonl"

# Serialize the whole post critical section (idempotency check -> comment_on_pr -> requested_head stamp)
# across ThreadingHTTPServer worker threads, so two concurrent submissions for the same PR can't both
# reach the GitHub write before either stamps requested_head (which would emit a DUPLICATE @codex review).
_POST_LOCK = threading.Lock()


def confirmation_text(pr_number) -> str:
    """The exact phrase the operator must type to confirm a post — PR-specific."""
    return "POST @codex review to #%s" % pr_number


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _audit_record(repo, pr_number, head, result, reason="") -> dict:
    rec = {
        "timestamp": _now_iso(), "action": "post_codex_review", "repo": repo,
        "pr_number": pr_number, "head_sha": head, "body": CODEX_REVIEW_BODY,
        "result": result, "actor": "dashboard",
    }
    if reason:
        rec["reason"] = reason
    return rec


def _audit(audit_dir: Optional[str], record: dict) -> None:
    """Append one JSON line to the LOCAL dashboard-write audit log. No secrets, no GitHub content dump.

    BEST-EFFORT: any filesystem failure is swallowed. The audit log is local bookkeeping; it must never
    mask a GitHub write that already happened (raising here would make the handler report failure and the
    operator retry → a duplicate ``@codex review``), nor swallow a validation refusal (the caller raises
    independently of whether this line was written)."""
    try:
        d = audit_dir or AUDIT_DIR
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, AUDIT_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _refuse(audit_dir, repo, pr_number, head, reason) -> None:
    """Audit a REFUSED write attempt (best-effort) then raise ``ValueError(reason)``. Every gate failure
    routes through here so the local trail also records rejected attempts, not only executed posts."""
    _audit(audit_dir, _audit_record(repo, pr_number, head, "refused", reason))
    raise ValueError(reason)


def post_codex_review_request(repo: str, pr_number, expected_head_sha: str, confirmation: str, *,
                              live: bool = True, candidates: Optional[Iterable] = None,
                              audit_dir: Optional[str] = None,
                              state_file: Optional[str] = None) -> dict:
    """Post EXACTLY ``@codex review`` to ``repo`` PR ``pr_number`` — and NOTHING else.

    Gates (each audits a ``refused`` line then raises ValueError, so nothing is posted): repo non-empty;
    PR number a positive int; ``expected_head_sha`` present; ``confirmation`` exactly equals
    :func:`confirmation_text`; if ``candidates`` is supplied, the PR must be in it (a CURRENT
    ``request_review`` candidate recomputed server-side — least authority); the PR's CURRENT head (read
    read-only) still equals ``expected_head_sha`` AND the PR is OPEN. Then the FIXED body is posted via
    the guarded GitHubWriter. After a successful post the audit + ``requested_head`` stamp are
    best-effort and never raise (so a local-bookkeeping failure can't mask the posted comment). Raises
    GhError on a gh failure during the read."""
    repo = (repo or "").strip()
    if not repo:
        _refuse(audit_dir, repo, pr_number, "", "repo is required")
    try:
        n = int(str(pr_number).strip())
        if n <= 0:
            raise ValueError
    except (TypeError, ValueError):
        _refuse(audit_dir, repo, pr_number, "", "PR number must be a positive integer")
    expected = (expected_head_sha or "").strip()
    if not expected:
        _refuse(audit_dir, repo, n, "", "expected_head_sha is required")
    if (confirmation or "").strip() != confirmation_text(n):
        _refuse(audit_dir, repo, n, "",
                "confirmation does not match — type exactly: %s" % confirmation_text(n))

    # Least authority: only post to a PR the dashboard CURRENTLY lists as a request_review candidate
    # (the caller recomputes the plan server-side). candidates=None means "not checked" (low-level use).
    if candidates is not None:
        try:
            allowed = {int(c) for c in candidates}
        except (TypeError, ValueError):
            allowed = set()
        if n not in allowed:
            _refuse(audit_dir, repo, n, "",
                    "PR #%s is not a current request_review candidate — refresh the Review Queue" % n)

    # verify the PR head is unchanged (read-only) so we can't post against a stale plan, and that it's open
    meta = ReadOnlyGitHub(repo).get_pr_meta(n)
    head = (meta.get("head_oid") or "").strip()
    if (meta.get("state") or "").upper() != "OPEN":
        _refuse(audit_dir, repo, n, head,
                "PR #%s is not OPEN (state=%s) — refusing to request review" % (n, meta.get("state")))
    if not head or head != expected:
        _refuse(audit_dir, repo, n, head,
                "PR #%s head changed (now %s, expected %s) — refresh Review Queue and retry"
                % (n, (head[:8] or "?"), expected[:8]))

    # Serialize the POST itself (not merely the bookkeeping) so two concurrent submissions for the same
    # PR — e.g. a double-click on the threaded server — can't both reach comment_on_pr before
    # requested_head is stamped (which would post a DUPLICATE @codex review and burn Codex review slots).
    # Idempotent: if review was already requested at THIS exact head (by an earlier post or the external
    # watcher), skip the write entirely. Everything after the post is best-effort and never raises.
    path = state_file or orch.state_path_for_repo(repo)
    with _POST_LOCK:
        try:
            st = orch.load_state(path)
        except Exception:
            st = {"requested_head": {}}
        if (st.get("requested_head") or {}).get(str(n)) == head:
            _audit(audit_dir, _audit_record(repo, n, head, "skipped_duplicate",
                                            "review already requested at this head"))
            return {"ok": True, "pr_number": n, "head_sha": head, "body": CODEX_REVIEW_BODY,
                    "duplicate": True}

        # post the FIXED body through the guarded writer (real write only when live=True)
        res = GitHubWriter(repo, live=bool(live)).comment_on_pr(n, CODEX_REVIEW_BODY)
        ok = bool(res.get("executed")) and not res.get("error")
        _audit(audit_dir, _audit_record(repo, n, head, "success" if ok else "failure"))
        if not ok:
            return {"ok": False, "error": res.get("error") or "post failed",
                    "pr_number": n, "head_sha": head}

        # the Dashboard ACTUALLY requested review here — stamp requested_head ONLY on success, merging
        # into existing state (never clobbering other PRs / converged / done).
        try:
            st.setdefault("requested_head", {})[str(n)] = head
            orch.save_state(st, path)
        except Exception:
            pass                                       # best-effort bookkeeping; the post already succeeded
    return {"ok": True, "pr_number": n, "head_sha": head, "body": CODEX_REVIEW_BODY}
