"""Environment node — verifies preconditions (dry-run: nothing is actually executed)."""

from __future__ import annotations

from devflow.state import DevflowState


def check_environment(state: DevflowState) -> dict:
    """Pretend to verify gh auth, a clean working tree and the base branch.

    In dry-run we record the *intended* checks rather than running any subprocess. We also derive
    a deterministic working branch name for later (simulated) branch/PR steps.
    """
    branch = f"devflow/{state['task_type']}-{state['thread_id']}"
    events = [
        "[check_environment] dry-run: would verify `gh auth status`, clean working tree, "
        "and current base branch — not executed.",
        f"[check_environment] derived branch name: {branch}",
    ]
    return {"branch_name": branch, "status": "running", "event_log": events}
