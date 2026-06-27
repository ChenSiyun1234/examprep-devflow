# -*- coding: utf-8 -*-
"""Implementation Packet: the safe handoff from devflow's approval gates to Claude Code.

devflow orchestrates + summarizes the workflow and records the human approval; the packet tells
**Claude Code** WHAT to implement, within an explicit scope and fixed safety boundaries. devflow
itself never edits repository files — the packet is the boundary.

This module is pure Python stdlib: ``build_packet`` / ``render_markdown`` are side-effect-free and
``write_packet`` only writes the two local files. No network, no ``gh``, no third-party deps.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

PACKET_JSON_NAME = "implementation-packet.json"
PACKET_MD_NAME = "implementation-packet.md"
SCHEMA_VERSION = 1

# Fixed safety boundaries embedded in every packet — Claude Code must obey these.
SAFETY_BOUNDARIES = [
    "Do not commit or expose secrets.",
    "Do not add or hardcode API keys.",
    "Do not perform unrelated rewrites/refactors outside the listed scope.",
    "Do not merge any pull request.",
    "Do not delete branches.",
    "Do not force-push.",
    "Do not run arbitrary destructive shell commands.",
    "Do not claim tests passed unless they actually ran — paste the real output.",
    "Ask the human for approval before expanding scope beyond the tasks below.",
]

GATE_LABELS = {
    "advisory_implementation": "advisory implementation",
    "blocking_fix": "blocking fix",
    "merge": "merge",
}


def safe_thread_slug(thread_id: str) -> str:
    """A filesystem-safe, collision-resistant slug for a thread id.

    Non ``[A-Za-z0-9-_.]`` characters become ``_`` (so ``/`` and ``\\`` can't create sub-paths),
    the slug is length-bounded, and an 8-char hash of the ORIGINAL id is appended — which also makes
    a bare ``.``/``..`` slug impossible (it becomes ``..-<hash>``), so no path traversal is possible.
    """
    tid = thread_id if thread_id else "thread"
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in tid)[:80] or "thread"
    digest = hashlib.sha1(tid.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def _fmt_comment(c) -> str:
    if isinstance(c, dict):
        path = c.get("path")
        note = c.get("note") or c.get("body") or c.get("summary")
        if path and note:
            return f"{path}: {note}"
        return str(path or note or c)
    return str(c)


def _as_list(v) -> list:
    """Coerce a checkpoint field to a list — defensively, since the checkpoint is on-disk state
    that may be hand-edited/legacy/corrupt. Only real sequences pass through; anything else -> []."""
    return list(v) if isinstance(v, (list, tuple)) else []


def build_packet(state: dict, gate: str, decision: str, generated_at: str) -> dict:
    """Build the structured Implementation Packet (a plain dict) from a devflow state snapshot.

    Hardened against a corrupt/foreign checkpoint: every field is type-coerced so a malformed value
    degrades gracefully instead of raising (the packet is built from on-disk, user-editable state).
    """
    state = state if isinstance(state, dict) else {}
    advisory = state.get("advisory_packet")
    advisory = advisory if isinstance(advisory, dict) else {}
    blocking = _as_list(state.get("blocking_comments"))
    non_blocking = _as_list(state.get("non_blocking_comments"))
    deferred = _as_list(state.get("deferred_followups"))
    is_rejection = str(decision).lower() == "rejected"

    steps = _as_list(advisory.get("recommended_steps"))
    summary = advisory.get("summary") if isinstance(advisory.get("summary"), str) else None

    # Approved scope: advisory steps (or, for a REAL advisory that only has a summary, the summary)
    # at the advisory gate, plus any blocking fixes.
    approved_scope = []
    if gate == "advisory_implementation":
        approved_scope += steps or ([summary] if summary else [])
    approved_scope += [f"fix: {_fmt_comment(c)}" for c in blocking]

    # Concrete tasks for Claude Code.
    tasks = []
    if gate == "advisory_implementation":
        tasks += steps
    tasks += [f"Address blocking review comment — {_fmt_comment(c)}" for c in blocking]
    if not tasks and summary:
        tasks = [summary]

    # Best-effort: only file-scoped comments contribute paths (real Codex comments are often not
    # file-scoped, so this list can legitimately be empty).
    files = [c["path"] for c in (blocking + non_blocking)
             if isinstance(c, dict) and isinstance(c.get("path"), str)]
    files += _as_list(advisory.get("files"))
    files = sorted({str(f) for f in files})

    out_of_scope = [f"(deferred) {_fmt_comment(c)}" for c in deferred]
    out_of_scope += [f"(non-blocking / optional) {_fmt_comment(c)}" for c in non_blocking]
    out_of_scope += [
        "Anything not explicitly listed in the tasks above.",
        "Product/runtime features of the exam-prep skill.",
        "Unrelated refactors or formatting churn.",
    ]

    tests = _as_list(state.get("checks_not_run")) or ["python -m unittest discover -s tests"]

    rejected_or_deferred = [_fmt_comment(c) for c in deferred]
    for field, label in (("human_approval", "advisory"), ("fix_approval", "fix"),
                         ("merge_approval", "merge")):
        if str(state.get(field) or "").lower() == "rejected":
            rejected_or_deferred.append(f"{label} gate: rejected")
    # Keep the recorded decision consistent: a rejected export approves nothing and is surfaced.
    if is_rejection:
        approved_scope = []
        entry = f"{GATE_LABELS.get(gate, gate)} gate: rejected"
        if entry not in rejected_or_deferred:
            rejected_or_deferred.append(entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "thread_id": state.get("thread_id"),
            "task_type": state.get("task_type"),
            "repo": state.get("repo"),
            "generated_at": generated_at,
            "issue_number": state.get("issue_number"),
            "issue_url": state.get("issue_url"),
            "pr_number": state.get("pr_number"),
            "pr_url": state.get("pr_url"),
        },
        "approval": {
            "gate": gate,
            "gate_label": GATE_LABELS.get(gate, gate),
            "decision": decision,
            "approved_scope": approved_scope,
            "rejected_or_deferred": rejected_or_deferred,
        },
        "advisory_review": {
            "advisory_summary": advisory.get("summary"),
            "review_summary": state.get("review_summary"),
            "blocking_comments": blocking,
            "non_blocking_comments": non_blocking,
            "deferred_followups": deferred,
        },
        "implementation_instructions": {
            "files_likely_touched": files,
            "tasks": tasks,
            "out_of_scope": out_of_scope,
            "tests_to_run": tests,
            "safety_rules": list(SAFETY_BOUNDARIES),
        },
        "safety_boundaries": list(SAFETY_BOUNDARIES),
    }


def _md_list(items, empty="_(none)_") -> str:
    items = [str(i) for i in (items or [])]
    return "\n".join(f"- {i}" for i in items) if items else empty


def render_markdown(packet: dict) -> str:
    """Render the packet as human-readable Markdown (the file Claude Code reads)."""
    m = packet.get("metadata", {})
    a = packet.get("approval", {})
    ar = packet.get("advisory_review", {})
    ii = packet.get("implementation_instructions", {})
    out = [
        "# Implementation Packet",
        "",
        "> Handoff from **devflow** (orchestration + human approval) to **Claude Code** "
        "(scoped implementation). devflow does not edit repository files itself; you do, within "
        "the scope and safety boundaries below.",
        "",
        "## Metadata",
        f"- thread_id: `{m.get('thread_id')}`",
        f"- task_type: {m.get('task_type')}",
        f"- repo: {m.get('repo')}",
        f"- generated_at: {m.get('generated_at')}",
    ]
    if m.get("issue_number"):
        out.append(f"- source issue: #{m['issue_number']} {m.get('issue_url') or ''}".rstrip())
    if m.get("pr_number"):
        out.append(f"- source PR: #{m['pr_number']} {m.get('pr_url') or ''}".rstrip())
    out += [
        "",
        "## Approval",
        f"- gate: {a.get('gate_label')} (`{a.get('gate')}`)",
        f"- decision: **{a.get('decision')}**",
        "- approved scope:",
        _md_list(a.get("approved_scope")),
        "- rejected / deferred:",
        _md_list(a.get("rejected_or_deferred")),
        "",
        "## Advisory / Review",
        f"- advisory summary: {ar.get('advisory_summary') or '_(none)_'}",
        "- review summary: " + (json.dumps(ar.get("review_summary"), ensure_ascii=False)
                                if ar.get("review_summary") else "_(none)_"),
        "- blocking comments:",
        _md_list([_fmt_comment(c) for c in (ar.get("blocking_comments") or [])]),
        "- non-blocking comments:",
        _md_list([_fmt_comment(c) for c in (ar.get("non_blocking_comments") or [])]),
        "- deferred follow-ups:",
        _md_list([_fmt_comment(c) for c in (ar.get("deferred_followups") or [])]),
        "",
        "## Implementation instructions for Claude Code",
        "- files likely touched:",
        _md_list(ii.get("files_likely_touched")),
        "- tasks:",
        _md_list(ii.get("tasks")),
        "- out of scope:",
        _md_list(ii.get("out_of_scope")),
        "- tests / checks to run:",
        _md_list(ii.get("tests_to_run")),
        "",
        "## Safety boundaries",
        _md_list(packet.get("safety_boundaries")),
        "",
    ]
    return "\n".join(out)


def write_packet(base_dir: str, thread_id: str, packet: dict,
                 markdown: Optional[str] = None) -> dict:
    """Write the packet JSON + Markdown under ``base_dir/<safe-thread-slug>/``. Returns the paths.

    This is the ONLY side effect: two local files in a tool-state directory. No GitHub, no network.
    """
    slug = safe_thread_slug(thread_id)
    pkt_dir = os.path.join(base_dir, slug)
    os.makedirs(pkt_dir, exist_ok=True)
    json_path = os.path.join(pkt_dir, PACKET_JSON_NAME)
    md_path = os.path.join(pkt_dir, PACKET_MD_NAME)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown if markdown is not None else render_markdown(packet))
    return {"dir": pkt_dir, "slug": slug, "json_path": json_path, "md_path": md_path}
