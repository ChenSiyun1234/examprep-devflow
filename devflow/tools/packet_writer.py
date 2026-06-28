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
                    return rest.strip()
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
    # reject ``..`` (traversal), ``.`` (current dir / whole repo), and empty (``//``) segments
    return not any(seg.strip() in ("", "..", ".") for seg in q.split("/"))


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

    # advisory steps/summary are UNTRUSTED checkpoint content too — sanitize any embedded unsafe path
    # prefix here, at the source, so every downstream use (approved_scope/tasks/fallback) is clean.
    steps = [_strip_unsafe_path_prefix(str(s)) for s in _as_list(advisory.get("recommended_steps"))]
    summary = advisory.get("summary") if isinstance(advisory.get("summary"), str) else None
    if summary:
        summary = _strip_unsafe_path_prefix(summary)

    # Blocking comments are actionable ONLY at the blocking-fix gate. At the merge gate they are
    # already-resolved history, and at the advisory gate they don't exist yet — turning them into
    # tasks there would tell Claude Code to (re-)edit resolved/irrelevant findings (scope creep).
    is_fix_gate = gate == "blocking_fix"

    # Approved scope: advisory steps (or, for a REAL advisory that only has a summary, the summary)
    # at the advisory gate, plus the blocking fixes at the fix gate.
    approved_scope = []
    if gate == "advisory_implementation":
        approved_scope += steps or ([summary] if summary else [])
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
    if gate == "advisory_implementation" and not tasks and summary:
        tasks = [summary]

    # files_likely_touched = edit targets. Only the (fix-gate) blocking comments + an advisory's own
    # file list contribute — NOT non-blocking comments (those are optional/out-of-scope). These paths
    # come from UNTRUSTED Codex content, so absolute paths and `..` traversal are rejected; rejected
    # paths are surfaced in out-of-scope, not silently dropped.
    raw_files = []
    if is_fix_gate:
        raw_files += [c["path"] for c in blocking
                      if isinstance(c, dict) and isinstance(c.get("path"), str)]
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
            level = len(stripped) - len(stripped.lstrip("#"))
            if level == 1:                         # only a top-level "# Heading" starts a section
                heading = stripped.lstrip("#").strip()
                current = _SCOPE_SECTION_ALIASES.get(heading.lower())
                if current:
                    sections.setdefault(current, [])
                elif heading:
                    unknown_headings.append(heading)   # surfaced (CLI warns) so its body isn't lost silently
                continue
            # a sub-heading ("## …") inside a section is CONTENT, not a section break — keep its text
            stripped = stripped.lstrip("#").strip()
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
    "pytest", "py.test", "unittest",
    "npm test", "npm run ", "yarn ", "pnpm ", "make ", "tox", "nox",
    "ruff", "flake8", "pylint", "mypy", "black --check", "isort --check",
    "pre-commit run", "cargo test", "cargo check", "go test", "go vet",
)
# `python -m <module>` is only a CHECK when <module> is a known validation runner — otherwise it can
# launch arbitrary code (`python -m http.server`, `python -m pip`, `python -m venv`, `python -m this`).
_PY_RUNNERS = ("python -m", "python3 -m", "py -m")
_PY_VALIDATION_MODULES = {"pytest", "unittest", "mypy", "ruff", "flake8", "pylint", "black", "isort",
                          "tox", "nox", "coverage", "doctest", "compileall", "pyflakes", "pytype", "bandit"}


_SHELL_META = ("&&", "||", ";", "|", "&", "`", "$(", "${", ">", "<", "\n", "\r")
# mutating / network sub-actions that must never be promoted into a "check" instruction, even under
# an allow-listed prefix (e.g. "npm run deploy", "pip install …", "ruff --fix").
# mutating / network sub-actions that must never be promoted into a "check" instruction, even under
# an allow-listed prefix (e.g. "make push", "npm run deploy", "make clean", "npm start", "ruff --fix").
# Word-bounded so a benign substring (e.g. "clean" in a test path "test_cleanup") doesn't trip it.
_SIDE_EFFECT_RE = re.compile(
    r"\b(install|uninstall|deploy|publish|release|upgrade|push|clean|delete|remove|merge|start|serve)\b",
    re.I)
_SIDE_EFFECT_FLAGS = ("--write", "--fix", "--apply")


def _check_is_allowlisted(cmd) -> bool:
    c = str(cmd).strip().lower()
    # reject shell chaining/redirection/substitution so an allow-listed prefix can't smuggle a second
    # command (e.g. "pytest && gh pr merge", "python -m x; rm -rf .", "make $(curl ...)").
    if any(meta in c for meta in _SHELL_META):
        return False
    if _SIDE_EFFECT_RE.search(c) or any(f in c for f in _SIDE_EFFECT_FLAGS):  # mutating sub-action -> not a check
        return False
    # 'python -m <module>': only a check if <module> is a known validation runner (not http.server/pip/venv).
    for runner in _PY_RUNNERS:
        if c == runner or c.startswith(runner + " "):
            rest = c[len(runner):].strip()
            return bool(rest) and rest.split()[0] in _PY_VALIDATION_MODULES
    # exact match OR prefix followed by a space — a bare-word boundary so "pytestfoo" / "makefile…"
    # can't sneak through a startswith on "pytest" / "make".
    return any(c == p.strip() or c.startswith(p.strip() + " ") for p in _SAFE_CHECK_PREFIXES)


# Words that PERMIT/override an action paired with a protected action = a scope rule trying to weaken
# a hard boundary (e.g. "commits are allowed", "ignore the no-push rule"). Such rules are quarantined.
_SAFETY_PERMISSIVE = (
    "allow", "allowed", "ok to", "okay to", "fine to", "is fine", "are fine", "is ok", "is okay",
    "are ok", "can ", "may ", "enable", "permit", "permitted", "ignore", "skip", "disable",
    "override", "no need", "no restriction", "no limit", "not required", "no requirement",
    "not necessary", "not mandatory", "optional", "feel free", "you can", "it's ok",
    "without restriction", "go ahead",
)
# Verbs/markers that make a protected-action rule a genuine PROHIBITION (it reinforces a boundary)
# rather than a permission. Permissive markers are checked FIRST, so "no restriction on pushing" is
# caught as permissive before "no" here would (mis)read it as prohibitive.
_SAFETY_PROHIBITIVE_RE = re.compile(
    r"\b(do\s+not|don'?t|never|no|not|disallow|must\s+not|may\s+not|cannot|can'?t|avoid|forbid|"
    r"prohibit|refuse|reject)\b", re.I)
# Protected git/PR/secret actions, with common INFLECTIONS (commit/commits/committed/committing,
# push/pushes, merge/merging, PR/PRs, …) — a permissive rule mentioning any of these would weaken a
# hard boundary. Inflection-aware so "commits are allowed" / "pushes are fine" are caught, not just
# the bare singular.
_SAFETY_PROTECTED_RE = re.compile(
    r"\b(commit(s|ted|ting)?|push(es|ed|ing)?|merg(e|es|ed|ing)|branch(es)?|delet(e|es|ed|ing)|"
    r"force[-\s]?push(es|ed|ing)?|secrets?|api[-\s]?keys?|tokens?|destructive|shell|"
    r"tests?|checks?|refactor(s|ing|ed)?|rewrite(s|ing)?|scope|"          # non-git hard boundaries too
    r"prs?|pull[-\s]?requests?)\b", re.I)


def _scope_safety_rule_conflicts(rule) -> bool:
    """True if a scope-file safety rule must NOT be appended to the canonical boundaries. It is
    PROTECTED-ACTION-driven: any rule naming a protected action (push/merge/commit/branch-delete/
    force-push/secret/PR…) is quarantined UNLESS it is clearly PROHIBITIVE. So permissive overrides
    ('push to origin when done', 'merging is ok', 'you can push') AND ambiguous protected-action rules
    are quarantined, while genuine prohibitions ('do not push', 'never delete branches', 'no secrets')
    are kept. When intent is unclear, quarantine — the canonical SAFETY_BOUNDARIES still apply."""
    low = str(rule).lower()
    if not _SAFETY_PROTECTED_RE.search(low):
        return False                                       # not about a protected action -> keep as-is
    if any(w in (" " + low + " ") for w in _SAFETY_PERMISSIVE):
        return True                                        # explicit permission -> quarantine
    if _SAFETY_PROHIBITIVE_RE.search(low):
        return False                                       # explicit prohibition -> keep (reinforces boundary)
    return True                                            # protected action, ambiguous intent -> quarantine


# A task line that IS itself a prohibited git/PR action (e.g. "merge the PR", "git push",
# "open a pull request") must not be handed to Claude Code as work — it's quarantined to out-of-scope.
_PROHIBITED_TASK_RE = re.compile(
    r"\b("
    r"git\s+(commit|push|merge|rebase|cherry-?pick)"                       # git <subcommand> (command form)
    r"|git\s+branch\s+(-[dD]\b|--delete\b)"                                # git branch -D / -d / --delete
    r"|gh\s+pr\s+(merge|create|ready|review|close)"                        # gh pr <subcommand>
    r"|gh\s+(release|workflow|run|api)\b"                                  # gh release/workflow/run/api
    r"|push(es|ing)?\s+(\w+\s+){0,2}(branch|changes|commits?|code|origin|remote|upstream)"
    r"|force[-\s]?push(es|ing)?"
    r"|(?<!pre-)commit(s|ting)?\b"                                         # bare 'commit' (NOT 'pre-commit')
    r"|merg(e|es|ing)\s+(the\s+|this\s+)?(pr|pull[-\s]request|branch)"
    r"|(open|create|raise|submit)\s+(a\s+|the\s+)?(pr|pull[-\s]request)"
    r"|delete\s+(the\s+)?branch|rebase|cherry[-\s]?pick"
    r"|github\s*actions|actions?\s+workflow|workflow\s+(file|yaml|yml)"    # GitHub Actions (boundary: never add)
    r"|ci\s+(workflow|pipeline)"
    r")", re.I)


# a workflow path can appear bare in a task (".github/workflows/...") where the leading \b of the
# alternation above can't anchor (the path starts with '.'); match it independently.
_WORKFLOW_PATH_RE = re.compile(r"\.github[/\\]workflows", re.I)


def _task_is_prohibited(t) -> bool:
    s = str(t)
    return bool(_PROHIBITED_TASK_RE.search(s)) or bool(_WORKFLOW_PATH_RE.search(s))


def _is_bare_unsafe_path(t) -> bool:
    """True if a task/scope item is ITSELF a bare unsafe path ('/etc/passwd', '../x.py', 'C:\\…')
    rather than prose — so it is never handed off as an edit target. Path-SHAPED only: ordinary prose
    (which also fails _is_safe_rel_path on a '%' or 'A:' substring) is NOT quarantined."""
    s = str(t).strip()
    looks_path = ("/" in s or "\\" in s or s.startswith(("~", "..", "$", "%"))
                  or (len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "\\/"))
    return bool(s) and looks_path and not _is_safe_rel_path(s)


def build_manual_packet(thread_id, task, repo, generated_at, scope: dict,
                        scope_file: Optional[str] = None) -> dict:
    """Build an Implementation Packet from explicit human-provided scope (source = manual_human_scope).

    The canonical SAFETY_BOUNDARIES are ALWAYS enforced — an incomplete scope file can add rules but
    can never remove them. File paths are filtered (no absolute/``..``) just like the export path."""
    scope = scope if isinstance(scope, dict) else {}
    # coerce every section to a list (a hand-written/foreign scope dict may carry None or a bare str)
    approved_scope = [s for s in _as_list(scope.get("approved_scope")) if str(s).strip()]
    tasks = [t for t in _as_list(scope.get("tasks")) if str(t).strip()] or list(approved_scope)
    # quarantine any scope item that IS a prohibited git/PR action — it must never be handed off as work;
    # sanitize a 'PATH: detail' unsafe-path prefix out of the kept items so a manual scope can't direct
    # edits outside the repo (mirrors how advisory-derived tasks are sanitized).
    prohibited = [t for t in (tasks + approved_scope) if _task_is_prohibited(t)]
    unsafe_tasks = [t for t in (tasks + approved_scope)
                    if not _task_is_prohibited(t) and _is_bare_unsafe_path(t)]
    _bad = lambda t: _task_is_prohibited(t) or _is_bare_unsafe_path(t)
    tasks = [_strip_unsafe_path_prefix(str(t)) for t in tasks if not _bad(t)]
    approved_scope = [_strip_unsafe_path_prefix(str(s)) for s in approved_scope if not _bad(s)]
    # if an explicit Tasks section existed but EVERY entry was quarantined, fall back to the (filtered)
    # approved scope as tasks — never emit an empty 'do ONLY the listed tasks' handoff.
    if not tasks and approved_scope:
        tasks = list(approved_scope)

    raw_files = [str(f) for f in _as_list(scope.get("files")) if str(f).strip()]
    # devflow must never add/modify GitHub Actions — a workflow file is repo-relative (passes the
    # path filter) but is quarantined as an edit target.
    def _is_workflow(p):
        # match the workflows DIRECTORY itself (bare, no trailing slash) AND any file under it, so a
        # bare ".github/workflows" target can't slip past a trailing-slash-only check.
        pn = p.replace("\\", "/").lower()
        return pn.rstrip("/").endswith(".github/workflows") or ".github/workflows/" in pn
    files = sorted({p for p in raw_files if _is_safe_rel_path(p) and not _is_workflow(p)})
    unsafe = sorted({p for p in raw_files if not _is_safe_rel_path(p)})
    workflows = sorted({p for p in raw_files if _is_safe_rel_path(p) and _is_workflow(p)})

    out_of_scope = [str(s) for s in _as_list(scope.get("out_of_scope")) if str(s).strip()]
    out_of_scope += [f"(ignored unsafe path — outside repo, do NOT touch) {p}" for p in unsafe]
    out_of_scope += [f"(ignored target — GitHub Actions workflow, do NOT touch) {p}" for p in workflows]
    out_of_scope += [f"(ignored task — prohibited git/PR action, do NOT perform) {t}" for t in prohibited]
    out_of_scope += [f"(ignored task — unsafe path outside repo, do NOT touch) {t}" for t in unsafe_tasks]

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
