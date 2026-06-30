"""Local DevFlow Dashboard (MVP).

A tiny, dependency-free (stdlib ``http.server``) local web UI over devflow's already-safe
operations. Localhost-only by default; READ-ONLY or DRY-RUN/local-file writes only — it never
performs real GitHub writes, merges, branch deletes, force-pushes, or arbitrary shell execution.
The CLI remains the source of truth; the dashboard only calls the same functions.
"""
