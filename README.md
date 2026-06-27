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
python -m devflow.cli watch-codex-reviews --repo owner/name          # prints ACTIONABLE_/NO_NEW_CODEX_REVIEWS

# advisory flow up to human approval; --real-github performs the guarded issue + @codex writes
python -m devflow.cli run-docs-advisory --thread-id demo [--real-github --max-polls 6 --poll-seconds 30]

# after approving a gate, export an Implementation Packet to hand off to Claude Code (local files only)
python -m devflow.cli run --task docs-advisory --thread-id demo --pause-at advisory
python -m devflow.cli export-implementation-packet --thread-id demo   # --gate/--decision optional
```

## Implementation Packet (handoff to Claude Code)

devflow orchestrates and summarizes; **Claude Code remains the code editor**. After Codex
advisory/review is summarized and you approve a gate, devflow can export a structured
**Implementation Packet** — the handoff boundary:

> Codex advisory/review → devflow summarizes → **you approve** → devflow exports a packet →
> Claude Code implements the scoped changes → you review → PR / Codex review continues.

```bash
# pause at a gate (writes a checkpoint), then export the packet
python -m devflow.cli run --task docs-advisory --thread-id demo --pause-at advisory
python -m devflow.cli export-implementation-packet --thread-id demo
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
