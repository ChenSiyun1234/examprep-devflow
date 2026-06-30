# -*- coding: utf-8 -*-
"""Service layer for the local DevFlow Dashboard.

Wraps the existing, already-safe devflow operations behind plain functions the dashboard HTTP layer
calls — so the HTTP layer never touches devflow internals directly and every operation is provably
safe in ONE place:

* **runs / checkpoints** are the stdlib-fallback JSON checkpoints :mod:`devflow.cli` already writes
  (``CKPT_DIR``); we only read them or run the workflow that writes them.
* **the workflow is ALWAYS the pure-stdlib DRY-RUN backend** (``build_graph(prefer_fallback=True)``)
  and ``real_github`` is NEVER enabled — so no GitHub mutation can happen from the dashboard, ever.
* **packets** are the same local files ``create``/``export-implementation-packet`` write
  (:func:`devflow.tools.packet_writer.write_packet`).
* **the watcher** reuses :func:`devflow.cli.cmd_watch_codex_reviews` VERBATIM (read-only) and only
  captures its output — no reimplementation, no divergence from the CLI's marker precedence.

Pure stdlib. No shell execution, no network writes, no LangGraph requirement.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import threading
from typing import Optional

from devflow import cli as _cli
from devflow.graph import build_graph, GATE_TO_NODE, NODE_FUNCS
from devflow.state import new_state, APPROVED, REJECTED, APPROVAL_GATES
from devflow.tools.packet_writer import (
    build_packet, build_manual_packet, parse_scope_markdown,
    render_manual_markdown, write_packet, PacketError,  # noqa: F401 (re-exported for the app layer)
)
from devflow.tools.review_orchestrator_runner import build_orchestration_result
from devflow.tools.fallback_review_prompt import (
    build_fallback_review_prompt, FOCUS_MODES, DIFF_BUDGETS,
)
from devflow.tools.codex_review_prompt import build_codex_review_prompt
from devflow.tools import packet_store
from devflow.tools import dashboard_writes
from devflow.tools.dashboard_writes import confirmation_text  # noqa: F401 (re-exported for the app layer)

# First-line markers cmd_watch_codex_reviews can emit (read-only watcher).
WATCH_MARKERS = ("ACTIONABLE_CODEX_REVIEWS", "CODEX_WATCH_INCOMPLETE",
                 "CODEX_QUOTA_LIMITED", "NO_NEW_CODEX_REVIEWS")

# run_watcher captures stdout by swapping the PROCESS-global sys.stdout; the dashboard's
# ThreadingHTTPServer runs requests in threads, so a concurrent create_run/decide_gate (which the
# dry-run nodes print from) would otherwise be captured into the watcher buffer. Serialize EVERY
# stdout-producing service call on this one lock so the watcher only ever captures its own output.
_STDOUT_LOCK = threading.Lock()

# gate alias <-> full gate name, reused from the CLI so the dashboard can't drift from it.
GATE_ALIASES = dict(_cli._GATE_ALIASES)            # {"advisory": "advisory_implementation", ...}
ALIAS_FOR_GATE = {full: alias for alias, full in GATE_ALIASES.items()}

_DEFAULT_REPO = "ZeKaiNie/universal-examprep-skill"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------------------------------
# Runs / checkpoints (read-only)
# ----------------------------------------------------------------------------------------
def list_runs() -> list:
    """List local devflow runs/checkpoints (read-only). Each item:
    ``{thread_id, status, paused_gate, paused_gate_alias, task_type, repo, updated_at}``."""
    runs = []
    ckpt_dir = _cli.CKPT_DIR
    if not os.path.isdir(ckpt_dir):
        return runs
    for name in os.listdir(ckpt_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(ckpt_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue                                  # skip unreadable / non-JSON checkpoints
        # only real devflow checkpoints (always carry thread_id + status) — skips the watcher's
        # codex_seen.json, which also lives in CKPT_DIR but is not a run.
        if not (isinstance(s, dict) and s.get("thread_id") and s.get("status")):
            continue
        try:
            mtime = os.path.getmtime(path)
            updated = (datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
                       .replace(microsecond=0).isoformat())
        except OSError:
            updated = None
        gate = s.get("paused_at_gate")
        runs.append({
            "thread_id": s.get("thread_id") or name[:-5],
            "status": s.get("status"),
            "paused_gate": gate,
            "paused_gate_alias": ALIAS_FOR_GATE.get(gate),
            "task_type": s.get("task_type"),
            "repo": s.get("repo"),
            "updated_at": updated,
        })
    runs.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return runs


def get_run(thread_id: str) -> Optional[dict]:
    """Load a single run's checkpoint state (read-only). None if missing/unreadable/non-object."""
    try:
        s = _cli._load_ckpt(thread_id)
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) else None


# ----------------------------------------------------------------------------------------
# Create / resume a DRY-RUN run (real_github is NEVER enabled here)
# ----------------------------------------------------------------------------------------
def create_run(thread_id: str, task_type: str = "docs-advisory", repo: str = "",
               pause_at: Optional[str] = None) -> dict:
    """Create + run a DRY-RUN devflow run on the pure-stdlib fallback backend. If it pauses at a gate
    the checkpoint is saved; otherwise it ran to completion and any stale checkpoint is cleared.
    ``real_github`` is forced False. Returns the final state. Raises ValueError on bad input."""
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    # thread_id is unique: refuse rather than silently clobber an existing run's paused gate / scope.
    if get_run(thread_id) is not None:
        raise ValueError("a run with thread_id %r already exists — choose a unique id "
                         "(or remove the existing run first)" % (thread_id,))
    pause_gate = None
    if pause_at:
        if pause_at not in GATE_ALIASES:
            raise ValueError("unknown pause_at gate %r" % (pause_at,))
        pause_gate = GATE_ALIASES[pause_at]
    # auto-approve every gate BEFORE the pause gate so the run advances to it; leave the pause gate
    # unseeded so it interrupts there (mirrors cli._approvals_from_args with --pause-at).
    approvals = {g: APPROVED for g in APPROVAL_GATES}
    if pause_gate:
        approvals.pop(pause_gate, None)
    state = new_state(task_type=(task_type or "docs-advisory"), thread_id=thread_id,
                      repo=(repo or "").strip() or _DEFAULT_REPO,
                      approvals=approvals, pause_at=pause_gate)
    state["real_github"] = False                      # belt-and-suspenders: dashboard never writes to GitHub
    app = build_graph(prefer_fallback=True)           # stdlib dry-run backend; never the LangGraph backend
    with _STDOUT_LOCK:                                 # dry-run nodes print -> guard vs the watcher's capture
        final = app.invoke(state)
    _persist(thread_id, final)
    return final


def decide_gate(thread_id: str, gate: str, decision: str) -> dict:
    """Resume a paused run with an approve/reject decision (DRY-RUN; ``real_github`` never enabled).
    ``gate`` is an alias (advisory|fix|merge). Raises ValueError on bad input / wrong state."""
    if gate not in GATE_ALIASES:
        raise ValueError("unknown gate %r" % (gate,))
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved|rejected, got %r" % (decision,))
    state = get_run(thread_id)
    if state is None:
        raise ValueError("no checkpoint for thread %r" % (thread_id,))
    if state.get("status") != "paused":
        raise ValueError("thread %r is not paused (status=%r)" % (thread_id, state.get("status")))
    # the dashboard is DRY-RUN only; resuming a live (--real-github) checkpoint would force it back to
    # dry-run and re-persist over the real provenance, breaking a later `devflow resume --real-github`.
    # Refuse it (the run is still inspectable/exportable, just not resumable from here).
    if state.get("real_github"):
        raise ValueError("run %r was started with --real-github (a live flow); the dashboard is "
                         "dry-run only and will not resume it — use the CLI: "
                         "devflow resume --real-github" % (thread_id,))
    full_gate = GATE_ALIASES[gate]
    paused_gate = state.get("paused_at_gate")
    # FAIL CLOSED: a paused checkpoint with no recorded gate (truncated / hand-edited / foreign) must
    # NOT let an operator-chosen gate be applied — refuse rather than guess a resume node from input.
    if not paused_gate:
        raise ValueError("thread %r is paused with no recorded gate; refusing to resume" % (thread_id,))
    # refuse a decision for a gate the thread is NOT paused at (a wrong button can't mark the
    # wrong gate's scope "approved").
    if full_gate != paused_gate:
        raise ValueError("thread is paused at %r, not %r"
                         % (ALIAS_FOR_GATE.get(paused_gate, paused_gate), gate))
    dec = APPROVED if decision == "approved" else REJECTED
    state.setdefault("approvals", {})[full_gate] = dec
    state["real_github"] = False                      # dashboard never does live writes on resume
    start = state.get("paused_at_node") or GATE_TO_NODE.get(full_gate)
    if start not in NODE_FUNCS:                        # stale node name -> use the node for the paused gate
        start = GATE_TO_NODE.get(paused_gate) or GATE_TO_NODE.get(full_gate)
    state["status"] = "running"
    app = build_graph(prefer_fallback=True)
    with _STDOUT_LOCK:                                 # dry-run nodes print -> guard vs the watcher's capture
        final = app.invoke(state, start_node=start)
    _persist(thread_id, final)
    return final


def _persist(thread_id: str, final: dict) -> None:
    """Save the checkpoint if the run is still paused, else clear any stale checkpoint."""
    if final.get("status") == "paused":
        _cli._save_ckpt(final)
    else:
        with contextlib.suppress(OSError):
            os.remove(_cli._ckpt_path(thread_id))


# ----------------------------------------------------------------------------------------
# Implementation Packets (read-only wrt GitHub/repo; writes two local files)
# ----------------------------------------------------------------------------------------
def export_packet(thread_id: str, decision: str = "approved", out_dir: Optional[str] = None) -> dict:
    """Export an Implementation Packet from a PAUSED run's checkpoint. Mirrors
    ``cmd_export_implementation_packet``'s safety checks. Returns ``{packet, paths, handoff}``.
    Raises ValueError (bad state) or PacketError (unsafe output path)."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved|rejected")
    state = get_run(thread_id)
    if state is None:
        raise ValueError("no checkpoint for thread %r" % (thread_id,))
    if (state.get("status") != "paused"
            or state.get("paused_at_gate") not in GATE_ALIASES.values()):
        raise ValueError("thread %r is not paused at a recognized approval gate "
                         "(status=%r, gate=%r)"
                         % (thread_id, state.get("status"), state.get("paused_at_gate")))
    gate = state.get("paused_at_gate")
    packet = build_packet(state, gate=gate, decision=decision, generated_at=_now_iso())
    paths = write_packet(out_dir or _cli.PACKETS_DIR, thread_id, packet)   # may raise PacketError
    return {"packet": packet, "paths": paths, "handoff": _export_handoff(packet, paths)}


def _export_handoff(packet: dict, paths: dict) -> str:
    if packet.get("approval", {}).get("decision") == REJECTED:
        return "Gate REJECTED — nothing to implement; the packet records the rejection."
    return ("Implement ONLY the scoped tasks in %s; run the listed checks; "
            "do not commit/push/merge; ask before expanding scope." % paths.get("md_path"))


def create_manual_packet(thread_id: str, task: str, repo: str, scope_markdown: str,
                         out_dir: Optional[str] = None) -> dict:
    """Create a manual-scope Implementation Packet from form-provided scope Markdown (no file read).
    Same builder as ``create-implementation-packet``. Returns ``{marker, packet, paths, task,
    suggested_prompt, unknown_headings}``. Raises ValueError if the scope has no concrete work."""
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    scope = parse_scope_markdown(scope_markdown or "")
    task = (task or "").strip() or scope.get("task") or "(untitled task)"
    packet = build_manual_packet(thread_id=thread_id, task=task,
                                 repo=(repo or "").strip() or _DEFAULT_REPO,
                                 generated_at=_now_iso(), scope=scope, scope_file=None)
    ii = packet["implementation_instructions"]
    if not (packet["approval"]["approved_scope"] or ii["tasks"]):
        raise ValueError("scope has no concrete work — add a '# Approved scope' or '# Tasks' "
                         "section (files alone, or only quarantined items, don't count)")
    paths = write_packet(out_dir or _cli.PACKETS_DIR, thread_id, packet,
                         markdown=render_manual_markdown(packet))
    return {"marker": "MANUAL_IMPLEMENTATION_PACKET_CREATED", "packet": packet, "paths": paths,
            "task": task, "suggested_prompt": packet.get("suggested_prompt"),
            "unknown_headings": scope.get("unknown_headings", [])}


# ----------------------------------------------------------------------------------------
# Codex watcher (read-only) — reuse the CLI command verbatim, capture its output
# ----------------------------------------------------------------------------------------
def run_watcher(repo: str, init: bool = False, limit: int = 50) -> dict:
    """Run the READ-ONLY Codex watcher sweep by calling :func:`devflow.cli.cmd_watch_codex_reviews`
    directly (NOT a subprocess) and capturing its output. Returns ``{marker, rc, output}``. The only
    side effect is the local seen-file the CLI already maintains — no GitHub mutation. Raises
    ValueError if ``repo`` is empty."""
    repo = (repo or "").strip()
    if not repo:
        raise ValueError("repo is required")
    ns = argparse.Namespace(repo=repo, limit=int(limit), seen_file=None, reset=False,
                            init=bool(init), json=False, body_chars=400, exit_actionable=False)
    buf = io.StringIO()
    with _STDOUT_LOCK:                             # exclude ALL other stdout producers during the swap
        with contextlib.redirect_stdout(buf):
            rc = _cli.cmd_watch_codex_reviews(ns)
    output = buf.getvalue()
    marker = next((m for line in output.splitlines()
                   for m in WATCH_MARKERS if line.strip() == m), None)
    return {"marker": marker, "rc": rc, "output": output}


# ----------------------------------------------------------------------------------------
# Review Queue / Orchestrator (READ-ONLY planner) — recommends actions, never mutates GitHub
# ----------------------------------------------------------------------------------------
ORCH_LIMIT_DEFAULT = 50
ORCH_LIMIT_MAX = 200


def run_orchestrator(repo: str, limit: int = ORCH_LIMIT_DEFAULT) -> dict:
    """Compute the READ-ONLY cross-PR orchestration plan for the dashboard.

    Validates input, clamps ``limit``, and delegates to the structured runner which uses ONLY
    ReadOnlyGitHub + the pure planner — no GitHub writes, no shell, no merge/comment/review-request.
    Returns the structured result (``{marker, repo, default_branch, open_prs, plan, errors, state_path,
    rate_limited}``). Raises ValueError on bad input; GhError propagates for a gh failure."""
    repo = (repo or "").strip()
    if not repo:
        raise ValueError("repo is required")
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = ORCH_LIMIT_DEFAULT
    lim = max(1, min(lim, ORCH_LIMIT_MAX))            # clamp to a sane window
    # HARD-FORCE persist_state=False (not merely a default): the dashboard never actually requests
    # reviews, so it must NEVER persist the planner's in-flight tracking — that would suppress a later
    # real request. No caller can make the dashboard persist.
    return build_orchestration_result(repo=repo, limit=lim, persist_state=False)


# ----------------------------------------------------------------------------------------
# GPT / ChatGPT FALLBACK review prompt builder (READ-ONLY text builder — never calls an LLM)
# ----------------------------------------------------------------------------------------
def build_gpt_review_prompt(repo: str, pr_number, focus: str = "general",
                            diff_budget: str = "compact",
                            include_existing_feedback: bool = True) -> dict:
    """Validate input and delegate to the read-only fallback-review prompt builder. Builds copyable TEXT
    only: NO LLM/API call, NO GitHub write, NO shell beyond the existing read-only gh layer. Raises
    ValueError on bad input (empty repo / non-positive PR number); GhError propagates for a gh failure."""
    repo = (repo or "").strip()
    if not repo:
        raise ValueError("repo is required")
    try:
        n = int(str(pr_number).strip())
    except (TypeError, ValueError):
        raise ValueError("PR number must be a positive integer")
    if n <= 0:
        raise ValueError("PR number must be a positive integer")
    focus = focus if focus in FOCUS_MODES else "general"          # clamp to a known focus mode
    diff_budget = diff_budget if diff_budget in DIFF_BUDGETS else "compact"   # clamp to a known budget
    return build_fallback_review_prompt(repo=repo, pr_number=n, focus=focus, diff_budget=diff_budget,
                                        include_existing_feedback=bool(include_existing_feedback))


def build_codex_prompt(repo: str, pr_number, diff_budget: str = "compact") -> dict:
    """Validate input and delegate to the read-only GUIDED Codex prompt builder. Builds copyable TEXT
    only (a ``@codex review`` comment the human pastes manually): NO LLM/Codex call, NO GitHub write, NO
    posting, NO shell beyond the existing read-only gh layer. Raises ValueError on bad input; GhError
    propagates for a gh failure."""
    repo = (repo or "").strip()
    if not repo:
        raise ValueError("repo is required")
    try:
        n = int(str(pr_number).strip())
    except (TypeError, ValueError):
        raise ValueError("PR number must be a positive integer")
    if n <= 0:
        raise ValueError("PR number must be a positive integer")
    diff_budget = diff_budget if diff_budget in DIFF_BUDGETS else "compact"
    return build_codex_review_prompt(repo=repo, pr_number=n, diff_budget=diff_budget)


# ----------------------------------------------------------------------------------------
# Implementation Packet lifecycle (read packets + LOCAL handoff status only — no GitHub, no shell, no LLM)
# ----------------------------------------------------------------------------------------
PACKET_STATUSES = packet_store.STATUSES


def list_packets(base_dir=None) -> list:
    """List Implementation Packets under the packets dir (read-only)."""
    return packet_store.list_packets(base_dir or _cli.PACKETS_DIR)


def get_packet(slug, base_dir=None):
    """Full normalized packet view for the detail page, or None if missing. Raises ValueError on an
    unsafe slug (path-traversal guard lives in packet_store)."""
    return packet_store.get_packet(base_dir or _cli.PACKETS_DIR, slug)


def set_packet_status(slug, status, base_dir=None) -> dict:
    """Update ONLY the local handoff-status.json for a packet. Raises ValueError on bad slug/status or a
    non-existent packet. No GitHub write."""
    return packet_store.write_status(base_dir or _cli.PACKETS_DIR, slug, status)


# ----------------------------------------------------------------------------------------
# The ONE real GitHub write the dashboard can do: post the fixed "@codex review" (gated by the app on
# --allow-github-writes + localhost). Delegates to the narrow guarded helper; no generic comment API.
# ----------------------------------------------------------------------------------------
def request_codex_review(repo, pr_number, expected_head_sha, confirmation, *, audit_dir=None) -> dict:
    """Post EXACTLY '@codex review' to a PR. The APP gates this on --allow-github-writes + localhost
    BEFORE calling here; this only validates the confirmation/head and delegates to the guarded writer.
    Returns the helper result; raises ValueError on a gate failure, GhError on a gh failure."""
    return dashboard_writes.post_codex_review_request(
        repo, pr_number, expected_head_sha, confirmation, live=True, audit_dir=audit_dir)
