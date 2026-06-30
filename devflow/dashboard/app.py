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
import urllib.parse
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


def _render_orchestration(result: dict) -> str:
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
    rr_html = (chips(rr) + "<p class='muted'>Recommended — copy this onto each PR yourself; the "
               "dashboard does <strong>not</strong> post it:</p><pre>@codex review</pre>") if rr \
        else "<p class='muted'>(none)</p>"

    rt_to = plan.get("retarget_to") or {}
    rt_html = chips(plan.get("needs_retarget"),
                    extra=lambda n: "retarget base &rarr; <code>%s</code>"
                    % e(rt_to.get(str(n)) or result.get("default_branch")))

    mn_html = chips(plan.get("mergeable_now"))
    if plan.get("mergeable_now"):
        mn_html += ("<p class='note'>Human merge preflight required — verify locally and merge via the "
                    "CLI/GitHub. The dashboard provides <strong>no</strong> merge button.</p>")

    return (
        card("Summary", summary)
        + (card("Errors", errors_html) if errs else "")
        + card("Ranking", ranking_html)
        + card("Request review (priority-ordered, ≤3 in-flight)", rr_html)
        + card("Findings to fix (P1/P2 or early P3)", chips(plan.get("findings_to_fix")))
        + card("Mergeable now (clean / converged)", mn_html)
        + card("Force-mergeable (≥3 rounds, only-minor P3)", chips(plan.get("force_mergeable")))
        + card("Ready then merge (un-draft first)", chips(plan.get("ready_then_merge")))
        + card("Resolve conflict (merge base in; never force-push)", chips(plan.get("needs_conflict")))
        + card("Needs retarget (parent PR merged)", rt_html)
        + card("Mergeability pending (GitHub still computing; re-run)", chips(plan.get("mergeable_unknown")))
        + card("In-flight (awaiting Codex)", chips(plan.get("in_flight")))
        + card("Raw plan (debug)", "<pre>%s</pre>" % e(json.dumps(result, ensure_ascii=False, indent=2))))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, allowed_hosts):
        # bind an IPv6 socket for an IPv6 host literal (e.g. ::1) — the default family is IPv4, so
        # without this `--host ::1` (advertised as a localhost name) would fail with an address error.
        if ":" in (addr[0] or ""):
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)
        self.allowed_hosts = set(allowed_hosts)


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
        self._send_html(_render("orchestrator.html", title="Review Queue", notice=notice, result=result))

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
        try:
            res = service.run_orchestrator(form.get("repo", ""), limit=(form.get("limit") or 50))
        except ValueError as ex:
            return self._page_orchestrator(notice=_alert("err", str(ex)))
        except GhError as ex:
            return self._page_orchestrator(notice=_alert("err", "gh error: %s" % ex))
        marker = res.get("marker")
        notice = _alert("ok" if marker == "ORCHESTRATION_PLAN" else "info", "marker: %s" % marker)
        self._page_orchestrator(notice=notice, result=_render_orchestration(res))


def _allowed_hosts(host: str) -> set:
    """Host-header names this server accepts: the loopback names plus the configured bind host."""
    return set(_LOCALHOST_NAMES) | {host.strip().lower()}


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> DashboardServer:
    """Build (but do not serve) the dashboard server bound to ``host:port``. Caller serves it."""
    httpd = DashboardServer((host, port), Handler, _allowed_hosts(host))
    return httpd


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Local DevFlow Dashboard (localhost-only, safe by default).")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="bind host (default 127.0.0.1 — localhost only; do NOT expose publicly)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default 8765)")
    args = p.parse_args(argv)
    if args.host.strip().lower() not in _LOCALHOST_NAMES:
        sys.stderr.write(
            "[dashboard] WARNING: binding %s is NOT localhost. This dashboard is a local dev tool "
            "with no authentication — do not expose it on an untrusted network.\n" % args.host)
    httpd = run_server(args.host, args.port)
    host_disp = "[%s]" % args.host if ":" in args.host else args.host    # bracket an IPv6 literal for the URI
    url = "http://%s:%d" % (host_disp, args.port)
    print("[dashboard] serving on %s  (Ctrl-C to stop; localhost-only, dry-run/read-only)" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
