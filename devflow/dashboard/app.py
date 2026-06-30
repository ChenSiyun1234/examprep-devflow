# -*- coding: utf-8 -*-
"""Local DevFlow Dashboard — a tiny stdlib ``http.server`` UI over the safe devflow operations.

Run it::

    python -m devflow.dashboard.app                 # http://127.0.0.1:8765

Safety posture (see :mod:`devflow.dashboard.service` for the enforcement):

* binds **127.0.0.1 by default** (localhost-only; never public) and rejects requests whose ``Host``
  header is not a localhost name (DNS-rebinding defense);
* performs ONLY read-only or DRY-RUN/local-file operations — **no** real GitHub writes, merge,
  branch delete, force-push, or arbitrary shell execution; there is no endpoint that runs a command;
* all dynamic content is HTML-escaped; request bodies are size-capped;
* adds **no** dependency — pure Python standard library.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import socket
import string
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from devflow.dashboard import service
from devflow.tools.packet_writer import PacketError
from devflow.tools.github_cli import GhError

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 1_000_000                       # 1 MB cap on a form POST body
# Host header hostnames always accepted (the loopback names). The configured bind host is added too.
_LOCALHOST_NAMES = {"127.0.0.1", "localhost", "::1"}


def e(s) -> str:
    """HTML-escape any value (None -> '')."""
    return html.escape("" if s is None else str(s))


def _render(page_template: str, *, title: str, **slots) -> str:
    """Render a page template, then wrap it in base.html. ``safe_substitute`` so a stray '$' in
    user content can never raise (and templates only use the slots we pass)."""
    with open(os.path.join(TEMPLATES_DIR, page_template), encoding="utf-8") as f:
        content = string.Template(f.read()).safe_substitute(**slots)
    with open(os.path.join(TEMPLATES_DIR, "base.html"), encoding="utf-8") as f:
        return string.Template(f.read()).safe_substitute(title=e(title), content=content)


def _alert(kind: str, msg: str) -> str:
    """A small notice block (kind in {ok, err, info})."""
    return '<div class="alert %s">%s</div>' % (e(kind), e(msg))


def _li(items) -> str:
    items = [i for i in (items or [])]
    if not items:
        return "<p class='muted'>(none)</p>"
    return "<ul>" + "".join("<li>%s</li>" % e(i) for i in items) + "</ul>"


def _fmt_item(x) -> str:
    if isinstance(x, dict):
        path = x.get("path")
        note = x.get("note") or x.get("body") or x.get("summary")
        if path and note:
            return "%s: %s" % (path, note)
        return str(path or note or x)
    return str(x)


def _payload_html(payload: dict) -> str:
    """Render the approval-gate CONTEXT (beyond the bare question) so the operator sees WHAT they are
    approving: the advisory, blocking comments, PR url, and review summary carried in interrupt_payload."""
    if not isinstance(payload, dict):
        return ""
    out = []
    for key, label in (("advisory", "advisory"), ("blocking_comments", "blocking comments"),
                       ("non_blocking_comments", "non-blocking comments"),
                       ("pr_url", "PR url"), ("review_summary", "review summary")):
        v = payload.get(key)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, list):
            body = _li([_fmt_item(i) for i in v])
        elif isinstance(v, dict):
            body = "<pre>%s</pre>" % e(json.dumps(v, ensure_ascii=False, indent=2))
        else:
            body = "<div>%s</div>" % e(v)
        out.append("<div class='payload'><div class='muted'>%s:</div>%s</div>" % (e(label), body))
    return "".join(out)


def _codex_write_forms(repo, nums, prs_by_num, limit=50) -> str:
    """Strongly-confirmed 'post @codex review' forms (one per request_review PR). Each posts the FIXED
    body @codex review only; the head SHA is pinned and a PR-specific confirmation phrase is required.
    ``limit`` (the window the plan was computed with) is carried through so the server-side candidate
    recompute on POST recognizes a PR shown only because the operator widened the limit past the default."""
    rows = []
    for n in nums:
        head = (prs_by_num.get(n) or {}).get("head") or ""
        conf = "POST @codex review to #%s" % n
        rows.append(
            "<form method='post' action='/codex-review-request' style='margin:8px 0'>"
            "<input type='hidden' name='repo' value='%s'>"
            "<input type='hidden' name='pr_number' value='%s'>"
            "<input type='hidden' name='expected_head_sha' value='%s'>"
            "<input type='hidden' name='limit' value='%s'>"
            "<strong>#%s</strong> <span class='muted'>head <code>%s</code></span> — type "
            "<code>%s</code>: <input type='text' name='confirmation' autocomplete='off' size='30' "
            "placeholder='%s' required> <button type='submit'>Post @codex review to #%s</button></form>"
            % (e(repo), e(n), e(head), e(limit), e(n), e((head or "")[:8]), e(conf), e(conf), e(n)))
    return ("<p class='note'>Each button posts a <strong>real</strong> GitHub PR comment whose body is "
            "EXACTLY <code>@codex review</code>. It does <strong>not</strong> merge, mark ready, retarget, "
            "request reviewers, close, push, or post the guided prompt. The post is refused unless the PR "
            "head still matches and the confirmation is exact.</p>" + "".join(rows))


def _mark_ready_forms(repo, nums, prs_by_num, limit=50) -> str:
    """Strongly-confirmed 'mark draft ready' forms (one per ready_then_merge PR). Each runs EXACTLY
    ``gh pr ready`` for that PR — never merges. The head SHA is pinned and a PR-specific confirmation
    phrase is required. ``limit`` is carried through so the POST-time candidate recompute matches."""
    rows = []
    for n in nums:
        head = (prs_by_num.get(n) or {}).get("head") or ""
        conf = "MARK #%s READY" % n
        rows.append(
            "<form method='post' action='/mark-ready' style='margin:8px 0'>"
            "<input type='hidden' name='repo' value='%s'>"
            "<input type='hidden' name='pr_number' value='%s'>"
            "<input type='hidden' name='expected_head_sha' value='%s'>"
            "<input type='hidden' name='limit' value='%s'>"
            "<strong>#%s</strong> <span class='muted'>head <code>%s</code></span> — type "
            "<code>%s</code>: <input type='text' name='confirmation' autocomplete='off' size='24' "
            "placeholder='%s' required> <button type='submit'>Mark ready for review</button></form>"
            % (e(repo), e(n), e(head), e(limit), e(n), e((head or "")[:8]), e(conf), e(conf)))
    return ("<p class='note'>Each button performs a <strong>real</strong> GitHub write — it marks the "
            "DRAFT PR <strong>ready for review</strong> and nothing else. It does <strong>not</strong> "
            "merge, request reviewers, retarget, push, or delete branches. It is refused unless the PR is "
            "still a draft, still OPEN, its head still matches, and the confirmation is exact.</p>"
            + "".join(rows))


def _render_orchestration(result: dict, allow_writes: bool = False, limit: int = 50) -> str:
    """Render the read-only orchestration plan as escaped cards + a ranking table + a raw debug blob.
    Recommends actions only; emits NO merge/comment/retarget buttons."""
    plan = result.get("plan") or {}
    prs_by_num = {p.get("number"): p for p in (result.get("open_prs") or [])}

    def chips(nums, extra=None):
        if not nums:
            return "<p class='muted'>(none)</p>"
        out = []
        for n in nums:
            p = prs_by_num.get(n) or {}
            bits = ["<strong>#%s</strong>" % e(n)]
            if p.get("title"):
                bits.append(e(p.get("title")))
            branch = p.get("branch") or p.get("head_ref")
            if branch:
                bits.append("<code>%s</code>" % e(branch))
            suffix = (" — " + extra(n)) if extra else ""
            out.append("<li>%s%s</li>" % (" · ".join(bits), suffix))
        return "<ul>" + "".join(out) + "</ul>"

    def card(title, body):
        return "<div class='card'><h3>%s</h3>%s</div>" % (e(title), body)

    # prefill link to the read-only GPT fallback-prompt builder (navigation only — no GitHub write).
    repo_q = urllib.parse.quote(result.get("repo") or "", safe="")

    def gpt_link(n):
        return ("<a href='/gpt-review?repo=%s&amp;pr=%s'>Build GPT fallback prompt</a>"
                % (repo_q, urllib.parse.quote(str(n), safe="")))

    def codex_link(n):
        return ("<a href='/codex-review-prompt?repo=%s&amp;pr=%s'>Build guided Codex prompt</a>"
                % (repo_q, urllib.parse.quote(str(n), safe="")))

    def prompt_links(n):                      # navigation only — no GitHub write
        return codex_link(n) + " · " + gpt_link(n)

    summary = (
        "<table class='kv'>"
        "<tr><th>marker</th><td><span class='marker'>%s</span></td></tr>"
        "<tr><th>repo</th><td>%s</td></tr>"
        "<tr><th>default branch</th><td>%s</td></tr>"
        "<tr><th>open PRs inspected</th><td>%s</td></tr>"
        "<tr><th>Codex rate-limited</th><td>%s</td></tr>"
        "<tr><th>state file</th><td><code>%s</code></td></tr></table>"
        % (e(result.get("marker")), e(result.get("repo")), e(result.get("default_branch")),
           e(len(result.get("open_prs") or [])),
           ("yes — back off" if result.get("rate_limited") else "no"), e(result.get("state_path"))))

    errs = result.get("errors") or []
    errors_html = ""
    if errs:
        errors_html = (_alert("err", "PR read errors: %d (sweep continued)" % len(errs))
                       + "<ul>" + "".join("<li><strong>#%s</strong>: %s</li>"
                                          % (e(x.get("pr")), e(x.get("error"))) for x in errs) + "</ul>")

    ranking = plan.get("ranking") or []
    if ranking:
        rows = "".join(
            "<tr><td>#%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (e(r.get("pr")), e((prs_by_num.get(r.get("pr")) or {}).get("title")), e(r.get("priority")),
               e(r.get("rounds")), ("clean" if r.get("clean") else ""), e(r.get("state")))
            for r in ranking)
        ranking_html = ("<table><thead><tr><th>PR</th><th>title</th><th>priority</th><th>rounds</th>"
                        "<th>clean</th><th>state</th></tr></thead><tbody>" + rows + "</tbody></table>")
    else:
        ranking_html = "<p class='muted'>(no open PRs)</p>"

    rr = plan.get("request_review") or []
    if not rr:
        rr_html = "<p class='muted'>(none)</p>"
    elif allow_writes:
        # writes are live: don't claim "the dashboard posts nothing" next to a real post button below
        rr_html = (chips(rr, extra=prompt_links)
                   + "<p class='muted'>Preferred review request — the <strong>bare</strong> trigger "
                   "<code>@codex review</code> (reliable: on Codex Cloud a guided brief can switch Codex "
                   "into code-change mode instead of review). Use the <strong>Post @codex review</strong> "
                   "form below to post it for real, or copy it. The guided Codex prompt is "
                   "<strong>optional</strong>.</p><pre>@codex review</pre>")
    else:
        rr_html = (chips(rr, extra=prompt_links)
                   + "<p class='muted'>Preferred review request — paste the <strong>bare</strong> trigger "
                   "below (reliable: on Codex Cloud a guided brief can switch Codex into code-change mode "
                   "instead of review). The guided Codex prompt is <strong>optional</strong>, for when you "
                   "want the shared policy applied. The dashboard posts <strong>nothing</strong>:</p>"
                   "<pre>@codex review</pre>")

    rt_to = plan.get("retarget_to") or {}
    rt_html = chips(plan.get("needs_retarget"),
                    extra=lambda n: "retarget base &rarr; <code>%s</code>"
                    % e(rt_to.get(str(n)) or result.get("default_branch")))

    mn_html = chips(plan.get("mergeable_now"))
    if plan.get("mergeable_now"):
        mn_html += ("<p class='note'>Human merge preflight required — verify locally and merge via the "
                    "CLI/GitHub. The dashboard provides <strong>no</strong> merge button.</p>")

    # build_plan folds the PRs it just RECOMMENDED into in_flight; but the dashboard neither posts the
    # request nor persists it, so show only the PRs that were ALREADY awaiting (in_flight minus the
    # freshly-recommended request_review set) — else a just-recommended PR would look "awaiting" already.
    requested = set(plan.get("request_review") or [])
    awaiting = [n for n in (plan.get("in_flight") or []) if n not in requested]
    awaiting_html = chips(awaiting)
    if requested:
        awaiting_html += ("<p class='muted'>Freshly recommended PRs are under <em>Request review</em>, "
                          "not here — the dashboard has not posted those requests.</p>")

    write_card = ""
    if allow_writes:
        body = (_codex_write_forms(result.get("repo"), rr, prs_by_num, limit) if rr
                else "<p class='muted'>(no request_review PRs to post to)</p>")
        write_card = card("Post @codex review — REAL GitHub write (opt-in)", body)

    # the mark-ready write lives INSIDE the "Ready then merge" card — the only bucket it may target
    rtm = plan.get("ready_then_merge") or []
    rtm_html = chips(rtm)
    if allow_writes and rtm:
        rtm_html += _mark_ready_forms(result.get("repo"), rtm, prs_by_num, limit)

    banner = (_alert("info", "GitHub writes ENABLED (localhost, opt-in): post @codex review to "
                             "request_review PRs, and mark a ready_then_merge DRAFT ready for review. "
                             "These are the ONLY writes — no merge/retarget/reviewer/close/push/delete.")
              if allow_writes else "")

    return (
        banner
        + card("Summary", summary)
        + (card("Errors", errors_html) if errs else "")
        + card("Ranking", ranking_html)
        + card("Request review (priority-ordered, ≤3 in-flight)", rr_html)
        + write_card
        + card("Findings to fix (P1/P2 or early P3)", chips(plan.get("findings_to_fix"), extra=prompt_links))
        + card("Mergeable now (clean / converged)", mn_html)
        + card("Force-mergeable (≥3 rounds, only-minor P3)", chips(plan.get("force_mergeable")))
        + card("Ready then merge (un-draft first)", rtm_html)
        + card("Resolve conflict (merge base in; never force-push)", chips(plan.get("needs_conflict")))
        + card("Needs retarget (parent PR merged)", rt_html)
        + card("Mergeability pending (GitHub still computing; re-run)", chips(plan.get("mergeable_unknown")))
        + card("Awaiting Codex (already requested, not by this view)", awaiting_html)
        + card("Raw plan (debug)", "<pre>%s</pre>" % e(json.dumps(result, ensure_ascii=False, indent=2))))


_FOCUS_OPTS = [
    ("general", "general — correctness, regression risk, edge cases, tests"),
    ("safety", "safety — security, CSRF/XSS, localhost-only, write boundaries"),
    ("tests", "tests — missing/flaky tests, fixtures, CI assumptions"),
    ("docs", "docs — accuracy vs actual behavior"),
    ("verify-fix", "verify-fix — were the listed review comments addressed?"),
]
_BUDGET_OPTS = [
    ("compact", "compact (~8k chars)"),
    ("medium", "medium (~20k chars)"),
    ("large", "large (~50k chars)"),
]


def _opts(pairs, selected) -> str:
    """Build <option> tags with the chosen value preselected (so a rebuilt form keeps the selection)."""
    return "".join("<option value='%s'%s>%s</option>"
                   % (e(v), " selected" if v == selected else "", e(label)) for v, label in pairs)


def _render_gpt_result(res: dict) -> str:
    """Render the fallback-review result: no-send warning, private-repo warning, a copyable textarea with
    the prompt, and a PR metadata card. All dynamic content escaped (the textarea too — the browser
    decodes the entities so the COPIED text is the original prompt, and `</textarea>` can't break out)."""
    warn = _alert("info", "This page does NOT call GPT or send your data anywhere — copy the prompt below "
                          "and paste it into GPT/ChatGPT yourself.")
    if res.get("private_repo_warning"):
        warn += _alert("err", "Private / proprietary repository: this prompt may include your code. Do NOT "
                              "paste it into external tools unless you are permitted to.")
    url = res.get("pr_url") or ""
    url_html = ("<a href='%s'>%s</a>" % (e(url), e(url))) if str(url).startswith("http") else e(url) or "—"
    meta = ("<div class='card'><h3>PR</h3><table class='kv'>"
            "<tr><th>repo</th><td>%s</td></tr>"
            "<tr><th>PR</th><td>#%s</td></tr>"
            "<tr><th>url</th><td>%s</td></tr>"
            "<tr><th>title</th><td>%s</td></tr>"
            "<tr><th>base &larr; head</th><td>%s &larr; %s</td></tr>"
            "<tr><th>head SHA</th><td><code>%s</code></td></tr>"
            "<tr><th>changed files</th><td>%s</td></tr>"
            "<tr><th>review modes</th><td>%s</td></tr>"
            "<tr><th>diff chars</th><td>%s</td></tr>"
            "<tr><th>diff truncated</th><td>%s</td></tr>"
            "<tr><th>feedback available</th><td>%s</td></tr>"
            "<tr><th>feedback truncated</th><td>%s</td></tr>"
            "<tr><th>description truncated</th><td>%s</td></tr>"
            "<tr><th>focus / budget</th><td>%s / %s</td></tr>"
            "</table></div>"
            % (e(res.get("repo")), e(res.get("pr_number")), url_html, e(res.get("title")),
               e(res.get("base")), e(res.get("head")), e(res.get("head_sha")),
               e(len(res.get("changed_files") or [])),
               e(", ".join(res.get("review_modes") or []) or "—"), e(res.get("diff_chars")),
               ("yes" if res.get("diff_truncated") else "no"),
               ("yes" if res.get("feedback_available", True) else "NO — read failed"),
               ("yes" if res.get("feedback_truncated") else "no"),
               ("yes" if res.get("body_truncated") else "no"),
               e(res.get("focus")), e(res.get("diff_budget"))))
    textarea = ("<div class='card'><h3>Prompt — copy &amp; paste into GPT/ChatGPT</h3>"
                "<textarea readonly rows='24' onclick='this.select()'>%s</textarea></div>"
                % e(res.get("prompt")))
    return warn + meta + textarea


def _render_codex_result(res: dict) -> str:
    """Render the guided Codex prompt result: a copy/paste warning, the Codex-Cloud behavior caveat, a
    copyable textarea (escaped), and a PR metadata card. The dashboard NEVER posts this."""
    warn = _alert("info", "This page does NOT post to GitHub or call Codex — copy the prompt below and "
                          "paste it into a GitHub PR comment yourself.")
    warn += _alert("info", "Heads-up: on Codex Cloud a guided brief alongside <code>@codex review</code> "
                           "can switch Codex into code-change mode instead of review. If you only want a "
                           "review, the bare <code>@codex review</code> is the reliable trigger; use this "
                           "guided prompt when you want the shared review policy applied.")
    url = res.get("pr_url") or ""
    url_html = ("<a href='%s'>%s</a>" % (e(url), e(url))) if str(url).startswith("http") else e(url) or "—"
    meta = ("<div class='card'><h3>PR</h3><table class='kv'>"
            "<tr><th>repo</th><td>%s</td></tr>"
            "<tr><th>PR</th><td>#%s</td></tr>"
            "<tr><th>url</th><td>%s</td></tr>"
            "<tr><th>title</th><td>%s</td></tr>"
            "<tr><th>base &larr; head</th><td>%s &larr; %s</td></tr>"
            "<tr><th>changed files</th><td>%s</td></tr>"
            "<tr><th>review modes</th><td>%s</td></tr>"
            "<tr><th>diff chars</th><td>%s</td></tr>"
            "<tr><th>diff truncated</th><td>%s</td></tr>"
            "<tr><th>budget</th><td>%s</td></tr></table></div>"
            % (e(res.get("repo")), e(res.get("pr_number")), url_html, e(res.get("title")),
               e(res.get("base")), e(res.get("head")), e(len(res.get("changed_files") or [])),
               e(", ".join(res.get("review_modes") or []) or "—"), e(res.get("diff_chars")),
               ("yes" if res.get("diff_truncated") else "no"), e(res.get("diff_budget"))))
    textarea = ("<div class='card'><h3>Guided Codex prompt — copy &amp; paste into a GitHub PR comment</h3>"
                "<textarea readonly rows='24' onclick='this.select()'>%s</textarea></div>"
                % e(res.get("prompt")))
    return warn + meta + textarea


def _packet_source_text(p: dict) -> str:
    bits = []
    if p.get("issue_number"):
        bits.append("issue #%s" % e(p.get("issue_number")))
    if p.get("pr_number"):
        bits.append("PR #%s" % e(p.get("pr_number")))
    return " / ".join(bits) or "—"


def _packet_index(packets: list) -> str:
    if not packets:
        return ("<p class='muted'>No packets yet. Export one from a paused run's detail page, or build a "
                "<a href='/manual'>Manual packet</a>.</p>")
    rows = []
    for p in packets:
        slug = p.get("slug") or ""
        rows.append(
            "<tr><td><a href='/packet/%s'>%s</a></td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s / %s</td><td>%s</td><td><span class='marker'>%s</span></td><td class='muted'>%s</td></tr>"
            % (urllib.parse.quote(slug, safe=""), e(slug), e(p.get("thread_id")), e(p.get("task")),
               e(p.get("repo")), e(p.get("gate")), e(p.get("decision")), _packet_source_text(p),
               e(p.get("status")), e(p.get("generated_at"))))
    return ("<table><thead><tr><th>slug</th><th>thread</th><th>task</th><th>repo</th>"
            "<th>gate / decision</th><th>source</th><th>status</th><th>generated</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _status_buttons(slug: str, current: str) -> str:
    btns = []
    for s in service.PACKET_STATUSES:
        cls = " class='ok'" if s == current else ""
        btns.append("<button name='status' value='%s'%s>%s</button>" % (e(s), cls, e(s)))
    return ("<form method='post' action='/packet-status' class='inline'>"
            "<input type='hidden' name='slug' value='%s'>%s</form>" % (e(slug), "".join(btns)))


def _render_packet_detail(p: dict, notice: str = "") -> str:
    """Render a packet's full detail (escaped) + local-only handoff-status buttons. No GitHub anything."""
    def card(title, body):
        return "<div class='card'><h3>%s</h3>%s</div>" % (e(title), body)

    meta = ("<table class='kv'>"
            "<tr><th>slug</th><td><code>%s</code></td></tr>"
            "<tr><th>thread_id</th><td>%s</td></tr>"
            "<tr><th>task</th><td>%s</td></tr>"
            "<tr><th>repo</th><td>%s</td></tr>"
            "<tr><th>source</th><td>%s</td></tr>"
            "<tr><th>generated_at</th><td>%s</td></tr>"
            "<tr><th>gate</th><td>%s</td></tr>"
            "<tr><th>decision</th><td>%s</td></tr>"
            "<tr><th>source issue / PR</th><td>%s</td></tr>"
            "<tr><th>markdown</th><td><code>%s</code></td></tr>"
            "<tr><th>json</th><td><code>%s</code></td></tr></table>"
            % (e(p.get("slug")), e(p.get("thread_id")), e(p.get("task")), e(p.get("repo")),
               e(p.get("source")), e(p.get("generated_at")), e(p.get("gate")), e(p.get("decision")),
               _packet_source_text(p), e(p.get("md_path")), e(p.get("json_path"))))

    status_card = (card("Handoff status",
                        "<p>current: <span class='marker'>%s</span></p>" % e(p.get("status"))
                        + _status_buttons(p.get("slug") or "", p.get("status") or "")
                        + "<p class='note'>Local only — writes "
                          "<code>.devflow/packets/&lt;slug&gt;/handoff-status.json</code>. "
                          "No GitHub write.</p>"))
    return (notice
            + status_card
            + card("Metadata", meta)
            + card("Approved scope", _li(p.get("approved_scope")))
            + card("Implementation tasks", _li(p.get("tasks")))
            + card("Files likely touched", _li(p.get("files_likely_touched")))
            + card("Out of scope", _li(p.get("out_of_scope")))
            + card("Tests / checks to run", _li(p.get("tests_to_run")))
            + card("Safety boundaries", _li(p.get("safety_boundaries")))
            + card("Suggested Claude Code handoff", "<pre>%s</pre>" % e(p.get("handoff"))))


def _writes_allowed_for_host(host, allow_writes) -> bool:
    """The localhost-only write boundary, as a pure predicate so it can be enforced at the server factory
    (not only in main()) and unit-tested without binding a socket. Writes are permitted ONLY when the
    operator opted in AND the bind host is a loopback name."""
    return bool(allow_writes) and (host or "").strip().lower() in _LOCALHOST_NAMES


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, allowed_hosts, allow_writes=False):
        # bind an IPv6 socket for an IPv6 host literal (e.g. ::1) — the default family is IPv4, so
        # without this `--host ::1` (advertised as a localhost name) would fail with an address error.
        if ":" in (addr[0] or ""):
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)
        self.allowed_hosts = set(allowed_hosts)
        # the ONE opt-in real-GitHub-write capability (post @codex review). Enforce the localhost-only
        # boundary HERE (not just in main()) so an embedding/test harness calling run_server() directly
        # can't enable writes on a non-loopback bind: writes stay off unless the bind host is a loopback.
        self.allow_writes = _writes_allowed_for_host(addr[0], allow_writes)


class Handler(BaseHTTPRequestHandler):
    server_version = "devflow-dashboard"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):                      # [::1]:8765 -> ::1
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in self.server.allowed_hosts

    def _same_origin_ok(self) -> bool:
        """CSRF defense for state-changing POSTs. The Host check stops DNS-rebinding, but a plain
        cross-origin ``<form>`` POST still carries an allowed ``Host: 127.0.0.1``, so also require the
        request to be same-origin when it comes from a browser: modern browsers send ``Sec-Fetch-Site``
        on every request and an attacker page cannot suppress it. Non-browser clients (curl/scripts)
        send neither header and are the operator's own tools — not a cross-site vector."""
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if origin:
            host = (self.headers.get("Host") or "").strip().lower()
            return origin.strip().lower() in ("http://" + host, "https://" + host)
        return True

    def _read_form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            # refuse an oversized body without reading it; close the connection so the unread bytes
            # can't be mis-parsed as the next request on this HTTP/1.1 keep-alive connection.
            self.close_connection = True
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _send_html(self, body: str, code: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # deny framing so a hostile page can't frame the dashboard and clickjack a same-origin POST
        # past the CSRF check.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _forbidden_host(self) -> None:
        self._send_html(_render("message.html", title="Forbidden",
                                heading="403 — host not allowed",
                                body="<p>This dashboard only accepts requests addressed to a "
                                     "localhost name. Open it via "
                                     "<code>http://127.0.0.1</code>.</p>"), code=403)

    def _forbidden_csrf(self) -> None:
        self._send_html(_render("message.html", title="Forbidden",
                                heading="403 — cross-site request blocked",
                                body="<p>State-changing requests must come from the dashboard itself "
                                     "(same-origin). Cross-site POSTs are rejected.</p>"), code=403)

    def _not_found(self) -> None:
        self._send_html(_render("message.html", title="Not found", heading="404",
                                body="<p>No such page. <a href='/'>Back to runs</a>.</p>"), code=404)

    def log_message(self, fmt, *args):                # keep the console quiet (and avoid log spoofing)
        pass

    # -- routing ---------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._forbidden_host()
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._page_runs()
        if path == "/new":
            return self._page_new_run()
        if path == "/manual":
            return self._page_manual()
        if path == "/watcher":
            return self._page_watcher()
        if path == "/orchestrator":
            return self._page_orchestrator()
        if path == "/gpt-review":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._page_gpt_review(repo=qs.get("repo", [""])[0], pr=qs.get("pr", [""])[0])
        if path == "/codex-review-prompt":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._page_codex_review(repo=qs.get("repo", [""])[0], pr=qs.get("pr", [""])[0])
        if path == "/packets":
            return self._page_packets()
        if path.startswith("/packet/"):
            return self._page_packet_detail(urllib.parse.unquote(path[len("/packet/"):]))
        if path.startswith("/run/"):
            return self._page_run_detail(urllib.parse.unquote(path[len("/run/"):]))
        return self._not_found()

    def do_POST(self):
        # Read (and thereby drain) the request body BEFORE the host check, so a rejected POST never
        # leaves unread body bytes that would desync the next request on a keep-alive connection.
        form = self._read_form()
        if not self._host_ok():
            return self._forbidden_host()
        if not self._same_origin_ok():        # CSRF: reject cross-site browser POSTs
            return self._forbidden_csrf()
        path = urllib.parse.urlparse(self.path).path
        if path == "/new":
            return self._post_new_run(form)
        if path == "/manual":
            return self._post_manual(form)
        if path == "/watcher":
            return self._post_watcher(form)
        if path == "/orchestrator":
            return self._post_orchestrator(form)
        if path == "/gpt-review":
            return self._post_gpt_review(form)
        if path == "/codex-review-prompt":
            return self._post_codex_review(form)
        if path == "/packet-status":
            return self._post_packet_status(form)
        if path == "/codex-review-request":
            return self._post_codex_review_request(form)
        if path == "/mark-ready":
            return self._post_mark_ready(form)
        if path == "/decide":
            return self._post_decide(form)
        if path == "/export":
            return self._post_export(form)
        return self._not_found()

    # -- GET pages -------------------------------------------------------------
    def _page_runs(self):
        runs = service.list_runs()
        if runs:
            rows = []
            for r in runs:
                gate = r.get("paused_gate_alias") or r.get("paused_gate") or ""
                tid = r.get("thread_id") or ""
                rows.append(
                    "<tr><td><a href='/run/%s'>%s</a></td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td>%s</td><td class='muted'>%s</td></tr>"
                    % (urllib.parse.quote(tid, safe=""), e(tid), e(r.get("status")),
                       e(gate), e(r.get("task_type")), e(r.get("repo")), e(r.get("updated_at"))))
            table = ("<table><thead><tr><th>thread_id</th><th>status</th><th>paused gate</th>"
                     "<th>task</th><th>repo</th><th>updated</th></tr></thead><tbody>"
                     + "".join(rows) + "</tbody></table>")
        else:
            table = ("<p class='muted'>No local runs yet. "
                     "<a href='/new'>Create a dry-run run</a>.</p>")
        done = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("done", [""])[0]
        notice = _alert("ok", "Run %s completed (no checkpoint to display)." % done) if done else ""
        self._send_html(_render("runs.html", title="Runs", rows=table, notice=notice))

    def _page_run_detail(self, thread_id: str, notice: str = ""):
        state = service.get_run(thread_id)
        if state is None:
            return self._not_found()
        status = state.get("status")
        paused_gate = state.get("paused_at_gate")
        gate_alias = service.ALIAS_FOR_GATE.get(paused_gate)

        fields = []
        for k in ("thread_id", "task_type", "repo", "status", "paused_at_gate", "paused_at_node",
                  "issue_number", "pr_number", "human_approval", "fix_approval", "merge_approval"):
            if state.get(k) not in (None, ""):
                fields.append("<tr><th>%s</th><td>%s</td></tr>" % (e(k), e(state.get(k))))
        fields_html = "<table class='kv'>" + "".join(fields) + "</table>"

        event_log = _li(state.get("event_log"))
        errors = state.get("errors") or []
        errors_html = (_alert("err", "errors: %d" % len(errors)) + _li(errors)) if errors \
            else "<p class='muted'>(no errors)</p>"

        if status == "paused" and gate_alias:
            payload = state.get("interrupt_payload") or {}
            # explicit export decision — exporting "approved" must be a deliberate choice, not the
            # default of a single button on an undecided gate.
            export_form = (
                "<form method='post' action='/export' class='inline'>"
                "<input type='hidden' name='thread_id' value='%s'>"
                "<button name='decision' value='approved'>Export approved packet</button>"
                "<button name='decision' value='rejected'>Export rejected packet</button>"
                "</form>" % e(thread_id))
            if state.get("real_github"):
                # a live (--real-github) checkpoint is read-only here — resuming it would clobber its
                # real provenance. Offer inspect + export only; Approve/Reject are disabled.
                gate_section = (
                    "<div class='card gate'><h3>Paused at gate: %s</h3>%s"
                    "<p class='note'>This run was started with <code>--real-github</code> (a live flow). "
                    "The dashboard is dry-run only — Approve/Reject are disabled; resume it via the CLI "
                    "(<code>devflow resume --real-github</code>). You can still export a packet.</p>"
                    "%s</div>"
                    % (e(gate_alias), _payload_html(payload), export_form))
            else:
                gate_section = (
                    "<div class='card gate'><h3>Paused at gate: %s</h3><p>%s</p>%s"
                    "<form method='post' action='/decide' class='inline'>"
                    "<input type='hidden' name='thread_id' value='%s'>"
                    "<input type='hidden' name='gate' value='%s'>"
                    "<button name='decision' value='approved' class='ok'>Approve</button>"
                    "<button name='decision' value='rejected' class='danger'>Reject</button></form>%s</div>"
                    % (e(gate_alias), e(payload.get("question") or ""), _payload_html(payload),
                       e(thread_id), e(gate_alias), export_form))
        else:
            gate_section = ("<p class='muted'>This run is <strong>%s</strong> — not paused at an "
                            "approval gate, so there is nothing to approve/reject.</p>" % e(status))

        self._send_html(_render(
            "run_detail.html", title="Run %s" % thread_id, thread_id=e(thread_id), notice=notice,
            fields=fields_html, event_log=event_log, errors=errors_html, gate_section=gate_section))

    def _page_new_run(self, notice: str = ""):
        self._send_html(_render("new_run.html", title="New run", notice=notice))

    def _page_manual(self, notice: str = ""):
        self._send_html(_render("manual_packet.html", title="Manual packet", notice=notice,
                                result=""))

    def _page_watcher(self, notice: str = "", result: str = ""):
        self._send_html(_render("watcher.html", title="Codex watcher", notice=notice, result=result))

    def _page_orchestrator(self, notice: str = "", result: str = ""):
        # mode-aware copy: the page's own header/intro/note must NOT claim "read-only / never posts" when
        # the opt-in @codex review write button is live, or an operator could think the button is dry-run.
        if getattr(self.server, "allow_writes", False):
            mode_tag = "(writes ENABLED — post @codex review · mark draft ready)"
            intro = ("Shows the same cross-PR plan as <code>orchestrate-reviews</code>. The plan is "
                     "advisory with <strong>two exceptions</strong>: GitHub writes are "
                     "<strong>enabled</strong> for this localhost session — each <em>request review</em> PR "
                     "has a <strong>Post @codex review</strong> button (posts the exact comment "
                     "<code>@codex review</code>), and each <em>ready then merge</em> DRAFT has a "
                     "<strong>Mark ready for review</strong> button (runs <code>gh pr ready</code>). Both "
                     "require a typed confirmation + head-match. Neither merges; it still never retargets, "
                     "requests reviewers, closes, pushes, or deletes branches. This is not a merge UI.")
            form_note = ("Requires <code>gh</code> installed and authenticated. Computing the plan is "
                         "read-only. <em>Request review</em> PRs offer a real <code>@codex review</code> "
                         "post; <em>ready then merge</em> drafts offer a real <strong>mark ready</strong> "
                         "(not a merge). Mergeable PRs show a human merge-preflight note, not a merge button.")
        else:
            mode_tag = "(read-only orchestrator)"
            intro = ("Shows the same cross-PR plan as <code>orchestrate-reviews</code>: who to request "
                     "review from, findings to fix, mergeable / force-mergeable / ready-then-merge PRs, "
                     "conflicts and retargets, and Codex rate-limit state. <strong>Advisory only</strong> "
                     "— it recommends next actions and never mutates GitHub (no comments, reviewer "
                     "requests, merges, mark-ready, retargets, pushes, or branch deletes), and it does "
                     "not replace human approval. This is not a merge UI.")
            form_note = ("Read-only. Requires <code>gh</code> installed and authenticated. For each "
                         "<em>request review</em> PR you get copyable <code>@codex review</code> text — "
                         "the dashboard never posts it. For mergeable PRs it shows a human merge-preflight "
                         "note, not a merge button.")
        self._send_html(_render("orchestrator.html", title="Review Queue", notice=notice, result=result,
                                mode_tag=mode_tag, intro=intro, form_note=form_note))

    # -- POST handlers ---------------------------------------------------------
    def _post_new_run(self, form):
        try:
            final = service.create_run(
                thread_id=form.get("thread_id", ""), task_type=form.get("task_type", "docs-advisory"),
                repo=form.get("repo", ""), pause_at=(form.get("pause_at") or None))
        except ValueError as ex:
            return self._page_new_run(notice=_alert("err", str(ex)))
        tid = final.get("thread_id") or form.get("thread_id", "")
        self._redirect_for_run(tid)

    def _post_decide(self, form):
        tid = form.get("thread_id", "")
        try:
            service.decide_gate(tid, form.get("gate", ""), form.get("decision", ""))
        except ValueError:
            pass                                       # stale/invalid -> just re-render the detail page
        self._redirect_for_run(tid)

    def _redirect_for_run(self, tid: str) -> None:
        """Detail page if the run still has a checkpoint (paused); else the runs page with a completion
        notice — a completed run's checkpoint is removed, so /run/<id> would 404."""
        quoted = urllib.parse.quote(tid, safe="")
        if service.get_run(tid) is None:
            self._redirect("/?done=" + quoted)
        else:
            self._redirect("/run/" + quoted)

    def _post_export(self, form):
        tid = form.get("thread_id", "")
        try:
            res = service.export_packet(tid, decision=form.get("decision", "approved"))
        except (ValueError, PacketError) as ex:
            return self._page_run_detail(tid, notice=_alert("err", "Export failed: %s" % ex))
        paths = res["paths"]
        notice = (_alert("ok", "Implementation Packet exported.")
                  + "<div class='card'><table class='kv'>"
                    "<tr><th>markdown</th><td><code>%s</code></td></tr>"
                    "<tr><th>json</th><td><code>%s</code></td></tr></table>"
                    "<p>%s</p></div>"
                    % (e(paths.get("md_path")), e(paths.get("json_path")), e(res.get("handoff"))))
        self._page_run_detail(tid, notice=notice)

    def _post_manual(self, form):
        try:
            res = service.create_manual_packet(
                thread_id=form.get("thread_id", ""), task=form.get("task", ""),
                repo=form.get("repo", ""), scope_markdown=form.get("scope_markdown", ""))
        except (ValueError, PacketError) as ex:
            return self._page_manual(notice=_alert("err", str(ex)))
        paths = res["paths"]
        warn = ""
        if res.get("unknown_headings"):
            warn = _alert("info", "ignored unrecognized scope sections: "
                          + ", ".join(res["unknown_headings"]))
        result = (
            "<div class='card'>"
            "<p class='marker'>%s</p>%s"
            "<table class='kv'>"
            "<tr><th>thread_id</th><td>%s</td></tr>"
            "<tr><th>task</th><td>%s</td></tr>"
            "<tr><th>markdown</th><td><code>%s</code></td></tr>"
            "<tr><th>json</th><td><code>%s</code></td></tr>"
            "</table>"
            "<h4>Suggested Claude Code handoff</h4><pre>%s</pre>"
            "</div>"
            % (e(res["marker"]), warn, e(res["thread_id"] if res.get("thread_id") else form.get("thread_id", "")),
               e(res["task"]), e(paths.get("md_path")), e(paths.get("json_path")),
               e(res.get("suggested_prompt"))))
        self._send_html(_render("manual_packet.html", title="Manual packet",
                                notice=_alert("ok", "Packet created."), result=result))

    def _post_watcher(self, form):
        try:
            res = service.run_watcher(form.get("repo", ""), init=bool(form.get("init")))
        except ValueError as ex:
            return self._page_watcher(notice=_alert("err", str(ex)))
        marker = res.get("marker")
        notice = _alert("ok", "marker: %s" % marker) if marker \
            else _alert("info", "No marker emitted (baseline recorded, or gh unavailable). See output.")
        result = ("<div class='card'><h4>Watcher output (read-only)</h4><pre>%s</pre></div>"
                  % e(res.get("output")))
        self._page_watcher_render(notice, result)

    def _page_watcher_render(self, notice, result):
        self._send_html(_render("watcher.html", title="Codex watcher", notice=notice, result=result))

    def _post_orchestrator(self, form):
        limit = form.get("limit") or 50
        try:
            res = service.run_orchestrator(form.get("repo", ""), limit=limit)
        except ValueError as ex:
            return self._page_orchestrator(notice=_alert("err", str(ex)))
        except GhError as ex:
            return self._page_orchestrator(notice=_alert("err", "gh error: %s" % ex))
        marker = res.get("marker")
        notice = _alert("ok" if marker == "ORCHESTRATION_PLAN" else "info", "marker: %s" % marker)
        # carry the SAME limit into the write forms so the POST-time candidate recompute matches this plan
        self._page_orchestrator(notice=notice,
                                result=_render_orchestration(res, self.server.allow_writes, limit))

    def _forbidden_writes(self) -> None:
        self._send_html(_render("message.html", title="Forbidden",
                                heading="403 — GitHub writes disabled",
                                body="<p>GitHub-write controls are off. Restart the dashboard with "
                                     "<code>--allow-github-writes</code> on a localhost bind to enable the "
                                     "opt-in writes (post <code>@codex review</code> · mark a draft "
                                     "ready).</p>"), code=403)

    def _post_codex_review_request(self, form):
        # GATE: writes must be enabled at startup (--allow-github-writes + localhost bind). The Host check
        # and same-origin CSRF check already ran for this POST. Nothing posts unless allow_writes is True.
        if not getattr(self.server, "allow_writes", False):
            return self._forbidden_writes()
        repo = form.get("repo", "")
        try:
            res = service.request_codex_review(repo, form.get("pr_number", ""),
                                               form.get("expected_head_sha", ""),
                                               form.get("confirmation", ""),
                                               limit=form.get("limit"))
        except ValueError as ex:
            return self._write_result_page(_alert("err", "Post refused: %s" % ex))
        except GhError as ex:
            return self._write_result_page(_alert("err", "gh error: %s" % ex))
        if res.get("ok") and res.get("duplicate"):
            # idempotent no-op: this dashboard already posted at this head — be honest (don't say "Posted")
            notice = _alert("info", "Already requested @codex review for #%s at this head — not re-posted."
                            % res.get("pr_number"))
        elif res.get("ok"):
            notice = _alert("ok", "Posted @codex review to #%s (head %s)."
                            % (res.get("pr_number"), str(res.get("head_sha"))[:8]))
        else:
            notice = _alert("err", "Post failed: %s" % res.get("error"))
        self._write_result_page(notice)

    def _post_mark_ready(self, form):
        # GATE: writes must be enabled at startup (--allow-github-writes + localhost bind). Host + CSRF
        # checks already ran for this POST. Nothing is written unless allow_writes is True.
        if not getattr(self.server, "allow_writes", False):
            return self._forbidden_writes()
        repo = form.get("repo", "")
        try:
            res = service.mark_ready_for_review(repo, form.get("pr_number", ""),
                                                form.get("expected_head_sha", ""),
                                                form.get("confirmation", ""),
                                                limit=form.get("limit"))
        except ValueError as ex:
            return self._write_result_page(_alert("err", "Mark-ready refused: %s" % ex))
        except GhError as ex:
            return self._write_result_page(_alert("err", "gh error: %s" % ex))
        if res.get("ok"):
            notice = _alert("ok", "Marked #%s ready for review. (Not merged — merge stays manual.)"
                            % res.get("pr_number"))
        else:
            notice = _alert("err", "Mark-ready failed: %s" % res.get("error"))
        self._write_result_page(notice)

    def _write_result_page(self, notice):
        self._send_html(_render("message.html", title="GitHub write", heading="GitHub write",
                                body=notice + "<p><a href='/orchestrator'>&larr; back to Review Queue</a></p>"))

    def _page_gpt_review(self, notice: str = "", result: str = "", repo: str = "", pr: str = "",
                         focus: str = "general", diff_budget: str = "compact",
                         include_feedback: bool = True):
        self._send_html(_render("gpt_review.html", title="GPT Review Prompt", notice=notice,
                                result=result, repo=e(repo), pr=e(pr),
                                focus_options=_opts(_FOCUS_OPTS, focus),
                                budget_options=_opts(_BUDGET_OPTS, diff_budget),
                                feedback_checked=("checked" if include_feedback else "")))

    def _post_gpt_review(self, form):
        repo, pr = form.get("repo", ""), form.get("pr_number", "")
        focus, budget = form.get("focus", "general"), form.get("diff_budget", "compact")
        include = bool(form.get("include_feedback"))
        try:
            res = service.build_gpt_review_prompt(repo, pr, focus=focus, diff_budget=budget,
                                                  include_existing_feedback=include)
        except (ValueError, GhError) as ex:
            msg = str(ex) if isinstance(ex, ValueError) else "gh error: %s" % ex
            return self._page_gpt_review(notice=_alert("err", msg), repo=repo, pr=pr, focus=focus,
                                         diff_budget=budget, include_feedback=include)
        self._page_gpt_review(result=_render_gpt_result(res), repo=res.get("repo") or repo,
                              pr=str(res.get("pr_number")), focus=res.get("focus") or focus,
                              diff_budget=res.get("diff_budget") or budget, include_feedback=include)

    def _page_codex_review(self, notice: str = "", result: str = "", repo: str = "", pr: str = "",
                           diff_budget: str = "compact"):
        self._send_html(_render("codex_review.html", title="Codex Review Prompt", notice=notice,
                                result=result, repo=e(repo), pr=e(pr),
                                budget_options=_opts(_BUDGET_OPTS, diff_budget)))

    def _post_codex_review(self, form):
        repo, pr = form.get("repo", ""), form.get("pr_number", "")
        budget = form.get("diff_budget", "compact")
        try:
            res = service.build_codex_prompt(repo, pr, diff_budget=budget)
        except (ValueError, GhError) as ex:
            msg = str(ex) if isinstance(ex, ValueError) else "gh error: %s" % ex
            return self._page_codex_review(notice=_alert("err", msg), repo=repo, pr=pr, diff_budget=budget)
        self._page_codex_review(result=_render_codex_result(res), repo=res.get("repo") or repo,
                                pr=str(res.get("pr_number")), diff_budget=res.get("diff_budget") or budget)

    def _page_packets(self, notice: str = ""):
        self._send_html(_render("packets.html", title="Packets", notice=notice,
                                rows=_packet_index(service.list_packets())))

    def _page_packet_detail(self, slug: str, notice: str = ""):
        try:
            p = service.get_packet(slug)
        except ValueError:
            return self._not_found()                       # unsafe slug / path traversal
        if p is None:
            return self._not_found()
        self._send_html(_render("packet_detail.html", title="Packet %s" % slug,
                                slug=e(p.get("slug")), body=_render_packet_detail(p, notice)))

    def _post_packet_status(self, form):
        slug = form.get("slug", "")
        try:
            service.set_packet_status(slug, form.get("status", ""))
        except ValueError:
            pass                                           # bad slug/status/packet -> re-render detail
        # PRG back to the detail page (only the local status file was touched)
        self._redirect("/packet/" + urllib.parse.quote(slug, safe=""))


def _allowed_hosts(host: str) -> set:
    """Host-header names this server accepts: the loopback names plus the configured bind host."""
    return set(_LOCALHOST_NAMES) | {host.strip().lower()}


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               allow_writes: bool = False) -> DashboardServer:
    """Build (but do not serve) the dashboard server bound to ``host:port``. Caller serves it.
    ``allow_writes`` enables the single opt-in GitHub-write control (post @codex review) — callers must
    pass it True ONLY for a localhost bind (main() enforces this)."""
    httpd = DashboardServer((host, port), Handler, _allowed_hosts(host), allow_writes=allow_writes)
    return httpd


def _open_browser(url: str) -> None:
    """Open ``url`` in a browser and report the ACTUAL outcome (webbrowser.open returns False — or raises —
    when there is no usable browser, e.g. a headless shell). Runs on a background thread."""
    try:
        ok = webbrowser.open(url)
    except Exception:
        ok = False
    if ok:
        print("[dashboard] opened %s in your browser." % url)
    else:
        print("[dashboard] could not open a browser automatically — open %s yourself." % url)


def _spawn_browser_open(url: str) -> None:
    """Open the browser on a DAEMON thread AFTER the serve loop is reachable, so a BLOCKING controller
    (a console browser like lynx/w3m) can't stall startup or block process exit. The listening socket is
    already bound, so the request queues and is served once serve_forever() runs."""
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Local DevFlow Dashboard (localhost-only, safe by default).")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="bind host (default 127.0.0.1 — localhost only; do NOT expose publicly)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default 8765)")
    p.add_argument("--open", action="store_true",
                   help="after starting, open the dashboard URL in your browser (localhost binds only)")
    p.add_argument("--allow-github-writes", action="store_true",
                   help="opt-in: enable the ONE real GitHub-write control (post '@codex review' from the "
                        "Review Queue, with typed confirmation). Localhost binds ONLY; off by default")
    args = p.parse_args(argv)
    is_local = args.host.strip().lower() in _LOCALHOST_NAMES
    if not is_local:
        sys.stderr.write(
            "[dashboard] WARNING: binding %s is NOT localhost. This dashboard is a local dev tool "
            "with no authentication — do not expose it on an untrusted network.\n" % args.host)
    # the ONE opt-in write capability is enabled ONLY with the flag AND a localhost bind.
    allow_writes = bool(args.allow_github_writes) and is_local
    if args.allow_github_writes and not is_local:
        sys.stderr.write(
            "[dashboard] REFUSED: --allow-github-writes requires a localhost bind; GitHub-write controls "
            "are DISABLED for %s.\n" % args.host)
    httpd = run_server(args.host, args.port, allow_writes=allow_writes)
    host_disp = "[%s]" % args.host if ":" in args.host else args.host    # bracket an IPv6 literal for the URI
    url = "http://%s:%d" % (host_disp, httpd.server_address[1])          # ACTUAL bound port (handles --port 0)
    print("[dashboard] serving on %s  (Ctrl-C to stop; localhost-only; %s)"
          % (url, "GitHub writes ENABLED (post @codex review only)" if allow_writes
             else "read-only / dry-run"))
    if args.open:
        # convenience only, stdlib webbrowser (no shell). NEVER auto-open a non-localhost bind.
        if is_local:
            _spawn_browser_open(url)
        else:
            print("[dashboard] --open skipped: %s is not a localhost bind; not auto-opening a browser."
                  % args.host)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
