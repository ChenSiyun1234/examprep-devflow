# -*- coding: utf-8 -*-
"""Build a copyable GPT/ChatGPT FALLBACK code-review prompt for a PR, from READ-ONLY GitHub data.

For when Codex review is quota-limited / unavailable, or the human wants a manual second opinion. This
module **NEVER** calls an LLM (no OpenAI / Anthropic / Codex API, no SDK import, no API-key env var),
**NEVER** sends code anywhere, and performs **NO** GitHub writes — it only READS via
:class:`ReadOnlyGitHub` (gh pr view / gh pr diff / gh api GET) and assembles TEXT the human copies and
pastes themselves. Pure stdlib.
"""

from __future__ import annotations

import re
from typing import Optional

from devflow.tools.github_cli import (
    ReadOnlyGitHub, GhError, is_codex_author, is_codex_quota_notice,
)

# diff char budgets per the page's "diff budget" control; the prompt is capped to these.
DIFF_BUDGETS = {"compact": 8000, "medium": 20000, "large": 50000}
FEEDBACK_BUDGET = 4000
BODY_BUDGET = 2000
FOCUS_MODES = ("general", "safety", "tests", "docs", "verify-fix")

# parse changed-file paths from the FULL unified diff (so the file list is complete even when the diff
# excerpt that goes into the prompt is later truncated).
_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)

_FOCUS_INSTRUCTIONS = {
    "general": "Review focus: correctness, regression risk, edge cases, and missing tests.",
    "safety": ("Review focus: security and local-dashboard safety — CSRF, XSS / output escaping, "
               "localhost-only binding, and GitHub write / permission boundaries."),
    "tests": "Review focus: missing tests, flaky tests, fixture / coverage gaps, and CI assumptions.",
    "docs": ("Review focus: documentation accuracy — user-facing claims and any mismatch between the "
             "docs and the actual behavior."),
    "verify-fix": ("Review focus: verify ONLY whether the listed review comments were actually "
                   "addressed by this diff. Do NOT expand scope or raise unrelated findings."),
}

_BASE_INSTRUCTIONS = """\
You are reviewing a pull request. Review ONLY the provided PR metadata and diff excerpt. Do not assume \
files or context not shown. If context is insufficient, say so explicitly.

Output findings in this format:
- Summary
- Blocking findings
- Non-blocking findings
- Tests to add or run
- Questions / missing context

For each finding:
- severity: P1 / P2 / P3
- file / path if known
- line or diff hunk if known
- why it matters
- concrete suggested fix

Do not claim tests passed unless this prompt includes evidence that they ran.
Do not suggest merging.
Do not suggest deleting branches.
Do not suggest force-push.
Do not suggest adding GitHub Actions unless the PR explicitly asks for CI changes.
Do not request secrets or API keys."""

_DEVFLOW_REMINDERS = """\
devflow project safety reminders (the PR must respect these):
- devflow must not merge without explicit human approval.
- no branch deletion; no force-push; no automatic commit / push / merge.
- no secrets / API keys.
- no GitHub Actions unless explicitly requested.
- prefer read-only / dry-run behavior unless the PR scope explicitly says otherwise."""


def _clip(text: str, budget: int):
    """Return (clipped_text, truncated_bool). Never silently drops content — the caller surfaces the flag."""
    text = text or ""
    if len(text) <= budget:
        return text, False
    return text[:budget], True


def _changed_files(diff: str) -> list:
    """Changed file paths parsed from the full unified diff (order-preserving, deduped)."""
    out, seen = [], set()
    for a, b in _DIFF_FILE_RE.findall(diff or ""):
        path = a if b == "/dev/null" else b           # deletes show b=/dev/null -> use the a-side path
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _build_feedback(gh, pr_number):
    """Existing Codex feedback (latest review summary + recent inline + recent conversation comments),
    most-recent-preferred and capped. Returns (text, truncated). Read-only; tolerant of gh failure."""
    try:
        sigs = gh.get_pr_codex_signals(pr_number)
    except GhError:
        return "", False

    def keep(items):
        return [c for c in (items or [])
                if is_codex_author(c.get("author")) and not is_codex_quota_notice(c.get("body"))]

    def first_line(c):
        body = (c.get("body") or "").strip()
        return body.splitlines()[0] if body else ""

    reviews = sorted([r for r in keep(sigs.get("reviews")) if (r.get("body") or "").strip()],
                     key=lambda r: r.get("created_at") or "")
    inline = sorted(keep(sigs.get("inline")), key=lambda c: c.get("created_at") or "")
    convs = sorted([c for c in keep(sigs.get("comments")) if (c.get("body") or "").strip()],
                   key=lambda c: c.get("created_at") or "")

    parts = []
    if reviews:
        parts.append("Latest Codex review summary:\n" + (reviews[-1].get("body") or "").strip())
    if inline:
        rows = ["- %s: %s" % (c.get("path") or "?", first_line(c)) for c in inline[-10:]]
        parts.append("Recent Codex inline comments (most recent last):\n" + "\n".join(rows))
    if convs:
        parts.append("Recent Codex conversation comments (most recent last):\n"
                     + "\n".join("- " + first_line(c) for c in convs[-5:]))
    return _clip("\n\n".join(parts), FEEDBACK_BUDGET)


def _assemble(repo, ov, private, focus, files, diff_excerpt, diff_truncated,
              feedback, feedback_truncated, include_feedback) -> str:
    out = [_BASE_INSTRUCTIONS, "", _FOCUS_INSTRUCTIONS[focus], "", _DEVFLOW_REMINDERS, ""]
    if private:
        out += ["NOTE: This PR is from a PRIVATE / proprietary repository. Confirm sharing is permitted "
                "before pasting this prompt or diff into any external tool.", ""]
    out += ["## PR metadata",
            "- repo: %s" % repo,
            "- PR: #%s" % ov.get("number"),
            "- url: %s" % (ov.get("url") or ""),
            "- title: %s" % (ov.get("title") or ""),
            "- base <- head: %s <- %s" % (ov.get("base_ref") or "?", ov.get("head_ref") or "?"),
            "- head SHA: %s" % (ov.get("head_oid") or "?"),
            "- changed files (%d):" % len(files)]
    out += (["  - %s" % f for f in files] or ["  (file list unavailable)"])
    body = (ov.get("body") or "").strip()
    if body:
        clipped, _ = _clip(body, BODY_BUDGET)
        out += ["", "## PR description (excerpt)", clipped]
    if include_feedback:
        out += ["", "## Existing Codex feedback", feedback if feedback else "(none found)"]
        if feedback_truncated:
            out.append("(Existing feedback was truncated; older signals omitted.)")
    out += ["", "## Diff excerpt", "```diff", diff_excerpt if diff_excerpt else "(no diff available)", "```"]
    if diff_truncated:
        out.append("Diff was truncated. Do not make claims about omitted sections.")
    return "\n".join(out)


def build_fallback_review_prompt(repo: str, pr_number: int, focus: str = "general",
                                 diff_budget: str = "compact",
                                 include_existing_feedback: bool = True) -> dict:
    """Build the structured fallback-review prompt for ``repo`` PR ``pr_number``. READ-ONLY. Returns a
    dict (repo, pr_number, pr_url, title, base, head, head_sha, changed_files, diff_chars,
    diff_truncated, feedback_truncated, private_repo_warning, focus, diff_budget, prompt). Raises
    GhError on a fatal gh failure (repo resolve / pr view)."""
    focus = focus if focus in FOCUS_MODES else "general"
    diff_budget = diff_budget if diff_budget in DIFF_BUDGETS else "compact"
    budget = DIFF_BUDGETS[diff_budget]

    gh = ReadOnlyGitHub(repo)
    repo_full = gh.resolve_repo()
    private = bool((gh.get_repo_info() or {}).get("private"))
    ov = gh.get_pr_overview(pr_number)
    try:
        diff = gh.get_pr_diff(pr_number) or ""
    except GhError:
        diff = ""                                      # diff unavailable -> empty excerpt, file list may be 0
    changed_files = _changed_files(diff)
    diff_excerpt, diff_truncated = _clip(diff, budget)
    feedback, feedback_truncated = ("", False)
    if include_existing_feedback:
        feedback, feedback_truncated = _build_feedback(gh, pr_number)

    prompt = _assemble(repo_full, ov, private, focus, changed_files, diff_excerpt, diff_truncated,
                       feedback, feedback_truncated, include_existing_feedback)
    return {
        "repo": repo_full, "pr_number": int(pr_number), "pr_url": ov.get("url"),
        "title": ov.get("title"), "base": ov.get("base_ref"), "head": ov.get("head_ref"),
        "head_sha": ov.get("head_oid"), "changed_files": changed_files,
        "diff_chars": len(diff_excerpt), "diff_truncated": diff_truncated,
        "feedback_truncated": feedback_truncated, "private_repo_warning": private,
        "focus": focus, "diff_budget": diff_budget, "prompt": prompt,
    }
