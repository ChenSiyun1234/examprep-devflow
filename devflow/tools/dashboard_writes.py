# -*- coding: utf-8 -*-
"""The narrow real-GitHub-writes the local Dashboard may perform, behind strong gating. Exactly THREE:

1. :func:`post_codex_review_request` — post the FIXED comment ``@codex review`` to a request_review PR.
2. :func:`mark_pr_ready_for_review` — mark a ready_then_merge DRAFT PR ready (``gh pr ready``).
3. :func:`retarget_pr_base` — change a needs_retarget PR's BASE branch to the planner's exact target
   (``gh pr edit --base``, base only — never title/body/reviewers/state).

There is deliberately NO generic write API here: the comment body is a module constant (never a
parameter), mark-ready takes no action/flag argument, and retarget only sets ``--base`` to a validated
simple branch name — so no caller can post arbitrary text or run an arbitrary GitHub mutation. None of
the three merges / requests reviewers / closes / deletes / pushes / force-pushes / converts-to-draft,
none calls an LLM, none handles secrets. The actual mutation goes through the existing guarded
:class:`GitHubWriter` (write-shape allow-list + secret scan).

Hardening (Codex reviews on PR #15):
* every write ATTEMPT is audited — including REFUSED ones (``result: "refused"``), so the local trail
  covers rejected attempts, not only executed writes;
* the audit write is BEST-EFFORT and never raises: a filesystem failure must not mask a GitHub write
  that already happened (which would make the operator retry);
* the post critical section is serialized with a process lock, with in-process idempotency, so two
  concurrent submissions can't double-post ``@codex review``;
* the caller passes the CURRENT candidate set (request_review / ready_then_merge) so a stale form can
  only act on a PR the dashboard still lists in that bucket (least authority).
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Iterable, Optional

from devflow.tools.github_cli import ReadOnlyGitHub, GitHubWriter, GhError, is_safe_base_ref
from devflow.tools import review_orchestrator as orch

CODEX_REVIEW_BODY = "@codex review"                    # the ONLY comment body this module may post
POST_ACTION = "post_codex_review"
MARK_READY_ACTION = "mark_ready_for_review"
RETARGET_ACTION = "retarget_pr_base"
AUDIT_DIR = os.path.join(".devflow", "actions")        # local tool-state, gitignored
AUDIT_FILE = "dashboard-writes.jsonl"

# Serialize the whole post critical section (idempotency check -> write -> stamp) across
# ThreadingHTTPServer worker threads, so two concurrent submissions for the same PR can't both reach the
# GitHub write before the first records it (which would emit a DUPLICATE @codex review).
_POST_LOCK = threading.Lock()

# What THIS dashboard process has actually posted: (repo, pr) -> head. Deliberately SEPARATE from the
# orchestrator's shared ``requested_head`` (which build_plan also writes to merely RECOMMEND a request,
# without posting) — keying idempotency off requested_head would make the first real post a silent no-op
# for any PR/head already present in orchestrator state. In-process is sufficient: the concern is a
# rapid double-submit within one session; a deliberate click in a fresh process is a real new request.
_POSTED = {}


def confirmation_text(pr_number) -> str:
    """The exact phrase the operator must type to confirm a ``@codex review`` post — PR-specific."""
    return "POST @codex review to #%s" % pr_number


def ready_confirmation_text(pr_number) -> str:
    """The exact phrase the operator must type to confirm marking a draft PR ready — PR-specific."""
    return "MARK #%s READY" % pr_number


def retarget_confirmation_text(pr_number, target_base) -> str:
    """The exact phrase the operator must type to confirm a base retarget — PR + target specific."""
    return "RETARGET #%s TO %s" % (pr_number, target_base)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _audit_record(action, repo, pr_number, head, result, reason="", body=None, extra=None) -> dict:
    rec = {
        "timestamp": _now_iso(), "action": action, "repo": repo, "pr_number": pr_number,
        "head_sha": head, "result": result, "actor": "dashboard",
    }
    if body is not None:                                # only the @codex review post carries a body
        rec["body"] = body
    if extra:                                           # e.g. retarget's from_base / to_base
        rec.update(extra)
    if reason:
        rec["reason"] = reason
    return rec


def _audit(audit_dir: Optional[str], record: dict) -> None:
    """Append one JSON line to the LOCAL dashboard-write audit log. No secrets, no GitHub content dump.

    BEST-EFFORT: any filesystem failure is swallowed. The audit log is local bookkeeping; it must never
    mask a GitHub write that already happened (raising here would make the handler report failure and the
    operator retry), nor swallow a validation refusal (the caller raises independently of whether this
    line was written)."""
    try:
        d = audit_dir or AUDIT_DIR
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, AUDIT_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _refuse(action, audit_dir, repo, pr_number, head, reason, extra=None) -> None:
    """Audit a REFUSED write attempt (best-effort) then raise ``ValueError(reason)``. Every gate failure
    routes through here so the local trail also records rejected attempts, not only executed writes."""
    _audit(audit_dir, _audit_record(action, repo, pr_number, head, "refused", reason, extra=extra))
    raise ValueError(reason)


def audit_failure(action, repo, pr_number, reason, audit_dir=None) -> None:
    """Record a FAILED write ATTEMPT that didn't reach (or complete) the GitHub write — e.g. a gh read
    error during the candidate recompute or PR-metadata read, BEFORE the write helper's own gates. The
    service layer calls this so the local trail covers gh-error failures too, not only refusals/executed
    writes. Best-effort (never raises); the caller still propagates the original error."""
    _audit(audit_dir, _audit_record(action, repo, pr_number, "", "failure", reason))


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
        _refuse(POST_ACTION, audit_dir, repo, pr_number, "", "repo is required")
    try:
        n = int(str(pr_number).strip())
        if n <= 0:
            raise ValueError
    except (TypeError, ValueError):
        _refuse(POST_ACTION, audit_dir, repo, pr_number, "", "PR number must be a positive integer")
    expected = (expected_head_sha or "").strip()
    if not expected:
        _refuse(POST_ACTION, audit_dir, repo, n, "", "expected_head_sha is required")
    if (confirmation or "") != confirmation_text(n):       # LITERAL, whitespace-sensitive (no .strip())
        _refuse(POST_ACTION, audit_dir, repo, n, "",
                "confirmation does not match — type exactly: %s" % confirmation_text(n))

    # Least authority: only post to a PR the dashboard CURRENTLY lists as a request_review candidate
    # (the caller recomputes the plan server-side). candidates=None means "not checked" (low-level use).
    if candidates is not None:
        try:
            allowed = {int(c) for c in candidates}
        except (TypeError, ValueError):
            allowed = set()
        if n not in allowed:
            _refuse(POST_ACTION, audit_dir, repo, n, "",
                    "PR #%s is not a current request_review candidate — refresh the Review Queue" % n)

    # verify the PR head is unchanged (read-only) so we can't post against a stale plan, and that it's open
    meta = ReadOnlyGitHub(repo).get_pr_meta(n)
    head = (meta.get("head_oid") or "").strip()
    if (meta.get("state") or "").upper() != "OPEN":
        _refuse(POST_ACTION, audit_dir, repo, n, head,
                "PR #%s is not OPEN (state=%s) — refusing to request review" % (n, meta.get("state")))
    if not head or head != expected:
        _refuse(POST_ACTION, audit_dir, repo, n, head,
                "PR #%s head changed (now %s, expected %s) — refresh Review Queue and retry"
                % (n, (head[:8] or "?"), expected[:8]))

    # Serialize the POST itself (not merely the bookkeeping) so two concurrent submissions for the same
    # PR — e.g. a double-click on the threaded server — can't both reach comment_on_pr before the first
    # records its post (which would emit a DUPLICATE @codex review). Idempotent ONLY against THIS
    # dashboard's own prior post (the _POSTED marker), NOT against the orchestrator's shared
    # requested_head — that one is also set by build_plan to merely RECOMMEND a request, so skipping on it
    # would silently drop the first real post. Everything after the post is best-effort and never raises.
    key = (repo, n)
    with _POST_LOCK:
        if _POSTED.get(key) == head:
            _audit(audit_dir, _audit_record(POST_ACTION, repo, n, head, "skipped_duplicate",
                                            "this dashboard already posted at this head",
                                            body=CODEX_REVIEW_BODY))
            return {"ok": True, "pr_number": n, "head_sha": head, "body": CODEX_REVIEW_BODY,
                    "duplicate": True}

        # post the FIXED body through the guarded writer (real write only when live=True)
        res = GitHubWriter(repo, live=bool(live)).comment_on_pr(n, CODEX_REVIEW_BODY)
        ok = bool(res.get("executed")) and not res.get("error")
        _audit(audit_dir, _audit_record(POST_ACTION, repo, n, head, "success" if ok else "failure",
                                        body=CODEX_REVIEW_BODY))
        if not ok:
            return {"ok": False, "error": res.get("error") or "post failed",
                    "pr_number": n, "head_sha": head}

        # record OUR post (in-process idempotency marker), then ALSO stamp the orchestrator's shared
        # requested_head so the read-only planner stops re-recommending it (now TRUE — we really posted).
        _POSTED[key] = head
        try:
            path = state_file or orch.state_path_for_repo(repo)
            st = orch.load_state(path)
            st.setdefault("requested_head", {})[str(n)] = head
            orch.save_state(st, path)
        except Exception:
            pass                                       # best-effort bookkeeping; the post already succeeded
    return {"ok": True, "pr_number": n, "head_sha": head, "body": CODEX_REVIEW_BODY}


def mark_pr_ready_for_review(repo: str, pr_number, expected_head_sha: str, confirmation: str, *,
                             live: bool = True, candidates: Optional[Iterable] = None,
                             audit_dir: Optional[str] = None) -> dict:
    """Mark DRAFT PR ``pr_number`` ready for review — and NOTHING else (no merge / retarget / reviewer /
    close / push / convert-to-draft).

    Gates (each audits a ``refused`` line then raises ValueError, so nothing is written): repo non-empty;
    PR number a positive int; ``expected_head_sha`` present; ``confirmation`` exactly equals
    :func:`ready_confirmation_text` (LITERAL, whitespace-sensitive); if ``candidates`` is supplied the PR
    must be in it (a CURRENT ``ready_then_merge`` candidate recomputed server-side — least authority);
    the PR (read read-only) is OPEN, still a DRAFT, and its CURRENT head still equals ``expected_head_sha``.
    Then ``gh pr ready`` runs via the guarded GitHubWriter. The audit is best-effort and never raises
    after a write that may already have happened. Does NOT touch orchestrator state. Raises GhError on a
    gh failure during the read."""
    repo = (repo or "").strip()
    if not repo:
        _refuse(MARK_READY_ACTION, audit_dir, repo, pr_number, "", "repo is required")
    try:
        n = int(str(pr_number).strip())
        if n <= 0:
            raise ValueError
    except (TypeError, ValueError):
        _refuse(MARK_READY_ACTION, audit_dir, repo, pr_number, "", "PR number must be a positive integer")
    expected = (expected_head_sha or "").strip()
    if not expected:
        _refuse(MARK_READY_ACTION, audit_dir, repo, n, "", "expected_head_sha is required")
    if (confirmation or "") != ready_confirmation_text(n):     # LITERAL, whitespace-sensitive (no .strip())
        _refuse(MARK_READY_ACTION, audit_dir, repo, n, "",
                "confirmation does not match — type exactly: %s" % ready_confirmation_text(n))

    # Least authority: only mark ready a PR the dashboard CURRENTLY lists under ready_then_merge.
    if candidates is not None:
        try:
            allowed = {int(c) for c in candidates}
        except (TypeError, ValueError):
            allowed = set()
        if n not in allowed:
            _refuse(MARK_READY_ACTION, audit_dir, repo, n, "",
                    "PR #%s is not in the current ready_then_merge set — refresh the Review Queue" % n)

    # read-only verification: OPEN, still a DRAFT, head unchanged — before any write.
    meta = ReadOnlyGitHub(repo).get_pr_meta(n)
    head = (meta.get("head_oid") or "").strip()
    if (meta.get("state") or "").upper() != "OPEN":
        _refuse(MARK_READY_ACTION, audit_dir, repo, n, head,
                "PR #%s is not OPEN (state=%s) — refusing to mark ready" % (n, meta.get("state")))
    if not meta.get("is_draft"):
        _refuse(MARK_READY_ACTION, audit_dir, repo, n, head,
                "PR #%s is not a draft (already ready) — nothing to do" % n)
    if not head or head != expected:
        _refuse(MARK_READY_ACTION, audit_dir, repo, n, head,
                "PR #%s head changed (now %s, expected %s) — refresh Review Queue and retry"
                % (n, (head[:8] or "?"), expected[:8]))

    # Serialize the write (shared with the post path) so two concurrent submissions can't race the gh
    # call. Marking ready is naturally idempotent — a second concurrent attempt finds the PR no longer a
    # draft and gh reports a benign failure; there is no harmful duplicate side-effect, so no extra
    # idempotency marker is needed. No orchestrator-state mutation (un-drafting is not a request/merge).
    with _POST_LOCK:
        res = GitHubWriter(repo, live=bool(live)).mark_pr_ready(n)
        ok = bool(res.get("executed")) and not res.get("error")
        _audit(audit_dir, _audit_record(MARK_READY_ACTION, repo, n, head, "success" if ok else "failure"))
        if not ok:
            return {"ok": False, "error": res.get("error") or "mark-ready failed",
                    "pr_number": n, "head_sha": head}
    return {"ok": True, "pr_number": n, "head_sha": head, "action": MARK_READY_ACTION}


def retarget_pr_base(repo: str, pr_number, expected_head_sha: str, expected_current_base: str,
                     target_base: str, confirmation: str, *, live: bool = True,
                     candidates: Optional[Iterable] = None, targets: Optional[dict] = None,
                     audit_dir: Optional[str] = None) -> dict:
    """Change DRAFT/OPEN PR ``pr_number``'s BASE branch to ``target_base`` — and NOTHING else (no merge /
    rebase / push / reviewer / title / body / state change).

    Gates (each audits a ``refused`` line then raises ValueError, so nothing is written): repo non-empty;
    PR number a positive int; ``expected_head_sha`` present; ``expected_current_base`` present;
    ``target_base`` present AND a safe simple branch name; ``confirmation`` exactly equals
    :func:`retarget_confirmation_text` (LITERAL, whitespace-sensitive); if ``candidates`` is supplied the
    PR must be in it (a CURRENT ``needs_retarget`` member — least authority); if ``targets`` is supplied
    the recomputed ``retarget_to[pr]`` must equal ``target_base`` (the operator can't pick a different
    base than the planner computed); the PR (read read-only) is OPEN, its head still equals
    ``expected_head_sha`` AND its base still equals ``expected_current_base``. Then ``gh pr edit --base``
    runs via the guarded GitHubWriter. Audit is best-effort. Does NOT touch orchestrator state (retarget
    changes the diff context — it must not pretend review was requested/completed). Raises GhError on a gh
    failure during the read."""
    repo = (repo or "").strip()
    target = (target_base or "").strip()
    cur_base = (expected_current_base or "").strip()
    ex = {"from_base": cur_base, "to_base": target}        # audit context for every refusal/attempt
    if not repo:
        _refuse(RETARGET_ACTION, audit_dir, repo, pr_number, "", "repo is required", extra=ex)
    try:
        n = int(str(pr_number).strip())
        if n <= 0:
            raise ValueError
    except (TypeError, ValueError):
        _refuse(RETARGET_ACTION, audit_dir, repo, pr_number, "", "PR number must be a positive integer",
                extra=ex)
    expected = (expected_head_sha or "").strip()
    if not expected:
        _refuse(RETARGET_ACTION, audit_dir, repo, n, "", "expected_head_sha is required", extra=ex)
    if not cur_base:
        _refuse(RETARGET_ACTION, audit_dir, repo, n, "", "expected_current_base is required", extra=ex)
    if not target:
        _refuse(RETARGET_ACTION, audit_dir, repo, n, "", "target_base is required", extra=ex)
    if not is_safe_base_ref(target):
        _refuse(RETARGET_ACTION, audit_dir, repo, n, "",
                "target_base %r is not a safe simple branch name" % target, extra=ex)
    if (confirmation or "") != retarget_confirmation_text(n, target):   # LITERAL, whitespace-sensitive
        _refuse(RETARGET_ACTION, audit_dir, repo, n, "",
                "confirmation does not match — type exactly: %s" % retarget_confirmation_text(n, target),
                extra=ex)

    # Least authority: only retarget a PR the dashboard CURRENTLY lists under needs_retarget, and only to
    # the EXACT base the planner computed for it (the operator can't substitute a different target).
    if candidates is not None:
        try:
            allowed = {int(c) for c in candidates}
        except (TypeError, ValueError):
            allowed = set()
        if n not in allowed:
            _refuse(RETARGET_ACTION, audit_dir, repo, n, "",
                    "PR #%s is not in the current needs_retarget set — refresh the Review Queue" % n,
                    extra=ex)
    if targets is not None and (targets.get(str(n)) or "") != target:
        _refuse(RETARGET_ACTION, audit_dir, repo, n, "",
                "target_base %r != the planner's retarget_to for #%s — refresh the Review Queue"
                % (target, n), extra=ex)

    # read-only verification: OPEN, head unchanged, base unchanged — before any write.
    meta = ReadOnlyGitHub(repo).get_pr_meta(n)
    head = (meta.get("head_oid") or "").strip()
    ex["head_sha"] = head
    if (meta.get("state") or "").upper() != "OPEN":
        _refuse(RETARGET_ACTION, audit_dir, repo, n, head,
                "PR #%s is not OPEN (state=%s) — refusing to retarget" % (n, meta.get("state")), extra=ex)
    if not head or head != expected:
        _refuse(RETARGET_ACTION, audit_dir, repo, n, head,
                "PR #%s head changed (now %s, expected %s) — refresh Review Queue and retry"
                % (n, (head[:8] or "?"), expected[:8]), extra=ex)
    cur = (meta.get("base_ref") or "").strip()
    if cur != cur_base:
        _refuse(RETARGET_ACTION, audit_dir, repo, n, head,
                "PR #%s base changed (now %s, expected %s) — refresh Review Queue and retry"
                % (n, cur or "?", cur_base), extra=ex)

    # Serialize the write (shared lock). Retarget only sets --base; no orchestrator-state mutation (the
    # diff context changed, so the planner must be recomputed by the operator — we don't pretend anything).
    with _POST_LOCK:
        res = GitHubWriter(repo, live=bool(live)).retarget_pr_base(n, target)
        ok = bool(res.get("executed")) and not res.get("error")
        _audit(audit_dir, _audit_record(RETARGET_ACTION, repo, n, head, "success" if ok else "failure",
                                        extra=ex))
        if not ok:
            return {"ok": False, "error": res.get("error") or "retarget failed",
                    "pr_number": n, "head_sha": head, "from_base": cur_base, "to_base": target}
    return {"ok": True, "pr_number": n, "head_sha": head, "from_base": cur_base, "to_base": target,
            "action": RETARGET_ACTION}
