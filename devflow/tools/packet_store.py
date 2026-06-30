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
import threading

from devflow.tools.packet_writer import PACKET_JSON_NAME, PACKET_MD_NAME, MANUAL_SOURCE

# serialize status writes: the dashboard's ThreadingHTTPServer can run two /packet-status POSTs for the
# same packet at once, and they would otherwise both write the same '<final>.tmp' and clobber each other.
_STATUS_LOCK = threading.Lock()

STATUS_FILE = "handoff-status.json"
# packet handoff lifecycle (local-only).
STATUSES = ("created", "handed_to_claude", "in_progress", "implemented", "blocked", "abandoned")
DEFAULT_STATUS = "created"

def safe_slug(slug: str) -> str:
    """Return ``slug`` if it is a safe single path component; else raise ValueError. Rejects empty,
    ``.``/``..``, any path separator, and any control char — but ACCEPTS the full charset
    ``packet_writer.safe_thread_slug`` emits (unicode-alnum plus ``-_.``), so a localized thread id like
    ``生命周期-00920077`` is not falsely rejected. (The realpath containment check in ``_packet_dir`` is
    the second line of defense against traversal.)"""
    s = (slug or "").strip()
    if not s or s in (".", "..") or "\x00" in s:
        raise ValueError("invalid packet slug")
    if "/" in s or "\\" in s or os.sep in s or (os.altsep and os.altsep in s):
        raise ValueError("invalid packet slug")       # must be a single path component (so '..' inside a
                                                       # single component like 'release..1-<hash>' is fine)
    if any(not (c.isalnum() or c in "-_.") for c in s):
        raise ValueError("invalid packet slug")        # same allow-set as safe_thread_slug (unicode-aware)
    return s


def _packet_dir(base_dir: str, slug: str) -> str:
    """Resolve ``base_dir/slug`` AND verify it stays inside ``base_dir`` (defense-in-depth vs traversal)."""
    s = safe_slug(slug)
    raw = os.path.join(base_dir, s)
    # refuse a symlink at the slug entry OR any ancestor (e.g. .devflow / .devflow/packets): otherwise
    # realpath would resolve a symlinked root and a read/write could land outside the packets tree.
    anc = raw
    while anc:
        if os.path.islink(anc):
            raise ValueError("refusing a symlinked path component: %s" % anc)
        parent = os.path.dirname(anc)
        if parent == anc:                              # reached the filesystem anchor / top of a rel path
            break
        anc = parent
    base = os.path.realpath(base_dir)
    path = os.path.realpath(raw)
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("packet path escapes the packets directory")
    return path


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _read_packet_json(pkt_dir: str):
    jp = os.path.join(pkt_dir, PACKET_JSON_NAME)
    if os.path.islink(jp):                             # don't follow a packet json symlinked outside the root
        return None
    try:
        if not os.path.isfile(jp):
            return None
        with open(jp, encoding="utf-8") as f:
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


def _packet_generated_at(pkt_dir: str):
    pkt = _read_packet_json(pkt_dir) or {}
    return (pkt.get("metadata") if isinstance(pkt.get("metadata"), dict) else {}).get("generated_at")


def read_status(base_dir: str, slug: str) -> str:
    """Current handoff status (default ``created``). Read-only; tolerant of a missing/garbled file. A
    status recorded for a DIFFERENT packet generation is treated as stale and reset to ``created`` — so a
    packet re-exported under the same slug doesn't inherit the previous run's ``implemented``/``abandoned``."""
    pkt_dir = _packet_dir(base_dir, slug)
    sp = os.path.join(pkt_dir, STATUS_FILE)
    if os.path.islink(sp) or not os.path.isfile(sp):   # missing / symlink / FIFO -> default, don't open
        return DEFAULT_STATUS
    try:
        with open(sp, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return DEFAULT_STATUS
    if not isinstance(record, dict) or record.get("status") not in STATUSES:
        return DEFAULT_STATUS
    gen, rec_gen = _packet_generated_at(pkt_dir), record.get("packet_generated_at")
    if gen is not None and rec_gen is not None and str(rec_gen) != str(gen):
        return DEFAULT_STATUS                          # status was for an earlier generation -> stale
    return record["status"]


def write_status(base_dir: str, slug: str, status: str) -> dict:
    """Write the LOCAL handoff status file ONLY (the single side effect). Validates slug + status; the dir
    must be a real packet. Stamps the packet's generated_at so a later regeneration invalidates it.
    Serialized + symlink/hardlink-guarded. Returns the record."""
    pkt_dir = _packet_dir(base_dir, slug)
    if status not in STATUSES:
        raise ValueError("invalid status %r (allowed: %s)" % (status, ", ".join(STATUSES)))
    if not os.path.isdir(pkt_dir) or _read_packet_json(pkt_dir) is None:
        # a safe-named but non-packet sibling dir must NOT receive a stray handoff-status.json
        raise ValueError("not a packet directory %r (no valid implementation-packet.json)" % (slug,))
    record = {"status": status, "updated_at": _now_iso(),
              "packet_generated_at": _packet_generated_at(pkt_dir)}
    final = os.path.join(pkt_dir, STATUS_FILE)
    tmp = final + ".tmp"
    with _STATUS_LOCK:                                  # serialize: concurrent /packet-status share <final>.tmp
        for p in (tmp, final):
            # never follow a planted symlink, or truncate a hard-linked / non-regular target (open(w) would
            # write THROUGH to a shared inode) — the endpoint promises to write only the local status file.
            if os.path.islink(p):
                raise ValueError("refusing to write through a symlinked status file: %s" % p)
            if os.path.exists(p) and (not os.path.isfile(p) or os.stat(p).st_nlink > 1):
                raise ValueError("refusing to write a hard-linked or non-regular status file: %s" % p)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, final)
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
        pkt_dir = os.path.join(base_dir, name)
        try:
            safe_slug(name)                              # skip dirs whose name isn't a safe slug
            # do NOT follow a symlinked entry (its target may be a valid packet OUTSIDE the root); skip
            # non-dirs. A single bad/unsafe entry must never break the whole index.
            if os.path.islink(pkt_dir) or not os.path.isdir(pkt_dir):
                continue
            packet = _read_packet_json(pkt_dir)
            if packet is None:                           # not a packet dir (no/garbled json) -> ignore
                continue
            info = _normalize(packet)
            info["slug"] = name
            info["status"] = read_status(base_dir, name)
        except (ValueError, OSError):                    # one entry's failure shouldn't break /packets
            continue
        out.append(info)
    # str() the sort key: generated_at is user-editable on-disk state — a non-string value (e.g. 123)
    # must not raise TypeError comparing against string timestamps.
    out.sort(key=lambda p: str(p.get("generated_at") or ""), reverse=True)
    return out
