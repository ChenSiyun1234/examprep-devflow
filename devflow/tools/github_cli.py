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

import json
import re
import shutil
import subprocess
import time
from typing import Optional


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
    t = (body or "").lower()
    return "usage limit" in t and ("code review" in t or "codex" in t)


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
        # file-level (inline) review comments — a separate endpoint from conversation comments
        repo = self.resolve_repo()
        return self._norm_comments(
            _gh_json_paginated(f"repos/{repo}/pulls/{int(pr_number)}/comments"))

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

    def list_prs(self, state: str = "open", limit: int = 50) -> list:
        """List PRs with SIZE metadata for ranking (read-only). ``state`` in {open, closed, merged,
        all}. Returns ``[{number,title,branch,head,state,additions,deletions,changed_files,url}]``."""
        repo = self.resolve_repo()
        st = state if state in ("open", "closed", "merged", "all") else "open"
        data = _gh_json(["pr", "list", "-R", repo, "--state", st,
                         "--json", "number,title,headRefName,headRefOid,state,additions,deletions,"
                                   "changedFiles,url",
                         "--limit", str(max(1, int(limit)))]) or []
        out = []
        for pr in data:
            out.append({"number": pr.get("number"), "title": pr.get("title"),
                        "branch": pr.get("headRefName"), "head": pr.get("headRefOid"),
                        "state": pr.get("state"),
                        "additions": pr.get("additions") or 0, "deletions": pr.get("deletions") or 0,
                        "changed_files": pr.get("changedFiles") or 0, "url": pr.get("url")})
        return out

    def codex_review_rounds(self, pr_number: int, head: Optional[str] = None) -> dict:
        """Count SUBSTANTIVE (non-quota) trusted-Codex reviews on a PR, and whether the given
        ``head`` SHA is already reviewed. Read-only. Returns ``{rounds, reviewed_on_head}``."""
        # a substantive review = non-quota Codex review with a body OR a meaningful state (an
        # empty-body COMMENTED/CHANGES_REQUESTED/APPROVED review whose content is in inline comments
        # still counts as a review round).
        revs = [r for r in self.get_pr_reviews(pr_number)
                if is_codex_author(r.get("author"))
                and not is_codex_quota_notice(r.get("body"))
                and ((r.get("body") or "").strip()
                     or (r.get("state") or "").upper() in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"))]
        on_head = bool(head) and any((r.get("commit_id") or "") == head for r in revs)
        return {"rounds": len(revs), "reviewed_on_head": on_head}

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
        # latest signal is that notice (so callers can back off), but base the actual review verdict
        # on the latest NON-quota signal — so a freshly-posted-then-rate-limited review isn't hidden,
        # and a bare quota notice is never mis-parsed as a verdict.
        quota_active = is_codex_quota_notice(max(candidates, key=_key).get("body"))
        real = [c for c in candidates if not is_codex_quota_notice(c.get("body"))]
        if not real:
            overall = max(candidates, key=_key)   # only quota notices from Codex -> signal, no review
            return {
                "source": overall["source"],
                "pr_number": int(pr_number),
                "author": overall["author"],
                "created_at": overall["created_at"],
                "url": overall.get("url"),
                "quota_limited": True,
                "has_review": False,
                "blocking": None,
                "items": [],
                "body": overall.get("body") or "",
            }
        latest = max(real, key=_key)
        packet = parse_review_packet(latest["body"], latest.get("state"))  # default: latest item's verdict
        # Reconcile with terminal review states:
        #  - a CHANGES_REQUESTED review stays in effect until a *newer* APPROVED clears it;
        #  - an APPROVED clears blocking only if it's the most recent signal — a newer plain comment
        #    with blocking language still counts (don't let an old approval hide it).
        stateful = [c for c in real if (c.get("state") or "").upper() in
                    ("CHANGES_REQUESTED", "APPROVED")]
        if stateful:
            newest_sf = max(stateful, key=lambda c: c.get("created_at") or "")
            nstate = (newest_sf.get("state") or "").upper()
            if nstate == "CHANGES_REQUESTED":
                packet["blocking"] = True
            elif (newest_sf.get("created_at") or "") >= (latest.get("created_at") or ""):
                packet["blocking"] = False   # APPROVED is the most recent signal
            # else: APPROVED predates a newer comment -> keep that comment's parsed verdict
        return {
            "source": latest["source"],
            "pr_number": int(pr_number),
            "author": latest["author"],
            "created_at": latest["created_at"],
            "url": latest.get("url"),
            "quota_limited": quota_active,
            "has_review": True,
            **packet,
        }


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
}
_FORBIDDEN_WRITE_TOKENS = {
    "merge", "delete", "--delete", "-d", "-D", "--force", "-f",
    "--force-with-lease", "push", "close", "--admin",
}
# obvious secret/token shapes — refuse to post content that looks like a credential.
# Covers all GitHub token prefixes (ghp_/gho_/ghu_/ghs_/ghr_ + fine-grained github_pat_),
# AWS keys, Slack tokens, Google API keys, and PEM private keys.
_SECRET_RE = re.compile(
    r"(gh[posur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_\-]{30,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def _assert_write_allowed(args: list[str]) -> None:
    """Refuse any write that is not an allow-listed create/comment, or that smells like
    merge/delete/force-push. The single write-safety chokepoint."""
    if tuple(args[:2]) not in _ALLOWED_WRITE_PREFIXES:
        raise GhError(f"refused: write op not in allow-list: {' '.join(args[:2]) or '(empty)'}")
    low = {a.lower() for a in args}
    bad = low & _FORBIDDEN_WRITE_TOKENS
    if bad:
        raise GhError(f"refused: forbidden token(s) in write op: {sorted(bad)}")


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
