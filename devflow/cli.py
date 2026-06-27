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

from devflow.graph import build_graph, GATE_TO_NODE
from devflow.state import (
    new_state, APPROVED, REJECTED, APPROVAL_GATES,
    GATE_ADVISORY, GATE_FIX, GATE_MERGE,
)
from devflow.tools.github_cli import ReadOnlyGitHub, check_gh_available, GhError
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
        nxt, _ = _lg_pending(app, cfg)
        if not nxt:
            print(f"[devflow] no paused LangGraph thread '{args.thread_id}'. Start one with:\n"
                  f"  python -m devflow.cli run --task <t> --thread-id {args.thread_id} "
                  f"--langgraph --pause-at <gate>")
            return 1
        print(f"[devflow] resume(langgraph) thread={args.thread_id} gate={args.gate} "
              f"decision={decision}")
        app.invoke(Command(resume=decision), cfg)   # native resume; approval is never inferred
        nxt2, payloads2 = _lg_pending(app, cfg)
        if nxt2:                              # paused at the next gate
            _print_lg_pause(nxt2, payloads2, args.thread_id)
            return 0
        _print_outcome(app.get_state(cfg).values)   # rejected -> safe-stop report; approved -> done
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
    if review:
        print(f"\nLatest Codex review: by {review['author']} ({review['source']}) "
              f"blocking={review['blocking']} state={review.get('state')}")
        if review["items"]:
            print("  items:")
            for it in review["items"][:20]:
                print(f"   - {it}")
        print(review["body"][:800])
    else:
        print("\nLatest Codex review: (none found)")
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
