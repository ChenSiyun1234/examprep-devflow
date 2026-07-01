"""Local DevFlow Dashboard (MVP).

A tiny, dependency-free (stdlib ``http.server``) local web UI over devflow's already-safe
operations. Localhost-only by default; dry-run/read-only unless the operator starts it with the
explicit localhost-only GitHub-write flag. Those opt-in writes are narrow guarded helpers only; the
dashboard never merges, deletes branches, force-pushes, or runs arbitrary shell commands.
The CLI remains the source of truth; the dashboard only calls the same functions.
"""
