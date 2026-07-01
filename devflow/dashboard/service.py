# -*- coding: utf-8 -*-
"""Service layer for the local DevFlow Dashboard.

Wraps the existing, already-safe devflow operations behind plain functions the dashboard HTTP layer
calls — so the HTTP layer never touches devflow internals directly and every operation is provably
safe in ONE place:

* **runs / checkpoints** are the stdlib-fallback JSON checkpoints :mod:`devflow.cli` already writes
  (``CKPT_DIR``); we only read them or run the workflow that writes them.
* **the workflow uses the pure-stdlib fallback backend** (``build_graph(prefer_fallback=True)``).
  Start defaults to dry-run; real GitHub advisory mode is an explicit localhost-only write path that
  creates an advisory issue/comment only and stops before implementation.
* **packets** are the same local files ``create``/``export-implementation-packet`` write
  (:func:`devflow.tools.packet_writer.write_packet`).
* **the watcher** reuses :func:`devflow.cli.cmd_watch_codex_reviews` VERBATIM (read-only) and only
  captures its output — no reimplementation, no divergence from the CLI's marker precedence.

Pure stdlib. No arbitrary shell execution, no LangGraph requirement.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import sys
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
from devflow.tools.github_cli import GhError, check_gh_available, _SECRET_RE
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
START_DEFAULT_REPO = "ChenSiyun1234/examprep-devflow"
AGENT_PROFILES = ("codex", "claude_code", "generic")
START_MODE_DRY_RUN = "dry-run"
START_MODE_REAL = "real"
START_REAL_CONFIRMATION = "START REAL ADVISORY"
START_REAL_DASHBOARD_MODE = "real-github-advisory"
START_REAL_ACTION = "start_real_advisory"
START_REAL_STATE_FILE = "start-real-advisory-state.json"
START_REAL_MAX_POLLS_DEFAULT = 1
START_REAL_POLL_SECONDS_DEFAULT = 0
START_REAL_MAX_POLLS_LIMIT = 10
START_REAL_POLL_SECONDS_LIMIT = 60

_START_REAL_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _safe_slug(text: str, fallback: str = "task", limit: int = 36) -> str:
    """ASCII-only slug for suggested thread ids; checkpoint paths add their own hash."""
    out = []
    last_dash = False
    for ch in (text or "").lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-")
    return (slug or fallback)[:limit].strip("-") or fallback


def suggest_thread_id(task: str = "", now: Optional[datetime.datetime] = None) -> str:
    """Return a bounded, checkpoint-safe thread id suggestion based on task + UTC timestamp."""
    ts = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    return "start-%s-%s" % (_safe_slug(task), ts.strftime("%Y%m%d-%H%M%S"))


def dashboard_environment_check() -> dict:
    """Read-only Start Wizard preflight: Python version plus gh availability/auth/account."""
    gh = check_gh_available()
    return {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "gh_available": bool(gh.get("available")),
        "gh_authenticated": bool(gh.get("authenticated")),
        "gh_account": gh.get("account"),
        "gh_error": gh.get("error"),
        "ok": bool(gh.get("available")) and bool(gh.get("authenticated")),
    }


def _agent_profile(value: str) -> str:
    value = (value or "").strip()
    if value not in AGENT_PROFILES:
        raise ValueError("agent_profile must be one of: %s" % ", ".join(AGENT_PROFILES))
    return value


def _bounded_int(value, name: str, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        out = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer" % name)
    if out < minimum or out > maximum:
        raise ValueError("%s must be between %d and %d" % (name, minimum, maximum))
    return out


def _redact_audit_text(text) -> str:
    """Keep the local audit trail useful without writing obvious tokens into it."""
    return _SECRET_RE.sub("[REDACTED]", "" if text is None else str(text))


def _start_real_state_path(audit_dir: Optional[str] = None, state_file: Optional[str] = None) -> str:
    if state_file:
        return state_file
    return os.path.join(audit_dir or dashboard_writes.AUDIT_DIR, START_REAL_STATE_FILE)


def _read_start_real_state(audit_dir: Optional[str] = None,
                           state_file: Optional[str] = None) -> dict:
    try:
        with open(_start_real_state_path(audit_dir, state_file), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_start_real_state(data: dict, audit_dir: Optional[str] = None,
                            state_file: Optional[str] = None) -> None:
    try:
        path = _start_real_state_path(audit_dir, state_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def _audit_start_real(record: dict, audit_dir: Optional[str] = None) -> None:
    """Best-effort append to the shared dashboard write audit log."""
    rec = {
        "timestamp": _now_iso(),
        "action": START_REAL_ACTION,
        "actor": "dashboard",
    }
    rec.update(record)
    try:
        dashboard_writes._audit(audit_dir, rec)
    except Exception:
        pass


def _audit_start_attempt(result: str, repo: str, thread_id: str, task: str,
                         agent_profile: str, reason: str = "",
                         issue_number=None, issue_url: str = "",
                         audit_dir: Optional[str] = None) -> None:
    rec = {
        "repo": (repo or "").strip(),
        "thread_id": (thread_id or "").strip(),
        "task": _redact_audit_text(task),
        "agent_profile": (agent_profile or "").strip(),
        "result": result,
        "issue_number": issue_number,
        "issue_url": issue_url or "",
    }
    if reason:
        rec["reason"] = _redact_audit_text(reason)
    _audit_start_real(rec, audit_dir=audit_dir)


def _refuse_start_real(repo: str, thread_id: str, task: str, agent_profile: str,
                       reason: str, audit_dir: Optional[str] = None) -> None:
    _audit_start_attempt("refused", repo, thread_id, task, agent_profile, reason,
                         audit_dir=audit_dir)
    raise ValueError(reason)


def _remember_start_real(final: dict, result: str, task: str,
                         audit_dir: Optional[str] = None,
                         state_file: Optional[str] = None) -> None:
    thread_id = final.get("thread_id") or ""
    if not thread_id:
        return
    data = _read_start_real_state(audit_dir, state_file)
    data[thread_id] = {
        "created_at": _now_iso(),
        "thread_id": thread_id,
        "repo": final.get("repo") or "",
        "task": _redact_audit_text(task),
        "agent_profile": final.get("agent_profile") or "",
        "issue_number": final.get("issue_number"),
        "issue_url": final.get("issue_url") or "",
        "result": result,
        "request_sent": _start_real_request_sent(final),
        "checkpoint_thread_id": thread_id if final.get("status") == "paused" else "",
    }
    _write_start_real_state(data, audit_dir, state_file)


def _start_real_errors(final: dict) -> list[str]:
    return [str(e) for e in (final.get("errors") or [])]


def _start_real_request_failed(final: dict) -> bool:
    return any(
        e.startswith("create_advisory_issue:")
        or e.startswith("request_codex_advisory:")
        for e in _start_real_errors(final)
    )


def _start_real_request_sent(final: dict) -> bool:
    return bool(final.get("issue_number")) and not _start_real_request_failed(final)


def _pause_start_real_timeout_checkpoint(final: dict) -> dict:
    """Keep a browser-inspectable/exportable checkpoint after a sent request times out."""
    paused = dict(final)
    gate = GATE_ALIASES["advisory"]
    event_log = list(paused.get("event_log") or [])
    event_log.append("[dashboard_start] real advisory request timed out; saved an advisory "
                     "gate checkpoint for inspection/export.")
    payload = dict(paused.get("interrupt_payload") or {})
    payload.update({
        "gate": gate,
        "question": "Codex advisory did not arrive within the bounded poll window.",
        "advisory": (paused.get("advisory_packet") or {}).get("summary"),
    })
    paused.update({
        "status": "paused",
        "paused_at_gate": gate,
        "paused_at_node": GATE_TO_NODE.get(gate),
        "interrupt_payload": payload,
        "event_log": event_log,
    })
    return paused


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


def create_start_run(task: str, thread_id: str = "", repo: str = "",
                     agent_profile: str = "codex") -> dict:
    """Create the Start Wizard's dry-run advisory flow and pause at the advisory approval gate.

    Agent profile is metadata only in this PR: it is recorded for future handoff wording and never
    calls an LLM, reads API keys, or changes GitHub write behavior.
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("task is required")
    thread_id = (thread_id or "").strip() or suggest_thread_id(task)
    if not thread_id:
        raise ValueError("thread_id is required")
    if get_run(thread_id) is not None:
        raise ValueError("a run with thread_id %r already exists - choose a unique id "
                         "(or remove the existing run first)" % (thread_id,))
    profile = _agent_profile(agent_profile or AGENT_PROFILES[0])
    repo = (repo or "").strip() or START_DEFAULT_REPO

    approvals = {g: APPROVED for g in APPROVAL_GATES}
    approvals.pop(GATE_ALIASES["advisory"], None)
    state = new_state(task_type=task, thread_id=thread_id, repo=repo,
                      approvals=approvals, pause_at=GATE_ALIASES["advisory"])
    state["task_text"] = task
    state["agent_profile"] = profile
    state["dashboard_start_mode"] = START_MODE_DRY_RUN
    state["real_github"] = False
    state["event_log"].append(
        "[dashboard_start] mode=dry-run agent_profile=%s repo=%s task=%s"
        % (profile, repo, task)
    )
    app = build_graph(prefer_fallback=True)
    with _STDOUT_LOCK:
        final = app.invoke(state)
    _persist(thread_id, final)
    return final


def _classify_start_real_result(final: dict) -> str:
    if final.get("status") == "paused":
        return "success"
    errors = _start_real_errors(final)
    request_or_poll_failure = any(
        e.startswith("create_advisory_issue:")
        or e.startswith("request_codex_advisory:")
        or e.startswith("wait_for_codex_advisory:")
        for e in errors
    )
    if request_or_poll_failure:
        return "failure"
    if (final.get("codex_advisory_status") == "timeout"
            and _start_real_request_sent(final)):
        return "timeout"
    return "failure" if errors else "success"


def create_start_real_advisory_run(task: str, thread_id: str = "", repo: str = "",
                                   agent_profile: str = "codex", confirmation: str = "",
                                   max_polls=None, poll_seconds=None, *,
                                   writes_enabled: bool = False,
                                   audit_dir: Optional[str] = None,
                                   state_file: Optional[str] = None) -> dict:
    """Launch the Start Wizard's real GitHub advisory mode.

    This is intentionally narrower than a generic dashboard write API: it creates the existing
    advisory issue, posts the existing advisory request comment, then bounded-polls for Codex and
    stops at the advisory approval gate. It never implements, creates a PR, pushes, or merges.
    """
    raw_task, raw_repo, raw_thread = task, repo, thread_id
    task = (task or "").strip()
    repo = (repo or "").strip()
    thread_id = (thread_id or "").strip()
    profile = (agent_profile or "").strip()

    if not writes_enabled:
        _refuse_start_real(raw_repo, raw_thread, raw_task, profile,
                           "real GitHub advisory requires --allow-github-writes on localhost",
                           audit_dir=audit_dir)
    if (confirmation or "") != START_REAL_CONFIRMATION:
        _refuse_start_real(repo, thread_id, task, profile,
                           "confirmation does not match - type exactly: %s"
                           % START_REAL_CONFIRMATION,
                           audit_dir=audit_dir)
    if not repo:
        _refuse_start_real(repo, thread_id, task, profile, "repo is required",
                           audit_dir=audit_dir)
    if not task:
        _refuse_start_real(repo, thread_id, task, profile, "task is required",
                           audit_dir=audit_dir)
    if not thread_id:
        _refuse_start_real(repo, thread_id, task, profile,
                           "thread_id is required for real GitHub advisory mode",
                           audit_dir=audit_dir)
    if profile not in AGENT_PROFILES:
        _refuse_start_real(repo, thread_id, task, profile,
                           "agent_profile must be one of: %s" % ", ".join(AGENT_PROFILES),
                           audit_dir=audit_dir)
    try:
        max_polls_i = _bounded_int(max_polls, "max_polls", START_REAL_MAX_POLLS_DEFAULT,
                                   0, START_REAL_MAX_POLLS_LIMIT)
        poll_seconds_i = _bounded_int(poll_seconds, "poll_seconds",
                                      START_REAL_POLL_SECONDS_DEFAULT,
                                      0, START_REAL_POLL_SECONDS_LIMIT)
    except ValueError as ex:
        _refuse_start_real(repo, thread_id, task, profile, str(ex), audit_dir=audit_dir)

    with _START_REAL_LOCK:
        if get_run(thread_id) is not None:
            _refuse_start_real(repo, thread_id, task, profile,
                               "a run with thread_id %r already exists - choose a unique id "
                               "(or remove the existing run first)" % (thread_id,),
                               audit_dir=audit_dir)
        seen = _read_start_real_state(audit_dir, state_file).get(thread_id)
        if isinstance(seen, dict) and (seen.get("request_sent") or
                                       seen.get("checkpoint_thread_id") or
                                       seen.get("result") in ("success", "timeout")):
            msg = "real advisory already started for thread_id %r" % thread_id
            if seen.get("issue_url"):
                msg += " (%s)" % seen.get("issue_url")
            _refuse_start_real(repo, thread_id, task, profile, msg, audit_dir=audit_dir)

        gh = check_gh_available()
        if not (gh.get("available") and gh.get("authenticated")):
            reason = gh.get("error") or "gh CLI is not available/authenticated"
            _audit_start_attempt("failure", repo, thread_id, task, profile, reason,
                                 audit_dir=audit_dir)
            raise GhError(reason)

        state = new_state(task_type=task, thread_id=thread_id, repo=repo, approvals={},
                          pause_at=GATE_ALIASES["advisory"], real_github=True,
                          max_polls=max_polls_i, poll_seconds=poll_seconds_i)
        state["task_text"] = task
        state["agent_profile"] = profile
        state["dashboard_start_mode"] = START_REAL_DASHBOARD_MODE
        state["real_github"] = True
        state["event_log"].append(
            "[dashboard_start] mode=real-github-advisory agent_profile=%s repo=%s "
            "task=%s max_polls=%s poll_seconds=%s"
            % (profile, repo, task, max_polls_i, poll_seconds_i)
        )
        app = build_graph(prefer_fallback=True)
        with _STDOUT_LOCK:
            final = app.invoke(state)

        result = _classify_start_real_result(final)
        final["dashboard_start_result"] = result
        if result == "timeout":
            final = _pause_start_real_timeout_checkpoint(final)
            final["dashboard_start_result"] = result
        _persist(thread_id, final)

        _audit_start_attempt(result, repo, thread_id, task, profile,
                             "; ".join(str(e) for e in (final.get("errors") or [])),
                             issue_number=final.get("issue_number"),
                             issue_url=final.get("issue_url") or "",
                             audit_dir=audit_dir)
        if result in ("success", "timeout") or _start_real_request_sent(final):
            _remember_start_real(final, result, task, audit_dir=audit_dir, state_file=state_file)
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
    # The dashboard can launch a live advisory, but it does not resume live checkpoints into
    # implementation/PR behavior. Refuse it (the run is still inspectable/exportable).
    if state.get("real_github"):
        raise ValueError("run %r was started with real_github; the dashboard will not resume a "
                         "live checkpoint - export an Implementation Packet instead" % (thread_id,))
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
# The narrow real GitHub writes the dashboard can do (gated by the app on --allow-github-writes +
# localhost): post the fixed "@codex review", mark a draft ready, and retarget a base. Each delegates to
# a narrow guarded helper; there is no generic comment/edit API.
# ----------------------------------------------------------------------------------------
def _current_request_review_candidates(repo, limit=None) -> list:
    """Recompute (READ-ONLY) the PR numbers the orchestrator CURRENTLY recommends requesting review for.
    The dashboard write may only target one of these (least authority), so a stale form can't post to an
    arbitrary OPEN PR. ``limit`` MUST be the same window the Review Queue page was rendered with, so a PR
    shown only because the operator widened the limit beyond the default 50 is still recognized as a
    candidate (run_orchestrator clamps/falls back on a bad value). Reuses the same persist_state=False
    planner the Review Queue renders."""
    result = run_orchestrator(repo, limit=limit if limit is not None else ORCH_LIMIT_DEFAULT)
    return list((result.get("plan") or {}).get("request_review") or [])


def request_codex_review(repo, pr_number, expected_head_sha, confirmation, *,
                         limit=None, audit_dir=None) -> dict:
    """Post EXACTLY '@codex review' to a PR. The APP gates this on --allow-github-writes + localhost
    BEFORE calling here; this recomputes the CURRENT request_review candidate set server-side (using the
    SAME ``limit`` the page was rendered with, so a stale/tampered form can only target a PR still
    recommended for review) and delegates the confirmation / head / OPEN / fixed-body gating to the
    guarded writer. Returns the helper result; raises ValueError on a gate failure, GhError on gh."""
    try:
        candidates = _current_request_review_candidates(repo, limit=limit)
        return dashboard_writes.post_codex_review_request(
            repo, pr_number, expected_head_sha, confirmation,
            live=True, candidates=candidates, audit_dir=audit_dir)
    except GhError as ex:
        # a gh read failure (candidate recompute / PR-metadata read) is a FAILED attempt — audit it too,
        # then propagate so the handler renders the error (the local trail must cover gh failures).
        dashboard_writes.audit_failure(dashboard_writes.POST_ACTION, repo, pr_number,
                                       "gh error: %s" % ex, audit_dir=audit_dir)
        raise


def _current_ready_then_merge_candidates(repo, limit=None) -> list:
    """Recompute (READ-ONLY) the PR numbers the orchestrator CURRENTLY lists under ``ready_then_merge``
    (converged DRAFT PRs that need un-drafting before merge). The mark-ready write may only target one of
    these (least authority). ``limit`` MUST be the window the Review Queue page was rendered with."""
    result = run_orchestrator(repo, limit=limit if limit is not None else ORCH_LIMIT_DEFAULT)
    return list((result.get("plan") or {}).get("ready_then_merge") or [])


def mark_ready_for_review(repo, pr_number, expected_head_sha, confirmation, *,
                          limit=None, audit_dir=None) -> dict:
    """Mark a DRAFT PR ready for review. The APP gates this on --allow-github-writes + localhost BEFORE
    calling here; this recomputes the CURRENT ready_then_merge candidate set server-side (using the SAME
    ``limit`` the page was rendered with, so a stale/tampered form can only target a PR still listed
    ready-then-merge) and delegates the confirmation / head / OPEN / DRAFT / fixed-shape gating to the
    guarded writer. This does NOT merge. Returns the helper result; raises ValueError on a gate failure,
    GhError on gh."""
    try:
        candidates = _current_ready_then_merge_candidates(repo, limit=limit)
        return dashboard_writes.mark_pr_ready_for_review(
            repo, pr_number, expected_head_sha, confirmation,
            live=True, candidates=candidates, audit_dir=audit_dir)
    except GhError as ex:
        # a gh read failure (candidate recompute / PR-metadata read) is a FAILED attempt — audit it too,
        # then propagate so the handler renders the error (the local trail must cover gh failures).
        dashboard_writes.audit_failure(dashboard_writes.MARK_READY_ACTION, repo, pr_number,
                                       "gh error: %s" % ex, audit_dir=audit_dir)
        raise


def _current_needs_retarget(repo, limit=None):
    """Recompute (READ-ONLY) the CURRENT ``needs_retarget`` PR numbers AND the planner's ``retarget_to``
    map (pr -> exact target base). The retarget write may only target one of these PRs, and only to the
    exact base the planner computed (least authority). ``limit`` MUST be the window the page used."""
    result = run_orchestrator(repo, limit=limit if limit is not None else ORCH_LIMIT_DEFAULT)
    plan = result.get("plan") or {}
    return list(plan.get("needs_retarget") or []), dict(plan.get("retarget_to") or {})


def retarget_pr_base(repo, pr_number, expected_head_sha, expected_current_base, target_base,
                     confirmation, *, limit=None, audit_dir=None) -> dict:
    """Retarget a PR's base branch. The APP gates this on --allow-github-writes + localhost BEFORE calling
    here; this recomputes the CURRENT needs_retarget set + retarget_to map server-side (using the SAME
    ``limit`` the page was rendered with, so a stale/tampered form can only retarget a PR the planner
    still lists, and only to the planner's exact target) and delegates the confirmation / head / base /
    OPEN / safe-ref / fixed-shape gating to the guarded writer. This does NOT merge and touches no
    orchestrator state. Returns the helper result; raises ValueError on a gate failure, GhError on gh."""
    try:
        candidates, targets = _current_needs_retarget(repo, limit=limit)
    except (ValueError, GhError) as ex:
        # the plan recompute failed BEFORE the helper's own audited gates (e.g. empty repo -> ValueError,
        # or a gh failure) — audit this refused/failed attempt so the trail isn't missing it, then re-raise.
        dashboard_writes.audit_failure(dashboard_writes.RETARGET_ACTION, repo, pr_number,
                                       "plan recompute failed: %s" % ex, audit_dir=audit_dir)
        raise
    try:
        return dashboard_writes.retarget_pr_base(
            repo, pr_number, expected_head_sha, expected_current_base, target_base, confirmation,
            live=True, candidates=candidates, targets=targets, audit_dir=audit_dir)
    except GhError as ex:
        # the helper's read (get_pr_meta) failed — audit it; the helper's ValueError gates self-audit.
        dashboard_writes.audit_failure(dashboard_writes.RETARGET_ACTION, repo, pr_number,
                                       "gh error: %s" % ex, audit_dir=audit_dir)
        raise
