# devflow — LangGraph development-workflow orchestrator (dry-run scaffold)

`devflow/` is a small orchestrator that models how a change moves through this repository:
advisory → implement → review → merge, with human approval gates and Codex (AI reviewer)
interactions. **This first PR is a dry-run scaffold only**: it builds the graph, runs mock nodes,
simulates Codex responses, and prints a report. It performs **no** real GitHub mutations, network
calls, or AI-provider API calls.

## How this differs from the Exam Prep product runtime

| | Exam Prep product (the skill) | devflow (this package) |
|---|---|---|
| Purpose | Help a student cram for an exam | Automate *development of this repo* |
| Audience | End users / students | Maintainers / CI |
| Runtime deps | Pure Python stdlib (no pip) | stdlib by default; LangGraph optional (dev only) |
| Side effects | Reads/writes the user's study workspace | *Would* touch GitHub — but disabled in this PR |

devflow is **developer tooling**, not part of the product. It must never be imported by, or add
dependencies to, the Exam Prep runtime.

## The state machine

```
start
  → check_environment
  → create_advisory_issue
  → request_codex_advisory
  → wait_for_codex_advisory ──(timeout)──────────────► post_merge_report (safe stop)
  → summarize_advisory
  → human_approval ──────────(rejected)──────────────► post_merge_report (safe stop)
  → apply_approved_changes
  → run_checks
  → commit_push_branch
  → create_draft_pr
  → request_codex_review
  → wait_for_codex_review ───(timeout)───────────────► post_merge_report (safe stop)
  → summarize_review ────────(no blocking comments)──► merge_readiness   (skip fix gate)
  → human_fix_approval ──────(rejected)──────────────► post_merge_report (safe stop)
  → fix_blocking_comments
  → request_codex_rereview
  → merge_readiness ─────────(not ready)─────────────► post_merge_report (safe stop)
  → human_merge_approval ────(rejected)──────────────► post_merge_report (safe stop)
  → claude_execute_merge     (dry-run: NEVER actually merges)
  → post_merge_report → END
```

State is a typed `TypedDict` (`devflow/state.py`). List fields (`event_log`, `errors`,
`blocking_comments`, `files_changed`, …) use an `operator.add` reducer so node updates *append*;
scalar fields are last-write-wins. The fallback runner reads the same annotations so both backends
merge identically. Key fields: `task_type, thread_id, repo, branch_name, issue_number, issue_url,
pr_number, pr_url, codex_advisory_status, codex_review_status, advisory_packet, review_summary,
blocking_comments, non_blocking_comments, deferred_followups, human_approval, merge_approval,
checks_run, checks_not_run, files_changed, errors, event_log`.

## Two interchangeable backends

`devflow/graph.py:build_graph()` returns either:

* **LangGraph backend** — used automatically if `langgraph` is importable. Builds a real
  `StateGraph`, compiles it with a `MemorySaver` checkpointer, and pauses at approval gates with
  native `interrupt()` (resumed via `Command(resume=...)`).
* **Fallback backend** — pure stdlib (the default here, since langgraph is an *optional* dev dep).
  A deterministic runner walks the same node/edge map and pauses by raising `DevflowInterrupt`.

Both expose `.invoke(state)` and the same nodes/routing, so behaviour is identical; only the
interrupt/checkpoint machinery differs.

The CLI **defaults to the stdlib backend** (fully supported). The real LangGraph backend is
opt-in and experimental via `--langgraph` (`pip install langgraph`); when selected, the CLI passes
a `config={"configurable": {"thread_id": …}}` so its `MemorySaver` checkpointer works, and the
fallback runner sets `_force_fallback` so approval gates always use `DevflowInterrupt` rather than
LangGraph's native `interrupt()` when running under the stdlib backend. `DevflowState` declares all
control channels (`fix_approval`, `merge_readiness_ready`, `rereview_done`, `_simulate`, …) so the
real `StateGraph` does not drop them. An explicit `pause_at` always pauses its gate, even if an
approval was seeded. `merge_readiness` requires a **completed** (re-)review — never merge-ready while
a re-review is only "requested".

## LangGraph Studio

`langgraph.json` (repo root) makes the graph loadable by `langgraph dev` / LangGraph Studio:

```json
{ "dependencies": ["."], "graphs": { "devflow": "./devflow/graph.py:make_graph" } }
```

`make_graph()` returns the **uncompiled** `StateGraph` (built by the shared `_build_state_graph()`),
so the LangGraph platform attaches its own checkpointer — that's what lets Studio drive threads and
render the approval gates as interrupts. Importing `devflow.graph` never builds a graph or imports
langgraph (it's imported lazily inside the builder), so the stdlib fallback path is unaffected.

Run it:

```bash
pip install -U "langgraph-cli[inmem]"
pip install -r devflow/requirements-dev.txt   # or: pip install -e ".[studio]"
langgraph dev
```

In Studio: the graph shows as `devflow`; all 20 nodes are inspectable; a thread can be run from an
input state; unseeded human-approval gates pause as resumable interrupts (`human_approval_gate`,
`human_fix_approval`, `human_merge_approval`); and the state channels are visible. It stays dry-run
(no real GitHub writes) unless `real_github` is set.

> **Node vs state-key naming.** LangGraph forbids a node name that equals a state channel, so the
> advisory-approval node is registered as **`human_approval_gate`** (the state field stays
> `human_approval`). This is asserted by a test so the graph keeps building under LangGraph/Studio.

## How human approval gates work

There are three gates, each calling `request_human_decision(...)` **exactly once per node
invocation** (one interrupt per node — never inside a loop):

1. **advisory implementation** (`human_approval`)
2. **blocking fix** (`human_fix_approval`)
3. **merge** (`human_merge_approval`)

A decision is resolved by (a) a value pre-seeded in `state["approvals"][gate]` — the dry-run policy
or the resume payload — otherwise (b) the workflow pauses: a real `interrupt()` under LangGraph, or
a `DevflowInterrupt` in fallback mode. A rejected gate routes to a safe stop. Approve/reject is never
inferred; the run halts until a decision is supplied.

## Resume: stdlib fallback vs real LangGraph

Both backends pause at the *same* three gates and never infer a decision; they differ only in how
the pause is persisted and resumed.

| | stdlib fallback (default) | real LangGraph backend (`--langgraph`) |
|---|---|---|
| pause mechanism | raises `DevflowInterrupt`, returns `status="paused"` | native `interrupt(payload)` |
| checkpoint | a JSON file per `thread_id` | a **SQLite** checkpointer keyed by `thread_id` |
| resume | re-run from `paused_at_node` with the gate seeded in `approvals` | `Command(resume="approved"\|"rejected")` |
| extra deps | none (pure stdlib) | `langgraph` + `langgraph-checkpoint-sqlite` |

`thread_id` is the stable key for a run in **both** backends: the id you pass to `run` is the id you
pass to `resume`, and it is the thread you select in LangGraph Studio.

```bash
# real LangGraph backend: pause at the advisory gate, resume natively
python -m devflow.cli run    --task docs-advisory --thread-id lg-demo --langgraph --pause-at advisory
python -m devflow.cli resume --thread-id lg-demo --gate advisory --decision approved --langgraph
python -m devflow.cli resume --thread-id lg-demo --gate advisory --decision rejected  --langgraph  # safe stop
```

Under the hood the approval node calls `interrupt(payload)`; resuming with
`Command(resume="approved")` makes that call *return* the decision, so state is preserved across the
gate and the graph continues from exactly where it paused. A rejected decision routes straight to the
post-run report — the implementation/merge nodes never run.

In **LangGraph Studio** the same gates appear as interrupts: run a thread, it pauses at
`human_approval_gate` / `human_fix_approval` / `human_merge_approval` showing the interrupt payload,
and you resume by supplying the decision — no CLI needed.

**Known limitations**
- The LangGraph *durable* CLI resume needs `langgraph-checkpoint-sqlite` (explicit optional dep, in
  the `[studio]`/`[langgraph]` extras); without it the CLI prints an install hint and exits non-zero.
  The stdlib fallback resume has no such dependency.
- `interrupt()` is called **once per gate, never inside a loop**. On resume the platform re-executes
  the node up to that `interrupt()` call; the approval nodes do no work before the call, so nothing is
  duplicated and no GitHub write is ever repeated.
- The LangGraph resume path is **dry-run only** in this PR — it does not re-enable `--real-github`.
  Guarded real writes remain on the stdlib `run-docs-advisory` path.
- Studio's UI (`langgraph dev`) additionally needs `langgraph-cli[inmem]` + a browser.

## What is dry-run in this PR (hard boundaries)

The scaffold **does not**: create GitHub issues, post `@codex` comments, create branches/PRs, push,
or merge; it does not edit product files, add LangGraph to the product runtime, or make any
Claude/Codex/OpenAI/Anthropic API calls. `devflow/tools/github_cli.py` is the single chokepoint for
GitHub operations and every method is a recorded no-op (`executed: False`). `run_checks` does **not**
execute checks and therefore never claims any passed — it records them under `checks_not_run`.
No secrets, no API keys, no paid CI, no GitHub Actions.

## Usage

```bash
# end-to-end dry-run (all gates auto-approved) -> prints a final report
python -m devflow.cli run --task docs-advisory --thread-id demo-1

# demonstrate a human-approval pause (interrupt) then resume
python -m devflow.cli run    --task docs-advisory --thread-id demo-2 --pause-at advisory
python -m devflow.cli resume --thread-id demo-2 --gate advisory --decision approved

# safe-stop routes
python -m devflow.cli run --task docs-advisory --thread-id demo-3 --reject merge
python -m devflow.cli run --task docs-advisory --thread-id demo-4 --simulate-review clean
python -m devflow.cli run --task docs-advisory --thread-id demo-5 --simulate-advisory timeout
```

Tests: `python -m unittest tests.test_devflow_graph`

## Read-only GitHub integration

`devflow/tools/github_cli.py` has two clearly separated layers:

* **Write layer** (`DryRunGitHub`) — issue/PR/branch/merge operations are recorded no-ops in this
  scaffold (`executed: False`).
* **Read-only layer** (`ReadOnlyGitHub` + `check_gh_available()`) — inspects issues, PRs, comments
  and reviews via the `gh` CLI. **Strictly read-only.** Every `gh` invocation passes through
  `_assert_read_only()`, the single safety chokepoint, which:
  * allow-lists only read shapes (`gh auth status`, `gh repo view`, `gh issue view`, `gh pr view`,
    `gh pr/issue list`, `gh pr diff`, and `gh api`);
  * refuses `gh api` with a write method (`-X POST/PUT/PATCH/DELETE`) or write-style field flags
    (`-f/--field/-F/--raw-field/--input` — which would make `gh api` POST);
  * runs **before** any subprocess is spawned, so a write-shaped command never executes.

There is no code path in the read-only layer that can create, comment, push, or merge.

### Functions

* `check_gh_available()` → `{available, authenticated, account, error}` (never raises)
* `ReadOnlyGitHub(repo=None).get_repo_info()`
* `.get_issue_comments(issue_number)` / `.get_pr_comments(pr_number)` / `.get_pr_reviews(pr_number)`
* `.find_latest_codex_advisory(issue_number)` — newest comment authored by a Codex account
* `.find_latest_codex_review(pr_number)` — newest Codex PR comment/review, with a light
  `blocking` heuristic (CHANGES_REQUESTED or "blocking/must fix" language) and parsed bullet items

Codex authorship is matched against an **exact trusted-login allowlist** (e.g.
`chatgpt-codex-connector[bot]`, `codex`) — not a loose "contains codex/chatgpt" check — so a
look-alike login on a public repo can't spoof the integration. Paginated reads use
`gh api --paginate --slurp` so multi-page results stay valid JSON, and PR reviews include inline
(file-level) review comments with the strongest review state preserved.

### Prerequisite (local)

An **authenticated `gh` CLI** is required for the read commands:

```bash
gh --version        # GitHub CLI installed
gh auth login       # one-time authentication
```

If `gh` is missing or unauthenticated, the read commands print a clear error and exit non-zero
(they never fall back to anything that mutates state).

### Examples

```bash
python -m devflow.cli github-check
python -m devflow.cli read-issue --issue 123 [--repo owner/name]
python -m devflow.cli read-pr    --pr 456     [--repo owner/name]
```

`--repo` defaults to the current repository. Tests for this layer mock `gh` entirely
(`tests/test_devflow_github_readonly.py`): they assert no write command can run, that a Codex
advisory/review is detected/parsed from sample comments/reviews, and that gh-unavailable errors
are clear.

## Read-only Codex review watcher (`watch-codex-reviews`)

A polling-friendly, **strictly read-only** command that scans a repo's **open** PRs for **new**
trusted-Codex reviews/comments and reports a machine-greppable marker.

```bash
python -m devflow.cli watch-codex-reviews --repo owner/name --init   # baseline once (no alert flood)
python -m devflow.cli watch-codex-reviews --repo owner/name          # then poll
```

How it works:

* lists open PRs with `gh pr list --state open` (`ReadOnlyGitHub.list_open_prs`, an allow-listed read);
* for each PR, takes the **latest trusted-Codex signal** via `find_latest_codex_review` (so a
  spoofed `codex-fan` login is ignored — same exact-login allowlist as the rest of the read layer);
* **dedupes** against a local seen file: per repo, per PR, it stores the latest item's
  `created_at|url` key; a PR is *new* iff that key changed since last run.

Output contract (the **marker is the first line**, so `grep`/`startswith` both work):

* `ACTIONABLE_CODEX_REVIEWS` — ≥1 open PR has new Codex feedback (followed by per-PR details);
* `NO_NEW_CODEX_REVIEWS` — nothing new (also when there are zero open PRs);
* `--init` records the current state as the baseline and prints **neither** marker (so the first
  real run isn't a flood of pre-existing reviews).

Flags: `--repo` (default: current repo), `--seen-file` (default `<tmp>/devflow_runs/codex_seen.json`),
`--limit` (max open PRs, clamped ≥1), `--reset` (re-alert this repo's current feedback; other repos
in a shared seen file are untouched), `--init`, `--json` (machine-readable summary in addition to the
marker), `--exit-actionable` (opt-in: exit `10` instead of `0` when actionable), `--body-chars`.

Safety: the command constructs only `ReadOnlyGitHub` (never a writer), every `gh` call passes the
`_assert_read_only` chokepoint, and the **only** filesystem write is the local seen file under the
temp dir — it is the tool's own state, **not** a GitHub artifact (no comment/commit/push/merge).
Exit codes follow the read-command convention (0 success — for both markers; 1 gh error; 2/3
gh unavailable/unauthenticated). Tests mock `gh` entirely
(`tests/test_devflow_watch_codex.py`): actionable/none/dedupe/spoof/per-PR-error/init/json/
exit-actionable plus an assertion that **every** spawned `gh` command is read-only.

## Implementation Packet (handoff to Claude Code)

The packet is the **boundary between orchestration and editing**: devflow summarizes the
advisory/review and records the human approval, then exports a structured packet that **Claude Code**
uses to make the scoped edits. devflow never edits repository files itself.

```
Codex advisory/review → devflow summarizes → human approves → devflow exports Implementation Packet
   → Claude Code implements scoped changes → human reviews → PR / Codex review continues
```

**Design (fits the checkpoint model).** A standalone, read-only command:

```bash
python -m devflow.cli run    --task docs-advisory --thread-id demo --pause-at advisory  # writes a checkpoint
python -m devflow.cli export-implementation-packet --thread-id demo --decision <approved|rejected> [--gate advisory]
```

`export-implementation-packet` loads the thread's **checkpoint** (which exists precisely when the run
is paused at a gate), builds the packet from that state, and writes two files. It deliberately does
**not** run the graph, add a graph node, call `gh`, or edit code — so the handoff is fully decoupled
from the workflow and has no side effects beyond the two local files. The gate defaults to the
thread's `paused_at_gate`; `--decision` is **required** (`approved` or `rejected`) — there is no
silent default, so an approval is never inferred.

**Output (local tool-state, gitignored — never tracked):**

```
.devflow/packets/<safe-thread-id>/implementation-packet.md
.devflow/packets/<safe-thread-id>/implementation-packet.json
```

`<safe-thread-id>` is sanitized (non-`[A-Za-z0-9-_.]` → `_`, length-bounded) and suffixed with a
hash of the original id, so no thread id can cause a path collision or directory traversal. `.devflow/`
is in `.gitignore`.

**Packet contents** (`devflow/tools/packet_writer.py`, pure builder + renderer):

1. **Metadata** — thread_id, task_type, repo, generated_at, source issue #/URL, source PR #/URL.
2. **Approval** — gate, decision, approved scope, rejected/deferred items.
3. **Advisory / review** — advisory summary, review summary, blocking & non-blocking comments,
   deferred follow-ups.
4. **Implementation instructions for Claude Code** — files likely touched, concrete tasks, explicit
   out-of-scope items, tests/checks to run, safety rules.
5. **Safety boundaries** — no secrets, no API keys, no unrelated rewrites, no commit/push/PR, no
   merge, no branch-deletion, no force-push, no "tests passed" claims unless they actually ran, ask
   before expanding scope.

**Simulated vs real advisories (important).** A dry-run `docs-advisory` with no real Codex input
uses a *simulated* advisory (`_simulated_packet`), whose `recommended_steps` are generic guidance
("scope to a dry-run scaffold", "add tests + docs"). The resulting packet therefore has a **generic
approved scope, no `files_likely_touched`, and no concrete tasks** — it is **not sufficient for real
implementation**. An actionable packet needs a **real** advisory/review: a real PR with blocking
comments yields concrete `tasks` and file paths. (Dedicated manual-scope packet support is a planned
follow-up.)

**Untrusted-input handling.** Advisory/review content and file paths are attacker-influenceable
(they originate from Codex/GitHub). The builder defends accordingly: `files_likely_touched` is
filtered to repo-relative paths (absolute paths and `..` traversal are rejected and listed under
out-of-scope), and the Markdown renderer collapses newlines in untrusted strings so injected content
cannot forge a new heading/section (e.g. a fake `## Safety boundaries`). The JSON keeps raw values.
A `--decision rejected` export clears the scope/tasks/files entirely so a rejected packet can never
read as "go implement this". `write_packet` refuses a symlinked output directory.

On export the CLI prints `IMPLEMENTATION_PACKET_EXPORTED`, the markdown + json paths, the thread id,
the source issue/PR (if any), and the suggested next Claude Code message (or, for a rejected gate, a
"nothing to implement" notice). Tests (`tests/test_devflow_packet.py`) cover packet build from an
approved advisory state, real-advisory summary fallback, inclusion of blocking/non-blocking comments,
rejected-decision consistency, corrupt/non-dict checkpoint degradation, untrusted path-traversal +
Markdown-injection neutralization, symlink-dir refusal, thread-id path sanitization, valid JSON, the
required Markdown sections, that `.devflow/` is gitignored, and that export performs no GitHub writes
/ never references the write layer.

### Manual scope: `create-implementation-packet`

`export-implementation-packet` is only as good as the underlying advisory — a **simulated/dry-run**
advisory yields a generic packet (no files, no concrete tasks) that must **not** be used for real
implementation. `create-implementation-packet` closes that gap: the human owner writes the scope in a
Markdown file and devflow builds a packet directly from it — no checkpoint/advisory required.

```bash
python -m devflow.cli create-implementation-packet \
  --thread-id check-runner-1 --task "Add allowlisted check runner" \
  --scope-file scope.md --repo ChenSiyun1234/examprep-devflow
```

**When to use which:**
- **`export-implementation-packet`** — hand off the scope from a real advisory/review at a paused gate.
- **`create-implementation-packet`** — hand off a concrete scope you wrote yourself, no advisory.

The scope file is parsed by a tiny, defensive parser (`parse_scope_markdown` — top-level `#` headings
→ sections; unknown headings ignored; missing sections defaulted). Recognized sections: Task, Approved
scope, Tasks (optional; falls back to Approved scope), Files likely touched, Out of scope, Checks to
run, Safety rules. The resulting packet is marked **`source: manual_human_scope`** (top-level and in
metadata) so it can never be mistaken for a generic simulated-advisory packet.

Same safety posture as the export path and then some: **no GitHub calls, no shell, no code edits**;
only the two local packet files are written (via the shared `write_packet`, which still refuses a
symlinked output dir). The **canonical `SAFETY_BOUNDARIES` are always embedded** — a scope file may
*add* rules but can never remove the hard ones (no secrets/keys, no commit/push/PR, no merge, no
branch-delete, no force-push, no false "tests passed"). Scope file paths are filtered (absolute / `..`
rejected and surfaced under out-of-scope), and untrusted scope text is `_md_safe`-sanitized in the
Markdown. The CLI prints `MANUAL_IMPLEMENTATION_PACKET_CREATED` + paths + thread id + task + the
suggested Claude Code prompt. Tested in `tests/test_devflow_create_packet.py` (parse, build, manual
source marking, unsafe-path filtering, non-removable safety, missing-file error, valid JSON, required
Markdown sections, parser wiring, no-gh, writes-only-under-out-dir).

## GitHub write mode (guarded, opt-in)

`GitHubWriter` adds the only *write* path devflow has. It is **off by default** and limited to four
operations — there is no merge, branch-delete, or force-push capability anywhere in the class.

| Capability | `create_advisory_issue` | `comment_on_issue` | `create_draft_pr` | `comment_on_pr` |
|---|---|---|---|---|
| present | ✅ | ✅ | ✅ (always `--draft`) | ✅ |

| **NOT present** | merge | branch delete | push / force-push | close/admin |

### Dry-run vs real mode

* **Dry-run (default, `real_github=False`)** — every write logs exactly what it *would* run
  (`[github-write:DRY-RUN] … :: gh issue create …`) and returns a simulated number/URL. Nothing is
  sent to GitHub; no `gh` subprocess is spawned.
* **Real mode (`real_github=True`, CLI `--real-github`)** — the four operations run real, guarded
  `gh` commands. Every command passes `_assert_write_allowed()` (allow-list of create/comment only;
  refuses any `merge`/`delete`/`--force`/`push` token) **and** `_assert_no_secrets()` (refuses
  content that looks like a token/key). Failures are returned as `{executed: False, error: …}` —
  the workflow fails safely rather than crashing.

### Bounded waiting (no infinite loops)

`wait_for_codex_advisory` / `wait_for_codex_review` poll via the read-only layer using
`bounded_poll(fetch, max_attempts, sleep_seconds)` — capped attempts, configurable sleep, default
small (`--max-polls 6 --poll-seconds 30`). On exhaustion they set a `timeout` status and a clear
error; they never loop forever. `--max-polls 0` means "do not poll" (immediate timeout), negative
`--poll-seconds` is rejected at the CLI, and the wait nodes refuse to poll issue/PR `#0` (a failed
create stops the run safely instead of acting on the wrong number).

**Resume safety:** a paused `--real-github` run resumes in **dry-run by default**; live writes must
be re-requested with `--real-github` on the `resume` command, so they can never silently persist
across a human-approval pause.

### Codex issue handoff (advisory)

1. `create_advisory_issue` opens an issue (real or simulated).
2. `request_codex_advisory` posts an `@codex …` comment asking for an advisory.
3. `wait_for_codex_advisory` bounded-polls the issue's comments for a Codex-authored reply
   (`find_latest_codex_advisory`).
4. `summarize_advisory` condenses it; `human_approval` **interrupts for approval before any repo
   edits**. Approval is never inferred.

### PR review handoff

1. `create_draft_pr` opens a draft PR; `request_codex_review` posts `@codex review …`.
2. `wait_for_codex_review` bounded-polls PR comments/reviews (`find_latest_codex_review`), with a
   light blocking/items parse.
3. Blocking comments route to `human_fix_approval`; clean reviews skip the fix gate.

### CLI

```bash
# dry-run (safe; no writes): create(sim) -> @codex(sim) -> simulate advisory -> summarize -> pause
python -m devflow.cli run-docs-advisory --thread-id demo

# REAL mode: real issue + @codex comment, bounded wait, then PAUSE at human approval (no edits)
python -m devflow.cli run-docs-advisory --real-github --max-polls 6 --poll-seconds 30 \
    --repo owner/name --thread-id demo
```

This PR stops at the human-approval summary: it never applies advisory changes, opens nothing past
the issue/comment, and never merges.

### Exact safety boundaries (this PR)

Never: merge a PR · delete a branch · force-push · push a branch · add GitHub Actions · add
secrets/API keys · implement product runtime · auto-apply a Codex advisory without human approval.

## Dependency note

The repo has no product dependency file and the product is intentionally stdlib-only. To avoid
silently changing that strategy, LangGraph is declared as an **optional dev dependency** scoped to
this tool in `devflow/requirements-dev.txt`. devflow runs fully without it (stdlib fallback);
installing it only upgrades the orchestrator to the real LangGraph backend.

## Planned next PRs

1. ~~Read-only GitHub integration~~ — **done** (`ReadOnlyGitHub` + `check_gh_available`).
2. ~~GitHub issue/PR write integration + Codex polling~~ — **done** (`GitHubWriter`, `--real-github`,
   `bounded_poll`; create issue / @codex comment / draft PR / bounded wait — no merge).
3. **Apply approved advisory changes** — real repo edits after the human-approval gate (still no
   merge), behind explicit confirmation.
4. **Merge approval execution** — wire `claude_execute_merge` to a real, human-approved merge
   (the only step that can merge; deliberately absent today).
