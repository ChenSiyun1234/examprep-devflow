# -*- coding: utf-8 -*-
"""Deterministic priority scoring for "which PR should Codex review next".

Pure stdlib, NO model call — a transparent, testable heuristic (devflow stays zero-dependency and
reproducible). Two axes, exactly as specified:

* **review coverage** — a never-reviewed PR needs review most; the need DECAYS as Codex review
  rounds accrue, so a heavily-reviewed PR yields its turn to fresher ones (fair sharing).
* **impact / type** — a big FEATURE has more new surface to review than a small BUG FIX.

``priority = 6*needs_review + 4*impact + type_adjust`` (clamped 0..100). Higher = review sooner.
The weights are deliberately simple and documented so the ranking is explainable and unit-testable.
"""
from __future__ import annotations

import re

# A "fix"-shaped change is deprioritized vs a feature — UNLESS it is large (a big "fix" is really a
# feature's worth of surface). Match common English + 中文 markers in the title/branch.
_BUGFIX_RE = re.compile(r"\b(fix|bugfix|hotfix|patch|typo|regression|revert)\b|修复|修正", re.I)
_FEATURE_RE = re.compile(r"\b(feat|feature|add|adds|implement|support|introduce)\b|新增|实现|支持", re.I)
_BIG_CHANGE = 400   # additions at/above this make a "fix" count as a feature (don't deprioritize)


def classify_type(title: str, branch: str, additions: int, deletions: int = 0) -> str:
    """'feature' | 'bugfix' | 'mixed' from the title + branch name (and size as a tie-breaker)."""
    text = f"{title or ''} {branch or ''}"
    bug = bool(_BUGFIX_RE.search(text))
    feat = bool(_FEATURE_RE.search(text))
    # size = additions + deletions so a big REMOVAL-heavy fix (e.g. "fix: drop legacy engine" +0/-2000)
    # isn't deprioritized as a small bugfix.
    if bug and (int(additions or 0) + int(deletions or 0)) < _BIG_CHANGE:
        return "bugfix"
    if feat:
        return "feature"
    return "mixed"


def score(additions=0, deletions=0, changed_files=0, title="", branch="",
          codex_rounds=0, reviewed_on_head=False) -> dict:
    """Score one PR's review priority from plain metadata (trivially unit-testable, no network).

    Returns ``{priority, needs_review, impact, type, codex_rounds}``.
    """
    rounds = max(0, int(codex_rounds or 0))
    # never reviewed -> max need; each prior round lowers it; a PR whose CURRENT head is already
    # reviewed drops further (its latest code has been seen -> little reason to request again now).
    needs_review = 10 if rounds == 0 else max(1, 9 - rounds)
    if reviewed_on_head:
        needs_review = max(0, needs_review - 4)
    # deletions count toward blast radius too (a removal-heavy PR still needs review), at half weight.
    churn = (max(0, int(additions or 0)) + max(0, int(deletions or 0)) // 2
             + 30 * max(0, int(changed_files or 0)))
    impact = min(10, round(churn / 120))                       # 0..10 rough blast radius
    ptype = classify_type(title, branch, additions, deletions)
    type_adjust = {"feature": 1, "mixed": 0, "bugfix": -2}[ptype]
    priority = max(0, min(100, 6 * needs_review + 4 * impact + type_adjust))
    return {"priority": priority, "needs_review": needs_review, "impact": impact,
            "type": ptype, "codex_rounds": rounds}
