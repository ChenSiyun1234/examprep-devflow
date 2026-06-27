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


class PacketError(RuntimeError):
    """Raised when a packet cannot be written safely (e.g. into a symlinked output directory)."""

# Fixed safety boundaries embedded in every packet — Claude Code must obey these. The packet is
# read on its own (without the terminal handoff), so the git/PR prohibitions live here too.
SAFETY_BOUNDARIES = [
    "Do not commit or expose secrets.",
    "Do not add or hardcode API keys.",
    "Do not perform unrelated rewrites/refactors outside the listed scope.",
    "Do not commit, push, or open pull requests — the human drives git and the PR.",
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


def _strip_unsafe_path_prefix(text: str) -> str:
    """Real Codex bullets often carry the location inside the note text as ``PATH: detail``. If that
    leading PATH is unsafe (absolute / drive / ``..`` / ``~`` / env / ``.``), drop it so the task
    text can't direct edits outside the repo; a benign or safe-path note is returned unchanged."""
    # A Windows drive path has a colon at index 1 ("C:\dir") that is NOT the "PATH: detail" delimiter
    # — skip it so the bare-colon fallback doesn't split the head down to just "C".
    drive = len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] in "\\/"
    # Try ": " (colon-space) FIRST, then a bare ":" for the "PATH:detail" (no-space) shape.
    for sep_str in (": ", ":"):
        start = 2 if (drive and sep_str == ":") else 0
        idx = text.find(sep_str, start)
        if idx != -1:
            head, rest = text[:idx], text[idx + len(sep_str):]
            if rest.strip():
                h = head.strip()
                looks_path = ("/" in h or "\\" in h or h.startswith(("~", ".", "$", "%"))
                              or "$" in h or "%" in h or (len(h) >= 2 and h[1] == ":"))
                if looks_path and not _is_safe_rel_path(h):
                    return _strip_unsafe_path_prefix(rest.strip())   # re-sanitize a chained 'P1: P2: …'
    # no "PATH: detail" shape, but the WHOLE text may itself be a bare unsafe path ('/etc/passwd',
    # '../x.py', 'C:\\...') -> never surface it as an edit target. Gate on path-SHAPE first so ordinary
    # prose ('Increase coverage to 80%', 'A: add tests') — which _is_safe_rel_path also rejects (the
    # '%'/drive checks) — is NOT mistaken for a path and is returned unchanged.
    s = text.strip()
    looks_bare_path = bool(s) and (
        "/" in s or "\\" in s or s.startswith(("~", "..", "$", "%"))
        or (len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "\\/"))
    if looks_bare_path and not _is_safe_rel_path(s):
        return "(unsafe path omitted)"
    return text


def _fmt_comment_task(c) -> str:
    """Format a review comment for a TASK / approved-scope line — like :func:`_fmt_comment` but it
    NEVER surfaces an unsafe (absolute / ``..`` / ``~`` / ``.``) path as an edit target, whether the
    path is in the structured ``path`` field OR embedded as a ``PATH: detail`` prefix in the note."""
    if isinstance(c, dict):
        path = c.get("path")
        note = c.get("note") or c.get("body") or c.get("summary")
        safe = isinstance(path, str) and _is_safe_rel_path(path)
        if safe and note:
            # sanitize the note too — an unsafe "PATH: detail" can be smuggled inside a safe-path note
            return f"{path}: {_strip_unsafe_path_prefix(str(note))}"
        if note:
            return _strip_unsafe_path_prefix(str(note))   # drop an unsafe path embedded in the note
        if safe:
            return path
        return "(review comment)"                 # no safe path, no note -> never echo a bad path
    return _strip_unsafe_path_prefix(str(c))       # string item -> same unsafe-prefix stripping


def _as_list(v) -> list:
    """Coerce a checkpoint field to a list — defensively, since the checkpoint is on-disk state
    that may be hand-edited/legacy/corrupt. Only real sequences pass through; anything else -> []."""
    return list(v) if isinstance(v, (list, tuple)) else []


def _is_safe_rel_path(p) -> bool:
    """True only for a repo-relative path with no traversal. Rejects absolute paths (POSIX, UNC, or
    Windows-drive) and any ``..`` segment, so an untrusted advisory/review can't point edits outside
    the repo. Used to filter ``files_likely_touched`` (which comes from untrusted Codex content).

    The path is stripped FIRST so leading/embedded whitespace can't smuggle an absolute path or
    ``..`` past the checks (e.g. ``" /etc/passwd"`` or ``".. /x"``)."""
    if not isinstance(p, str):
        return False
    q = p.strip().replace("\\", "/")
    if not q:
        return False
    if q.startswith("/"):                         # POSIX absolute or UNC (//server)
        return False
    if len(q) >= 2 and q[1] == ":":               # Windows drive, e.g. C:/...
        return False
    if q.startswith("~"):                         # home-relative, e.g. ~/.ssh/config
        return False
    if "$" in q or "%" in q:                      # env-rooted, e.g. $HOME/.gitconfig, %APPDATA%\...
        return False
    if q.split("/")[0].lower() == ".git":         # never edit Git control files (.git/...) —
        return False                              # .gitignore / .github/ stay fine (exact first segment)
    # reject ``..`` (traversal), ``.`` (current dir / whole repo), and empty (``//``) segments
    return not any(seg.strip() in ("", "..", ".") for seg in q.split("/"))


def _md_safe(s) -> str:
    """Neutralize untrusted text for Markdown rendering: collapse newlines so the content can't
    start a new block (e.g. a forged ``## Safety boundaries`` heading) and trim. The JSON packet
    keeps the raw, faithful value — only the human-readable Markdown is sanitized."""
    return str(s).replace("\r", " ").replace("\n", " ").strip()


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

    # advisory steps/summary are UNTRUSTED checkpoint content too — sanitize any embedded unsafe path
    # prefix here, at the source, so every downstream use (approved_scope/tasks/fallback) is clean.
    steps = [_strip_unsafe_path_prefix(str(s)) for s in _as_list(advisory.get("recommended_steps"))]
    summary = advisory.get("summary") if isinstance(advisory.get("summary"), str) else None
    if summary:
        summary = _strip_unsafe_path_prefix(summary)
    # SCOPE/TASKS fallback prefers the FULL advisory body — summarize_advisory truncates `summary` to
    # ~200 chars, which would hand Claude Code an incomplete scope. The human-facing advisory_summary
    # still uses the short summary.
    body_full = _strip_unsafe_path_prefix(str(advisory.get("body"))) if isinstance(advisory.get("body"), str) else None
    scope_fallback = body_full or summary

    # Blocking comments are actionable ONLY at the blocking-fix gate. At the merge gate they are
    # already-resolved history, and at the advisory gate they don't exist yet — turning them into
    # tasks there would tell Claude Code to (re-)edit resolved/irrelevant findings (scope creep).
    is_fix_gate = gate == "blocking_fix"

    # Approved scope: advisory steps (or, for a REAL advisory that only has a summary, the summary)
    # at the advisory gate, plus the blocking fixes at the fix gate.
    approved_scope = []
    if gate == "advisory_implementation":
        approved_scope += steps or ([scope_fallback] if scope_fallback else [])
    if is_fix_gate:
        approved_scope += [f"fix: {_fmt_comment_task(c)}" for c in blocking]

    # Concrete tasks for Claude Code (unsafe paths are dropped from the task text by _fmt_comment_task).
    tasks = []
    if gate == "advisory_implementation":
        tasks += steps
    if is_fix_gate:
        tasks += [f"Address blocking review comment — {_fmt_comment_task(c)}" for c in blocking]
    # Summary fallback ONLY at the advisory gate — a merge-gate (or fix-gate-with-no-blocking) packet
    # must not turn the carried advisory summary into "go implement this" (merge approves no new work).
    if gate == "advisory_implementation" and not tasks and scope_fallback:
        tasks = [scope_fallback]

    # files_likely_touched = edit targets. Only the (fix-gate) blocking comments + an advisory's own
    # file list contribute — NOT non-blocking comments (those are optional/out-of-scope). These paths
    # come from UNTRUSTED Codex content, so absolute paths and `..` traversal are rejected; rejected
    # paths are surfaced in out-of-scope, not silently dropped.
    raw_files = []
    if is_fix_gate:
        raw_files += [c["path"] for c in blocking
                      if isinstance(c, dict) and isinstance(c.get("path"), str)]
        # real review bullets carry the file inside the note text ('src/foo.py: fix …'), not a
        # structured `path` field — recover a leading SAFE path so files_likely_touched isn't empty.
        for c in blocking:
            note = c.get("note") if isinstance(c, dict) else (c if isinstance(c, str) else None)
            if isinstance(note, str) and ":" in note:
                head = note.split(":", 1)[0].strip()
                # a real file target looks like a path ('/' or a '.ext'); never a bare drive letter 'C'
                # (from 'C:\\…') or a one-word prose head, both of which _is_safe_rel_path would accept.
                if ("/" in head or "." in head) and _is_safe_rel_path(head):
                    raw_files.append(head)
    # advisory's own file list belongs ONLY to the advisory gate — at the merge gate (which approves
    # no new work) it would wrongly present edit targets. Only accept STRING entries (no str()-coerce).
    if gate == "advisory_implementation":
        raw_files += [f for f in _as_list(advisory.get("files")) if isinstance(f, str)]
    files = sorted({p for p in raw_files if _is_safe_rel_path(p)})
    unsafe_files = sorted({p for p in raw_files if not _is_safe_rel_path(p)})

    out_of_scope = [f"(deferred) {_fmt_comment(c)}" for c in deferred]
    out_of_scope += [f"(non-blocking / optional) {_fmt_comment(c)}" for c in non_blocking]
    out_of_scope += [f"(ignored unsafe path — outside repo, do NOT touch) {p}" for p in unsafe_files]
    out_of_scope += [
        "Anything not explicitly listed in the tasks above.",
        "Product/runtime features of the exam-prep skill.",
        "Unrelated refactors or formatting churn.",
    ]

    # The runnable validation command — NOT the dry-run `checks_not_run` labels (e.g. "unit tests
    # (dry-run: not executed)"), which are descriptive, not executable.
    tests = ["python -m unittest discover -s tests"]

    rejected_or_deferred = [_fmt_comment(c) for c in deferred]
    for field, label in (("human_approval", "advisory"), ("fix_approval", "fix"),
                         ("merge_approval", "merge")):
        if str(state.get(field) or "").lower() == "rejected":
            rejected_or_deferred.append(f"{label} gate: rejected")
    # Keep a rejected export internally consistent: nothing is approved and there is NOTHING to
    # implement — clear scope/tasks/files so the packet can't be (mis)read as "go implement this",
    # even if it is opened without the terminal handoff message.
    if is_rejection:
        approved_scope = []
        tasks = []
        files = []
        out_of_scope = ["REJECTED: do not implement anything from this packet."] + out_of_scope
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
    items = [_md_safe(i) for i in (items or [])]   # sanitize: untrusted content can't inject blocks
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
        f"- thread_id: `{_md_safe(m.get('thread_id'))}`",
        f"- task_type: {_md_safe(m.get('task_type'))}",
        f"- repo: {_md_safe(m.get('repo'))}",
        f"- generated_at: {_md_safe(m.get('generated_at'))}",
    ]
    if m.get("issue_number"):
        out.append(f"- source issue: #{_md_safe(m['issue_number'])} {_md_safe(m.get('issue_url') or '')}".rstrip())
    if m.get("pr_number"):
        out.append(f"- source PR: #{_md_safe(m['pr_number'])} {_md_safe(m.get('pr_url') or '')}".rstrip())
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
        "- advisory summary: " + (_md_safe(ar.get("advisory_summary"))
                                  if ar.get("advisory_summary") else "_(none)_"),
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
    # Refuse a symlinked packet dir, output base, OR any ANCESTOR component of a relative base (e.g. a
    # stale `.devflow -> .`): os.makedirs(exist_ok=True) would follow such a symlink and write the
    # packet outside the intended tool-state location. For a relative base we walk its ancestors up to
    # the cwd; for an explicit absolute --out-dir we trust the user's chosen location (check it + slug).
    # Refuse a symlink at the packet dir, the output base, OR any ancestor — relative (up to its top
    # component, e.g. a stale `.devflow -> .`) AND absolute (up to the filesystem anchor, e.g. a
    # symlinked parent of an explicit --out-dir). os.makedirs(exist_ok=True) would follow such a
    # symlink and write the packet outside the intended tool-state location.
    anc = pkt_dir
    while anc:
        if os.path.islink(anc):
            raise PacketError(f"refusing to write under a symlinked path component: {anc}")
        parent = os.path.dirname(anc)
        if parent == anc:        # reached an absolute anchor ("/" or "C:\\")
            break
        anc = parent
    # a regular file at the packet dir (or a file as an ancestor) makes os.makedirs raise
    # FileExistsError/NotADirectoryError — which cmd_export only catches as PacketError. Surface the
    # clean refusal instead of an uncaught traceback.
    try:
        os.makedirs(pkt_dir, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as e:
        raise PacketError(f"refusing to write: a path component is a regular file, not a directory: {pkt_dir}") from e
    json_path = os.path.join(pkt_dir, PACKET_JSON_NAME)
    md_path = os.path.join(pkt_dir, PACKET_MD_NAME)
    # ...and refuse if either packet FILE is itself a symlink — open(..., "w") would follow it and
    # could overwrite a tracked repo file (or any writable path) on a repeated export.
    for p in (json_path, md_path):
        if os.path.islink(p):
            raise PacketError(f"refusing to overwrite a symlinked packet file: {p}")
        # refuse a hard-linked or non-regular existing target — open(w) would truncate the shared
        # inode (e.g. a hard link to a tracked file), writing through to it.
        if os.path.exists(p) and (not os.path.isfile(p) or os.stat(p).st_nlink > 1):
            raise PacketError(f"refusing to overwrite a hard-linked or non-regular packet file: {p}")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown if markdown is not None else render_markdown(packet))
    return {"dir": pkt_dir, "slug": slug, "json_path": json_path, "md_path": md_path}
