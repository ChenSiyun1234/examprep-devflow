"""devflow command-line entry point.

    python -m devflow.cli run --task docs-advisory --thread-id demo-1

Everything is dry-run. By default all three human-approval gates auto-approve so the workflow
runs end-to-end and prints a final report. Use the flags to exercise the human-in-the-loop
behaviour:

    --reject GATE        reject a gate (advisory|fix|merge) -> safe stop
    --pause-at GATE      pause at a gate (interrupt) instead of auto-approving
    --simulate-review X  X in {blocking, clean, timeout}
    --simulate-advisory X X in {ready, timeout}

Resume a paused thread:

    python -m devflow.cli resume --thread-id demo-1 --gate advisory --decision approved
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from devflow.graph import build_graph, GATE_TO_NODE, NODE_FUNCS
from devflow.state import (
    new_state, APPROVED, REJECTED, APPROVAL_GATES,
    GATE_ADVISORY, GATE_FIX, GATE_MERGE,
)
from devflow.tools.github_cli import ReadOnlyGitHub, check_gh_available, GhError
from devflow.tools.packet_writer import (
    build_packet, write_packet, PacketError,
    parse_scope_markdown, build_manual_packet, render_manual_markdown,
)
from devflow.tools.review_priority import score as score_review_priority
from devflow.tools.review_orchestrator_runner import build_orchestration_result
from devflow._compat import HAS_LANGGRAPH

# Windows GBK consoles otherwise mangle the report box-drawing / CJK text.
for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8")
    except Exception:
        pass

_GATE_ALIASES = {"advisory": GATE_ADVISORY, "fix": GATE_FIX, "merge": GATE_MERGE}

# checkpoint dir for cross-invocation resume (tool's own state — NOT a product/GitHub artifact)
CKPT_DIR = os.path.join(tempfile.gettempdir(), "devflow_runs")


def _ckpt_path(thread_id: str) -> str:
    # append a hash of the ORIGINAL id so distinct ids that sanitize to the same name
    # (e.g. "demo/a" vs "demo_a") never collide. Bound the slug so a very long thread-id can't
    # exceed the filesystem's per-component name limit; the hash keeps it unique after truncation.
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in thread_id)[:80]
    digest = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:8]
    return os.path.join(CKPT_DIR, f"{safe}-{digest}.json")


def _save_ckpt(state: dict) -> str:
    os.makedirs(CKPT_DIR, exist_ok=True)
    p = _ckpt_path(state["thread_id"])
    serializable = {k: v for k, v in state.items() if k != "interrupt_payload" or isinstance(v, (dict, list, str, int, float, type(None)))}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    return p


def _load_ckpt(thread_id: str) -> dict:
    with open(_ckpt_path(thread_id), "r", encoding="utf-8") as f:
        return json.load(f)


def _clear_fallback_ckpt(thread_id: str) -> None:
    """Best-effort remove the stdlib-fallback JSON checkpoint for a thread, so a later non-langgraph
    `resume` can't load obsolete state after a langgraph run/resume has completed. Never raises."""
    try:
        os.remove(_ckpt_path(thread_id))
    except OSError:
        pass


def _approvals_from_args(args) -> dict:
    approvals = {g: APPROVED for g in APPROVAL_GATES}
    for g in (args.reject or []):
        approvals[_GATE_ALIASES[g]] = REJECTED
    if args.pause_at:
        approvals.pop(_GATE_ALIASES[args.pause_at], None)  # not seeded -> will pause/interrupt
    return approvals


def _print_outcome(state: dict) -> None:
    if state.get("final_report"):
        print(state["final_report"])
    if state.get("status") == "paused":
        gate = state.get("paused_at_gate")
        print("\n*** PAUSED at human-approval gate: "
              f"{gate} ***")
        print("Interrupt payload:")
        print(json.dumps(state.get("interrupt_payload", {}), ensure_ascii=False, indent=2))
        alias = next((a for a, full in _GATE_ALIASES.items() if full == gate), gate)
        print(f"\nResume with:\n  python -m devflow.cli resume --thread-id "
              f"{state['thread_id']} --gate {alias} --decision approved")


def _invoke(app, state, start_node=None):
    """Invoke either backend. The real LangGraph backend (opt-in) needs a per-thread config with
    a configurable.thread_id for its MemorySaver checkpointer; the stdlib fallback uses start_node."""
    if getattr(app, "backend", "") == "langgraph":
        return app.invoke(state, config={"configurable": {"thread_id": state.get("thread_id", "devflow")}})
    return app.invoke(state, start_node=start_node) if start_node else app.invoke(state)


def cmd_run(args) -> int:
    if args.langgraph:                       # real LangGraph backend: native interrupt/resume
        return _run_langgraph(args)
    # default: fully-supported stdlib backend (JSON-checkpoint pause/resume)
    state = new_state(
        task_type=args.task, thread_id=args.thread_id, repo=args.repo,
        approvals=_approvals_from_args(args),
    )
    if args.simulate_advisory or args.simulate_review:
        state["_simulate"] = {"advisory": args.simulate_advisory or "ready",
                              "review": args.simulate_review or "blocking"}
    app = build_graph(prefer_fallback=True)
    print(f"[devflow] backend={getattr(app, 'backend', '?')}  dry_run=True  "
          f"task={args.task}  thread={args.thread_id}")
    final = app.invoke(state)
    if final.get("status") == "paused":
        _save_ckpt(final)
    else:
        # completed run: clear any stale checkpoint so a later `resume` can't load obsolete state
        try:
            os.remove(_ckpt_path(args.thread_id))
        except OSError:
            pass
    _print_outcome(final)
    return 0


# ====================================================================================
# Real LangGraph backend — native interrupt() pause + Command(resume=...) resume.
# Uses a SQLite checkpointer so a pause in one `run` process can be resumed by a later
# `resume` process (cross-process durability). Dry-run only: no real GitHub writes here.
# ====================================================================================
LG_CKPT = os.path.join(CKPT_DIR, "langgraph-checkpoints.sqlite")
_LG_SQLITE_HINT = (
    "[devflow] the LangGraph backend's durable resume needs the SQLite checkpointer (optional dep).\n"
    '  pip install "langgraph-checkpoint-sqlite"   (or: pip install -e ".[studio]")')


def _langgraph_saver():
    """Return a SqliteSaver context manager (durable, cross-process) or None if not installed."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception:
        return None
    os.makedirs(CKPT_DIR, exist_ok=True)
    return SqliteSaver.from_conn_string(LG_CKPT)


def _lg_pending(app, cfg):
    """(next_nodes, [interrupt payloads]) at the current checkpoint; next_nodes empty == finished."""
    snap = app.get_state(cfg)
    payloads = []
    for task in getattr(snap, "tasks", ()) or ():
        for itr in getattr(task, "interrupts", ()) or ():
            payloads.append(getattr(itr, "value", itr))
    return tuple(snap.next or ()), payloads


def _print_lg_pause(next_nodes, payloads, thread_id):
    print(f"\n*** PAUSED (LangGraph interrupt) at: {', '.join(next_nodes)} ***")
    alias = "advisory"
    if payloads:
        print("Interrupt payload:")
        print(json.dumps(payloads[0], ensure_ascii=False, indent=2))
        gate = (payloads[0] or {}).get("gate")
        alias = next((a for a, full in _GATE_ALIASES.items() if full == gate), "advisory")
    print(f"\nResume with:\n  python -m devflow.cli resume --thread-id {thread_id} "
          f"--gate {alias} --decision approved --langgraph")


def _run_langgraph(args) -> int:
    if not HAS_LANGGRAPH:
        print("[devflow] --langgraph needs langgraph: pip install -r devflow/requirements-dev.txt")
        return 4
    cm = _langgraph_saver()
    if cm is None:
        print(_LG_SQLITE_HINT)
        return 4
    from devflow.graph import _build_state_graph
    # real_github stays False (new_state default): the langgraph pause/resume path is dry-run only.
    state = new_state(task_type=args.task, thread_id=args.thread_id, repo=args.repo,
                      approvals=_approvals_from_args(args))
    if args.simulate_advisory or args.simulate_review:
        state["_simulate"] = {"advisory": args.simulate_advisory or "ready",
                              "review": args.simulate_review or "blocking"}
    cfg = {"configurable": {"thread_id": args.thread_id}}
    # drop any stale stdlib-fallback checkpoint for this thread BEFORE running, so that if the
    # langgraph run pauses, a stray plain `resume` can't load the obsolete fallback state instead of
    # being told to resume with --langgraph.
    _clear_fallback_ckpt(args.thread_id)
    print(f"[devflow] backend=langgraph (sqlite checkpointer)  dry_run=True  "
          f"task={args.task}  thread={args.thread_id}")
    with cm as saver:
        app = _build_state_graph().compile(checkpointer=saver)
        app.invoke(state, cfg)
        nxt, payloads = _lg_pending(app, cfg)
        if nxt:                               # paused at an approval gate
            _print_lg_pause(nxt, payloads, args.thread_id)
            return 0
        _print_outcome(app.get_state(cfg).values)
        _clear_fallback_ckpt(args.thread_id)   # completed -> drop any stale stdlib checkpoint
    return 0


def _resume_langgraph(args) -> int:
    if not HAS_LANGGRAPH:
        print("[devflow] --langgraph needs langgraph: pip install -r devflow/requirements-dev.txt")
        return 4
    cm = _langgraph_saver()
    if cm is None:
        print(_LG_SQLITE_HINT)
        return 4
    from devflow.graph import _build_state_graph
    from langgraph.types import Command
    decision = APPROVED if args.decision == "approved" else REJECTED
    cfg = {"configurable": {"thread_id": args.thread_id}}
    with cm as saver:
        app = _build_state_graph().compile(checkpointer=saver)
        nxt, payloads = _lg_pending(app, cfg)
        if not nxt:
            print(f"[devflow] no paused LangGraph thread '{args.thread_id}'. Start one with:\n"
                  f"  python -m devflow.cli run --task <t> --thread-id {args.thread_id} "
                  f"--langgraph --pause-at <gate>")
            return 1
        # Validate the operator-supplied --gate against where the thread is ACTUALLY paused, so a
        # resume can't approve a different gate than intended (args.gate was previously only logged).
        paused_gate = (payloads[0] or {}).get("gate") if payloads else None
        paused_alias = next((a for a, full in _GATE_ALIASES.items() if full == paused_gate), None)
        if paused_alias and args.gate and args.gate != paused_alias:
            print(f"[devflow] refusing to resume: thread '{args.thread_id}' is paused at the "
                  f"'{paused_alias}' gate, not '{args.gate}'. Re-run with --gate {paused_alias}.")
            return 1
        print(f"[devflow] resume(langgraph) thread={args.thread_id} gate={args.gate} "
              f"decision={decision}")
        app.invoke(Command(resume=decision), cfg)   # native resume; approval is never inferred
        nxt2, payloads2 = _lg_pending(app, cfg)
        if nxt2:                              # paused at the next gate
            _print_lg_pause(nxt2, payloads2, args.thread_id)
            return 0
        _print_outcome(app.get_state(cfg).values)   # rejected -> safe-stop report; approved -> done
        _clear_fallback_ckpt(args.thread_id)        # completed -> drop any stale stdlib checkpoint
    return 0


def cmd_resume(args) -> int:
    if getattr(args, "langgraph", False):    # native LangGraph resume via Command(resume=...)
        return _resume_langgraph(args)
    try:
        state = _load_ckpt(args.thread_id)
    except FileNotFoundError:
        print(f"[devflow] no checkpoint for thread '{args.thread_id}'. Run it first.\n"
              "Note: `resume` supports the stdlib backend only. A run started with --langgraph "
              "pauses via LangGraph's native interrupt (no JSON checkpoint is written) and must be "
              "resumed through LangGraph's own Command(resume=...) — not wired into this CLI yet.")
        return 1
    gate = _GATE_ALIASES[args.gate]
    decision = APPROVED if args.decision == "approved" else REJECTED
    state.setdefault("approvals", {})[gate] = decision
    # Safety: a resume defaults to DRY-RUN even if the original run was --real-github. Live writes
    # must be re-requested explicitly on resume, so they can never silently persist across a pause.
    want_live = bool(getattr(args, "real_github", False))
    if want_live:
        # DEFAULT-DENY: only allow a live resume if provenance EXPLICITLY proves each existing
        # artifact id is real (issue_simulated/pr_simulated == False). A missing flag (e.g. an old
        # checkpoint from before provenance tracking) is treated as simulated → refuse, so we can
        # never live-comment on an unrelated real issue/PR that happens to share a fake id.
        issue_unproven = state.get("issue_number") and state.get("issue_simulated", True)
        pr_unproven = state.get("pr_number") and state.get("pr_simulated", True)
        if issue_unproven or pr_unproven:
            print("[devflow] refusing --real-github resume: this thread's issue/PR ids are not "
                  "proven real (simulated or unknown provenance). Re-run the flow with --real-github "
                  "from the start to use real ids.")
            return 1
    state["real_github"] = want_live
    start = state.get("paused_at_node") or GATE_TO_NODE.get(gate)
    if start not in NODE_FUNCS:
        # a checkpoint from before a node rename stores a stale node name -> fall back to the node for
        # the gate the checkpoint ACTUALLY paused at (state['paused_at_gate']), not the operator's
        # --gate (which could be wrong for this thread and resume it at the wrong gate).
        start = GATE_TO_NODE.get(state.get("paused_at_gate")) or GATE_TO_NODE.get(gate)
    state["status"] = "running"
    app = build_graph(prefer_fallback=True)  # resume uses the stdlib runner's start_node support
    print(f"[devflow] resume thread={args.thread_id} gate={args.gate} decision={decision} "
          f"real_github={state['real_github']}")
    final = app.invoke(state, start_node=start)
    if final.get("status") == "paused":
        _save_ckpt(final)
    else:
        try:
            os.remove(_ckpt_path(args.thread_id))
        except OSError:
            pass
    _print_outcome(final)
    return 0


# ---- read-only GitHub commands (no writes) ----
def _require_gh() -> int:
    st = check_gh_available()
    if not st.get("available"):
        print(f"[devflow] {st['error']}")
        return 2
    if not st.get("authenticated"):
        print(f"[devflow] {st['error']}\nRun `gh auth login` first.")
        return 3
    return 0


def cmd_github_check(args) -> int:
    st = check_gh_available()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("authenticated") else 1


def cmd_read_issue(args) -> int:
    rc = _require_gh()
    if rc:
        return rc
    gh = ReadOnlyGitHub(args.repo)
    try:
        comments = gh.get_issue_comments(args.issue)
        advisory = gh.find_latest_codex_advisory(args.issue)
    except GhError as e:
        print(f"[devflow] gh error: {e}")
        return 1
    print(f"[read-issue] #{args.issue} — {len(comments)} comment(s)")
    for c in comments:
        print(f"  - {c['author']} @ {c['created_at']}: {(c['body'] or '')[:100]}")
    if advisory:
        print(f"\nLatest Codex advisory: by {advisory['author']} @ {advisory['created_at']}")
        print(advisory["body"][:800])
    else:
        print("\nLatest Codex advisory: (none found)")
    return 0


def cmd_read_pr(args) -> int:
    rc = _require_gh()
    if rc:
        return rc
    gh = ReadOnlyGitHub(args.repo)
    try:
        comments = gh.get_pr_comments(args.pr)
        reviews = gh.get_pr_reviews(args.pr)
        review = gh.find_latest_codex_review(args.pr)
    except GhError as e:
        print(f"[devflow] gh error: {e}")
        return 1
    print(f"[read-pr] #{args.pr} — {len(comments)} comment(s), {len(reviews)} review(s)")
    for r in reviews:
        print(f"  review: {r['author']} [{r['state']}] @ {r['created_at']}")
    if review and review.get("has_review", True):
        print(f"\nLatest Codex review: by {review['author']} ({review['source']}) "
              f"blocking={review['blocking']} state={review.get('state')}")
        if review["items"]:
            print("  items:")
            for it in review["items"][:20]:
                print(f"   - {it}")
        print(review["body"][:800])
    elif review:   # a quota-only signal (has_review False) is NOT a review — label it as such
        print(f"\nLatest Codex signal: usage-limit notice (no review yet) by {review['author']} "
              f"@ {review.get('created_at')}")
    else:
        print("\nLatest Codex review: (none found)")
    return 0


# ---- read-only Codex review watcher (no writes; dedupes via a LOCAL seen file) ----
# The seen file is the tool's OWN state (same temp dir as run checkpoints). It is NOT a GitHub
# artifact — writing it is not a commit/push/comment and touches nothing on GitHub.
def _codex_seen_path(override=None) -> str:
    return override or os.path.join(CKPT_DIR, "codex_seen.json")


def _load_codex_seen(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):   # missing or corrupt -> start fresh (never crash the watcher)
        return {}
    if not isinstance(data, dict):
        return {}
    # Tolerate PARTIAL corruption / legacy slices too: keep only dict repo-slices whose per-PR
    # entries are themselves dicts, so a present-but-non-dict slice/entry can't crash the use sites.
    # case-fold the top-level repo key (GitHub repos are case-insensitive) so a legacy mixed-case
    # slice written before key-normalization still matches the lowercased lookup, instead of being
    # missed and re-alerting every previously-seen review.
    out = {}
    for repo, slc in data.items():
        if not isinstance(slc, dict):
            continue
        # MERGE (not overwrite) into the lowercased key so a file holding both `Owner/Repo` and
        # `owner/repo` keeps every PR entry instead of the later slice clobbering the earlier one.
        dst = out.setdefault(repo.lower(), {})
        for pr, entry in slc.items():
            if not isinstance(entry, dict):
                continue
            cur = dst.get(pr)
            # on a same-PR collision across case-variant slices, keep the FRESHEST entry: strictly-newer
            # created_at wins; on an equal timestamp prefer the FULLER key (more same-second URLs, e.g.
            # 'T|review,inline' over 'T|review') so a stale partial key can't overwrite a fresher one and
            # re-alert already-seen feedback.
            e_ts, c_ts = (entry.get("created_at") or ""), ((cur or {}).get("created_at") or "")
            e_key, c_key = (entry.get("key") or ""), ((cur or {}).get("key") or "")
            if cur is None or e_ts > c_ts or (e_ts == c_ts and len(e_key) > len(c_key)):
                dst[pr] = entry
    return out


def _save_codex_seen(path: str, data: dict, only_repo: str = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Merge against the LATEST on-disk state so a concurrent writer's OTHER-repo slices aren't lost,
    # and write atomically (temp + os.replace) so a crash mid-write can't truncate the shared file.
    merged = _load_codex_seen(path)
    if only_repo is not None:
        merged[only_repo] = data.get(only_repo, {})    # update only our repo; keep others' fresh state
    else:
        merged.update(data)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cmd_watch_codex_reviews(args) -> int:
    """Read-only: scan OPEN PRs for NEW trusted-Codex reviews/comments, deduped against a local seen
    file. First-line marker, in precedence order: ACTIONABLE_CODEX_REVIEWS (new feedback to act on) >
    CODEX_WATCH_INCOMPLETE (a PR read failed — sweep incomplete, retry; don't trust a clean result) >
    CODEX_QUOTA_LIMITED (only rate-limit notices — a scheduler can back off) > NO_NEW_CODEX_REVIEWS.
    Never edits code, comments, commits, pushes, or merges."""
    rc = _require_gh()
    if rc:
        return rc
    gh = ReadOnlyGitHub(args.repo)
    try:
        repo = gh.resolve_repo()
        prs = gh.list_open_prs(limit=args.limit)
    except GhError as e:
        print(f"[devflow] gh error: {e}")
        return 1

    repo_key = repo.lower()    # GitHub repos are case-insensitive -> one seen slice regardless of --repo casing
    seen_path = _codex_seen_path(args.seen_file)
    seen = _load_codex_seen(seen_path)
    # --reset / --init start THIS repo's slice fresh, but never clobber other repos in the file.
    repo_seen = {} if (args.reset or args.init) else dict(seen.get(repo_key, {}))
    actionable, checked, errors, quota_prs = [], [], [], []

    for pr in prs:
        num = pr.get("number")
        if num is None:
            continue
        checked.append(num)
        try:
            review = gh.find_latest_codex_review(num)   # latest TRUSTED-Codex signal, or None
        except GhError as e:                            # one PR failing must not abort the sweep
            errors.append((num, str(e)))
            continue
        if not review:
            continue
        if review.get("quota_limited"):                 # Codex code review is rate-limited on this PR
            # only flag a quota notice as a back-off signal when it is NEW — a persistent OLD quota comment
            # must not emit CODEX_QUOTA_LIMITED on every run, or a scheduler backing off on it would never
            # return to normal polling after the limit resets.
            qkey = review.get("dedupe_key") or f"{review.get('created_at') or ''}|{review.get('url') or ''}"
            if not args.init and (repo_seen.get(str(num)) or {}).get("quota_key") != qkey:
                quota_prs.append(num)
            repo_seen.setdefault(str(num), {})["quota_key"] = qkey   # record so it isn't re-flagged
        if not review.get("has_review", True):
            continue                                    # a bare quota notice is not an actionable review
        # dedupe key covers ALL Codex signals at the latest timestamp (so a same-second newly-visible
        # comment still counts as new); fall back to created_at|url for older return shapes.
        key = review.get("dedupe_key") or f"{review.get('created_at') or ''}|{review.get('url') or ''}"
        stored_key = (repo_seen.get(str(num)) or {}).get("key")
        # a stored key matching EITHER the current key or the legacy (quota-inclusive) key counts as
        # already-seen, so upgrading the dedupe scheme doesn't re-alert an older review.
        already_seen = stored_key is not None and stored_key in (key, review.get("legacy_dedupe_key"))
        entry = {"key": key, "created_at": review.get("created_at"),
                 "url": review.get("url"), "blocking": review.get("blocking")}
        if args.init:                       # baseline: record current state, do not alert
            repo_seen[str(num)] = entry
        elif not already_seen:
            actionable.append({"pr": num, "title": pr.get("title"), "review": review})
            repo_seen[str(num)] = entry
        elif stored_key != key:
            # seen via the LEGACY key -> MIGRATE the stored key forward to the current dedupe_key. Else the
            # stale legacy key keeps matching and a later real change (e.g. a same-second inline becoming
            # visible) whose new key still equals legacy_dedupe_key would never be reported.
            repo_seen[str(num)] = entry

    seen[repo_key] = repo_seen
    _save_codex_seen(seen_path, seen, only_repo=repo_key)  # local tool state only — no GitHub mutation

    # --init records a baseline and emits NEITHER marker, so the first real run isn't a flood of
    # pre-existing reviews.
    if args.init:
        for e_num, e_msg in errors:           # surface PRs that could NOT be baselined (don't hide them)
            print(f"  ! PR #{e_num}: gh error — NOT baselined: {e_msg}")
        print(f"[watch-codex] baseline recorded for {len(checked) - len(errors)} open PR(s) in {repo}; "
              f"{len(errors)} errored (NOT baselined); no markers emitted (seen={seen_path})")
        return 0

    # Marker FIRST (a bare line) so strict consumers can match it; human details + optional JSON
    # follow. ACTIONABLE/NO_NEW is carried by this string, NOT the exit code (exit stays 0 like
    # read-pr, unless --exit-actionable is requested).
    # Precedence: real feedback to act on > partial-failure (incomplete sweep) > rate-limited (back off)
    # > nothing new. INCOMPLETE outranks QUOTA: a failed PR read may hide actionable feedback, so a
    # consumer must NOT back off for quota when the sweep didn't actually inspect everything.
    marker = ("ACTIONABLE_CODEX_REVIEWS" if actionable
              else "CODEX_WATCH_INCOMPLETE" if errors
              else "CODEX_QUOTA_LIMITED" if quota_prs
              else "NO_NEW_CODEX_REVIEWS")
    print(marker)
    print(f"[watch-codex] repo={repo} open_prs={len(prs)} checked={len(checked)} "
          f"new={len(actionable)} quota_limited={len(quota_prs)} (read-only; seen={seen_path})")
    for e_num, e_msg in errors:
        print(f"  ! PR #{e_num}: gh error: {e_msg}")
    if quota_prs:
        print("Codex code review is rate-limited (usage limits) on PR(s): "
              + ", ".join(f"#{n}" for n in quota_prs) + " — back off and retry later.")
    if actionable:
        print("PRs with new Codex feedback: " + ", ".join(f"#{a['pr']}" for a in actionable))
        for a in actionable:
            r = a["review"]
            print(f"\n=== PR #{a['pr']}: {a['title']} ===")
            print(f"   NEW Codex {r['source']} by {r['author']} @ {r.get('created_at')} "
                  f"blocking={r.get('blocking')}")
            if r.get("url"):
                print(f"   url: {r['url']}")
            for it in (r.get("items") or [])[:20]:
                print(f"   - {it}")
            body = (r.get("body") or "").strip()
            if body:
                print(f"   body: {body[:args.body_chars]}")
    if args.json:
        print(json.dumps({
            "repo": repo, "open_prs": len(prs), "checked": checked,
            "actionable": [{"pr": a["pr"], "title": a["title"], "source": a["review"]["source"],
                            "author": a["review"]["author"],
                            "created_at": a["review"].get("created_at"),
                            "url": a["review"].get("url"),
                            "blocking": a["review"].get("blocking")} for a in actionable],
            "quota_limited": quota_prs,
            "errors": [{"pr": n, "error": m} for n, m in errors],
            "marker": marker,
        }, ensure_ascii=False, indent=2))
    if args.exit_actionable and actionable:
        return 10   # opt-in: a distinct nonzero so shells can branch; default stays 0
    return 0


def cmd_rank_codex_reviews(args) -> int:
    """Read-only: rank PRs by Codex-review PRIORITY (unreviewed + big-feature first; well-reviewed
    or small-bugfix last) and recommend the top-N to request review on next. Deterministic, stdlib
    heuristic (see review_priority.score). Prints RANKED_CODEX_REVIEW_QUEUE + a ranked table + the
    recommended batch; never edits code, comments, commits, pushes, or merges."""
    rc = _require_gh()
    if rc:
        return rc
    gh = ReadOnlyGitHub(args.repo)
    try:
        repo = gh.resolve_repo()
        if args.include_merged:
            # fetch OPEN and MERGED separately so --limit counts ELIGIBLE PRs, not closed-unmerged
            # ones that would consume the limit before filtering (retroactive review of merged work).
            # INTERLEAVE open+merged before the final --limit cap, so merged candidates aren't all
            # crowded out behind the open PRs when there are >= limit open ones.
            opens = gh.list_prs(state="open", limit=args.limit)
            merged = gh.list_prs(state="merged", limit=args.limit)
            prs = []
            for i in range(max(len(opens), len(merged))):
                if i < len(opens):
                    prs.append(opens[i])
                if i < len(merged):
                    prs.append(merged[i])
            prs = prs[:args.limit]
        else:
            prs = gh.list_prs(state="open", limit=args.limit)
    except GhError as e:
        print(f"[devflow] gh error: {e}")
        return 1

    ranked, errors = [], []
    for pr in prs:
        num = pr.get("number")
        if num is None:
            continue
        try:
            cov = gh.codex_review_rounds(num, head=pr.get("head"))
        except GhError as e:                            # a failed lookup must not be ranked as rounds=0
            errors.append((num, str(e)))                # (which mints MAX priority) — surface, don't rank
            continue
        s = score_review_priority(
            additions=pr["additions"], deletions=pr["deletions"], changed_files=pr["changed_files"],
            title=pr["title"], branch=pr["branch"], codex_rounds=cov["rounds"],
            reviewed_on_head=cov["reviewed_on_head"])
        ranked.append({**pr, **s, "reviewed_on_head": cov["reviewed_on_head"],
                       "quota_limited": cov.get("quota_limited", False)})

    # highest priority first; tie-break by need, then PR number (stable + deterministic)
    ranked.sort(key=lambda r: (-r["priority"], -r["needs_review"], r["number"]))
    # don't recommend requesting review on a CURRENTLY rate-limited PR — Codex just said it can't
    # review now; such PRs are still shown in the table but kept out of recommend_next.
    top = [r["number"] for r in ranked if not r.get("quota_limited")][:args.top]

    # ANY coverage-lookup failure makes the ranking INCOMPLETE — a failed (possibly highest-priority) PR
    # is silently absent from recommend_next, so a driver must retry/alert rather than spend slots on a
    # partial batch. Errors take precedence over a non-empty (but incomplete) ranking.
    marker = ("RANK_CODEX_INCOMPLETE" if errors
              else "RANKED_CODEX_REVIEW_QUEUE" if ranked
              else "NO_PRS_TO_RANK")
    print(marker)
    print(f"[rank-codex] repo={repo} ranked={len(ranked)} recommend_next={top} "
          f"(read-only; top={args.top})")
    for r in ranked:
        flag = "  <-- request next" if r["number"] in top else ""
        head_note = " head-reviewed" if r["reviewed_on_head"] else ""
        q_note = " rate-limited" if r.get("quota_limited") else ""
        print(f"  #{r['number']:>3} prio={r['priority']:>3} {r['type']:>7} "
              f"needs={r['needs_review']} impact={r['impact']} rounds={r['codex_rounds']}{head_note}{q_note} "
              f"+{r['additions']}/-{r['deletions']} {r['state']}{flag}  {(r['title'] or '')[:48]}")
    for e_num, e_msg in errors:
        print(f"  ! PR #{e_num}: gh error: {e_msg}")
    if args.json:
        print(json.dumps({
            "repo": repo, "marker": marker, "recommend_next": top,
            "ranked": [{"pr": r["number"], "priority": r["priority"], "type": r["type"],
                        "needs_review": r["needs_review"], "impact": r["impact"],
                        "codex_rounds": r["codex_rounds"], "reviewed_on_head": r["reviewed_on_head"],
                        "quota_limited": r.get("quota_limited", False),
                        "state": r["state"], "additions": r["additions"], "deletions": r["deletions"],
                        "title": r["title"]} for r in ranked],
            "errors": [{"pr": n, "error": m} for n, m in errors],
        }, ensure_ascii=False, indent=2))
    return 0


# ---- read-only cross-PR review ORCHESTRATOR (planner): recommends actions, never mutates GitHub ----
def _print_plan_section(label: str, prs) -> None:
    if prs:
        print(f"  {label}: " + ", ".join(f"#{n}" for n in prs))


def cmd_orchestrate_reviews(args) -> int:
    """Read-only: compute the cross-PR Codex-review orchestration PLAN — a priority ranking plus who to
    request review from (priority-ordered, head-aware, capped at 3 in-flight), which PRs are mergeable /
    force-mergeable (>=3 rounds + only-minor P3) / need a CONFLICT resolved / need a stacked RETARGET to
    main, which still have findings to fix, and whether Codex is rate-limited. Prints ORCHESTRATION_PLAN
    (with the plan) or NO_ACTION_NEEDED.

    RECOMMENDS only: it NEVER comments, merges, deletes, retargets, or pushes — execution stays with the
    human (devflow's confirmation posture). The single side effect is the tool's OWN local tracking file
    (head-aware in-flight + converged pins), which is NOT a GitHub artifact."""
    rc = _require_gh()
    if rc:
        return rc
    # single source of truth: the structured runner computes the SAME plan the dashboard uses (no stdout
    # scraping). persist_state mirrors the old behaviour: persist head-aware in-flight tracking unless --dry.
    try:
        result = build_orchestration_result(
            repo=args.repo, limit=args.limit, state_file=args.state_file,
            mark_converged=args.mark_converged, persist_state=not args.dry)
    except GhError as e:
        print(f"[devflow] gh error: {e}")
        return 1
    repo, plan, default_branch = result["repo"], result["plan"], result["default_branch"]
    open_prs, errors = result["open_prs"], result["errors"]
    actionable = result["marker"] == "ORCHESTRATION_PLAN"
    print(result["marker"])
    print(f"[orchestrate] repo={repo} open_prs={len(open_prs)} "
          f"{'RATE-LIMITED' if plan['rate_limited'] else 'codex-ok'} (read-only; state={result['state_path']})")
    for e in errors:
        print(f"  ! PR #{e['pr']}: gh error: {e['error']}")
    _print_plan_section("REQUEST REVIEW (priority-ordered, <=3 in-flight)", plan["request_review"])
    _print_plan_section("MERGEABLE now (clean / converged)", plan["mergeable_now"])
    _print_plan_section("FORCE-MERGEABLE (>=3 rounds, only-minor P3)", plan["force_mergeable"])
    _print_plan_section("READY then MERGE (un-draft first)", plan["ready_then_merge"])
    _print_plan_section(f"RESOLVE CONFLICT (merge {default_branch} in; never force-push)", plan["needs_conflict"])
    if plan["needs_retarget"]:
        targets = plan.get("retarget_to") or {}
        print("  RETARGET (parent PR merged): " + ", ".join(
            f"#{n}->{targets.get(str(n), default_branch)}" for n in plan["needs_retarget"]))
    _print_plan_section("MERGEABILITY PENDING (GitHub still computing; re-run)", plan["mergeable_unknown"])
    _print_plan_section("FINDINGS to fix (P1/P2 or early P3)", plan["findings_to_fix"])
    if args.json:
        print(json.dumps({"repo": repo, "marker": result["marker"], "plan": plan, "errors": errors},
                         ensure_ascii=False, indent=2))
    if args.exit_actionable and actionable:
        return 10   # opt-in: a distinct nonzero so shells can branch; default stays 0
    return 0


# ---- Implementation Packet export (safe handoff to Claude Code; NO repo edits, NO gh writes) ----
PACKETS_DIR = os.path.join(".devflow", "packets")   # tool-state, gitignored — not tracked product data


def cmd_export_implementation_packet(args) -> int:
    """Export a structured Implementation Packet from a paused thread's checkpoint.

    devflow summarizes + records the human approval; the packet hands the SCOPED work to Claude Code.
    This command is read-only w.r.t. GitHub and the repo: it loads the local checkpoint, builds the
    packet, and writes two local files. It NEVER edits code, runs the workflow, or calls gh.
    """
    try:
        state = _load_ckpt(args.thread_id)
    except (OSError, ValueError):
        print(f"[devflow] no checkpoint for thread '{args.thread_id}'. Pause at a gate first, e.g.:\n"
              f"  python -m devflow.cli run --task <task> --thread-id {args.thread_id} "
              f"--pause-at advisory")
        return 1
    if not isinstance(state, dict):
        # a valid-JSON but non-object checkpoint (e.g. a stale/hand-edited `[]`) — degrade, don't crash
        print(f"[devflow] checkpoint for thread '{args.thread_id}' is not a devflow state object "
              f"(got {type(state).__name__}); re-run a paused workflow to regenerate it.")
        return 1
    # Only export from a thread actually PAUSED at a recognized approval gate. A stale/completed/
    # hand-edited checkpoint (status done/stopped, or no paused_at_gate) must not silently default to
    # the advisory gate and emit an "approved" packet for a non-existent approval boundary.
    if state.get("status") != "paused" or state.get("paused_at_gate") not in _GATE_ALIASES.values():
        print(f"[devflow] thread '{args.thread_id}' is not paused at a recognized approval gate "
              f"(status={state.get('status')!r}, paused_at_gate={state.get('paused_at_gate')!r}); "
              f"pause at a gate first: run ... --pause-at <advisory|fix|merge>.")
        return 1

    # If --gate is given, it must not contradict the gate the thread is actually paused at — otherwise
    # a script/typo could mark fix/merge scope "approved" for a thread that only reached the advisory
    # gate. Trust the checkpoint's paused_at_gate; refuse a conflicting override.
    paused_gate = state.get("paused_at_gate")
    if args.gate:
        gate = _GATE_ALIASES[args.gate]
        if paused_gate and gate != paused_gate:
            alias = next((a for a, full in _GATE_ALIASES.items() if full == paused_gate), paused_gate)
            print(f"[devflow] --gate {args.gate} conflicts with the thread's paused gate "
                  f"'{paused_gate}' (it is paused at '{alias}'). Refusing; omit --gate or pass --gate {alias}.")
            return 1
    else:
        gate = paused_gate or GATE_ADVISORY
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    packet = build_packet(state, gate=gate, decision=args.decision, generated_at=generated_at)
    out_dir = args.out_dir or PACKETS_DIR
    try:
        paths = write_packet(out_dir, args.thread_id, packet)
    except PacketError as e:
        print(f"[devflow] {e}")
        return 1

    m = packet["metadata"]
    rejected = packet["approval"]["decision"] == REJECTED
    print("IMPLEMENTATION_PACKET_EXPORTED")
    print(f"  markdown:  {paths['md_path']}")
    print(f"  json:      {paths['json_path']}")
    print(f"  thread_id: {m.get('thread_id')}")
    print(f"  gate:      {packet['approval']['gate']}  decision: {packet['approval']['decision']}")
    if m.get("issue_number"):
        print(f"  source issue: #{m['issue_number']} {m.get('issue_url') or ''}".rstrip())
    if m.get("pr_number"):
        print(f"  source PR:    #{m['pr_number']} {m.get('pr_url') or ''}".rstrip())
    if rejected:
        print("\nGate REJECTED — nothing to implement; no handoff. The packet records the rejection.")
    else:
        print("\nNext (hand off to Claude Code):")
        print(f"  Implement ONLY the scoped tasks in {paths['md_path']}; run the listed checks; do "
              f"not commit/push/merge; ask before expanding scope.")
    return 0


def cmd_create_implementation_packet(args) -> int:
    """Create an Implementation Packet from a HUMAN-PROVIDED Markdown scope file.

    Unlike ``export-implementation-packet`` (which reads a paused checkpoint), this needs no prior
    advisory — the human supplies a concrete scope directly, so it never produces the generic packet
    a simulated dry-run advisory would. Read-only w.r.t. GitHub + the repo: it reads one local file
    and writes the two packet files. No gh, no workflow, no code edits.
    """
    try:
        with open(args.scope_file, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"[devflow] could not read scope file '{args.scope_file}': {e}\n"
              f"  provide a Markdown scope file (see README: create-implementation-packet).")
        return 1

    scope = parse_scope_markdown(text)
    for h in scope.get("unknown_headings", []):
        print(f"[devflow] note: ignored unrecognized scope section '# {h}' (its lines were dropped)")
    task = args.task or scope.get("task") or "(untitled task)"
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    packet = build_manual_packet(thread_id=args.thread_id, task=task, repo=args.repo,
                                 generated_at=generated_at, scope=scope, scope_file=args.scope_file)
    # Refuse a scope with no concrete WORK: a files-only scope (paths but no approved scope/tasks) is
    # not enough — it would recreate the unusable generic-packet failure mode this command exists to
    # avoid. Require an approved scope or tasks (files alone, or only quarantined items, don't count).
    ii = packet["implementation_instructions"]
    if not (packet["approval"]["approved_scope"] or ii["tasks"]):
        print(f"[devflow] scope file '{args.scope_file}' has no concrete implementation work "
              f"(needs a '# Approved scope' or '# Tasks' section, not just files). Not writing a packet.")
        return 1
    out_dir = args.out_dir or PACKETS_DIR
    try:
        paths = write_packet(out_dir, args.thread_id, packet,
                             markdown=render_manual_markdown(packet))
    except PacketError as e:
        print(f"[devflow] {e}")
        return 1

    print("MANUAL_IMPLEMENTATION_PACKET_CREATED")
    print(f"  markdown:  {paths['md_path']}")
    print(f"  json:      {paths['json_path']}")
    print(f"  thread_id: {args.thread_id}")
    print(f"  task:      {task}")
    print("\nNext (hand off to Claude Code):")
    print(f"  {packet['suggested_prompt']}")
    return 0


def cmd_run_docs_advisory(args) -> int:
    """Advisory flow up to the human-approval gate. Real mode does the issue + @codex writes, then
    bounded-polls for the advisory, summarizes, and PAUSES for approval before any repo edits."""
    if args.real_github:
        rc = _require_gh()
        if rc:
            return rc
        print("[devflow] REAL GitHub mode: will create a real advisory issue and post an '@codex' "
              "comment, then STOP at human approval before any repo edits. No merge, no push.")
    state = new_state(
        task_type=args.task, thread_id=args.thread_id, repo=args.repo,
        approvals={},  # nothing seeded -> the workflow pauses at the advisory-approval gate
        real_github=args.real_github, max_polls=args.max_polls, poll_seconds=args.poll_seconds,
    )
    app = build_graph(prefer_fallback=not args.langgraph)
    print(f"[devflow] run-docs-advisory backend={getattr(app, 'backend', '?')} "
          f"real_github={args.real_github} max_polls={args.max_polls} "
          f"poll_seconds={args.poll_seconds} thread={args.thread_id}")
    final = _invoke(app, state)   # routes the langgraph backend through the per-thread config
    packet = final.get("advisory_packet") or {}
    if packet.get("summary"):
        print(f"\nCodex advisory summary:\n  {packet['summary']}")
    if final.get("status") == "paused":
        _save_ckpt(final)
    _print_outcome(final)
    if final.get("codex_advisory_status") == "timeout":
        print("\n[devflow] Codex advisory TIMED OUT — stopped safely; nothing further was done.")
    return 0


def _nonneg_int(v: str) -> int:
    """argparse type: reject negative integers with a clear message (no silent clamp)."""
    iv = int(v)
    if iv < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {iv})")
    return iv


def _pos_int(v: str) -> int:
    """argparse type: reject integers < 1 (used where 0 is meaningless, e.g. a PR-list limit)."""
    iv = int(v)
    if iv < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 (got {iv})")
    return iv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devflow", description="Dry-run LangGraph devflow orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the dry-run workflow")
    r.add_argument("--task", default="docs-advisory", help="task type (e.g. docs-advisory)")
    r.add_argument("--thread-id", required=True, help="thread id for this run")
    r.add_argument("--repo", default="ZeKaiNie/universal-examprep-skill")
    r.add_argument("--reject", action="append", choices=list(_GATE_ALIASES),
                   help="reject a gate (repeatable) -> safe stop")
    r.add_argument("--pause-at", choices=list(_GATE_ALIASES),
                   help="pause (interrupt) at this gate instead of auto-deciding")
    r.add_argument("--simulate-advisory", choices=["ready", "timeout"])
    r.add_argument("--simulate-review", choices=["blocking", "clean", "timeout"])
    r.add_argument("--langgraph", action="store_true",
                   help="use the EXPERIMENTAL real LangGraph backend (requires `pip install "
                        "langgraph`); default is the fully-supported stdlib backend")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="resume a paused thread with an approval decision")
    rs.add_argument("--thread-id", required=True)
    rs.add_argument("--gate", required=True, choices=list(_GATE_ALIASES))
    rs.add_argument("--decision", required=True, choices=["approved", "rejected"])
    rs.add_argument("--real-github", action="store_true",
                    help="re-enable real gh writes on this resume (default: dry-run, even if the "
                         "original run used --real-github)")
    rs.add_argument("--langgraph", action="store_true",
                    help="resume a thread paused on the real LangGraph backend (Command(resume=...))")
    rs.set_defaults(func=cmd_resume)

    # --- read-only GitHub commands ---
    gc = sub.add_parser("github-check", help="check gh availability + authentication (read-only)")
    gc.set_defaults(func=cmd_github_check)

    ri = sub.add_parser("read-issue", help="read an issue's comments + latest Codex advisory")
    ri.add_argument("--issue", type=int, required=True)
    ri.add_argument("--repo", default=None, help="owner/name (default: current repo)")
    ri.set_defaults(func=cmd_read_issue)

    rp = sub.add_parser("read-pr", help="read a PR's comments/reviews + latest Codex review")
    rp.add_argument("--pr", type=int, required=True)
    rp.add_argument("--repo", default=None, help="owner/name (default: current repo)")
    rp.set_defaults(func=cmd_read_pr)

    wc = sub.add_parser("watch-codex-reviews",
                        help="read-only: scan open PRs for NEW trusted-Codex reviews/comments (deduped)")
    wc.add_argument("--repo", default=None, help="owner/name (default: current repo)")
    wc.add_argument("--seen-file", default=None,
                    help="local dedupe state file (default: <tmp>/devflow_runs/codex_seen.json)")
    wc.add_argument("--limit", type=_pos_int, default=50,
                    help="max open PRs to inspect (must be >= 1)")
    wc.add_argument("--reset", action="store_true",
                    help="ignore prior seen state for this repo (treat all current feedback as new)")
    wc.add_argument("--init", action="store_true",
                    help="baseline: record current Codex state as seen WITHOUT alerting (avoids a "
                         "first-run flood); emits neither marker")
    wc.add_argument("--json", action="store_true",
                    help="also print a machine-readable JSON summary")
    wc.add_argument("--exit-actionable", action="store_true",
                    help="return exit code 10 (not 0) when there is new Codex feedback")
    wc.add_argument("--body-chars", type=_nonneg_int, default=600,
                    help="truncate each Codex item body preview to N chars (default 600)")
    wc.set_defaults(func=cmd_watch_codex_reviews)

    oc = sub.add_parser("orchestrate-reviews",
                        help="read-only: compute the cross-PR Codex-review PLAN (priority/requests/"
                             "merge-readiness/conflicts) — RECOMMENDS only, never mutates GitHub")
    oc.add_argument("--repo", default=None, help="owner/name (default: current repo)")
    oc.add_argument("--limit", type=_pos_int, default=50,
                    help="max PRs to inspect (must be >= 1)")
    oc.add_argument("--state-file", default=None,
                    help="local tracking state (default: <tmp>/devflow_runs/orchestrate_state.json)")
    oc.add_argument("--mark-converged", type=int, action="append", metavar="PR",
                    help="pin a PR's CURRENT head as agent-verified-clean (merge rule 3); repeatable")
    oc.add_argument("--dry", action="store_true",
                    help="compute the plan WITHOUT persisting the local tracking state")
    oc.add_argument("--json", action="store_true",
                    help="also print a machine-readable JSON plan")
    oc.add_argument("--exit-actionable", action="store_true",
                    help="return exit code 10 (not 0) when the plan recommends any action")
    oc.set_defaults(func=cmd_orchestrate_reviews)

    rk = sub.add_parser("rank-codex-reviews",
                        help="read-only: rank PRs by Codex-review priority (unreviewed + big-feature "
                             "first) and recommend the top-N to request review on next")
    rk.add_argument("--repo", default=None, help="owner/name (default: current repo)")
    rk.add_argument("--limit", type=_pos_int, default=50,
                    help="max PRs to inspect (must be >= 1)")
    rk.add_argument("--top", type=_pos_int, default=3,
                    help="how many top-priority PRs to recommend requesting review on (default 3)")
    rk.add_argument("--include-merged", action="store_true",
                    help="also rank MERGED PRs (e.g. ones merged before review was available) for "
                         "retroactive review, not just open PRs")
    rk.add_argument("--json", action="store_true",
                    help="also print a machine-readable JSON ranking")
    rk.set_defaults(func=cmd_rank_codex_reviews)

    # --- Implementation Packet export (handoff to Claude Code; local files only, no edits/writes) ---
    ep = sub.add_parser("export-implementation-packet",
                        help="export a scoped Implementation Packet for Claude Code from a paused thread")
    ep.add_argument("--thread-id", required=True, help="thread id of a paused (checkpointed) run")
    ep.add_argument("--gate", choices=list(_GATE_ALIASES), default=None,
                    help="gate being approved (default: the thread's paused gate, else advisory)")
    ep.add_argument("--decision", choices=[APPROVED, REJECTED], required=True,
                    help="REQUIRED — the human's decision recorded in the packet (no silent default)")
    ep.add_argument("--out-dir", default=None,
                    help="output base dir (default: .devflow/packets, which is gitignored)")
    ep.set_defaults(func=cmd_export_implementation_packet)

    # --- create a packet from a HUMAN-provided scope file (no advisory/checkpoint needed) ---
    cp = sub.add_parser("create-implementation-packet",
                        help="create an Implementation Packet from a human-provided Markdown scope file")
    cp.add_argument("--thread-id", required=True, help="thread id (names the packet output dir)")
    cp.add_argument("--scope-file", required=True, help="path to a Markdown scope file")
    cp.add_argument("--task", default=None, help="task title (overrides the scope file's '# Task')")
    cp.add_argument("--repo", default="ZeKaiNie/universal-examprep-skill", help="owner/name")
    cp.add_argument("--out-dir", default=None,
                    help="output base dir (default: .devflow/packets, which is gitignored)")
    cp.set_defaults(func=cmd_create_implementation_packet)

    # --- advisory flow up to human approval (dry-run by default; --real-github opts in) ---
    rda = sub.add_parser("run-docs-advisory",
                         help="advisory issue -> @codex -> bounded wait -> summarize -> human approval")
    rda.add_argument("--task", default="docs-advisory")
    rda.add_argument("--thread-id", default="docs-advisory-1")
    rda.add_argument("--repo", default="ZeKaiNie/universal-examprep-skill")
    rda.add_argument("--real-github", action="store_true",
                     help="perform REAL guarded gh writes (issue + @codex comment); default dry-run")
    rda.add_argument("--max-polls", type=_nonneg_int, default=6,
                     help="bounded wait: max poll attempts (0 = do not poll)")
    rda.add_argument("--poll-seconds", type=_nonneg_int, default=30,
                     help="bounded wait: sleep between polls (must be >= 0)")
    rda.add_argument("--langgraph", action="store_true",
                     help="use the EXPERIMENTAL real LangGraph backend (default: stdlib backend)")
    rda.set_defaults(func=cmd_run_docs_advisory)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
