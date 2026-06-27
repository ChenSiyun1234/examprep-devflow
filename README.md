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

# human-approval pause (interrupt) then resume
python -m devflow.cli run    --task docs-advisory --thread-id demo-2 --pause-at advisory
python -m devflow.cli resume --thread-id demo-2 --gate advisory --decision approved

# read-only GitHub inspection (needs an authenticated `gh` CLI)
python -m devflow.cli github-check
python -m devflow.cli read-issue --issue 123 --repo owner/name
python -m devflow.cli read-pr    --pr 456     --repo owner/name

# advisory flow up to human approval; --real-github performs the guarded issue + @codex writes
python -m devflow.cli run-docs-advisory --thread-id demo [--real-github --max-polls 6 --poll-seconds 30]
```

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
