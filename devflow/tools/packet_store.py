# -*- coding: utf-8 -*-
"""Read-only-ish local store for Implementation Packet LIFECYCLE tracking.

Lists/reads the packets that ``export``/``create-implementation-packet`` already wrote under a packets
base dir (default ``.devflow/packets``), and tracks a purely LOCAL handoff status per packet in
``<slug>/handoff-status.json``. The ONLY write is that local status file.

PURE stdlib: **NEVER** calls GitHub, runs a shell, or calls an LLM. Hardened against path traversal —
a slug must be a single safe path component AND its resolved dir must stay inside the base dir.
"""

from __future__ import annotations

import datetime
import json
import os
import re

from devflow.tools.packet_writer import PACKET_JSON_NAME, PACKET_MD_NAME, MANUAL_SOURCE

STATUS_FILE = "handoff-status.json"
# packet handoff lifecycle (local-only).
STATUSES = ("created", "handed_to_claude", "in_progress", "implemented", "blocked", "abandoned")
DEFAULT_STATUS = "created"

# a slug is a single path component as produced by packet_writer.safe_thread_slug: [A-Za-z0-9._-] + a hash.
_SAFE_SLUG_RE = re.compile(r"[A-Za-z0-9._-]+")


def safe_slug(slug: str) -> str:
    """Return ``slug`` if it is a safe single path component; else raise ValueError. Rejects empty,
    ``.``/``..``, any separator, and anything outside ``[A-Za-z0-9._-]`` — so no path traversal."""
    s = (slug or "").strip()
    if not s or s in (".", "..") or ".." in s or not _SAFE_SLUG_RE.fullmatch(s):
        raise ValueError("invalid packet slug")
    return s


def _packet_dir(base_dir: str, slug: str) -> str:
    """Resolve ``base_dir/slug`` AND verify it stays inside ``base_dir`` (defense-in-depth vs traversal)."""
    s = safe_slug(slug)
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, s))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("packet path escapes the packets directory")
    return path


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _read_packet_json(pkt_dir: str):
    try:
        with open(os.path.join(pkt_dir, PACKET_JSON_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalize(packet: dict) -> dict:
    """Flatten the packet json (both the advisory-export and manual-scope shapes) into the fields the UI
    needs. Tolerant of a missing/foreign shape (the json is on-disk, user-editable state)."""
    packet = packet if isinstance(packet, dict) else {}
    m = packet.get("metadata") if isinstance(packet.get("metadata"), dict) else {}
    ap = packet.get("approval") if isinstance(packet.get("approval"), dict) else {}
    ii = (packet.get("implementation_instructions")
          if isinstance(packet.get("implementation_instructions"), dict) else {})
    is_manual = (packet.get("source") == MANUAL_SOURCE) or (m.get("source") == MANUAL_SOURCE)

    def _list(v):
        return list(v) if isinstance(v, (list, tuple)) else []

    return {
        "thread_id": m.get("thread_id"),
        "task": m.get("task") or m.get("task_type"),
        "repo": m.get("repo"),
        "generated_at": m.get("generated_at"),
        "gate": ap.get("gate_label") or ap.get("gate") or ("manual scope" if is_manual else None),
        "decision": ap.get("decision") or ("(manual scope)" if is_manual else None),
        "source": MANUAL_SOURCE if is_manual else "advisory_export",
        "issue_number": m.get("issue_number"), "issue_url": m.get("issue_url"),
        "pr_number": m.get("pr_number"), "pr_url": m.get("pr_url"),
        "approved_scope": _list(ap.get("approved_scope")),
        "tasks": _list(ii.get("tasks")),
        "files_likely_touched": _list(ii.get("files_likely_touched")),
        "out_of_scope": _list(ii.get("out_of_scope")),
        "tests_to_run": _list(ii.get("tests_to_run")),
        "safety_boundaries": _list(packet.get("safety_boundaries")) or _list(ii.get("safety_rules")),
        "suggested_prompt": packet.get("suggested_prompt"),
    }


def read_status(base_dir: str, slug: str) -> str:
    """Current handoff status (default ``created``). Read-only; tolerant of a missing/garbled file."""
    pkt_dir = _packet_dir(base_dir, slug)
    try:
        with open(os.path.join(pkt_dir, STATUS_FILE), encoding="utf-8") as f:
            data = json.load(f)
        status = (data or {}).get("status") if isinstance(data, dict) else None
    except (OSError, ValueError):
        status = None
    return status if status in STATUSES else DEFAULT_STATUS


def write_status(base_dir: str, slug: str, status: str) -> dict:
    """Write the LOCAL handoff status file ONLY (the single side effect). Validates slug + status; the
    packet dir must already exist (a status can't be set for a non-existent packet). Returns the record."""
    pkt_dir = _packet_dir(base_dir, slug)
    if status not in STATUSES:
        raise ValueError("invalid status %r (allowed: %s)" % (status, ", ".join(STATUSES)))
    if not os.path.isdir(pkt_dir):
        raise ValueError("no such packet %r" % (slug,))
    record = {"status": status, "updated_at": _now_iso()}
    tmp = os.path.join(pkt_dir, STATUS_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(pkt_dir, STATUS_FILE))
    return record


def get_packet(base_dir: str, slug: str):
    """Full normalized packet view for the detail page, or None if the dir has no valid packet json.
    Raises ValueError for an unsafe slug. Read-only."""
    pkt_dir = _packet_dir(base_dir, slug)
    packet = _read_packet_json(pkt_dir)
    if packet is None:
        return None
    info = _normalize(packet)
    md_path = os.path.join(pkt_dir, PACKET_MD_NAME)
    info.update({
        "slug": safe_slug(slug), "dir": pkt_dir,
        "json_path": os.path.join(pkt_dir, PACKET_JSON_NAME), "md_path": md_path,
        "status": read_status(base_dir, slug), "handoff": _handoff_text(info, md_path),
    })
    return info


def _handoff_text(info: dict, md_path: str) -> str:
    """The suggested Claude Code handoff prompt: the manual packet's own, else a synthesized export line."""
    if info.get("suggested_prompt"):
        return info["suggested_prompt"]
    if str(info.get("decision") or "").lower() == "rejected":
        return "Gate REJECTED — nothing to implement; the packet records the rejection."
    return ("Implement ONLY the scoped tasks in %s; run the listed checks; do not commit/push/merge; "
            "ask before expanding scope." % md_path)


def list_packets(base_dir: str) -> list:
    """List packets under ``base_dir`` (each subdir that holds a valid ``implementation-packet.json``),
    newest first by generated_at. Read-only; ignores malformed / non-packet dirs."""
    out = []
    if not os.path.isdir(base_dir):
        return out
    for name in os.listdir(base_dir):
        try:
            safe_slug(name)                              # skip dirs whose name isn't a safe slug
        except ValueError:
            continue
        pkt_dir = os.path.join(base_dir, name)
        if not os.path.isdir(pkt_dir):
            continue
        packet = _read_packet_json(pkt_dir)
        if packet is None:                               # not a packet dir (no/garbled json) -> ignore
            continue
        info = _normalize(packet)
        info["slug"] = name
        info["status"] = read_status(base_dir, name)
        out.append(info)
    out.sort(key=lambda p: p.get("generated_at") or "", reverse=True)
    return out
