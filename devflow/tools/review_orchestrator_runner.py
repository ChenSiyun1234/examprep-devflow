# -*- coding: utf-8 -*-
"""Structured (non-printing) runner for the read-only cross-PR orchestration plan.

Returns the SAME data ``cmd_orchestrate_reviews`` computes, as a plain dict, so both the CLI and the
dashboard consume one source of truth instead of scraping stdout. **READ-ONLY**: it uses
:class:`ReadOnlyGitHub` plus the pure :func:`review_orchestrator.classify` / ``build_plan`` — there is
no code path here that comments, requests reviewers, merges, retargets, pushes, or calls any GitHub
write API.

Local tracking state (head-aware in-flight + converged pins) is READ to compute the plan, and
persisted ONLY when ``persist_state=True``. ``build_plan`` records recommended requests into the
in-memory ``requested_head``; the dashboard passes ``persist_state=False`` so that bookkeeping is
discarded — the dashboard never actually requests reviews, so it must not leave behind in-flight
state that would suppress a later real request.
"""

from __future__ import annotations

import calendar
import time
from typing import Optional

from devflow.tools.github_cli import ReadOnlyGitHub, GhError
from devflow.tools import review_orchestrator as orch


def build_orchestration_result(repo: str, limit: int = 50, state_file: Optional[str] = None,
                               mark_converged: Optional[list] = None,
                               persist_state: bool = False,
                               now: Optional[int] = None) -> dict:
    """Compute the read-only orchestration plan for ``repo``.

    Returns ``{marker, repo, default_branch, open_prs, plan, errors, state_path, rate_limited}`` where
    ``marker`` is ``"ORCHESTRATION_PLAN"`` / ``"NO_ACTION_NEEDED"``, ``open_prs`` is the inspected open
    stack (number/title/branch/base for the UI), ``plan`` is the full :func:`review_orchestrator.build_plan`
    output, and ``errors`` is ``[{"pr": n, "error": msg}]`` for PRs whose read failed (the sweep
    continues). Raises :class:`GhError` on a FATAL gh failure (repo resolve / repo info / pr list /
    merged-head lookup). Performs NO GitHub writes.
    """
    if now is None:
        now = calendar.timegm(time.gmtime())
    gh = ReadOnlyGitHub(repo)
    errors = []
    repo_full = gh.resolve_repo()
    # tracking state is namespaced by repo (a pin can't leak across forks with overlapping PR numbers);
    # an explicit state_file always wins. Read-only unless persist_state.
    state_path = state_file or orch.state_path_for_repo(repo_full)
    state = orch.load_state(state_path)
    default_branch = gh.get_repo_info().get("default_branch") or "main"   # not hardcoded 'main'
    open_prs = gh.list_prs(state="open", limit=max(1, int(limit)))        # bounds the OPEN stack inspected

    # --mark-converged: pin the CURRENT head of the named PR(s) as agent-verified-clean (merge rule 3).
    for num in (mark_converged or []):
        meta = gh.get_pr_meta(num)
        if meta.get("head_oid"):
            state["converged"][str(num)] = meta["head_oid"]

    classified = []
    for p in open_prs:
        num = p.get("number")
        if num is None:
            continue
        try:
            meta = gh.get_pr_meta(num)
            signals = gh.get_pr_codex_signals(num)
        except GhError as e:                         # one PR failing must not abort the whole sweep
            errors.append((num, str(e)))
            continue
        classified.append(orch.classify(meta, signals, converged=state["converged"],
                                         requested_head=state["requested_head"], now=now))

    # detect merged parents by TARGETED lookup of the stack's non-default base branches (an older merged
    # parent can fall outside a --limit window). EXCLUDE a base that is still an OPEN PR's head (a live
    # parent) — its branch may also belong to an older MERGED PR, which must not trigger a spurious retarget.
    open_heads = {p.get("head_ref") for p in open_prs if p.get("head_ref")}
    candidate_bases = {p.get("base_ref") for p in open_prs
                       if p.get("base_ref") and p.get("base_ref") != default_branch
                       and p.get("base_ref") not in open_heads}
    merged_branches = gh.merged_heads(candidate_bases)

    plan = orch.build_plan(classified, merged_branches=merged_branches, converged=state["converged"],
                           requested_head=state["requested_head"], done=state["done"], now=now,
                           default_branch=default_branch)
    if persist_state:                                # tool-local tracking only; NEVER a GitHub write
        orch.save_state(state, state_path)

    return {
        "marker": "ORCHESTRATION_PLAN" if orch.has_actions(plan) else "NO_ACTION_NEEDED",
        "repo": repo_full,
        "default_branch": default_branch,
        "open_prs": open_prs,
        "plan": plan,
        "rate_limited": bool(plan.get("rate_limited")),
        "errors": [{"pr": n, "error": m} for n, m in errors],
        "state_path": state_path,
    }
