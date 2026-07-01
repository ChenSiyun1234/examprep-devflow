# devflow

A LangGraph-based **development-workflow orchestrator** for the Exam-Prep project's repositories:
it models how a change moves through a repo — advisory → implement → review → merge — with
human-approval gates and Codex (AI reviewer) interactions.

> **This is developer tooling, not a product.** It is intentionally a **separate repository** from
> the exam-prep skill/product: devflow automates *developing* the repos, it is not a feature of the
> exam-prep runtime. It must never be imported by, or add dependencies to, that product.

## Status

A **dry-run scaffold** plus read-only and **guarded** GitHub integration. By default everything is
dry-run / read-only: no GitHub issues/PRs are created, no `@codex` comments are posted, nothing is
pushed or merged, and there are no Claude/Codex/OpenAI API calls. Real GitHub *writes* exist only
behind an explicit `--real-github` flag and are limited to creating issues/PRs and commenting —
there is **no merge, branch-delete, or force-push capability anywhere**.

## Install

Pure Python standard library — no install needed to run the dry-run/stdlib path.

```bash
# optional: the real LangGraph backend (otherwise a built-in stdlib fallback is used)
pip install -r devflow/requirements-dev.txt
```

## Usage

```bash
# end-to-end dry-run (all gates auto-approved) -> prints a final report
python -m devflow.cli run --task docs-advisory --thread-id demo-1

# human-approval pause (interrupt) then resume — stdlib fallback (JSON checkpoint)
python -m devflow.cli run    --task docs-advisory --thread-id demo-2 --pause-at advisory
python -m devflow.cli resume --thread-id demo-2 --gate advisory --decision approved

# same pause/resume on the real LangGraph backend (native interrupt + Command(resume=...))
python -m devflow.cli run    --task docs-advisory --thread-id lg-demo --langgraph --pause-at advisory
python -m devflow.cli resume --thread-id lg-demo --gate advisory --decision approved --langgraph
# rejecting safe-stops (no implementation, no merge):
python -m devflow.cli resume --thread-id lg-demo --gate advisory --decision rejected  --langgraph

# read-only GitHub inspection (needs an authenticated `gh` CLI)
python -m devflow.cli github-check
python -m devflow.cli read-issue --issue 123 --repo owner/name
python -m devflow.cli read-pr    --pr 456     --repo owner/name

# read-only Codex review watcher: scan OPEN PRs for NEW trusted-Codex feedback (deduped)
python -m devflow.cli watch-codex-reviews --repo owner/name --init   # baseline first (no alert flood)
python -m devflow.cli watch-codex-reviews --repo owner/name          # first-line marker (precedence below)
# ACTIONABLE_CODEX_REVIEWS > CODEX_WATCH_INCOMPLETE (read failed, retry) > CODEX_QUOTA_LIMITED (back off) > NO_NEW_CODEX_REVIEWS

# read-only cross-PR review ORCHESTRATOR: prints the recommended PLAN across the whole open-PR stack
# (priority-ordered review requests behind a <=3 in-flight cap, merge-ready / force-mergeable PRs,
# conflicts + stacked retargets to resolve, findings to fix, rate-limit). RECOMMENDS only — never merges.
python -m devflow.cli orchestrate-reviews --repo owner/name          # prints ORCHESTRATION_PLAN/NO_ACTION_NEEDED
python -m devflow.cli orchestrate-reviews --repo owner/name --json --exit-actionable

# advisory flow up to human approval; --real-github performs the guarded issue + @codex writes
python -m devflow.cli run-docs-advisory --thread-id demo [--real-github --max-polls 6 --poll-seconds 30]

# after approving a gate, export an Implementation Packet to hand off to Claude Code (local files only)
python -m devflow.cli run --task docs-advisory --thread-id demo --pause-at advisory
python -m devflow.cli export-implementation-packet --thread-id demo --decision approved  # --gate optional
```

## Cross-PR review orchestrator (read-only planner)

`watch-codex-reviews` tells you *which* PRs have new Codex feedback. `orchestrate-reviews` goes a step
further and computes a **deterministic plan across the whole open-PR stack**:

- **priority ranking** — unreviewed + big-feature PRs rank above well-reviewed or small-bugfix ones;
- **request review** — the PRs to send `@codex review` to next, priority-ordered behind a **≤3
  in-flight cap**. In-flight is **head-aware**: once a fix advances a PR's head, its old request goes
  stale and the PR re-enters the queue (this is the single gate for *all* requests — initial and
  re-review-after-a-fix, so you never hand-poke `@codex` out of band);
- **mergeable / force-mergeable** — PRs Codex called clean (rule 1), or that have been through ≥3
  rounds with only minor **P3** findings left (rule 2, severity read from Codex's own P1/P2/P3 badges);
- **resolve conflict / retarget** — a merge-ready PR that conflicts with `main` (resolve by merging
  `main` in — never force-push), or a stacked child whose base PR already merged (retarget to `main`);
- **findings to fix** — PRs with P1/P2 (or early P3) findings still open;
- **rate-limited** — whether Codex's globally-most-recent signal is a usage-limit notice.

It is **strictly read-only and never mutates GitHub** — it *recommends*; a human (devflow's
confirmation posture) or an external executor acts. There is no merge / comment / delete / push
capability here, exactly as in the rest of devflow. The only side effect is the tool's own local
tracking file (`<tmp>/devflow_runs/orchestrate_state.json`: head-aware in-flight + `--mark-converged`
pins), which is not a GitHub artifact. Use `--dry` to compute the plan without touching that file.

```bash
python -m devflow.cli orchestrate-reviews --repo owner/name            # ORCHESTRATION_PLAN / NO_ACTION_NEEDED
python -m devflow.cli orchestrate-reviews --repo owner/name --json     # + machine-readable plan
python -m devflow.cli orchestrate-reviews --repo owner/name --mark-converged 7  # pin #7's head as clean
```

## Implementation Packet (handoff to Claude Code)

devflow orchestrates and summarizes; **Claude Code remains the code editor**. After Codex
advisory/review is summarized and you approve a gate, devflow can export a structured
**Implementation Packet** — the handoff boundary:

> Codex advisory/review → devflow summarizes → **you approve** → devflow exports a packet →
> Claude Code implements the scoped changes → you review → PR / Codex review continues.

```bash
# pause at a gate (writes a checkpoint), then export the packet (--decision is required)
python -m devflow.cli run --task docs-advisory --thread-id demo --pause-at advisory
python -m devflow.cli export-implementation-packet --thread-id demo --decision approved
```

It writes two local files under a gitignored tool-state dir:

```
.devflow/packets/<safe-thread-id>/implementation-packet.md
.devflow/packets/<safe-thread-id>/implementation-packet.json
```

The packet contains: metadata (thread/task/repo/generated_at, source issue & PR), the approval
(gate, decision, approved scope, rejected/deferred), advisory/review content (summaries, blocking &
non-blocking comments, deferred follow-ups), implementation instructions for Claude Code (files
likely touched, tasks, out-of-scope, tests to run, safety rules), and explicit safety boundaries.

**This command makes no GitHub calls and never edits repository files** — it only reads the local
checkpoint and writes the two packet files. devflow does not implement code; the packet tells Claude
Code what to implement, within scope. On export it prints `IMPLEMENTATION_PACKET_EXPORTED`, the two
paths, the thread id, source issue/PR, and the suggested next Claude Code message.

> ⚠️ **Dry-run / simulated advisories produce a *generic* packet.** A `docs-advisory` run without
> real Codex input uses a simulated advisory, so the packet's approved scope is generic guidance
> ("scope to a dry-run scaffold", "add tests + docs") with **no `files likely touched` and no
> concrete tasks** — it is **not enough for real implementation**. For an actionable packet, either
> export from a thread backed by a **real** Codex advisory/review (a real PR with blocking comments
> yields concrete tasks + file paths), or use **`create-implementation-packet`** below to provide a
> concrete scope yourself.

### `create-implementation-packet` — packet from a human-provided scope

When there is no real advisory (or the simulated one is too generic), the human owner can write the
scope directly in a Markdown file and generate a packet from it — no checkpoint needed. The packet is
marked **`source: manual_human_scope`** so it can't be confused with a generic simulated-advisory one.

```bash
python -m devflow.cli create-implementation-packet \
  --thread-id check-runner-1 \
  --task "Add allowlisted check runner" \
  --scope-file scope.md \
  --repo ChenSiyun1234/examprep-devflow
```

**When to use which:**
- **`export-implementation-packet`** — you already ran the workflow to a paused gate and want to hand
  off the (real) advisory/review scope.
- **`create-implementation-packet`** — you want to hand off a concrete scope you wrote yourself,
  without first running an advisory.

Both write the same `.devflow/packets/<safe-thread-id>/implementation-packet.{md,json}` files, make
**no GitHub calls, run no shell, and never edit repository code**. The canonical safety boundaries
(no secrets/keys, no commit/push/PR, no merge, no branch-delete, no force-push, no false "tests
passed") are always embedded — a scope file may *add* rules but can never remove them. File paths in
the scope are filtered: absolute paths and `..` traversal are rejected (listed under out-of-scope).

Example `scope.md`:

```markdown
# Task
Add allowlisted check runner

# Approved scope
Implement a safe, allowlisted check runner for devflow.

# Files likely touched
- devflow/cli.py
- devflow/tools/check_runner.py
- tests/test_devflow_check_runner.py

# Out of scope
- arbitrary shell execution
- GitHub Actions
- automatic merge

# Checks to run
- python -m unittest discover -s tests

# Safety rules
- no secrets
- no branch deletion
```

On success it prints `MANUAL_IMPLEMENTATION_PACKET_CREATED`, the two paths, the thread id, the task,
and the suggested next Claude Code message.

## Dashboard (local web UI)

A tiny **local** dashboard to drive the common safe workflow from buttons/forms instead of typing
`python -m devflow.cli …`. It is pure Python standard library — **no install or dependency needed**:

```bash
python -m devflow.dashboard.app        # then open http://127.0.0.1:8765
python -m devflow.dashboard.app --open # start + open the URL in your browser (localhost binds only)
devflow-dashboard --open               # same, via the console script once the package is installed
# options: --host 127.0.0.1 (default; localhost-only) --port 8765
#          --allow-github-writes  opt-in: enable the 3 narrow writes (post '@codex review' / mark ready /
#                                 retarget base); localhost only
```

`--open` is a convenience (stdlib `webbrowser`, no shell); it is **skipped for a non-localhost `--host`**
(the warning still prints) and changes nothing about the read-only / dry-run safety posture.

`--allow-github-writes` is **off by default**. Without it the dashboard performs **no** real GitHub writes
at all (every button is read-only or dry-run). With it — and **only** on a localhost bind (enforced in the
server factory) — the Review Queue gains **three** narrow opt-in write controls described below; passing it
with a non-localhost `--host` is **refused** (a `REFUSED:` line prints and writes stay disabled). The three
controls are: (1) post the fixed comment `@codex review`, (2) mark a draft PR ready for review, and
(3) retarget a `needs_retarget` PR's base branch to the planner's exact target. All three require a typed
confirmation; **none merges** — and there is **no** other GitHub write.

Pages: **Runs** (list local checkpoints — thread id, status, paused gate) · **Run detail** (state
fields, event log, errors, and — when paused — the gate payload with **Approve / Reject / Export
Implementation Packet** buttons) · **New run** (create a dry-run run, optionally paused at a gate) ·
**Manual packet** (build an Implementation Packet from a Markdown scope form — same as
`create-implementation-packet` — and see the paths + suggested Claude Code handoff) · **Codex
watcher** (run the read-only `watch-codex-reviews` sweep and show its marker) · **Review Queue** (the
read-only `orchestrate-reviews` cross-PR plan) · **Codex Prompt** (build a copyable guided `@codex review`
comment — not posted) · **GPT Review Prompt** (build a copyable manual-review prompt — see below) ·
**Packets** (Implementation Packet lifecycle — see below).

**Packets (`/packets`, `/packet/<slug>`) — packet lifecycle, local-only.** A packet-centric view of every
Implementation Packet already generated under `.devflow/packets`: the index lists slug / thread / task /
repo / gate / decision / source issue·PR / handoff status; the detail page shows the full packet
(metadata, approved scope, tasks, files likely touched, out-of-scope, tests, safety boundaries, suggested
Claude Code handoff, and the md/json paths). It also tracks a **local-only** handoff lifecycle —
`created → handed_to_claude → in_progress → implemented → blocked → abandoned` — whose buttons write **only**
`.devflow/packets/<slug>/handoff-status.json`. **No GitHub reads or writes**, no LLM, no shell; slugs are
validated against path traversal and all packet fields are HTML-escaped.

**Review Queue (orchestrator) — read-only & advisory.** Surfaces the same cross-PR plan as
`orchestrate-reviews`: a priority ranking plus who to request review from, findings to fix, mergeable /
force-mergeable / ready-then-merge PRs, conflicts / retargets, mergeability-pending, in-flight, and the
global Codex rate-limit state. By default it **recommends** actions and **executes none**: for
*request review* it shows copyable `@codex review` text (it does not post it); for *mergeable* PRs it
shows a "human merge preflight required" note (there is **no** merge button). It does **not** merge, mark
ready, retarget, push, force-push, delete branches, request reviewers, or call any general GitHub write
API — and it does **not** replace human approval. By default it does **not** persist the orchestrator's
local tracking state (the dashboard never actually requests reviews, so it must not record in-flight
state). Both the CLI and the page call one structured helper
(`devflow/tools/review_orchestrator_runner.build_orchestration_result`), so there is no stdout scraping
and no behavioural drift.

**The opt-in writes (off unless `--allow-github-writes` on localhost).** When, and only when, the
dashboard was started with `--allow-github-writes` on a localhost bind, the Review Queue gains three — and
only three — confirm forms. Every attempt (success, failure, or refused) appends one line to a **local**
audit log, `.devflow/actions/dashboard-writes.jsonl` (timestamp, action, repo, PR, head SHA, result, and
for the comment its fixed body / for the retarget its from_base + to_base — no secrets, no GitHub
content). All mutations go through the existing guarded `GitHubWriter` (write-shape allow-list + secret
scan); the narrow helpers are in `devflow/tools/dashboard_writes`. **None merges** — merge stays a
human/manual step, out of scope.

1. **`Post @codex review`** (each *request review* PR). Posts a **real** PR comment whose body is
   **exactly** `@codex review` — the body is a hard-coded constant, never an input, so there is no
   arbitrary-comment box. Refused unless you type the exact phrase `POST @codex review to #<PR>`, the PR's
   current head still matches the page, and the PR is OPEN. A successful post stamps the local
   orchestrator's `requested_head` (serialized so concurrent posts don't double-request) so the planner
   stops re-recommending it. (Honest caveat: like *any* PR comment, this can trigger the **target repo's
   own** `on: issue_comment` workflows if defined — inherent to commenting; the dashboard itself never
   calls `gh workflow` / `workflow_dispatch`.)

2. **`Mark ready for review`** (each *ready then merge* DRAFT PR). Runs **exactly** `gh pr ready <PR>` —
   it un-drafts the PR and nothing else (no `--undo`/convert-to-draft, no flags, no merge). Refused unless
   you type the exact phrase `MARK #<PR> READY`, the PR is still a member of the current `ready_then_merge`
   set, its head still matches the page, and it is still OPEN **and still a draft**. It does **not** merge,
   request reviewers, retarget, close, push, or delete branches, and it touches no orchestrator state.
   (Honest caveat: like readying *any* PR, this can trigger the **target repo's own**
   `pull_request: ready_for_review` workflows if defined — inherent to un-drafting; the dashboard itself
   never invokes Actions.) Helper: `devflow/tools/dashboard_writes.mark_pr_ready_for_review` → the narrow
   `GitHubWriter.mark_pr_ready`.

3. **`Retarget base to <target>`** (each *needs retarget* PR). Runs **exactly**
   `gh pr edit <PR> -R <repo> --base <target>` — it changes **only** the base branch to the **exact**
   target the planner computed (`retarget_to`), and nothing else. This is **not** a generic `pr edit`: the
   write guard rejects any `pr edit` flag other than `-R`/`--base`, so title/body/reviewers/state/draft are
   never touched; it is **not** a merge, rebase, push, or force-push. The target must be a safe simple
   branch name (`[A-Za-z0-9._/-]`, no `..`/leading-`-`/slashes/whitespace/`:`/backslash/metacharacters).
   Refused unless you type the exact phrase `RETARGET #<PR> TO <target>`, the PR is still in the current
   `needs_retarget` set, the recomputed `retarget_to[PR]` still equals your target, its head **and** its
   current base still match the page, and it is still OPEN. It touches **no** orchestrator state; because
   the base change alters the PR diff, **any prior Codex review is stale** — re-request `@codex review`
   and recompute the Review Queue afterwards (the planner keys "clean" by head SHA, which a base change
   does not move, so it will not on its own ask for a fresh review). Helper:
   `devflow/tools/dashboard_writes.retarget_pr_base` → the narrow `GitHubWriter.retarget_pr_base`.

**GPT fallback prompt builder (`/gpt-review`) — read-only text builder.** Creates a copyable
GPT/ChatGPT code-review prompt for a PR from read-only GitHub data (PR metadata, changed files, a capped
diff excerpt, and — optionally — recent Codex feedback), with a focus mode (general / safety / tests /
docs / verify-fix) and a diff budget (compact / medium / large). Useful when Codex is quota-limited or
you want a manual second opinion. It **does not call GPT/OpenAI/Anthropic/Codex or any LLM** (no SDK, no
API key, no network beyond read-only `gh`) and **sends your code nowhere** — it only builds text you copy
and paste yourself. The prompt instructs the model to use a strict P1/P2/P3 findings format and to never
suggest merge / push / branch-delete / GitHub Actions / secrets; truncated diffs and feedback are flagged.
**Private repos require human judgment before pasting into external tools** (the page warns prominently).
Backed by `devflow/tools/fallback_review_prompt.build_fallback_review_prompt` (read-only).

**Safe by default — what it does / does not do.**

- It is **local-only**: binds `127.0.0.1` by default and rejects requests whose `Host` header is not
  a localhost name (DNS-rebinding defense). State-changing POSTs additionally require a same-origin
  request (`Sec-Fetch-Site` / `Origin` validation), so another site you have open can't forge an
  action against the dashboard (CSRF defense). It is **not** exposed publicly and has no authentication —
  do not bind it to a public interface.
- Every action is **read-only or dry-run by default**: runs use the pure-stdlib fallback backend with
  `real_github` forced off, so it performs **no** real GitHub writes; packets are the same local
  files the CLI writes; the watcher is the CLI's read-only sweep. The **only** exceptions are the three
  opt-in controls — `Post @codex review`, `Mark ready for review`, and `Retarget base` — which exist only
  under `--allow-github-writes` on a localhost bind (see the Review Queue section above).
- It **cannot** merge, request reviewers, close PRs, delete branches, force-push, push, rebase, add
  GitHub Actions, run arbitrary shell commands (there is no such endpoint), edit code, post any comment
  other than the fixed `@codex review`, run a generic `gh pr edit` (only base-retarget is allowed), or
  perform any GitHub write other than those three opt-in controls (and only when explicitly opted in). The
  CLI remains fully supported and is the source of truth; the dashboard only calls the same functions (see
  `devflow/dashboard/service.py` and the narrow `devflow/tools/dashboard_writes.py`).

**Not yet (out of scope for this MVP):** the real LangGraph backend (the dashboard always uses the
stdlib fallback), authentication, public deployment, and any GitHub write beyond the three opt-in,
localhost-only, strongly-confirmed controls (post `@codex review` · mark a draft ready · retarget a base).
**Merge stays a human/manual step** — there is no merge button.

## LangGraph Studio (`langgraph dev`)

The graph is Studio-loadable: `langgraph.json` (repo root) points Studio at the
`make_graph` factory in `devflow/graph.py`, which returns the **uncompiled** `StateGraph` so the
LangGraph platform supplies its own persistence (threads + interrupts).

```bash
pip install -U "langgraph-cli[inmem]"
pip install -r devflow/requirements-dev.txt
langgraph dev
```

Expected behavior in Studio:
- the graph appears as **`devflow`**;
- you can inspect all nodes (the 20-node advisory → review → merge workflow);
- you can run a thread by supplying an input state (e.g. `{"task_type": "docs-advisory",
  "thread_id": "demo", "approvals": {}}`);
- the **human-approval gates appear as interrupts** (the run pauses at `human_approval_gate` /
  `human_fix_approval` / `human_merge_approval`); resume by supplying the gate decision;
- the current **state fields are visible** in the state panel.

Everything in Studio is still **dry-run by default** (no real GitHub writes) — `real_github` stays
False unless you explicitly set it.

> Note: `make_graph` is a factory (not a module-level `graph` variable) on purpose — importing
> `devflow.graph` must not require langgraph or build a graph, and without langgraph a module-level
> object would be the stdlib fallback, which Studio can't render.

## Tests

```bash
python -m unittest discover -s tests
```

## Layout

```
devflow/
  state.py          typed workflow state (TypedDict; Python 3.8-compatible)
  graph.py          builds the graph (real LangGraph if installed, else a stdlib fallback runner)
  cli.py            command-line entry point
  _compat.py        LangGraph/stdlib interrupt shim
  nodes/            dry-run workflow nodes (environment / advisory / approval / pr_review / merge)
  tools/github_cli.py   DryRunGitHub (write no-ops) + ReadOnlyGitHub + guarded GitHubWriter
  dashboard/        local web UI (stdlib http.server): app.py + service.py + templates/ (localhost-only)
docs/devflow-langgraph.md   design notes: state machine, approval gates, safety boundaries, roadmap
tests/                lightweight tests (gh fully mocked; no network)
```

## Safety boundaries

Never: merge a PR · delete a branch · force-push · add GitHub Actions · add secrets/API keys ·
implement product runtime · auto-apply a Codex advisory without human approval.

## Provenance

Extracted from the `universal-examprep-skill` repository, where it was first scaffolded across three
stacked draft PRs (now closed in favour of this standalone repo). See `docs/devflow-langgraph.md`
for the full design and the planned next steps (real GitHub reads → guarded writes → Codex polling →
human-approved merge execution).
