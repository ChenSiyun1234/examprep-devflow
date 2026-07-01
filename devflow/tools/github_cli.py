"""GitHub tool layer for devflow.

Two clearly separated pieces:

* :class:`DryRunGitHub` — *write* operations (issue/PR/branch/merge). In this scaffold these are
  recorded no-ops; a future, explicitly-flagged PR will add a real backend.
* Read-only layer (:class:`ReadOnlyGitHub` + :func:`check_gh_available`) — inspects issues, PRs,
  comments and reviews via the ``gh`` CLI. **Strictly read-only**: every ``gh`` invocation passes
  through :func:`_assert_read_only`, which refuses anything that is not an allow-listed read shape
  (and refuses ``gh api`` with a non-GET method or write-style ``-f/--field`` flags). There is no
  code path here that can create, comment, push, or merge.
"""

from __future__ import annotations

import calendar
import json
import re
import shutil
import subprocess
import time
from typing import Optional

# a quota notice older than this no longer counts as a CURRENT rate-limit (so a stale notice can't
# suppress a PR forever once Codex's limit has reset — codex_review_rounds has no seen-state/expiry).
_QUOTA_ACTIVE_TTL_SECS = 3 * 3600

# Codex sometimes delivers a review as a CONVERSATION comment carrying a 'Reviewed commit: <sha>' marker
# (not a review object). Same pattern review_orchestrator.py uses, so coverage counting agrees with it:
# a marked comment is a real review round (of that sha); a generic Codex comment is NOT.
_REVIEWED_COMMIT_RE = re.compile(r"reviewed commit:?\**\s*`?([0-9a-f]{7,40})", re.I)


# ======================================================================================
# Write layer (recorded no-ops in this scaffold) — unchanged from the initial scaffold
# ======================================================================================
class DryRunGitHub:
    """Records intended GitHub *write* operations without performing them."""

    def __init__(self, repo: str):
        self.repo = repo
        self.calls: list[dict] = []
        self._issue_seq = 1000
        self._pr_seq = 2000

    def _record(self, op: str, **kwargs) -> dict:
        entry = {"op": op, "repo": self.repo, **kwargs, "executed": False, "dry_run": True}
        self.calls.append(entry)
        return entry

    def create_issue(self, title: str, body: str, labels: Optional[list] = None) -> dict:
        self._issue_seq += 1
        n = self._issue_seq
        self._record("create_issue", title=title, labels=labels or [])
        return {"number": n, "url": f"https://github.com/{self.repo}/issues/{n}", "simulated": True}

    def comment(self, target: str, number: int, body: str) -> dict:
        self._record("comment", target=target, number=number, body_preview=body[:80])
        return {"posted": False, "simulated": True}

    def create_branch(self, name: str, base: str = "main") -> dict:
        self._record("create_branch", name=name, base=base)
        return {"branch": name, "base": base, "simulated": True}

    def push_branch(self, name: str) -> dict:
        self._record("push_branch", name=name)
        return {"pushed": False, "simulated": True}

    def create_pr(self, head: str, base: str, title: str, body: str, draft: bool = True) -> dict:
        self._pr_seq += 1
        n = self._pr_seq
        self._record("create_pr", head=head, base=base, title=title, draft=draft)
        return {"number": n, "url": f"https://github.com/{self.repo}/pull/{n}",
                "draft": draft, "simulated": True}

    def merge_pr(self, number: int, method: str = "squash") -> dict:
        self._record("merge_pr", number=number, method=method)
        return {"merged": False, "simulated": True, "note": "dry-run: merge not executed"}


# ======================================================================================
# Read-only layer
# ======================================================================================
class GhError(RuntimeError):
    """Raised for gh unavailability, auth failure, refused (non-read-only) commands, or gh errors."""


# allow-listed read-only command shapes (matched on the leading 1-2 tokens)
_ALLOWED_READ_PREFIXES = {
    ("auth", "status"),
    ("repo", "view"),
    ("issue", "view"),
    ("pr", "view"),
    ("pr", "diff"),
    ("pr", "list"),
    ("issue", "list"),
    ("api",),  # GET only — enforced below
}
# flags that turn `gh api` into a write (gh api defaults to POST when any field is supplied)
_API_WRITE_FLAGS = {"-f", "--field", "-F", "--raw-field", "--input"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _assert_read_only(args: list[str]) -> None:
    """Refuse any gh invocation that is not an allow-listed read. The single safety chokepoint."""
    if not args:
        raise GhError("empty gh command")
    if args[0] == "api":
        for i, tok in enumerate(args):
            base = tok.split("=", 1)[0]               # handle both `--field x` and `--field=x`
            if base in _API_WRITE_FLAGS:
                raise GhError(f"refused: write-style `gh api` flag {tok!r}")
            # compact short field flags with an attached value: -fbody=hi, -Ffoo=bar
            if len(tok) > 2 and tok[0] == "-" and tok[1] in ("f", "F") and tok[2] != "-":
                raise GhError(f"refused: write-style `gh api` field flag {tok!r}")
            # method via -X/--method (space, `=`, or attached compact form -XPUT)
            method = None
            if base in ("-X", "--method"):
                method = tok.split("=", 1)[1] if "=" in tok else (args[i + 1] if i + 1 < len(args) else "")
            elif tok.startswith("-X") and len(tok) > 2:
                method = tok[2:]
            if method and method.upper() in _WRITE_METHODS:
                raise GhError(f"refused: `gh api` method {method.upper()}")
        return
    if tuple(args[:2]) in _ALLOWED_READ_PREFIXES or (args[0],) in _ALLOWED_READ_PREFIXES:
        return
    raise GhError(f"refused: non-read-only gh command: {' '.join(args[:3])}…")


def _spawn_gh(args: list[str], timeout: int = 60) -> str:
    """Actually run ``gh`` and return stdout. Callers MUST gate args first (read or write guard)."""
    if shutil.which("gh") is None:
        raise GhError("gh CLI not found on PATH. Install it and run `gh auth login`.")
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as e:  # pragma: no cover - covered by shutil.which guard
        raise GhError(f"gh CLI not found: {e}")
    except subprocess.TimeoutExpired:
        raise GhError(f"gh command timed out after {timeout}s: {' '.join(args[:3])}…")
    if proc.returncode != 0:
        raise GhError((proc.stderr or proc.stdout or "gh command failed").strip())
    return proc.stdout


def _run_gh(args: list[str], timeout: int = 60) -> str:
    """Run an allow-listed read-only ``gh`` command and return stdout. Raises :class:`GhError`."""
    _assert_read_only(args)               # safety gate BEFORE any process is spawned
    return _spawn_gh(args, timeout=timeout)


def _gh_json(args: list[str], timeout: int = 60):
    out = _run_gh(args, timeout=timeout)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as e:
        raise GhError(f"could not parse gh JSON output: {e}")


def _flatten_pages(slurped):
    """`gh api --paginate --slurp` yields one JSON array whose elements are per-page responses
    (each itself an array). Flatten one level. Tolerant of an already-flat list."""
    if isinstance(slurped, list) and slurped and isinstance(slurped[0], list):
        return [item for page in slurped for item in page]
    if isinstance(slurped, list):
        return slurped
    return [] if slurped is None else [slurped]


def _gh_json_paginated(path: str, timeout: int = 60):
    """Paginated GET pinned to github.com (so a stray ``GH_HOST`` can't redirect the read to an
    Enterprise host after a github.com auth check), kept valid JSON across pages via ``--slurp``."""
    return _flatten_pages(_gh_json(
        ["api", "--hostname", "github.com", path, "--paginate", "--slurp"], timeout=timeout))


def check_gh_available() -> dict:
    """Report gh availability + authentication. Never raises — returns a structured status."""
    if shutil.which("gh") is None:
        return {"available": False, "authenticated": False,
                "error": "gh CLI not found on PATH. Install GitHub CLI and run `gh auth login`."}
    try:
        # scope to github.com: `gh auth status` (no host) exits non-zero if ANY known host (e.g. a
        # stale Enterprise login) has issues, which would wrongly block valid github.com reads.
        out = _run_gh(["auth", "status", "--hostname", "github.com"])
    except GhError as e:
        return {"available": True, "authenticated": False,
                "error": f"gh is installed but not authenticated for github.com: {e}"}
    account = None
    m = re.search(r"account\s+(\S+)", out) or re.search(r"Logged in to \S+ account (\S+)", out)
    if m:
        account = m.group(1)
    return {"available": True, "authenticated": True, "account": account, "error": None}


# -- Codex detection / parsing ---------------------------------------------------------
# Exact, trusted Codex/ChatGPT-connector logins. We match EXACTLY (case-insensitive) rather than
# "login contains codex/chatgpt", because on a public repo anyone could pick a login like
# "codex-fan" and spoof the integration.
_TRUSTED_CODEX_LOGINS = {
    "chatgpt-codex-connector[bot]", "chatgpt-codex-connector",
    "codex", "codex[bot]",
}


def is_codex_author(login: Optional[str]) -> bool:
    return bool(login) and login.strip().lower() in _TRUSTED_CODEX_LOGINS


def is_codex_quota_notice(body: Optional[str]) -> bool:
    """True if a trusted-Codex message is the "you've hit your usage limits" rate-limit notice
    (e.g. "You have reached your Codex usage limits for code reviews."). This is NOT a review:
    callers must not treat it as actionable, and can use it to back off. Matched precisely (the
    limit phrase AND a code-review/codex context) so a real finding that merely mentions a "usage
    limit" in the code under review isn't misread as rate-limiting."""
    t = (body or "").strip().lower()
    if not t:
        return False
    # Require the COMPLETE bot-notice shape, not just the opening sentence: the canonical notice is
    # "You have reached your Codex usage limits for code reviews. You can see your limits in the …".
    # A real review that QUOTES only the first sentence and then gives actual feedback (still under 600
    # chars) lacks the "you can see your limits" continuation, so it isn't dropped as a rate-limit notice.
    return (t.startswith("you have reached your codex usage limits for code review")
            and "you can see your limits" in t and len(t) < 600)


# Tie-break for "latest" when GitHub's 1-second timestamps collide (Codex posts a review plus
# several review-comments in the same second). A higher rank + url makes a *new* same-second signal
# win the tie so dedupe keys advance and the new feedback is not silently swallowed.
_CODEX_SOURCE_RANK = {"pr_review": 3, "pr_review_comment": 2, "pr_comment": 1}


def parse_review_packet(body: str, state: Optional[str] = None) -> dict:
    """Light, defensive parse of a Codex review body into a structured-ish packet.

    Heuristics only (no model call): a review is treated as *blocking* if its review state is
    CHANGES_REQUESTED or the body mentions blocking language. Bullet lines are collected as items.
    """
    text = body or ""
    low = text.lower()
    # A CHANGES_REQUESTED review state is authoritative. Otherwise use the text heuristic, but let
    # explicit negations ("no blocking issues", "not blocking", "do not request changes") win so a
    # clean review isn't mis-flagged. (?<!non-) keeps "non-blocking" from matching either.
    state_blocking = (state or "").upper() == "CHANGES_REQUESTED"
    # allow optional words (e.g. "a", "any", "major") between the negator and the blocking term:
    # "not a blocking issue", "not a required change", "no major blocking concerns", etc.
    negated = bool(re.search(
        r"\bno\s+(?:\w+\s+){0,3}blocking\b|\bnot\s+(?:\w+\s+){0,3}blocking\b"
        r"|\bno\s+(?:\w+\s+){0,3}required\s+changes?\b|\bnot\s+(?:\w+\s+){0,3}required\s+changes?\b"
        r"|\b(?:do|does|did)\s+not\s+request\s+changes\b|\bdon'?t\s+request\s+changes\b"
        r"|\bno\s+changes?\s+requested\b", low))
    text_blocking = bool(
        re.search(r"(?<!non-)\bblocking\b|\bmust fix\b|\brequired change\b|\brequest changes\b", low))
    blocking = state_blocking or (text_blocking and not negated)
    bullets = [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip().startswith(("-", "*"))]
    return {
        "state": state,
        "blocking": blocking,
        "items": bullets,
        "body": text,
    }


class ReadOnlyGitHub:
    """Read-only inspection of a repo's issues, PRs, comments, and reviews via ``gh``."""

    def __init__(self, repo: Optional[str] = None):
        self._repo = repo

    # repo resolution -------------------------------------------------------------------
    def resolve_repo(self) -> str:
        if not self._repo:
            data = _gh_json(["repo", "view", "--json", "nameWithOwner"])
            self._repo = (data or {}).get("nameWithOwner")
            if not self._repo:
                raise GhError("could not determine repo; pass repo='owner/name' or run inside a repo")
        return self._repo

    def get_repo_info(self) -> dict:
        repo = self._repo or "{owner}/{repo}"
        args = ["repo", "view"] + ([repo] if self._repo else []) + [
            "--json", "nameWithOwner,name,owner,defaultBranchRef,url,isPrivate"]
        data = _gh_json(args) or {}
        return {
            "name_with_owner": data.get("nameWithOwner"),
            "name": data.get("name"),
            "owner": (data.get("owner") or {}).get("login"),
            "default_branch": (data.get("defaultBranchRef") or {}).get("name"),
            "url": data.get("url"),
            "private": data.get("isPrivate"),
        }

    # PR listing ------------------------------------------------------------------------
    def list_open_prs(self, limit: int = 50) -> list:
        """List OPEN pull requests (read-only: ``gh pr list``). Returns ``[{number,title,
        updated_at,url}]``. ``limit`` is clamped to >= 1 (``gh pr list --limit`` rejects 0)."""
        repo = self.resolve_repo()
        data = _gh_json(["pr", "list", "-R", repo, "--state", "open",
                         "--json", "number,title,updatedAt,url",
                         "--limit", str(max(1, int(limit)))]) or []
        out = []
        for pr in data:
            out.append({"number": pr.get("number"), "title": pr.get("title"),
                        "updated_at": pr.get("updatedAt"), "url": pr.get("url")})
        return out

    def list_prs(self, state: str = "open", limit: int = 50) -> list:
        """List PRs in a given state (open/merged/closed/all) with branches + size metadata — read-only
        (``gh pr list``). Returns dicts carrying BOTH the orchestrator's fields (head_ref, base_ref) and
        the ranker's (branch, head, additions, deletions, changed_files). Used to plan the OPEN stack /
        detect stacked children whose (MERGED) base branch still exists, and to score review priority.
        ``limit`` is clamped to >= 1."""
        repo = self.resolve_repo()
        st = state if state in ("open", "closed", "merged", "all") else "open"
        data = _gh_json(["pr", "list", "-R", repo, "--state", st, "--json",
                         "number,title,state,headRefName,baseRefName,headRefOid,additions,deletions,"
                         "changedFiles,url",
                         "--limit", str(max(1, int(limit)))]) or []
        return [{"number": pr.get("number"), "title": pr.get("title"), "state": pr.get("state"),
                 "head_ref": pr.get("headRefName"), "base_ref": pr.get("baseRefName"),
                 "branch": pr.get("headRefName"), "head": pr.get("headRefOid"),
                 "additions": pr.get("additions") or 0, "deletions": pr.get("deletions") or 0,
                 "changed_files": pr.get("changedFiles") or 0, "url": pr.get("url")} for pr in data]

    def merged_heads(self, branches) -> dict:
        """Of the given branch names, return ``{branch: base_ref}`` for those that are the HEAD of a MERGED
        PR (read-only). Used to detect a stacked child whose parent PR already merged WITHOUT scanning the
        whole merged history (a ``--state merged --limit N`` window can miss an OLDER merged parent). One
        targeted ``gh pr list --head <branch>`` per candidate branch — bounded by the (small) set of stack
        bases. The base_ref lets a child retarget to its merged parent's ACTUAL base (which may itself not
        be the default branch in a multi-level stack), not blindly to the default branch."""
        repo = self.resolve_repo()
        out = {}
        for b in {x for x in branches if x}:
            data = _gh_json(["pr", "list", "-R", repo, "--state", "merged", "--head", str(b),
                             "--json", "number,baseRefName", "--limit", "1"]) or []
            if data:
                out[b] = data[0].get("baseRefName")
        return out

    # comments / reviews ----------------------------------------------------------------
    @staticmethod
    def _norm_comments(raw) -> list:
        out = []
        for c in raw or []:
            out.append({
                "author": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "created_at": c.get("created_at"),
                "url": c.get("html_url"),
            })
        return out

    def get_issue_comments(self, issue_number: int) -> list:
        repo = self.resolve_repo()
        return self._norm_comments(
            _gh_json_paginated(f"repos/{repo}/issues/{int(issue_number)}/comments"))

    def get_pr_comments(self, pr_number: int) -> list:
        # PR conversation comments live on the issues endpoint for the same number
        repo = self.resolve_repo()
        return self._norm_comments(
            _gh_json_paginated(f"repos/{repo}/issues/{int(pr_number)}/comments"))

    def get_pr_review_comments(self, pr_number: int) -> list:
        # file-level (inline) review comments — a separate endpoint from conversation comments.
        # Preserve commit_id (the reviewed SHA) so coverage can tell whether the CURRENT head was
        # reviewed via inline-ONLY feedback. (Additive vs _norm_comments; other callers ignore it.)
        repo = self.resolve_repo()
        raw = _gh_json_paginated(f"repos/{repo}/pulls/{int(pr_number)}/comments")
        return [{"author": (c.get("user") or {}).get("login"), "body": c.get("body") or "",
                 "created_at": c.get("created_at"), "commit_id": c.get("commit_id"),
                 "url": c.get("html_url")} for c in (raw or [])]

    def get_pr_reviews(self, pr_number: int) -> list:
        repo = self.resolve_repo()
        raw = _gh_json_paginated(f"repos/{repo}/pulls/{int(pr_number)}/reviews")
        out = []
        for r in raw or []:
            out.append({
                "author": (r.get("user") or {}).get("login"),
                "body": r.get("body") or "",
                "state": r.get("state"),
                "created_at": r.get("submitted_at"),
                "commit_id": r.get("commit_id"),
                "url": r.get("html_url"),
            })
        return out

    # Codex helpers ---------------------------------------------------------------------
    def find_latest_codex_advisory(self, issue_number: int) -> Optional[dict]:
        comments = [c for c in self.get_issue_comments(issue_number) if is_codex_author(c["author"])]
        if not comments:
            return None
        latest = max(comments, key=lambda c: c.get("created_at") or "")
        return {
            "source": "issue_comment",
            "issue_number": int(issue_number),
            "author": latest["author"],
            "created_at": latest["created_at"],
            "url": latest["url"],
            "body": latest["body"],
        }

    def find_latest_codex_review(self, pr_number: int) -> Optional[dict]:
        candidates = []
        for c in self.get_pr_comments(pr_number):
            if is_codex_author(c["author"]):
                candidates.append({**c, "source": "pr_comment", "state": None})
        for c in self.get_pr_review_comments(pr_number):       # inline/file-level review comments
            if is_codex_author(c["author"]):
                candidates.append({**c, "source": "pr_review_comment", "state": None})
        for r in self.get_pr_reviews(pr_number):
            if is_codex_author(r["author"]):
                candidates.append({**r, "source": "pr_review"})
        if not candidates:
            return None
        # tie-break same-second signals by (timestamp, source rank, url) so a brand-new review
        # posted in the same second as a seen comment is still picked as "latest" (see watcher dedupe)
        def _key(c):
            return (c.get("created_at") or "",
                    _CODEX_SOURCE_RANK.get(c.get("source"), 0),
                    c.get("url") or "")
        # A Codex "usage limits" notice is rate-limiting, NOT a review. Detect whether the OVERALL
        # latest signal is that notice (so callers can back off), but base the verdict on the latest
        # NON-quota signal — so a freshly-posted-then-rate-limited review isn't hidden, and a bare
        # quota notice is never mis-parsed as a verdict.
        # the OVERALL latest signal is a quota notice if ANY candidate at the max timestamp is one — a
        # same-second review can otherwise win the rank tie and hide a co-timestamped quota notice.
        _qmax = max((c.get("created_at") or "") for c in candidates)
        quota_active = any(is_codex_quota_notice(c.get("body"))
                           for c in candidates if (c.get("created_at") or "") == _qmax)
        real = [c for c in candidates if not is_codex_quota_notice(c.get("body"))]
        # Dedupe key for the watcher over the NON-quota signals at their latest timestamp: cover ALL
        # signals sharing that timestamp (not just the highest-ranked one), so a newly-visible
        # same-second lower-ranked comment advances the key — while a bare quota notice arriving AFTER
        # a seen review does NOT (it would otherwise re-alert stale feedback). Falls back to all
        # candidates only when the PR has nothing but quota notices.
        _basis = real or candidates
        _max_ts = max((c.get("created_at") or "") for c in _basis)
        dedupe_key = _max_ts + "|" + ",".join(sorted(
            (c.get("url") or "") for c in _basis if (c.get("created_at") or "") == _max_ts))
        # Legacy compatibility (QUOTA case only): when a newer quota notice exists, the OLD watcher stored
        # that quota notice's single `created_at|url`. Its timestamp is NEWER than the latest real review,
        # so accepting it can't collide with a fresh baseline (which keys off the real review at an earlier
        # second) -> safe. We deliberately do NOT expose the SAME-SECOND non-quota single key: it is
        # identical to what a fresh baseline stores when only the review was present, so accepting it would
        # SUPPRESS the intended same-second re-alert (a NEW lower-ranked comment posted in a seen review's
        # second). That live feature outweighs a one-time, self-healing re-alert on a pre-upgrade seen file.
        legacy_dedupe_key = None
        if quota_active and real:
            # pick the QUOTA notice's url at _qmax — NOT a same-second review that outranks it. Keying off
            # the quota url keeps the legacy key distinct from any review baseline, so it can't suppress a
            # same-second inline-comment re-alert.
            quota_at_qmax = [c for c in candidates if (c.get("created_at") or "") == _qmax
                             and is_codex_quota_notice(c.get("body"))]
            if quota_at_qmax:
                overall_q = max(quota_at_qmax, key=_key)
                legacy_dedupe_key = (_qmax or "") + "|" + (overall_q.get("url") or "")
        if not real:
            overall = max(candidates, key=_key)   # only quota notices from Codex -> signal, no review
            return {
                "source": overall["source"],
                "pr_number": int(pr_number),
                "author": overall["author"],
                "created_at": overall["created_at"],
                "url": overall.get("url"),
                "dedupe_key": dedupe_key,
                "quota_limited": True,
                "has_review": False,
                "blocking": None,
                "items": [],
                "body": overall.get("body") or "",
            }
        latest = max(real, key=_key)
        packet = parse_review_packet(latest["body"], latest.get("state"))  # default: latest item's verdict
        # A same-second lower-ranked signal can advance the dedupe key (re-alerting) while `latest`
        # stays the higher-ranked review; merge that newer signal's items so the alert isn't empty of
        # the actually-new content.
        _lt = latest.get("created_at") or ""
        cotimestamped_blocking = False
        cotimestamped_extra = []
        for c in real:
            if c is not latest and (c.get("created_at") or "") == _lt:
                cp = parse_review_packet(c.get("body"), c.get("state"))
                for it in (cp.get("items") or []):
                    if it not in packet["items"]:
                        packet["items"].append(it)
                if cp.get("blocking"):
                    cotimestamped_blocking = True
                # a co-timestamped comment that is PLAIN PROSE (no bullet items) would otherwise be
                # invisible — the alert would re-fire (new dedupe key) but still show only `latest`'s
                # body/url. Surface its body + url so the new feedback is actually displayed.
                cbody = (c.get("body") or "").strip()
                if cbody and cbody not in (packet.get("body") or ""):
                    cotimestamped_extra.append((c.get("url"), cbody))
        # a co-timestamped lower-ranked signal that is itself blocking must SURFACE its verdict, not
        # just its items — otherwise we'd re-alert the fix while reporting blocking=False.
        if cotimestamped_blocking:
            packet["blocking"] = True
        if cotimestamped_extra:
            extra = "\n\n".join(f"[also @ same timestamp] {u or ''}\n{b}" for u, b in cotimestamped_extra)
            packet["body"] = ((packet.get("body") or "").rstrip() + "\n\n" + extra).strip()
        # Reconcile with terminal review states:
        #  - a CHANGES_REQUESTED review stays in effect until a *newer* APPROVED clears it;
        #  - an APPROVED clears blocking only if it's the most recent signal — a newer plain comment
        #    with blocking language still counts (don't let an old approval hide it);
        #  - a SAME-SECOND APPROVED must not bury a co-timestamped blocking comment.
        stateful = [c for c in real if (c.get("state") or "").upper() in
                    ("CHANGES_REQUESTED", "APPROVED")]
        if stateful:
            newest_sf = max(stateful, key=lambda c: c.get("created_at") or "")
            nstate = (newest_sf.get("state") or "").upper()
            _sf_ts, _lt_ts = (newest_sf.get("created_at") or ""), (latest.get("created_at") or "")
            if nstate == "CHANGES_REQUESTED":
                packet["blocking"] = True
            elif _sf_ts > _lt_ts:
                packet["blocking"] = False   # a STRICTLY newer APPROVED clears blocking
            elif _sf_ts == _lt_ts and not cotimestamped_blocking:
                packet["blocking"] = False   # same-second APPROVED clears only if nothing blocks at that second
            # else: APPROVED predates a newer comment (or a co-timestamped blocker stands) -> keep verdict
        return {
            "source": latest["source"],
            "pr_number": int(pr_number),
            "author": latest["author"],
            "created_at": latest["created_at"],
            "url": latest.get("url"),
            "dedupe_key": dedupe_key,
            "legacy_dedupe_key": legacy_dedupe_key,
            "quota_limited": quota_active,
            "has_review": True,
            **packet,
        }

    def codex_review_rounds(self, pr_number: int, head: Optional[str] = None) -> dict:
        """Count SUBSTANTIVE (non-quota) trusted-Codex reviews on a PR, whether the given ``head`` SHA
        is already reviewed, and whether the PR is CURRENTLY rate-limited (latest Codex signal — review
        OR comment — is a usage-limits notice). Read-only. Returns ``{rounds, reviewed_on_head,
        quota_limited}``."""
        reviews = self.get_pr_reviews(pr_number)
        comments = self.get_pr_comments(pr_number)
        inline = self.get_pr_review_comments(pr_number)
        # a substantive review = non-quota Codex review with a body OR a meaningful state (an empty-body
        # COMMENTED/CHANGES_REQUESTED/APPROVED review whose content is in inline comments still counts).
        revs = [r for r in reviews
                if is_codex_author(r.get("author"))
                and not is_codex_quota_notice(r.get("body"))
                and ((r.get("body") or "").strip()
                     or (r.get("state") or "").upper() in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"))]
        # a PR can also be "reviewed" via a Codex CONVERSATION comment (find_latest_codex_review treats
        # those as reviews) — count substantive non-quota ones so it isn't mis-scored rounds=0.
        # those as reviews) — but ONLY when the comment carries a 'Reviewed commit: <sha>' marker (a real
        # comment-based review), NOT a generic status/ack comment, which the orchestrator treats as silence.
        crevs = [c for c in comments if is_codex_author(c.get("author"))
                 and not is_codex_quota_notice(c.get("body"))
                 and _REVIEWED_COMMIT_RE.search(c.get("body") or "")]
        # file-level INLINE review comments are review coverage too.
        codex_inline = [c for c in inline if is_codex_author(c.get("author"))
                        and not is_codex_quota_notice(c.get("body"))]
        # count comment-only coverage by RUN (distinct timestamp), EXCLUDING timestamps already covered
        # by a review object: one run that posts a review + a same-second comment is ONE round, not two.
        rev_times = {r.get("created_at") for r in revs}
        crev_runs = len({c.get("created_at") for c in crevs} - rev_times)
        rounds = len(revs) + crev_runs
        if rounds == 0 and codex_inline:
            # inline-ONLY feedback: count distinct reviewed heads (each is a separate run) so a PR reviewed
            # inline-only several times decays like any other — not pinned at 1 forever. Floor 1.
            rounds = len({c.get("commit_id") for c in codex_inline if c.get("commit_id")}) or 1
        # current head is reviewed if a review OBJECT, an INLINE comment, OR a marked CONVERSATION comment
        # ('Reviewed commit: <head>') carries this head — a comment-only review of the head still counts.
        def _crev_sha(c):
            m = _REVIEWED_COMMIT_RE.search(c.get("body") or "")
            return m.group(1) if m else ""
        on_head = bool(head) and (any((r.get("commit_id") or "") == head for r in revs)
                                  or any((c.get("commit_id") or "") == head for c in codex_inline)
                                  or any(_crev_sha(c)[:7] == head[:7] for c in crevs))
        # quota state from the latest Codex signal — review OR conversation comment OR INLINE comment (a
        # later inline review must be able to clear an earlier quota notice). ANY max-ts quota wins the
        # tie (like the watcher), but only while RECENT so a stale quota doesn't suppress forever.
        sigs = [(r.get("created_at"), r.get("body")) for r in reviews if is_codex_author(r.get("author"))]
        sigs += [(c.get("created_at"), c.get("body")) for c in comments if is_codex_author(c.get("author"))]
        sigs += [(c.get("created_at"), c.get("body")) for c in inline if is_codex_author(c.get("author"))]
        sigs = [(t, b) for t, b in sigs if t]
        quota_limited = False
        if sigs:
            max_ts = max(t for t, _ in sigs)
            if any(is_codex_quota_notice(b) for t, b in sigs if t == max_ts):
                try:
                    age = calendar.timegm(time.gmtime()) - calendar.timegm(
                        time.strptime(max_ts, "%Y-%m-%dT%H:%M:%SZ"))
                except (ValueError, TypeError):
                    age = 0
                quota_limited = age < _QUOTA_ACTIVE_TTL_SECS
        return {"rounds": rounds, "reviewed_on_head": on_head, "quota_limited": quota_limited}

    # orchestration reads (read-only; preserve fields the normalized helpers drop) ---------
    def get_pr_meta(self, pr_number: int) -> dict:
        """Read-only PR metadata + diffstat for orchestration: mergeable / base / head / draft +
        additions/changedFiles (for the priority heuristic). Uses ``gh pr view --json`` — a read that
        passes ``_assert_read_only``; NO mutation."""
        repo = self.resolve_repo()
        data = _gh_json(["pr", "view", str(int(pr_number)), "-R", repo, "--json",
                         "number,title,state,mergeable,baseRefName,headRefName,headRefOid,isDraft,"
                         "additions,deletions,changedFiles"]) or {}
        return {
            "number": data.get("number"), "title": data.get("title"), "state": data.get("state"),
            "mergeable": data.get("mergeable"), "base_ref": data.get("baseRefName"),
            "head_ref": data.get("headRefName"), "head_oid": data.get("headRefOid"),
            "is_draft": data.get("isDraft"), "additions": int(data.get("additions") or 0),
            "deletions": int(data.get("deletions") or 0),
            "changed_files": int(data.get("changedFiles") or 0),
        }

    def get_pr_codex_signals(self, pr_number: int) -> dict:
        """Read-only fetch of the three Codex review surfaces, PRESERVING the id / commit_id /
        pull_request_review_id fields that the normalized helpers drop. The orchestrator needs them to
        (a) match a review to the current head (commit_id) and (b) tie an inline finding to its OWN
        review by ``pull_request_review_id`` so re-anchored comments from older reviews don't count.
        Returns ``{reviews, inline, comments}``. All GET endpoints — passes ``_assert_read_only``."""
        repo = self.resolve_repo()
        n = int(pr_number)
        reviews = [{
            "author": (r.get("user") or {}).get("login"), "body": r.get("body") or "",
            "state": r.get("state"), "created_at": r.get("submitted_at"),
            "commit_id": r.get("commit_id"), "id": r.get("id"), "url": r.get("html_url"),
        } for r in (_gh_json_paginated(f"repos/{repo}/pulls/{n}/reviews") or [])]
        inline = [{
            "author": (c.get("user") or {}).get("login"), "body": c.get("body") or "",
            "created_at": c.get("created_at"), "commit_id": c.get("commit_id"),
            "review_id": c.get("pull_request_review_id"), "id": c.get("id"), "url": c.get("html_url"),
            "path": c.get("path"),
        } for c in (_gh_json_paginated(f"repos/{repo}/pulls/{n}/comments") or [])]
        comments = [{
            "author": (c.get("user") or {}).get("login"), "body": c.get("body") or "",
            "created_at": c.get("created_at"), "id": c.get("id"), "url": c.get("html_url"),
        } for c in (_gh_json_paginated(f"repos/{repo}/issues/{n}/comments") or [])]
        return {"reviews": reviews, "inline": inline, "comments": comments}

    def get_pr_overview(self, pr_number: int) -> dict:
        """Read-only PR overview for the fallback-review prompt builder: title/body/base/head/SHA/url +
        diffstat. Uses ``gh pr view --json`` (passes ``_assert_read_only``). Distinct from get_pr_meta
        so the orchestrator's lean per-PR sweep doesn't pay to fetch the (possibly large) body."""
        repo = self.resolve_repo()
        data = _gh_json(["pr", "view", str(int(pr_number)), "-R", repo, "--json",
                         "number,title,body,state,baseRefName,headRefName,headRefOid,url,"
                         "additions,deletions,changedFiles"]) or {}
        return {
            "number": data.get("number"), "title": data.get("title"), "body": data.get("body") or "",
            "state": data.get("state"), "base_ref": data.get("baseRefName"),
            "head_ref": data.get("headRefName"), "head_oid": data.get("headRefOid"),
            "url": data.get("url"), "additions": int(data.get("additions") or 0),
            "deletions": int(data.get("deletions") or 0),
            "changed_files": int(data.get("changedFiles") or 0),
        }

    def get_pr_diff(self, pr_number: int) -> str:
        """Read-only unified diff for a PR (``gh pr diff`` — an allow-listed read; NO mutation). Returns
        the raw diff text (caller caps it). Raises GhError if the diff can't be fetched."""
        repo = self.resolve_repo()
        return _run_gh(["pr", "diff", str(int(pr_number)), "-R", repo])


# ======================================================================================
# Guarded write layer (real GitHub mutations — opt-in, no merge/delete/force-push)
# ======================================================================================
# Only these write shapes may ever be constructed. There is deliberately NO merge, NO branch
# delete, NO push/force-push capability in this layer.
_ALLOWED_WRITE_PREFIXES = {
    ("issue", "create"),
    ("issue", "comment"),
    ("pr", "create"),
    ("pr", "comment"),
    ("pr", "ready"),          # mark a DRAFT pr ready for review — un-draft only (NEVER --undo / draft)
    ("pr", "edit"),           # ONLY the exact base-retarget shape — enforced by _assert_write_allowed
}
# Forbidden gh SUBCOMMANDS (the verb at args[1]) — redundant with the allow-list but kept as defense.
# Checked ONLY at the verb position, never against flag VALUES, so a legitimate --base/--body VALUE that
# happens to equal one of these words (e.g. a real branch literally named `merge`) is NOT mis-refused.
_FORBIDDEN_SUBCOMMANDS = {"merge", "delete", "close", "push", "undo"}
# Forbidden FLAGS — checked against flag-like args (starting with '-') anywhere in the argv, so a
# dangerous flag on an allow-listed subcommand (e.g. `pr create --force`, `pr ready --undo`) is refused.
_FORBIDDEN_FLAGS = {
    "--delete", "-d", "-D", "--force", "-f", "--force-with-lease", "--admin", "--undo",
}
# `pr edit` is NOT a generic editor here: the ONLY flags it may carry are ``-R`` (repo) and ``--base``
# (retarget). Any other pr-edit flag (--title/--body/--add-reviewer/--state/--draft/--ready/…) is refused,
# so `pr edit` can only ever change the base branch — never title/body/reviewers/state/draft.
_PR_EDIT_ALLOWED_FLAGS = {"-r", "--base"}

# a safe base-branch / ref name for --base: only [A-Za-z0-9._/-], no leading '-'/'/', no trailing '/',
# no '..', no whitespace/backslash/':'/control chars/shell metachars (the charset already excludes them).
_BRANCH_CHARSET = re.compile(r"^[A-Za-z0-9._/-]+$")


def is_safe_base_ref(name) -> bool:
    """True iff ``name`` is a safe, simple branch/ref name usable as a ``--base`` value: non-empty,
    only ``[A-Za-z0-9._/-]`` (so no whitespace, backslash, ``:``, control chars or shell metacharacters),
    not starting with ``-`` or ``/``, not ending with ``/``, and containing no ``..``."""
    if not name or not isinstance(name, str) or len(name) > 255:
        return False
    if not _BRANCH_CHARSET.match(name):
        return False
    if name.startswith("-") or name.startswith("/") or name.endswith("/") or ".." in name:
        return False
    return True
# obvious secret/token shapes — refuse to post content that looks like a credential.
# Covers all GitHub token prefixes (ghp_/gho_/ghu_/ghs_/ghr_ + fine-grained github_pat_),
# AWS keys, Slack tokens, Google API keys, and PEM private keys.
_SECRET_RE = re.compile(
    r"(gh[posur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_\-]{30,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def _norm_token(a: str) -> str:
    """Normalize an arg for the forbidden-token check: a long flag may be passed as ``--flag=value``
    (gh/pflag accepts that), so strip ``=value`` from FLAG-like args (those starting with ``-``) so
    ``--undo=true`` / ``--force=1`` still match. Non-flag args are left intact, so a comment body that
    merely contains ``=`` (e.g. ``merge=now``) is NOT mistaken for the bare token ``merge``."""
    a = a.lower()
    return a.split("=", 1)[0] if a.startswith("-") else a


def _assert_write_allowed(args: list[str]) -> None:
    """Refuse any write that is not an allow-listed create/comment/ready/base-retarget, or that smells
    like merge/delete/force-push/undo. The single write-safety chokepoint."""
    prefix = tuple(args[:2])
    if prefix not in _ALLOWED_WRITE_PREFIXES:
        raise GhError(f"refused: write op not in allow-list: {' '.join(args[:2]) or '(empty)'}")
    # forbidden SUBCOMMAND at the verb position (args[1]) — never scans flag VALUES, so a --base/--body
    # value that merely equals a word like 'merge'/'push' (a real branch name) is not mis-refused.
    if len(args) > 1 and _norm_token(args[1]) in _FORBIDDEN_SUBCOMMANDS:
        raise GhError(f"refused: forbidden subcommand: {args[1]}")
    # forbidden FLAGS anywhere (flag-like args only) — catches dangerous flags on an allowed subcommand.
    flags = {_norm_token(a) for a in args if a.startswith("-")}
    bad = flags & _FORBIDDEN_FLAGS
    if bad:
        raise GhError(f"refused: forbidden flag(s) in write op: {sorted(bad)}")
    if prefix == ("pr", "edit"):
        # `pr edit` is permitted ONLY for base retarget: the sole flags may be -R and --base. Any other
        # flag (title/body/reviewer/state/draft/ready/milestone/…) turns this into a generic editor — reject.
        extra = flags - _PR_EDIT_ALLOWED_FLAGS
        if extra:
            raise GhError(f"refused: `pr edit` may only retarget the base (-R/--base); got {sorted(extra)}")
        if "--base" not in flags:
            raise GhError("refused: `pr edit` is allowed only to set --base (base retarget)")


def _assert_no_secrets(*texts: Optional[str]) -> None:
    for t in texts:
        if t and _SECRET_RE.search(t):
            raise GhError("refused: content appears to contain a secret/token — not posting")


def _shorten(args: list[str], width: int = 70) -> str:
    parts = []
    for a in args:
        a = a.replace("\n", " ")
        parts.append(a if len(a) <= width else a[:width] + "…")
    return "gh " + " ".join(parts)


def _parse_url_number(out: str) -> dict:
    """gh issue/pr create prints the created URL on stdout."""
    url = (out or "").strip().splitlines()[-1].strip() if out.strip() else ""
    number = None
    m = re.search(r"/(\d+)(?:[/#].*)?$", url)
    if m:
        number = int(m.group(1))
    return {"url": url, "number": number}


def bounded_poll(fetch, max_attempts: int, sleep_seconds: float, sleep_fn=time.sleep) -> dict:
    """Call ``fetch()`` up to ``max_attempts`` times, sleeping ``sleep_seconds`` between tries,
    stopping as soon as it returns a truthy value. Bounded — never an infinite loop.

    ``max_attempts <= 0`` means "do not poll" (0 attempts, immediate not-found). A negative
    ``sleep_seconds`` is clamped to 0 so ``time.sleep`` can never raise."""
    target = int(max_attempts)
    if target <= 0:                       # honor 0 / negative: no poll at all
        return {"found": False, "result": None, "attempts": 0}
    sleep_seconds = max(0, sleep_seconds)  # never pass a negative interval to sleep
    attempts = 0
    for attempts in range(1, target + 1):
        result = fetch()
        if result:
            return {"found": True, "result": result, "attempts": attempts}
        if attempts < target:
            sleep_fn(sleep_seconds)
    return {"found": False, "result": None, "attempts": attempts}


class GitHubWriter:
    """Guarded GitHub *write* operations.

    Default mode is DRY-RUN (``live=False``): every call logs exactly what it WOULD do and returns a
    simulated result — nothing is sent to GitHub. Real mutations happen ONLY when constructed with an
    explicit ``live=True`` (wired to the CLI ``--real-github`` flag). Capabilities are limited to
    creating issues/PRs and commenting; there is no merge, branch-delete, or force-push path.
    """

    def __init__(self, repo: str, live: bool = False, logger=print):
        if not repo:
            raise GhError("GitHubWriter requires an explicit repo 'owner/name'")
        self.repo = repo
        self.live = bool(live)
        self.calls: list[dict] = []
        self._log = logger
        self._issue_seq = 1000
        self._pr_seq = 2000

    def _exec(self, args: list[str], op: str, desc: str, sim: dict, parse=None) -> dict:
        try:
            _assert_write_allowed(args)                   # write-shape gate
            _assert_no_secrets(*[a for a in args if isinstance(a, str)])
        except GhError as e:                              # user-controlled input must not crash us
            self.calls.append({"op": op, "args": list(args), "live": self.live, "executed": False})
            return {"executed": False, "error": str(e),
                    "log": f"[github-write:REFUSED] {desc} :: {e}"}
        mode = "LIVE" if self.live else "DRY-RUN"
        line = f"[github-write:{mode}] {desc} :: {_shorten(args)}"
        self._log(line)                                  # print/log EXACTLY what we are doing
        self.calls.append({"op": op, "args": list(args), "live": self.live, "executed": self.live})
        if not self.live:
            return {"executed": False, "dry_run": True, "log": line, **sim}
        try:
            out = _spawn_gh(args, timeout=120)            # guarded above; real mutation
        except GhError as e:                             # fail safely — never crash the workflow
            return {"executed": False, "error": str(e), "log": line}
        result = parse(out) if parse else {"output": (out or "").strip()}
        result.update({"executed": True, "log": line})
        return result

    def create_advisory_issue(self, title: str, body: str, labels: Optional[list] = None) -> dict:
        self._issue_seq += 1
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body", body]
        if labels:
            args += ["--label", ",".join(labels)]
        sim = {"number": self._issue_seq,
               "url": f"https://github.com/{self.repo}/issues/{self._issue_seq}", "simulated": True}
        return self._exec(args, "create_advisory_issue", f"create advisory issue {title!r}", sim,
                          parse=_parse_url_number)

    def comment_on_issue(self, issue_number: int, body: str) -> dict:
        args = ["issue", "comment", str(int(issue_number)), "-R", self.repo, "--body", body]
        sim = {"posted": False, "simulated": True}
        return self._exec(args, "comment_on_issue", f"comment on issue #{issue_number}", sim)

    def create_draft_pr(self, title: str, body: str, base: str, head: str) -> dict:
        self._pr_seq += 1
        args = ["pr", "create", "-R", self.repo, "--draft",
                "--title", title, "--body", body, "--base", base, "--head", head]
        sim = {"number": self._pr_seq,
               "url": f"https://github.com/{self.repo}/pull/{self._pr_seq}",
               "draft": True, "simulated": True}
        return self._exec(args, "create_draft_pr", f"create DRAFT PR {head}->{base}", sim,
                          parse=_parse_url_number)

    def comment_on_pr(self, pr_number: int, body: str) -> dict:
        args = ["pr", "comment", str(int(pr_number)), "-R", self.repo, "--body", body]
        sim = {"posted": False, "simulated": True}
        return self._exec(args, "comment_on_pr", f"comment on PR #{pr_number}", sim)

    def mark_pr_ready(self, pr_number: int) -> dict:
        """Mark a DRAFT pull request ready for review: EXACTLY ``gh pr ready <pr> -R <repo>`` and nothing
        else. A single fixed write shape — no ``--undo`` (convert-to-draft), no other flags, no merge /
        close / edit. Passes ``_assert_write_allowed`` via the ``(pr, ready)`` prefix."""
        args = ["pr", "ready", str(int(pr_number)), "-R", self.repo]
        sim = {"ready": True, "simulated": True}
        return self._exec(args, "mark_pr_ready", f"mark PR #{pr_number} ready for review", sim)

    def retarget_pr_base(self, pr_number: int, target_base: str) -> dict:
        """Change a PR's base branch: EXACTLY ``gh pr edit <pr> -R <repo> --base <target_base>`` — and
        nothing else. NOT a generic ``pr edit``: the guard rejects any pr-edit flag other than -R/--base,
        so title/body/reviewers/state/draft are never touched, and there is no merge/close/push. The base
        must be a safe simple branch name (validated here as defense-in-depth; the dashboard also checks)."""
        if not is_safe_base_ref(target_base):
            self.calls.append({"op": "retarget_pr_base", "args": [], "live": self.live, "executed": False})
            return {"executed": False, "error": "refused: unsafe base ref %r" % (target_base,),
                    "log": "[github-write:REFUSED] retarget PR #%s base :: unsafe ref" % pr_number}
        args = ["pr", "edit", str(int(pr_number)), "-R", self.repo, "--base", str(target_base)]
        sim = {"retargeted": True, "base": str(target_base), "simulated": True}
        return self._exec(args, "retarget_pr_base",
                          f"retarget PR #{pr_number} base -> {target_base}", sim)
