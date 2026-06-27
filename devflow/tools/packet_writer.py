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
import re
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
    return not any(seg.strip() == ".." for seg in q.split("/"))


# Collapses the FULL set of line/paragraph separators that Python treats as line breaks (str.split-
# lines), not just \r\n — so untrusted text can't start a new Markdown block via U+2028/U+2029/NEL/etc.
_LINE_BREAKS_RE = re.compile("[\r\n\u2028\u2029\x85\x0b\x0c]+")


def _md_safe(s) -> str:
    """Neutralize untrusted text for Markdown rendering: collapse any line/paragraph separator so the
    content can't start a new block (e.g. a forged ``## Safety boundaries`` heading) and trim. The
    JSON packet keeps the raw, faithful value — only the human-readable Markdown is sanitized."""
    return _LINE_BREAKS_RE.sub(" ", str(s)).strip()


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
    # file-scoped, so this list can legitimately be empty). These paths come from UNTRUSTED Codex
    # content, so absolute paths and `..` traversal are rejected — the packet must never direct
    # edits outside the repo. Rejected paths are surfaced in out-of-scope, not silently dropped.
    raw_files = [c["path"] for c in (blocking + non_blocking)
                 if isinstance(c, dict) and isinstance(c.get("path"), str)]
    raw_files += [str(f) for f in _as_list(advisory.get("files"))]
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

    tests = _as_list(state.get("checks_not_run")) or ["python -m unittest discover -s tests"]

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
        out.append(f"- source issue: #{m['issue_number']} {_md_safe(m.get('issue_url') or '')}".rstrip())
    if m.get("pr_number"):
        out.append(f"- source PR: #{m['pr_number']} {_md_safe(m.get('pr_url') or '')}".rstrip())
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
    # Refuse a pre-existing symlinked packet dir (or output base): os.makedirs(exist_ok=True) would
    # happily follow it and write the packet outside the intended tool-state location.
    for p in (base_dir, pkt_dir):
        if os.path.islink(p):
            raise PacketError(f"refusing to write into a symlinked path: {p}")
    os.makedirs(pkt_dir, exist_ok=True)
    json_path = os.path.join(pkt_dir, PACKET_JSON_NAME)
    md_path = os.path.join(pkt_dir, PACKET_MD_NAME)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown if markdown is not None else render_markdown(packet))
    return {"dir": pkt_dir, "slug": slug, "json_path": json_path, "md_path": md_path}


# ======================================================================================
# Manual (human-provided) scope packets — created directly from a Markdown scope file,
# WITHOUT a prior advisory/checkpoint. Source is marked ``manual_human_scope`` so a generic
# simulated-advisory packet can never be mistaken for a concrete, human-approved scope.
# ======================================================================================
MANUAL_SOURCE = "manual_human_scope"

# Maps a scope-file heading (lower-cased, '#'s stripped) to a canonical section key. Defensive +
# forgiving: unknown headings are ignored, missing sections default to empty.
_SCOPE_SECTION_ALIASES = {
    "task": "task",
    "approved scope": "approved_scope", "scope": "approved_scope",
    "tasks": "tasks", "tasks to perform": "tasks",
    "files likely touched": "files", "files": "files",
    "out of scope": "out_of_scope", "out-of-scope": "out_of_scope",
    "checks to run": "checks", "checks": "checks",
    "tests to run": "checks", "tests": "checks",
    "tests/checks to run": "checks", "tests / checks to run": "checks",
    "safety rules": "safety", "safety": "safety", "safety boundaries": "safety",
}


def parse_scope_markdown(text: str) -> dict:
    """Parse a simple Markdown scope file into section lists. Intentionally tiny + defensive (no
    Markdown library): top-level ``#`` headings start sections; bullet/plain lines become items;
    unrecognized headings are ignored. Returns a dict with keys: task (str|None), approved_scope,
    tasks, files, out_of_scope, checks, safety (all lists)."""
    sections: dict = {}
    unknown_headings: list = []
    current = None
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            current = _SCOPE_SECTION_ALIASES.get(heading.lower())
            if current:
                sections.setdefault(current, [])
            elif heading:
                unknown_headings.append(heading)   # surfaced (CLI warns) so its body isn't lost silently
            continue
        if current and stripped:
            # strip a leading bullet marker: - , * , + , or •
            item = (stripped[2:].strip()
                    if len(stripped) > 1 and stripped[0] in "-*+•" and stripped[1] == " "
                    else stripped)
            if item:
                sections.setdefault(current, []).append(item)
    task_lines = sections.pop("task", [])
    out = {"task": task_lines[0] if task_lines else None, "unknown_headings": unknown_headings}
    for key in ("approved_scope", "tasks", "files", "out_of_scope", "checks", "safety"):
        out[key] = sections.get(key, [])
    return out


# Allow-list of safe check-command prefixes. A manual scope's "# Checks to run" is promoted into the
# packet's runnable instructions, so only well-known *validation* commands pass through verbatim; an
# out-of-policy/destructive command (rm -rf, gh pr merge, git push, curl|sh, ...) is quarantined.
_SAFE_CHECK_PREFIXES = (
    "python -m ", "python3 -m ", "py -m ", "pytest", "py.test", "unittest",
    "npm test", "npm run ", "yarn ", "pnpm ", "make ", "tox", "nox",
    "ruff", "flake8", "pylint", "mypy", "black --check", "isort --check",
    "pre-commit run", "cargo test", "cargo check", "go test", "go vet",
)


def _check_is_allowlisted(cmd) -> bool:
    c = str(cmd).strip().lower()
    return any(c.startswith(pfx) for pfx in _SAFE_CHECK_PREFIXES)


# Words that PERMIT/override an action paired with a protected action = a scope rule trying to weaken
# a hard boundary (e.g. "commits are allowed", "ignore the no-push rule"). Such rules are quarantined.
_SAFETY_PERMISSIVE = (
    "allow", "allowed", "ok to", "okay to", "fine to", "is fine", "are fine", "can ", "may ",
    "enable", "permit", "permitted", "ignore", "skip", "disable", "override", "no need",
    "feel free", "you can", "it's ok", "without restriction", "go ahead",
)
_SAFETY_PROTECTED = (
    "commit", "push", "pull request", " pr ", "merge", "branch", "force-push", "force push",
    "secret", "api key", "token", "destructive", "shell", "delete",
)


def _scope_safety_rule_conflicts(rule) -> bool:
    """True if a scope-file safety rule appears to PERMIT/relax a hard prohibition (so it must not be
    appended to the canonical boundaries). Conservative: a borderline rule is quarantined, not admitted."""
    low = " " + str(rule).lower() + " "
    return (any(w in low for w in _SAFETY_PERMISSIVE)
            and any(x in low for x in _SAFETY_PROTECTED))


def build_manual_packet(thread_id, task, repo, generated_at, scope: dict,
                        scope_file: Optional[str] = None) -> dict:
    """Build an Implementation Packet from explicit human-provided scope (source = manual_human_scope).

    The canonical SAFETY_BOUNDARIES are ALWAYS enforced — an incomplete scope file can add rules but
    can never remove them. File paths are filtered (no absolute/``..``) just like the export path."""
    scope = scope if isinstance(scope, dict) else {}
    # coerce every section to a list (a hand-written/foreign scope dict may carry None or a bare str)
    approved_scope = [s for s in _as_list(scope.get("approved_scope")) if str(s).strip()]
    tasks = [t for t in _as_list(scope.get("tasks")) if str(t).strip()] or list(approved_scope)

    raw_files = [str(f) for f in _as_list(scope.get("files")) if str(f).strip()]
    files = sorted({p for p in raw_files if _is_safe_rel_path(p)})
    unsafe = sorted({p for p in raw_files if not _is_safe_rel_path(p)})

    out_of_scope = [str(s) for s in _as_list(scope.get("out_of_scope")) if str(s).strip()]
    out_of_scope += [f"(ignored unsafe path — outside repo, do NOT touch) {p}" for p in unsafe]

    # Checks: only allow-listed validation commands are promoted into runnable instructions; an
    # out-of-policy/destructive command is quarantined (kept visible, but NOT a runnable instruction).
    raw_checks = [str(c).strip() for c in _as_list(scope.get("checks")) if str(c).strip()]
    safe_checks = [c for c in raw_checks if _check_is_allowlisted(c)]
    rejected_checks = [c for c in raw_checks if not _check_is_allowlisted(c)]
    tests = safe_checks or ["python -m unittest discover -s tests"]
    out_of_scope += [f"(ignored check — not auto-run; needs human approval) {c}" for c in rejected_checks]

    # Safety: canonical boundaries are never removable; a scope rule that tries to PERMIT a hard
    # prohibition is quarantined (it cannot weaken the boundaries), only genuine *extra* rules are added.
    safety = list(SAFETY_BOUNDARIES)
    rejected_rules = []
    for r in _as_list(scope.get("safety")):
        r = str(r).strip()
        if not r:
            continue
        if _scope_safety_rule_conflicts(r):
            rejected_rules.append(r)
        elif r not in safety:
            safety.append(r)
    out_of_scope += [f"(ignored scope safety rule — cannot weaken hard boundaries) {r}"
                     for r in rejected_rules]

    # Relax the file clause when no safe files are listed, so an otherwise valid packet (tasks but no
    # files, or all files filtered) isn't made impossible by "touch only the listed files".
    file_clause = ("touch only the listed files, " if files
                   else "keep changes minimal and scoped to the tasks, ")
    suggested = (f"Implement the manual-scope Implementation Packet for task \"{task}\" "
                 f"(thread {thread_id}). Do ONLY the listed tasks, {file_clause}run the listed "
                 f"checks and paste the real output; do not commit/push/merge; ask before "
                 f"expanding scope.")

    return {
        "schema_version": SCHEMA_VERSION,
        "source": MANUAL_SOURCE,
        "metadata": {
            "thread_id": thread_id,
            "task": task,
            "repo": repo,
            "generated_at": generated_at,
            "source": MANUAL_SOURCE,
            "scope_file": scope_file,
        },
        "approval": {
            "source": MANUAL_SOURCE,
            "approved_scope": approved_scope,
        },
        "implementation_instructions": {
            "tasks": tasks,
            "files_likely_touched": files,
            "out_of_scope": out_of_scope,
            "tests_to_run": tests,
            "safety_rules": safety,
        },
        "safety_boundaries": safety,
        "suggested_prompt": suggested,
    }


def render_manual_markdown(packet: dict) -> str:
    """Render a manual-scope packet as Markdown. Untrusted scope text is sanitized via ``_md_safe``."""
    m = packet.get("metadata", {})
    ap = packet.get("approval", {})
    ii = packet.get("implementation_instructions", {})
    out = [
        "# Implementation Packet (manual human scope)",
        "",
        f"> **Source: `{packet.get('source')}`** — created from a human-provided scope file, NOT a "
        "Codex advisory. devflow does not edit repository files itself; Claude Code implements within "
        "the scope and safety boundaries below.",
        "",
        "## Metadata",
        f"- thread_id: `{_md_safe(m.get('thread_id'))}`",
        f"- task: {_md_safe(m.get('task'))}",
        f"- repo: {_md_safe(m.get('repo'))}",
        f"- generated_at: {_md_safe(m.get('generated_at'))}",
        f"- source: {_md_safe(m.get('source'))}",
    ]
    if m.get("scope_file"):
        out.append(f"- scope_file: {_md_safe(m.get('scope_file'))}")
    out += [
        "",
        "## Approved scope",
        _md_list(ap.get("approved_scope")),
        "",
        "## Tasks",
        _md_list(ii.get("tasks")),
        "",
        "## Files likely touched",
        _md_list(ii.get("files_likely_touched")),
        "",
        "## Out of scope",
        _md_list(ii.get("out_of_scope")),
        "",
        "## Tests / checks to run",
        _md_list(ii.get("tests_to_run")),
        "",
        "## Safety boundaries",
        _md_list(packet.get("safety_boundaries")),
        "",
        "## Suggested Claude Code prompt",
        _md_safe(packet.get("suggested_prompt")),
        "",
    ]
    return "\n".join(out)
