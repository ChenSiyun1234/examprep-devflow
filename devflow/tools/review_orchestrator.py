# -*- coding: utf-8 -*-
"""Cross-PR Codex-review orchestration — the read-only PLANNER.

devflow's workflow graph drives a SINGLE change through human-approval gates. This module is the
complementary, cross-PR concern: given the whole open-PR stack and each PR's Codex review state, it
computes a deterministic *plan* — who to request review from (priority-ordered, behind a small
in-flight cap), which PRs are merge-ready, which need a conflict resolved or a stacked rebase-to-main,
which still have findings to fix, and whether Codex is rate-limited.

It is **strictly advisory / read-only toward GitHub**. It NEVER merges, comments, deletes, or pushes;
it recommends actions for a human (or an external executor) to take, preserving devflow's
human-confirmation posture and its "this scaffold never merges" guarantee. The only side effect is the
tool's OWN local tracking state (head-aware in-flight + converged pins) under the temp dir — not a
GitHub artifact, mirroring ``watch-codex-reviews``' seen-file.

Pure Python stdlib. The decision logic (priority, classify, build_plan, all predicates/regex helpers)
is side-effect-free and unit-testable with fixtures; GitHub reads happen in the CLI via the read-only
``ReadOnlyGitHub`` layer (``get_pr_meta`` / ``get_pr_codex_signals``) and are passed in here as data.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import tempfile
import time
from typing import Optional

from devflow.tools.github_cli import is_codex_author

# ---- tuning constants (the load-bearing ones from the watcher; vestigial ones intentionally dropped) ----
INFLIGHT_CAP = 3            # max concurrent outstanding "@codex review" requests recommended at once
FORCE_MERGE_ROUNDS = 3      # after this many Codex rounds, a PR with ONLY minor (P3) findings is force-mergeable

STATE_DIR = os.path.join(tempfile.gettempdir(), "devflow_runs")
STATE_PATH = os.path.join(STATE_DIR, "orchestrate_state.json")

# ---- pure heuristics / parsing ----
# A severity marker in a REVIEW BODY denies "clean" (P1/P2/P3 or imperative blocking language).
_SEVERITY_RE = re.compile(r"\bP[123]\b|must[-\s]?fix|blocking|should fix|needs? fix|changes? requested", re.I)
# Codex tags each inline finding with a P1/P2/P3 badge; read it so the planner can tell merge-vs-fix.
_BADGE_RE = re.compile(r"\bP([123])\b")
_BUGFIX_RE = re.compile(r"\b(fix|bugfix|hotfix|patch|typo|regression|revert)\b|修复|修正", re.I)
_FEATURE_RE = re.compile(r"\b(feat|feature|add|adds|implement|support|introduce)\b|新增|实现|支持", re.I)
_CLEAN_VERDICT_RE = re.compile(
    r"did\s*n'?o?t find any (major )?(issues|problems|concerns)"
    r"|no (major )?(issues|problems|concerns)( found| identified)?\b"
    r"|looks good to me|\blgtm\b", re.I)
_REVIEWED_COMMIT_RE = re.compile(r"reviewed commit:?\**\s*`?([0-9a-f]{7,40})", re.I)


def parse_ts(s) -> int:
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0


def is_quota(body) -> bool:
    """A Codex usage-limits (rate-limit) notice — NOT a review. Matched on the notice opener so a real
    review that merely mentions usage limits isn't dropped."""
    t = (body or "").lower()
    return "reached your codex usage limits" in t and len(t) < 600


def is_clean_verdict(body) -> bool:
    return bool(_CLEAN_VERDICT_RE.search(body or ""))


def finding_severity(body) -> int:
    m = _BADGE_RE.search(body or "")
    return int(m.group(1)) if m else 3        # an untagged finding is treated as minor (P3)


def priority(meta: dict, rounds: int, reviewed_on_head: bool):
    """Deterministic review-priority score -> (priority 0..100, needs). Unreviewed + big-feature ranks
    high; well-reviewed or small-bugfix ranks low."""
    needs = 10 if rounds == 0 else max(1, 9 - rounds)
    if reviewed_on_head:
        needs = max(0, needs - 4)
    impact = min(10, round((int(meta.get("additions") or 0) + 30 * int(meta.get("changed_files") or 0)) / 120))
    text = f"{meta.get('title') or ''} {meta.get('head_ref') or ''}"
    bug = bool(_BUGFIX_RE.search(text)) and int(meta.get("additions") or 0) < 400
    feat = bool(_FEATURE_RE.search(text))
    adj = -2 if bug else (1 if feat else 0)
    return max(0, min(100, 6 * needs + 4 * impact + adj)), needs


def classify(meta: dict, signals: dict, *, converged: dict, requested_head: dict, now: int) -> dict:
    """Derive a PR's full review state from already-fetched read-only data (no gh here -> pure/testable).

    ``meta`` = ReadOnlyGitHub.get_pr_meta(); ``signals`` = get_pr_codex_signals() ({reviews, inline,
    comments}). Mirrors the watcher's classify(): head-matched rounds, clean-vs-findings with the
    re-anchoring defense (inline matched to its OWN review by review_id), worst P-badge severity,
    head-aware awaiting, quota, and priority.
    """
    n = meta.get("number")
    head = meta.get("head_oid") or ""
    merged = meta.get("state") == "MERGED"
    reviews = [r for r in (signals.get("reviews") or []) if is_codex_author(r.get("author"))]
    comments = signals.get("comments") or []
    inline = signals.get("inline") or []
    codex_comments = [c for c in comments if is_codex_author(c.get("author"))]

    real = [r for r in reviews if not is_quota(r.get("body")) and (r.get("body") or "").strip()]
    rounds = len(real)
    on_head = (rounds > 0) if merged else any((r.get("commit_id") or "") == head for r in real)
    head_real = real if merged else [r for r in real if (r.get("commit_id") or "") == head]
    latest_real = max(head_real, key=lambda r: r.get("created_at") or "") if head_real else None
    latest_real_at = (latest_real or {}).get("created_at", "") if latest_real else ""
    review_key = f"{latest_real_at}|{(latest_real or {}).get('url', '')}" if latest_real else ""

    # CLEAN vs FINDINGS (OPEN PRs only). A merged PR is never 'clean'/auto-merge-able.
    findings_on_head, clean, max_severity = 0, False, None
    if not merged:
        # (a) a clean verdict delivered as a CONVERSATION COMMENT whose 'Reviewed commit' == the head.
        for c in codex_comments:
            if is_clean_verdict(c.get("body")):
                mm = _REVIEWED_COMMIT_RE.search(c.get("body") or "")
                if mm and head[:7] == mm.group(1)[:7]:
                    clean = True
        # (b) ...or a review OBJECT on head: count ITS OWN inline findings (matched by review_id, so
        # re-anchored comments from older reviews don't count) and read their worst severity badge.
        if latest_real:
            rid = latest_real.get("id")
            own = [c for c in inline if is_codex_author(c.get("author"))
                   and not is_quota(c.get("body")) and c.get("review_id") == rid]
            findings_on_head = len(own)
            sev = [finding_severity(c.get("body")) for c in own]
            max_severity = min(sev) if sev else None       # 1=P1 (worst) … 3=P3; None = no findings
            if findings_on_head == 0 and not _SEVERITY_RE.search((latest_real.get("body") or "")):
                clean = True

    # our own outstanding "@codex review" requests (any non-Codex author) -> awaiting iff newer than
    # the latest substantive review on head.
    my_reqs = [c for c in comments if not is_codex_author(c.get("author"))
               and "@codex" in (c.get("body") or "").lower()]
    latest_req_at = max((c.get("created_at") or "" for c in my_reqs), default="")
    awaiting = bool(latest_req_at) and latest_req_at > latest_real_at
    req_age = (now - parse_ts(latest_req_at)) if awaiting else 0
    pending = sum(1 for c in my_reqs if (c.get("created_at") or "") > latest_real_at)

    # latest Codex signal (review OR comment) decides this PR's quota state.
    sigs = ([{"ts": r.get("created_at") or "", "body": r.get("body")} for r in reviews]
            + [{"ts": c.get("created_at") or "", "body": c.get("body")} for c in codex_comments])
    latest_sig = max(sigs, key=lambda s: s["ts"]) if sigs else None
    latest_sig_ts = (latest_sig or {}).get("ts", "")
    latest_quota = is_quota((latest_sig or {}).get("body")) if latest_sig else False

    # responded_after_req: only a review / quota-or-clean comment counts as a real response (a generic
    # Codex comment counts as SILENCE). Reactions (👀) are a watcher-only refinement; omitted here.
    responded = False
    if awaiting:
        if any((r.get("created_at") or "") > latest_req_at for r in reviews):
            responded = True
        elif any((c.get("created_at") or "") > latest_req_at
                 and (is_quota(c.get("body")) or is_clean_verdict(c.get("body"))) for c in codex_comments):
            responded = True

    prio, needs = priority(meta, rounds, on_head)
    return {
        "num": n, "branch": meta.get("head_ref"), "head": head, "merged": merged,
        "state": meta.get("state"), "mergeable": meta.get("mergeable"), "base_ref": meta.get("base_ref"),
        "rounds": rounds, "reviewed_on_head": on_head, "review_key": review_key,
        "has_head_review": bool(latest_real), "awaiting": awaiting, "req_age": req_age, "pending": pending,
        "latest_quota": latest_quota, "latest_sig_ts": latest_sig_ts, "responded_after_req": responded,
        "findings_on_head": findings_on_head, "clean": clean, "max_severity": max_severity,
        "priority": prio, "needs": needs,
    }


def force_mergeable(c: dict) -> bool:
    """Rule 2: ≥FORCE_MERGE_ROUNDS rounds AND the latest review's findings are all minor (worst P3)."""
    return (c["findings_on_head"] > 0 and c["max_severity"] is not None
            and c["max_severity"] >= 3 and c["rounds"] >= FORCE_MERGE_ROUNDS)


def ok_to_merge(c: dict, converged: dict) -> bool:
    """Mergeable-without-the-agent: Codex clean (rule 1) OR multi-round+only-minor (rule 2) OR the
    agent previously pinned THIS exact head as converged (rule 3)."""
    return (c["clean"] or force_mergeable(c)
            or (converged.get(str(c["num"])) == c["head"] and bool(c["head"])))


def is_inflight(c: dict, requested_head: dict) -> bool:
    """Head-aware: a request holds an in-flight slot only while it is FOR THE CURRENT head. Once a fix
    advances the head, the tracked request goes stale -> the PR re-enters the priority gate. Untracked
    (no entry) falls back to the bare 'awaiting' signal so an advisory run without local tracking still
    avoids re-recommending an obviously-outstanding request."""
    rh = requested_head.get(str(c["num"]))
    if rh is None:
        return c["awaiting"]
    return c["awaiting"] and rh == c["head"]


def build_plan(classified: list, *, converged: dict, requested_head: dict, done, now: int,
               merged_branches=None) -> dict:
    """Compute the cross-PR action plan from classified OPEN PRs + tracking state. PURE: recommends,
    never executes. ``requested_head`` is mutated in place to record recommended requests (head-aware
    in-flight across runs); the caller decides whether to persist it. ``merged_branches`` is the set of
    head branches of MERGED PRs (for stacked-child retarget detection); if omitted it is derived from
    any merged entries in ``classified``.
    """
    cls = sorted(classified, key=lambda c: (-c["priority"], c["num"]))
    done = set(done or [])
    if merged_branches is None:
        merged_branches = {c.get("branch") for c in cls if c["merged"] and c.get("branch")}
    else:
        merged_branches = set(merged_branches)

    plan = {
        "ranking": [{"pr": c["num"], "priority": c["priority"], "rounds": c["rounds"],
                     "clean": c["clean"], "state": c["state"]} for c in cls],
        "request_review": [], "mergeable_now": [], "force_mergeable": [], "needs_conflict": [],
        "needs_retarget": [], "findings_to_fix": [], "in_flight": [], "rate_limited": False,
    }

    # 1) merge-ready PRs (recommendations only): retarget a stacked child whose base PR merged; flag a
    #    CONFLICTING merge-ready PR for the human to resolve (merge main in, never force-push); else
    #    it's mergeable now.
    for c in cls:
        if c["num"] in done or c["state"] != "OPEN" or not ok_to_merge(c, converged):
            continue
        base, mergeable = c["base_ref"], c["mergeable"]
        if base and base != "main" and base in merged_branches:
            plan["needs_retarget"].append(c["num"])
        elif mergeable == "CONFLICTING" and base == "main":
            plan["needs_conflict"].append(c["num"])
        elif mergeable == "MERGEABLE" and base == "main":
            plan["mergeable_now"].append(c["num"])
            if force_mergeable(c):
                plan["force_mergeable"].append(c["num"])

    # 2) fresh findings to fix (P1/P2, or early P3) — the human/agent fix path; not merge-ready.
    for c in cls:
        if (c["num"] not in done and not ok_to_merge(c, converged)
                and c["has_head_review"] and c["review_key"]):
            plan["findings_to_fix"].append(c["num"])

    # 3) global rate-limit from the single globally-most-recent Codex signal (not per-PR).
    active = [c for c in cls if c["num"] not in done]
    sigs = [c for c in active if c.get("latest_sig_ts")]
    gl = max(sigs, key=lambda c: c["latest_sig_ts"]) if sigs else None
    plan["rate_limited"] = bool(gl and gl["latest_quota"])

    # 4) request review: the SINGLE gate for ALL @codex requests (initial AND re-review-after-fix),
    #    priority-ordered and capped at INFLIGHT_CAP. A post-fix PR is no longer is_inflight (head
    #    advanced past the tracked request), so it re-queues here.
    in_flight = [c["num"] for c in active if is_inflight(c, requested_head)]
    if not plan["rate_limited"]:
        for c in cls:
            if len(in_flight) >= INFLIGHT_CAP:
                break
            if (c["num"] in done or c["num"] in in_flight or is_inflight(c, requested_head)
                    or c["needs"] <= 0 or c["reviewed_on_head"] or ok_to_merge(c, converged)):
                continue   # a merge-ready PR (clean / converged) never needs another review request
            plan["request_review"].append(c["num"])
            in_flight.append(c["num"])
            requested_head[str(c["num"])] = c["head"]   # tool-local: this request is for the current head
    plan["in_flight"] = in_flight
    return plan


def has_actions(plan: dict) -> bool:
    return any(plan.get(k) for k in ("request_review", "mergeable_now", "needs_conflict",
                                     "needs_retarget", "findings_to_fix"))


# ---- tool-local tracking state (NOT a GitHub artifact: head-aware in-flight + converged pins) ----
def _default_state() -> dict:
    return {"converged": {}, "requested_head": {}, "done": [], "processed": {}, "nudges": {}}


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        st = _default_state()
        if isinstance(data, dict):
            st.update({k: data.get(k, st[k]) for k in st})
        return st
    except (OSError, ValueError):
        return _default_state()


def save_state(state: dict, path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
