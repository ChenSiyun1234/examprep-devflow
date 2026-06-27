"""Advisory phase nodes — create an advisory issue, ask Codex for an advisory, wait for it
(bounded poll in real mode), and summarize it.

Dry-run mode (default): GitHub writes are logged no-ops and the Codex advisory is simulated.
Real mode (``state['real_github']``): the create/comment go through the guarded ``GitHubWriter``
and the wait node polls real issue comments via ``ReadOnlyGitHub`` with a bounded number of tries.
"""

from __future__ import annotations

from devflow.state import DevflowState
from devflow.tools.github_cli import GitHubWriter, ReadOnlyGitHub, bounded_poll, GhError


def _writer(state: DevflowState) -> GitHubWriter:
    return GitHubWriter(state["repo"], live=bool(state.get("real_github")))


def create_advisory_issue(state: DevflowState) -> dict:
    # No labels: `gh issue create --label` fails if the label doesn't already exist on the repo,
    # which would break live issue creation before the @codex request. Keep it label-free.
    res = _writer(state).create_advisory_issue(
        title=f"[advisory] {state['task_type']}",
        body="Automated advisory request from devflow. Please provide an implementation advisory.",
    )
    upd = {"event_log": [f"[create_advisory_issue] {res.get('log', '')}"]}
    if res.get("error"):
        return {**upd, "errors": [f"create_advisory_issue: {res['error']}"]}
    upd["issue_number"] = res.get("number")
    upd["issue_url"] = res.get("url")
    upd["issue_simulated"] = bool(res.get("simulated"))   # provenance: refuse live resume on fakes
    return upd


def request_codex_advisory(state: DevflowState) -> dict:
    issue = state.get("issue_number")
    if not issue:   # issue creation failed/unparsed — stop safely instead of commenting on #0
        return {"codex_advisory_status": "timeout",
                "errors": ["request_codex_advisory: no issue number (creation failed) — "
                           "refusing to comment on #0"],
                "event_log": ["[request_codex_advisory] stopped: no issue number; "
                              "not commenting on #0."]}
    res = _writer(state).comment_on_issue(
        issue, "@codex please provide an implementation advisory for this task.")
    if res.get("error"):
        # the comment failed → the request was never sent, so don't poll for a response. Terminal
        # safe-stop (timeout) routes to post_merge_report instead of waiting the full budget.
        return {"codex_advisory_status": "timeout",
                "errors": [f"request_codex_advisory: {res['error']}"],
                "event_log": [f"[request_codex_advisory] FAILED to post '@codex' "
                              f"({res['error']}); stopping instead of polling."]}
    return {"codex_advisory_status": "requested",
            "event_log": [f"[request_codex_advisory] {res.get('log', '')}"]}


def _simulated_packet(state: DevflowState) -> dict:
    return {
        "task_type": state["task_type"],
        "recommended_steps": [
            "scope the change to a dry-run scaffold",
            "model the workflow as a typed state graph",
            "add tests + docs; avoid real side effects",
        ],
        "risks": ["scope creep into product runtime", "accidental real GitHub mutations"],
        "source": "simulated-codex-advisory",
    }


def wait_for_codex_advisory(state: DevflowState) -> dict:
    """Real mode: bounded poll of issue comments for a Codex advisory. Dry-run: simulate.

    ``state['_simulate']['advisory'] == 'timeout'`` forces the timeout branch in dry-run/tests.
    """
    if state.get("codex_advisory_status") == "timeout":   # request already failed/safe-stopped
        return {"event_log": ["[wait_for_codex_advisory] skipped: advisory request did not "
                              "succeed; not polling."]}
    if not state.get("issue_number"):   # nothing to poll — don't hit issue #0
        return {"codex_advisory_status": "timeout",
                "errors": ["wait_for_codex_advisory: no issue number to poll"],
                "event_log": ["[wait_for_codex_advisory] stopped: no issue number."]}
    if state.get("real_github"):
        repo = state["repo"]
        issue = state.get("issue_number")
        sleep_fn = (state.get("_sleep_fn") or None)
        kwargs = {"sleep_fn": sleep_fn} if sleep_fn else {}
        try:
            poll = bounded_poll(
                lambda: ReadOnlyGitHub(repo).find_latest_codex_advisory(issue),
                max_attempts=int(state.get("max_polls", 6)),
                sleep_seconds=int(state.get("poll_seconds", 30)),
                **kwargs,
            )
        except GhError as e:
            return {"codex_advisory_status": "timeout",
                    "errors": [f"wait_for_codex_advisory: {e}"],
                    "event_log": [f"[wait_for_codex_advisory] gh error: {e}"]}
        if poll["found"]:
            return {"codex_advisory_status": "ready", "advisory_packet": poll["result"],
                    "event_log": [f"[wait_for_codex_advisory] Codex advisory found "
                                  f"after {poll['attempts']} poll(s)."]}
        return {"codex_advisory_status": "timeout",
                "errors": [f"no Codex advisory after {poll['attempts']} poll(s)"],
                "event_log": [f"[wait_for_codex_advisory] TIMEOUT after {poll['attempts']} "
                              f"bounded poll(s) — Codex did not respond."]}

    # dry-run simulation
    sim = (state.get("_simulate") or {}).get("advisory", "ready")
    if sim == "timeout":
        return {"codex_advisory_status": "timeout",
                "errors": ["codex advisory did not arrive within the (simulated) bound"],
                "event_log": ["[wait_for_codex_advisory] dry-run: simulated TIMEOUT."]}
    return {"codex_advisory_status": "ready", "advisory_packet": _simulated_packet(state),
            "event_log": ["[wait_for_codex_advisory] dry-run: simulated Codex advisory received."]}


def summarize_advisory(state: DevflowState) -> dict:
    packet = state.get("advisory_packet") or {}
    steps = packet.get("recommended_steps", [])
    if steps:
        summary = "Advisory: " + "; ".join(steps)
    elif packet.get("body"):
        summary = "Advisory (from Codex): " + packet["body"][:200].replace("\n", " ")
    else:
        summary = "Advisory: (empty)"
    return {"advisory_packet": {**packet, "summary": summary},
            "event_log": [f"[summarize_advisory] {summary}"]}
