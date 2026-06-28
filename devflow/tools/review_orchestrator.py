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

from devflow.tools.github_cli import is_codex_author, parse_review_packet

# ---- tuning constants (the load-bearing ones from the watcher; vestigial ones intentionally dropped) ----
INFLIGHT_CAP = 3            # max concurrent outstanding "@codex review" requests recommended at once
FORCE_MERGE_ROUNDS = 3      # after this many Codex rounds, a PR with ONLY minor (P3) findings is force-mergeable

STATE_DIR = os.path.join(tempfile.gettempdir(), "devflow_runs")
STATE_PATH = os.path.join(STATE_DIR, "orchestrate_state.json")


def state_path_for_repo(repo: str) -> str:
    """Default tracking-state path NAMESPACED by repo, so a convergence/in-flight pin for one repo can't
    leak to a same-numbered PR in another (forks / copied repos with overlapping PR numbers)."""
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in (repo or "default"))
    return os.path.join(STATE_DIR, f"orchestrate_{safe}.json")

# ---- pure heuristics / parsing ----
# Codex tags each inline finding with a P1/P2/P3 badge; read it so the planner can tell merge-vs-fix
# (and to deny "clean" when the summary body itself carries an explicit badge).
_BADGE_RE = re.compile(r"\bP([123])\b")
_BUGFIX_RE = re.compile(r"\b(fix|bugfix|hotfix|patch|typo|regression|revert)\b|修复|修正", re.I)
_FEATURE_RE = re.compile(r"\b(feat|feature|add|adds|implement|support|introduce)\b|新增|实现|支持", re.I)
_CLEAN_VERDICT_RE = re.compile(
    r"did\s*n'?o?t find any (major )?(issues|problems|concerns)"
    r"|no (major )?(issues|problems|concerns)( found| identified)?\b"
    r"|looks good to me|\blgtm\b", re.I)
_REVIEWED_COMMIT_RE = re.compile(r"reviewed commit:?\**\s*`?([0-9a-f]{7,40})", re.I)
# Our outstanding ask is specifically a REVIEW request ("@codex review", "@codex please review") — a
# different command ("@codex address that feedback") must NOT be read as an in-flight review.
_REVIEW_REQUEST_RE = re.compile(r"@codex\b.{0,30}\breview\b", re.I | re.S)


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
        # (a) a clean verdict as a CONVERSATION COMMENT whose 'Reviewed commit' == head — but ONLY if it
        # is at least as new as the latest review on head, so a STALE clean comment can't override a
        # later review that added findings.
        clean_comment = False
        for c in codex_comments:
            if is_clean_verdict(c.get("body")):
                mm = _REVIEWED_COMMIT_RE.search(c.get("body") or "")
                if (mm and head[:7] == mm.group(1)[:7]
                        and (c.get("created_at") or "") >= latest_real_at):
                    clean_comment = True
        # (b) ...or a review OBJECT on head with no own inline findings (matched by review_id so
        # re-anchored older comments don't count), no severity marker in the body, AND not a
        # CHANGES_REQUESTED state (which is authoritative — never 'clean' even with a neutral body).
        clean_review = False
        if latest_real:
            rid = latest_real.get("id")
            own = [c for c in inline if is_codex_author(c.get("author"))
                   and not is_quota(c.get("body")) and c.get("review_id") == rid]
            findings_on_head = len(own)
            sev = [finding_severity(c.get("body")) for c in own]
            max_severity = min(sev) if sev else None       # 1=P1 (worst) … 3=P3; None = no findings
            # a CHANGES_REQUESTED review stays blocking until a NEWER APPROVED on head clears it — a
            # later COMMENTED review does NOT. So look at the newest STATEFUL review, not just latest_real.
            sf = [r for r in head_real if (r.get("state") or "").upper() in ("CHANGES_REQUESTED", "APPROVED")]
            newest_sf = max(sf, key=lambda r: r.get("created_at") or "") if sf else None
            requested_changes = bool(newest_sf and (newest_sf.get("state") or "").upper() == "CHANGES_REQUESTED")
            # the body must not indicate problems — but a NEGATED phrase ("no blocking issues found") is
            # clean, so use devflow's negation-aware parser instead of a bare keyword match; also reject an
            # explicit P1/P2/P3 badge in the summary body.
            body = latest_real.get("body") or ""
            body_blocking = bool(parse_review_packet(body, latest_real.get("state")).get("blocking"))
            clean_review = (findings_on_head == 0 and not requested_changes
                            and not body_blocking and not _BADGE_RE.search(body))
        clean = clean_comment or clean_review

    # our own outstanding "@codex review" REQUESTS (a non-Codex review/re-review ask, not just any
    # @codex command) -> awaiting iff newer than the latest substantive review on head.
    my_reqs = [c for c in comments if not is_codex_author(c.get("author"))
               and _REVIEW_REQUEST_RE.search(c.get("body") or "")]
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
        "is_draft": bool(meta.get("is_draft")),
        "rounds": rounds, "rounds_on_head": len(head_real), "reviewed_on_head": on_head,
        "review_key": review_key,
        "has_head_review": bool(latest_real), "awaiting": awaiting, "req_age": req_age, "pending": pending,
        "latest_quota": latest_quota, "latest_sig_ts": latest_sig_ts, "responded_after_req": responded,
        "findings_on_head": findings_on_head, "clean": clean, "max_severity": max_severity,
        "priority": prio, "needs": needs,
    }


def force_mergeable(c: dict) -> bool:
    """Rule 2: the CURRENT head has been reviewed >=FORCE_MERGE_ROUNDS times (rounds_on_head, NOT the
    PR's all-time rounds — stale rounds from previous heads must not let a barely-reviewed new revision
    force-merge) AND its latest review's findings are all minor (worst P3)."""
    return (c["findings_on_head"] > 0 and c["max_severity"] is not None
            and c["max_severity"] >= 3 and c.get("rounds_on_head", 0) >= FORCE_MERGE_ROUNDS)


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
    # a request ANSWERED with a quota notice (latest_quota) did NOT get a review -> it must not keep
    # holding a slot, else after the global rate-limit clears the PR is stuck (skipped, never re-asked).
    if c.get("latest_quota"):
        return False
    rh = requested_head.get(str(c["num"]))
    if rh is None:
        return c["awaiting"]
    return c["awaiting"] and rh == c["head"]


def build_plan(classified: list, *, converged: dict, requested_head: dict, done, now: int,
               merged_branches=None, default_branch: str = "main") -> dict:
    """Compute the cross-PR action plan from classified OPEN PRs + tracking state. PURE: recommends,
    never executes. ``requested_head`` is mutated in place to record recommended requests (head-aware
    in-flight across runs); the caller decides whether to persist it. ``merged_branches`` is the set of
    head branches of MERGED PRs (for stacked-child retarget detection); if omitted it is derived from
    any merged entries in ``classified``. ``default_branch`` is the repo's real base (``main`` /
    ``master`` / ``develop``) — a PR is merge-ready only once its base is that branch.
    """
    cls = sorted(classified, key=lambda c: (-c["priority"], c["num"]))
    done = set(done or [])
    base_branch = default_branch or "main"
    if merged_branches is None:
        merged_branches = {c.get("branch") for c in cls if c["merged"] and c.get("branch")}
    else:
        merged_branches = set(merged_branches)

    plan = {
        "ranking": [{"pr": c["num"], "priority": c["priority"], "rounds": c["rounds"],
                     "clean": c["clean"], "state": c["state"]} for c in cls],
        "request_review": [], "mergeable_now": [], "force_mergeable": [], "ready_then_merge": [],
        "needs_conflict": [], "needs_retarget": [], "mergeable_unknown": [], "findings_to_fix": [],
        "in_flight": [], "rate_limited": False,
    }

    # 1) RETARGET (INDEPENDENT of review state): ANY open PR whose base branch's parent PR has merged
    #    must be repointed to the default branch FIRST — else we'd request review / fixes against a
    #    stale base.
    for c in cls:
        if (c["num"] not in done and c["state"] == "OPEN" and c["base_ref"]
                and c["base_ref"] != base_branch and c["base_ref"] in merged_branches):
            plan["needs_retarget"].append(c["num"])
    retarget = set(plan["needs_retarget"])

    # 2) merge-ready PRs (clean / force-mergeable / converged) whose base is already the default branch:
    #    a CONFLICTING one is flagged for the human to resolve (merge the base in, never force-push); a
    #    still-DRAFT one goes to ready_then_merge (un-draft first); otherwise it's mergeable now.
    for c in cls:
        if (c["num"] in done or c["state"] != "OPEN" or c["num"] in retarget
                or c["base_ref"] != base_branch or not ok_to_merge(c, converged)):
            continue
        if c["mergeable"] == "CONFLICTING":
            plan["needs_conflict"].append(c["num"])
        elif c["mergeable"] == "MERGEABLE":
            if c.get("is_draft"):
                plan["ready_then_merge"].append(c["num"])   # a draft must be marked ready before merging
            else:
                plan["mergeable_now"].append(c["num"])
                if force_mergeable(c):
                    plan["force_mergeable"].append(c["num"])
        else:
            # GitHub reports UNKNOWN/None while still COMPUTING mergeability (e.g. right after a push) —
            # surface the ready PR as pending-recheck instead of silently dropping it to NO_ACTION.
            plan["mergeable_unknown"].append(c["num"])

    # 3) fresh findings to fix — but NOT a PR that must retarget first (re-review against main may change
    #    its findings); not merge-ready.
    for c in cls:
        if (c["num"] not in done and c["num"] not in retarget and not ok_to_merge(c, converged)
                and c["has_head_review"] and c["review_key"]):
            plan["findings_to_fix"].append(c["num"])

    # 4) global rate-limit from the single globally-most-recent Codex signal (not per-PR).
    active = [c for c in cls if c["num"] not in done]
    sigs = [c for c in active if c.get("latest_sig_ts")]
    gl = max(sigs, key=lambda c: c["latest_sig_ts"]) if sigs else None
    plan["rate_limited"] = bool(gl and gl["latest_quota"])

    # 5) request review: the SINGLE gate for ALL @codex requests (initial AND re-review-after-fix),
    #    priority-ordered and capped at INFLIGHT_CAP. A post-fix PR is no longer is_inflight (head
    #    advanced past the tracked request), so it re-queues here. Skip PRs that must retarget first.
    # a PR that must RETARGET first holds no review slot — its outstanding request is against a stale
    # base, so it must not crowd unrelated PRs out of the in-flight cap.
    in_flight = [c["num"] for c in active if is_inflight(c, requested_head) and c["num"] not in retarget]
    if not plan["rate_limited"]:
        for c in cls:
            if len(in_flight) >= INFLIGHT_CAP:
                break
            if (c["num"] in done or c["num"] in in_flight or c["num"] in retarget
                    or is_inflight(c, requested_head)
                    or c["needs"] <= 0 or c["reviewed_on_head"] or ok_to_merge(c, converged)):
                continue   # merge-ready / must-retarget PRs never need another review request
            plan["request_review"].append(c["num"])
            in_flight.append(c["num"])
            requested_head[str(c["num"])] = c["head"]   # tool-local: this request is for the current head
    plan["in_flight"] = in_flight
    return plan


def has_actions(plan: dict) -> bool:
    return any(plan.get(k) for k in ("request_review", "mergeable_now", "ready_then_merge",
                                     "needs_conflict", "needs_retarget", "mergeable_unknown",
                                     "findings_to_fix"))


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
    # `os.path.dirname` of a BARE filename ("orch.json") is "" -> makedirs("") raises; fall back to "."
    # so a bare --state-file still works.
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
