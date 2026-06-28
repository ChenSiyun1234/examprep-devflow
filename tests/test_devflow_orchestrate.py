# -*- coding: utf-8 -*-
"""Tests for the cross-PR review orchestrator (planner) + its read-only gh reads + CLI command.

The decision logic is pure (fixtures, no gh). The gh-touching parts are tested with a fully mocked
`gh` (no network) and ASSERT every spawned command is read-only.

    python -m unittest tests.test_devflow_orchestrate
"""

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from devflow.tools import review_orchestrator as orch
from devflow.tools import github_cli as G
from devflow.tools.github_cli import ReadOnlyGitHub, _assert_read_only, GhError

CODEX = "chatgpt-codex-connector[bot]"
ME = "siyun"


def meta(num=5, head="aaaaaaa", state="OPEN", mergeable="MERGEABLE", base="main",
         title="feat: add thing", branch="feat/thing", adds=50, files=2):
    return {"number": num, "title": title, "state": state, "mergeable": mergeable, "base_ref": base,
            "head_ref": branch, "head_oid": head, "is_draft": True, "additions": adds,
            "deletions": 0, "changed_files": files}


def review(body, commit, rid, at, state=None, author=CODEX):
    return {"author": author, "body": body, "state": state, "created_at": at,
            "commit_id": commit, "id": rid, "url": f"r{rid}"}


def inline(body, review_id, at, commit="aaaaaaa", author=CODEX):
    return {"author": author, "body": body, "created_at": at, "commit_id": commit,
            "review_id": review_id, "id": 9000 + review_id, "url": f"c{review_id}"}


def comment(body, at, author=CODEX):
    return {"author": author, "body": body, "created_at": at, "id": 1, "url": "k1"}


def signals(reviews=(), inline_=(), comments=()):
    return {"reviews": list(reviews), "inline": list(inline_), "comments": list(comments)}


def classify(m, s, *, converged=None, requested_head=None, now=10_000_000_000):
    return orch.classify(m, s, converged=converged or {}, requested_head=requested_head or {}, now=now)


class TestHelpers(unittest.TestCase):
    def test_priority_unreviewed_feature_outranks_reviewed_bugfix(self):
        feat = meta(num=1, title="feat: big feature", branch="feat/x", adds=300, files=4)
        bug = meta(num=2, title="fix: tiny typo", branch="fix/typo", adds=5, files=1)
        p_feat, n_feat = orch.priority(feat, rounds=0, reviewed_on_head=False)
        p_bug, _ = orch.priority(bug, rounds=5, reviewed_on_head=True)
        self.assertEqual(n_feat, 10)              # never reviewed -> max need
        self.assertGreater(p_feat, p_bug)

    def test_priority_need_decays_with_rounds_and_reviewed_head(self):
        m = meta()
        self.assertEqual(orch.priority(m, 0, False)[1], 10)
        self.assertEqual(orch.priority(m, 4, False)[1], 5)
        self.assertEqual(orch.priority(m, 4, True)[1], 1)   # 5 - 4 = 1

    def test_finding_severity_reads_badge_else_minor(self):
        self.assertEqual(orch.finding_severity("![P1 Badge] bad"), 1)
        self.assertEqual(orch.finding_severity("a P2 issue"), 2)
        self.assertEqual(orch.finding_severity("nit, no badge"), 3)   # untagged -> P3

    def test_is_quota_keys_off_opener_not_generic_phrase(self):
        self.assertTrue(orch.is_quota("You have reached your Codex usage limits for code reviews."))
        self.assertFalse(orch.is_quota("This review discusses usage limits for code reviews in the diff."))

    def test_is_clean_verdict(self):
        self.assertTrue(orch.is_clean_verdict("Didn't find any major issues."))
        self.assertTrue(orch.is_clean_verdict("LGTM"))
        self.assertFalse(orch.is_clean_verdict("Please fix the null deref"))


class TestClassify(unittest.TestCase):
    def test_clean_review_on_head(self):
        s = signals(reviews=[review("Reviewed commit: aaaaaaa", "aaaaaaa", 1, "2026-01-01T00:00:00Z")])
        c = classify(meta(head="aaaaaaa"), s)
        self.assertTrue(c["clean"])
        self.assertEqual(c["findings_on_head"], 0)
        self.assertIsNone(c["max_severity"])

    def test_findings_with_p2_badge_not_clean(self):
        s = signals(reviews=[review("Codex Review", "aaaaaaa", 7, "2026-01-01T00:00:00Z")],
                    inline_=[inline("![P2 Badge] fix this", 7, "2026-01-01T00:00:00Z"),
                             inline("![P3 Badge] nit", 7, "2026-01-01T00:00:00Z")])
        c = classify(meta(head="aaaaaaa"), s)
        self.assertFalse(c["clean"])
        self.assertEqual(c["findings_on_head"], 2)
        self.assertEqual(c["max_severity"], 2)        # worst of P2/P3

    def test_reanchored_comment_from_older_review_not_counted(self):
        # latest review id=7 has ZERO own inline; the P2 inline belongs to OLDER review id=3 -> ignored
        s = signals(reviews=[review("old", "old", 3, "2026-01-01T00:00:00Z"),
                             review("Reviewed commit: aaaaaaa", "aaaaaaa", 7, "2026-01-02T00:00:00Z")],
                    inline_=[inline("![P2 Badge] stale", 3, "2026-01-01T00:00:00Z")])
        c = classify(meta(head="aaaaaaa"), s)
        self.assertEqual(c["findings_on_head"], 0)
        self.assertTrue(c["clean"])                    # latest review has no own findings -> clean

    def test_clean_verdict_comment_matching_head(self):
        s = signals(comments=[comment("Codex Review: Didn't find any issues. Reviewed commit: aaaaaaa",
                                       "2026-01-02T00:00:00Z")])
        self.assertTrue(classify(meta(head="aaaaaaa1234567"), s)["clean"])
        # ...but a clean verdict for a DIFFERENT commit must not mark this head clean
        s2 = signals(comments=[comment("Didn't find any issues. Reviewed commit: deadbee",
                                        "2026-01-02T00:00:00Z")])
        self.assertFalse(classify(meta(head="aaaaaaa1234567"), s2)["clean"])

    def test_awaiting_when_request_newer_than_review(self):
        s = signals(reviews=[review("old", "aaaaaaa", 1, "2026-01-01T00:00:00Z")],
                    comments=[comment("@codex review", "2026-01-03T00:00:00Z", author=ME)])
        c = classify(meta(head="aaaaaaa"), s)
        self.assertTrue(c["awaiting"])

    def test_quota_from_latest_signal(self):
        s = signals(comments=[comment("You have reached your Codex usage limits for code reviews.",
                                      "2026-01-05T00:00:00Z")])
        self.assertTrue(classify(meta(), s)["latest_quota"])

    def test_responded_after_req_only_counts_real_responses(self):
        base = review("old", "aaaaaaa", 1, "2026-01-01T00:00:00Z")
        req = comment("@codex review", "2026-01-03T00:00:00Z", author=ME)
        # a GENERIC Codex comment after the request is SILENCE, not a response (don't suppress a nudge)
        generic = comment("Working on it — creating an environment.", "2026-01-04T00:00:00Z")
        c = classify(meta(head="aaaaaaa"), signals(reviews=[base], comments=[req, generic]))
        self.assertTrue(c["awaiting"])
        self.assertFalse(c["responded_after_req"])
        self.assertGreaterEqual(c["pending"], 1)         # the request is newer than the review
        self.assertGreater(c["req_age"], 0)
        # ...but a CLEAN-VERDICT comment after the request DOES count as a response
        clean = comment("Didn't find any issues.", "2026-01-04T00:00:00Z")
        c2 = classify(meta(head="aaaaaaa"), signals(reviews=[base], comments=[req, clean]))
        self.assertTrue(c2["responded_after_req"])

    def test_spoofed_codex_login_ignored(self):
        # a non-trusted 'codex-fan' login must NOT be treated as Codex (anti-spoof via is_codex_author)
        s = signals(reviews=[review("Reviewed commit: aaaaaaa", "aaaaaaa", 1, "2026-01-01T00:00:00Z",
                                    author="codex-fan")])
        c = classify(meta(head="aaaaaaa"), s)
        self.assertEqual(c["rounds"], 0)
        self.assertFalse(c["has_head_review"])

    def test_stale_clean_comment_does_not_override_newer_findings(self):
        # Codex r1 F1 (P1): an OLDER clean comment must NOT keep a PR clean when a LATER review on the
        # same head adds findings
        s = signals(
            comments=[comment("Didn't find any issues. Reviewed commit: aaaaaaa", "2026-01-01T00:00:00Z")],
            reviews=[review("Codex Review", "aaaaaaa", 9, "2026-01-02T00:00:00Z")],   # newer review...
            inline_=[inline("![P2 Badge] fix this", 9, "2026-01-02T00:00:00Z")])       # ...with a P2 finding
        c = classify(meta(head="aaaaaaa"), s)
        self.assertFalse(c["clean"])
        self.assertEqual(c["max_severity"], 2)

    def test_changes_requested_review_not_clean(self):
        # Codex r1 F3 (P1): a CHANGES_REQUESTED state is authoritative even with a neutral body + no inline
        s = signals(reviews=[review("Codex Review", "aaaaaaa", 5, "2026-01-01T00:00:00Z",
                                    state="CHANGES_REQUESTED")])
        self.assertFalse(classify(meta(head="aaaaaaa"), s)["clean"])

    def test_non_review_codex_command_not_awaiting(self):
        # Codex r1 F7 (P2): a different @codex command must NOT count as an outstanding review request
        base = review("old", "aaaaaaa", 1, "2026-01-01T00:00:00Z")
        cmd = comment("@codex address that feedback please", "2026-01-03T00:00:00Z", author=ME)
        self.assertFalse(classify(meta(head="aaaaaaa"), signals(reviews=[base], comments=[cmd]))["awaiting"])
        req = comment("@codex review", "2026-01-03T00:00:00Z", author=ME)   # an actual review request IS
        self.assertTrue(classify(meta(head="aaaaaaa"), signals(reviews=[base], comments=[req]))["awaiting"])

    def test_negated_blocking_body_is_clean(self):
        # Codex r2 P2: a clean verdict body containing the word 'blocking' ("No blocking issues found")
        # must still be clean (negation-aware), not pushed to findings_to_fix
        s = signals(reviews=[review("No blocking issues found.", "aaaaaaa", 3, "2026-01-01T00:00:00Z")])
        self.assertTrue(classify(meta(head="aaaaaaa"), s)["clean"])

    def test_outstanding_changes_requested_survives_later_comment(self):
        # Codex r2 P2: a CHANGES_REQUESTED review stays blocking even after a later COMMENTED review with
        # no inline — GitHub keeps it blocking until an approval/dismissal
        s = signals(reviews=[
            review("please fix", "aaaaaaa", 1, "2026-01-01T00:00:00Z", state="CHANGES_REQUESTED"),
            review("Codex Review", "aaaaaaa", 2, "2026-01-02T00:00:00Z", state="COMMENTED")])
        self.assertFalse(classify(meta(head="aaaaaaa"), s)["clean"])

    def test_empty_body_review_with_inline_findings_counted(self):
        # Codex r4 P2: a review with NO top-level body but inline findings is still a real reviewed round
        s = signals(reviews=[review("", "aaaaaaa", 5, "2026-01-01T00:00:00Z")],   # empty body...
                    inline_=[inline("![P2 Badge] fix this", 5, "2026-01-01T00:00:00Z")])  # ...but inline P2
        c = classify(meta(head="aaaaaaa"), s)
        self.assertEqual(c["rounds"], 1)
        self.assertTrue(c["has_head_review"])
        self.assertEqual(c["findings_on_head"], 1)
        self.assertEqual(c["max_severity"], 2)

    def test_old_answered_request_on_prior_head_not_awaiting(self):
        # Codex r4 P2: an old @codex request ANSWERED on a prior head must not look "still awaiting" once
        # the head advances with no review yet (untracked) — else it's stuck in-flight, never re-requested
        s = signals(
            reviews=[review("findings", "oldhead", 1, "2026-01-02T00:00:00Z")],         # review of OLD head
            comments=[comment("@codex review", "2026-01-01T00:00:00Z", author=ME)])      # request BEFORE it
        self.assertFalse(classify(meta(head="newhead"), s)["awaiting"])


class TestMergePredicates(unittest.TestCase):
    def _c(self, **kw):
        base = {"num": 5, "head": "h", "findings_on_head": 0, "max_severity": None, "rounds": 0,
                "rounds_on_head": 0, "clean": False, "awaiting": False, "latest_quota": False}
        base.update(kw)
        return base

    def test_force_mergeable_only_minor_after_head_rounds(self):
        # rounds counted on the CURRENT head (Codex r3 P2): >=3 head reviews + only-minor
        self.assertTrue(orch.force_mergeable(self._c(findings_on_head=2, max_severity=3, rounds_on_head=3)))
        self.assertFalse(orch.force_mergeable(self._c(findings_on_head=2, max_severity=2, rounds_on_head=5)))  # P2
        self.assertFalse(orch.force_mergeable(self._c(findings_on_head=2, max_severity=3, rounds_on_head=2)))  # <3
        # stale all-time rounds must NOT satisfy the threshold for a barely-reviewed new head
        self.assertFalse(orch.force_mergeable(self._c(findings_on_head=1, max_severity=3, rounds=9,
                                                      rounds_on_head=1)))

    def test_ok_to_merge_three_paths(self):
        self.assertTrue(orch.ok_to_merge(self._c(clean=True), {}))
        self.assertTrue(orch.ok_to_merge(self._c(findings_on_head=1, max_severity=3, rounds_on_head=3), {}))
        self.assertTrue(orch.ok_to_merge(self._c(head="h"), {"5": "h"}))     # converged pin
        self.assertFalse(orch.ok_to_merge(self._c(head="h"), {"5": "other"}))

    def test_is_inflight_head_aware(self):
        c = self._c(awaiting=True, head="h2")
        self.assertTrue(orch.is_inflight(c, {"5": "h2"}))          # tracked for current head
        self.assertFalse(orch.is_inflight(c, {"5": "h1"}))         # head advanced -> stale -> re-queue
        self.assertTrue(orch.is_inflight(c, {}))                   # untracked -> falls back to awaiting
        self.assertFalse(orch.is_inflight(self._c(awaiting=False), {}))

    def test_quota_reply_drops_out_of_inflight(self):
        # Codex r3 P2: a request answered with a quota notice must not keep holding a slot
        self.assertFalse(orch.is_inflight(self._c(awaiting=True, head="h2", latest_quota=True), {"5": "h2"}))
        self.assertTrue(orch.is_inflight(self._c(awaiting=True, head="h2", latest_quota=False), {"5": "h2"}))


class TestBuildPlan(unittest.TestCase):
    def _classified(self, **kw):
        base = {"num": 1, "branch": "b1", "head": "h1", "merged": False, "state": "OPEN",
                "mergeable": "MERGEABLE", "base_ref": "main", "is_draft": False, "rounds": 0,
                "rounds_on_head": 0, "reviewed_on_head": False, "review_key": "",
                "has_head_review": False, "awaiting": False,
                "req_age": 0, "pending": 0, "latest_quota": False, "latest_sig_ts": "",
                "responded_after_req": False, "findings_on_head": 0, "clean": False,
                "max_severity": None, "priority": 50, "needs": 10}
        base.update(kw)
        return base

    def test_request_review_capped_and_priority_ordered(self):
        cs = [self._classified(num=i, head=f"h{i}", priority=100 - i) for i in range(1, 6)]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertEqual(plan["request_review"], [1, 2, 3])       # top-3 by priority, cap=3
        self.assertEqual(plan["in_flight"], [1, 2, 3])

    def test_request_skips_reviewed_and_inflight(self):
        cs = [self._classified(num=1, head="h1", reviewed_on_head=True),               # already reviewed
              self._classified(num=2, head="h2", awaiting=True),                        # in-flight (untracked)
              self._classified(num=3, head="h3")]
        plan = orch.build_plan(cs, converged={}, requested_head={"2": "h2"}, done=[], now=1,
                               merged_branches=set())
        self.assertEqual(plan["request_review"], [3])

    def test_mergeable_and_force_mergeable(self):
        cs = [self._classified(num=1, clean=True),
              self._classified(num=2, findings_on_head=1, max_severity=3, rounds_on_head=3,
                               has_head_review=True)]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertIn(1, plan["mergeable_now"])
        self.assertIn(2, plan["mergeable_now"])
        self.assertEqual(plan["force_mergeable"], [2])

    def test_retarget_pr_does_not_fill_inflight_cap(self):
        # Codex r3 P2: a needs_retarget PR's stale-base request must not occupy an in-flight slot
        cs = [self._classified(num=1, head="h1", base_ref="feat/parent", awaiting=True),
              self._classified(num=2, head="h2", priority=40)]
        plan = orch.build_plan(cs, converged={}, requested_head={"1": "h1"}, done=[], now=1,
                               merged_branches={"feat/parent"})
        self.assertEqual(plan["needs_retarget"], [1])
        self.assertNotIn(1, plan["in_flight"])      # the stale-base request doesn't fill a slot
        self.assertIn(2, plan["request_review"])    # so the unrelated PR still gets one

    def test_unknown_mergeability_surfaced_not_dropped(self):
        # Codex r3 P2: a clean PR whose mergeability GitHub is still computing (UNKNOWN) is surfaced
        cs = [self._classified(num=1, clean=True, mergeable="UNKNOWN")]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertEqual(plan["mergeable_unknown"], [1])
        self.assertEqual(plan["mergeable_now"], [])
        self.assertTrue(orch.has_actions(plan))

    def test_conflict_wakes_for_merge_ready_pr(self):
        cs = [self._classified(num=1, clean=True, mergeable="CONFLICTING")]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertEqual(plan["needs_conflict"], [1])
        self.assertEqual(plan["mergeable_now"], [])

    def test_retarget_when_base_branch_merged(self):
        cs = [self._classified(num=1, clean=True, base_ref="feat/parent")]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1,
                               merged_branches={"feat/parent"})
        self.assertEqual(plan["needs_retarget"], [1])

    def test_retarget_independent_of_merge_readiness(self):
        # Codex r1 F8 (P2): an UNREVIEWED child whose parent merged must be flagged to retarget FIRST and
        # NOT be requested for review against the stale base
        cs = [self._classified(num=1, head="h1", base_ref="feat/parent")]   # not clean, no head review
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1,
                               merged_branches={"feat/parent"})
        self.assertEqual(plan["needs_retarget"], [1])
        self.assertEqual(plan["request_review"], [])       # never request review against a stale base

    def test_clean_draft_goes_to_ready_then_merge(self):
        # Codex r1 F5 (P2): a clean DRAFT is not directly mergeable — it must be un-drafted first
        cs = [self._classified(num=1, clean=True, is_draft=True)]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertEqual(plan["ready_then_merge"], [1])
        self.assertEqual(plan["mergeable_now"], [])

    def test_default_branch_not_hardcoded_main(self):
        # Codex r2 P2: a clean PR based on the repo's DEFAULT branch (e.g. 'master') is mergeable
        cs = [self._classified(num=1, clean=True, base_ref="master")]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1,
                               merged_branches=set(), default_branch="master")
        self.assertEqual(plan["mergeable_now"], [1])
        # ...but with the default 'main', a 'master'-based PR is NOT treated as merge-ready
        plan2 = orch.build_plan([self._classified(num=1, clean=True, base_ref="master")],
                                converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertEqual(plan2["mergeable_now"], [])

    def test_findings_to_fix(self):
        cs = [self._classified(num=1, has_head_review=True, review_key="k", findings_on_head=1,
                               max_severity=2, rounds=1)]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertEqual(plan["findings_to_fix"], [1])
        self.assertEqual(plan["mergeable_now"], [])

    def test_rate_limited_blocks_requests(self):
        cs = [self._classified(num=1, latest_sig_ts="2026-01-05T00:00:00Z", latest_quota=True)]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[], now=1, merged_branches=set())
        self.assertTrue(plan["rate_limited"])
        self.assertEqual(plan["request_review"], [])

    def test_done_pr_excluded(self):
        cs = [self._classified(num=1, clean=True)]
        plan = orch.build_plan(cs, converged={}, requested_head={}, done=[1], now=1, merged_branches=set())
        self.assertEqual(plan["mergeable_now"], [])


class TestState(unittest.TestCase):
    def test_load_missing_or_corrupt_returns_default(self):
        self.assertEqual(orch.load_state(os.path.join(tempfile.gettempdir(), "no_such_orch_xyz.json")),
                         orch._default_state())

    def test_save_then_load_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(), "orch_state.json")
        st = orch._default_state()
        st["requested_head"]["5"] = "abc"
        st["converged"]["6"] = "def"
        orch.save_state(st, path)
        self.assertEqual(orch.load_state(path)["requested_head"]["5"], "abc")
        self.assertEqual(orch.load_state(path)["converged"]["6"], "def")

    def test_save_state_bare_filename(self):
        # Codex r1 F2 (P2): a bare --state-file (no dir component) must not crash on makedirs('')
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)
        orch.save_state(orch._default_state(), "bare_orch.json")
        self.assertTrue(os.path.exists(os.path.join(d, "bare_orch.json")))

    def test_state_path_namespaced_by_repo(self):
        # Codex r1 F6 (P3): the default state path must differ per repo so pins don't leak across forks
        self.assertNotEqual(orch.state_path_for_repo("ownerA/repo"),
                            orch.state_path_for_repo("ownerB/repo"))

    def test_load_state_degrades_wrong_typed_slice(self):
        # Codex r4 P3: a slice with the wrong JSON type degrades to its default (no crash on next build)
        path = os.path.join(tempfile.mkdtemp(), "orch.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"requested_head": [], "converged": {"5": "h"}, "done": "nope"}, f)
        st = orch.load_state(path)
        self.assertEqual(st["requested_head"], {})       # wrong-type list -> default {}
        self.assertEqual(st["converged"], {"5": "h"})    # valid slice kept
        self.assertEqual(st["done"], [])                 # wrong-type str -> default []


# ---- gh-touching layer: fully mocked gh, ASSERT read-only ----
def _fake_gh(payloads, recorder):
    """Return a fake subprocess.run for gh. `payloads` maps a matcher to stdout JSON. EVERY command is
    routed through _assert_read_only so a write shape would raise — proving the layer is read-only."""
    def fake_run(cmd, **kw):
        recorder.append(cmd)
        args = cmd[1:]
        _assert_read_only(args)                       # would raise GhError on any write shape
        if args[:2] == ["repo", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"nameWithOwner": "o/r"}), stderr="")
        if args[:2] == ["pr", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(payloads.get("list", [])), stderr="")
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(payloads.get("view", {})), stderr="")
        if args and args[0] == "api":
            path = next((a for a in args[1:] if a.startswith("repos/")), "")
            for key in ("reviews", "comments"):
                if path.endswith("/" + key) and "/pulls/" in path:
                    return SimpleNamespace(returncode=0, stdout=json.dumps(payloads.get("pulls_" + key, [])), stderr="")
            if path.endswith("/comments") and "/issues/" in path:
                return SimpleNamespace(returncode=0, stdout=json.dumps(payloads.get("issue_comments", [])), stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")
    return fake_run


class TestReadOnlyOrchestrationReads(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.payloads = {
            "list": [{"number": 5, "title": "feat: x", "state": "OPEN",
                      "headRefName": "feat/x", "baseRefName": "main"}],
            "view": {"number": 5, "title": "feat: x", "state": "OPEN", "mergeable": "MERGEABLE",
                     "baseRefName": "main", "headRefName": "feat/x", "headRefOid": "aaaaaaa",
                     "isDraft": True, "additions": 10, "deletions": 0, "changedFiles": 2},
            "pulls_reviews": [{"user": {"login": CODEX}, "body": "Codex Review", "state": "COMMENTED",
                               "submitted_at": "2026-01-01T00:00:00Z", "commit_id": "aaaaaaa",
                               "id": 7, "html_url": "r7"}],
            "pulls_comments": [{"user": {"login": CODEX}, "body": "![P2 Badge] fix",
                                "created_at": "2026-01-01T00:00:00Z", "commit_id": "aaaaaaa",
                                "pull_request_review_id": 7, "id": 1, "html_url": "c1"}],
            "issue_comments": [],
        }
        self.p_which = mock.patch.object(G.shutil, "which", return_value="gh")
        self.p_run = mock.patch.object(G.subprocess, "run", side_effect=_fake_gh(self.payloads, self.calls))
        self.p_which.start(); self.p_run.start()
        self.addCleanup(self.p_which.stop); self.addCleanup(self.p_run.stop)

    def test_get_pr_meta_parses_and_is_read_only(self):
        m = ReadOnlyGitHub("o/r").get_pr_meta(5)
        self.assertEqual(m["mergeable"], "MERGEABLE")
        self.assertEqual(m["head_oid"], "aaaaaaa")
        self.assertEqual(m["changed_files"], 2)

    def test_get_pr_codex_signals_preserves_ids(self):
        s = ReadOnlyGitHub("o/r").get_pr_codex_signals(5)
        self.assertEqual(s["reviews"][0]["commit_id"], "aaaaaaa")
        self.assertEqual(s["reviews"][0]["id"], 7)
        self.assertEqual(s["inline"][0]["review_id"], 7)          # pull_request_review_id preserved

    def test_list_prs_returns_state_and_branches(self):
        prs = ReadOnlyGitHub("o/r").list_prs(state="all")
        self.assertEqual(prs[0]["head_ref"], "feat/x")
        self.assertEqual(prs[0]["base_ref"], "main")

    def test_every_spawned_command_is_read_only(self):
        gh = ReadOnlyGitHub("o/r")
        gh.get_pr_meta(5); gh.get_pr_codex_signals(5); gh.list_prs(state="all")
        self.assertTrue(self.calls)
        for cmd in self.calls:
            _assert_read_only(cmd[1:])      # must not raise for any spawned command

    def test_merged_heads_targeted_and_read_only(self):
        # Codex r2 F4: merged_heads checks SPECIFIC branches (not a limited window). The setUp fake
        # returns a PR for any `pr list`, so feat/x is detected as a merged head.
        heads = ReadOnlyGitHub("o/r").merged_heads({"feat/x"})
        self.assertIn("feat/x", heads)
        for cmd in self.calls:
            _assert_read_only(cmd[1:])


def _raw_view(num=5, head="aaaaaaa", mergeable="MERGEABLE", base="main", state="OPEN",
              title="feat: x", adds=10, files=2, draft=False):
    """Raw `gh pr view --json` shape (field names as GitHub returns them)."""
    return {"number": num, "state": state, "mergeable": mergeable, "baseRefName": base,
            "headRefName": f"feat/{num}", "headRefOid": head, "title": title,
            "additions": adds, "deletions": 0, "changedFiles": files, "isDraft": draft}


def _cli_fake_gh(recorder, *, prs, views, signals_map=None, error_prs=(), merged_prs=(),
                 default_branch="main"):
    """Flexible read-only gh fake for the orchestrate CLI: per-PR `pr view` (views[num]) + Codex
    signals (signals_map[num] -> {reviews,inline,comments} raw github json). `pr list --state merged`
    returns merged_prs, any other state returns prs. PRs in error_prs fail their `pr view`
    (returncode 1 -> GhError). `default_branch` sets the repo's defaultBranchRef. Every command passes
    through _assert_read_only."""
    signals_map = signals_map or {}

    def fake_run(cmd, **kw):
        recorder.append(cmd)
        args = cmd[1:]
        _assert_read_only(args)
        if args[:2] == ["repo", "view"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"nameWithOwner": "o/r", "defaultBranchRef": {"name": default_branch}}))
        if args[:2] == ["pr", "list"]:
            state = args[args.index("--state") + 1] if "--state" in args else "open"
            data = merged_prs if state == "merged" else prs
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if args[:2] == ["pr", "view"]:
            num = int(args[2])
            if num in error_prs:
                return SimpleNamespace(returncode=1, stdout="", stderr="simulated gh failure")
            return SimpleNamespace(returncode=0, stdout=json.dumps(views.get(num, {})), stderr="")
        if args and args[0] == "api":
            path = next((a for a in args[1:] if a.startswith("repos/")), "")
            m = re.search(r"/(pulls|issues)/(\d+)/(\w+)", path)
            if m:
                surface, num, kind = m.group(1), int(m.group(2)), m.group(3)
                sig = signals_map.get(num, {})
                if surface == "pulls" and kind == "reviews":
                    return SimpleNamespace(returncode=0, stdout=json.dumps(sig.get("reviews", [])), stderr="")
                if surface == "pulls" and kind == "comments":
                    return SimpleNamespace(returncode=0, stdout=json.dumps(sig.get("inline", [])), stderr="")
                if surface == "issues" and kind == "comments":
                    return SimpleNamespace(returncode=0, stdout=json.dumps(sig.get("comments", [])), stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")
    return fake_run


def _run_orchestrate(argv, fake):
    """Run cmd_orchestrate_reviews with a mocked gh + gh-available, capturing stdout -> (rc, out)."""
    from devflow import cli
    args = cli.build_parser().parse_args(argv)
    buf = io.StringIO()
    with mock.patch.object(G.shutil, "which", return_value="gh"), \
         mock.patch.object(G.subprocess, "run", side_effect=fake), \
         mock.patch("devflow.cli.check_gh_available",
                    return_value={"available": True, "authenticated": True, "account": "t", "error": None}), \
         redirect_stdout(buf):
        rc = cli.cmd_orchestrate_reviews(args)
    return rc, buf.getvalue()


def _plan_from(out):
    return json.loads(out[out.index("{"):])["plan"]


class TestOrchestrateCommand(unittest.TestCase):
    def test_command_plan_marker_request_and_persisted_state(self):
        calls = []
        prs = [{"number": 5, "title": "feat: x", "state": "OPEN", "headRefName": "feat/5", "baseRefName": "main"}]
        fake = _cli_fake_gh(calls, prs=prs, views={5: _raw_view(5, head="aaaaaaa")})   # #5 unreviewed
        state_file = os.path.join(tempfile.mkdtemp(), "orch.json")
        rc, out = _run_orchestrate(
            ["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file, "--json"], fake)
        self.assertEqual(rc, 0)
        self.assertIn("ORCHESTRATION_PLAN", out)        # marker is actually printed
        self.assertIn("REQUEST REVIEW", out)
        self.assertIn("#5", out)
        self.assertEqual(_plan_from(out)["request_review"], [5])
        # the recommended request was persisted to local tracking for THIS head (head-aware in-flight)
        self.assertEqual(orch.load_state(state_file)["requested_head"]["5"], "aaaaaaa")
        for cmd in calls:
            _assert_read_only(cmd[1:])

    def test_command_dry_does_not_persist_state(self):
        calls = []
        prs = [{"number": 5, "title": "feat: x", "state": "OPEN", "headRefName": "feat/5", "baseRefName": "main"}]
        fake = _cli_fake_gh(calls, prs=prs, views={5: _raw_view(5)})
        state_file = os.path.join(tempfile.mkdtemp(), "orch.json")
        rc, _ = _run_orchestrate(
            ["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file, "--dry"], fake)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(state_file))    # --dry computes the plan but persists nothing

    def test_command_mark_converged_makes_pr_mergeable_then_stale(self):
        calls = []
        prs = [{"number": 5, "title": "feat: x", "state": "OPEN", "headRefName": "feat/5", "baseRefName": "main"}]
        state_file = os.path.join(tempfile.mkdtemp(), "orch.json")
        # #5 has no Codex review of its own -> only the converged PIN can make it merge-ready
        rc, out = _run_orchestrate(
            ["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file, "--json", "--mark-converged", "5"],
            _cli_fake_gh(calls, prs=prs, views={5: _raw_view(5, head="aaaaaaa")}))
        self.assertEqual(rc, 0)
        self.assertEqual(orch.load_state(state_file)["converged"]["5"], "aaaaaaa")
        self.assertIn(5, _plan_from(out)["mergeable_now"])          # merge rule 3
        # ...once the head advances, the stale pin no longer makes it mergeable (re-queues for review)
        rc2, out2 = _run_orchestrate(
            ["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file, "--json"],
            _cli_fake_gh([], prs=prs, views={5: _raw_view(5, head="bbbbbbb")}))
        plan2 = _plan_from(out2)
        self.assertNotIn(5, plan2["mergeable_now"])
        self.assertEqual(plan2["request_review"], [5])

    def test_command_per_pr_gh_error_does_not_abort_sweep(self):
        calls = []
        prs = [{"number": 5, "title": "a", "state": "OPEN", "headRefName": "feat/5", "baseRefName": "main"},
               {"number": 6, "title": "b", "state": "OPEN", "headRefName": "feat/6", "baseRefName": "main"}]
        fake = _cli_fake_gh(calls, prs=prs, views={6: _raw_view(6, head="bbbbbbb")}, error_prs={5})
        state_file = os.path.join(tempfile.mkdtemp(), "orch.json")
        rc, out = _run_orchestrate(
            ["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file, "--json"], fake)
        self.assertEqual(rc, 0)                          # one failing PR must not abort the sweep
        data = json.loads(out[out.index("{"):])
        self.assertTrue(any(e["pr"] == 5 for e in data["errors"]))
        self.assertEqual(data["plan"]["request_review"], [6])   # the healthy PR is still planned
        for cmd in calls:
            _assert_read_only(cmd[1:])

    def test_command_retargets_child_of_merged_parent(self):
        # Codex r1 F4+F8: open stack + merged history are fetched SEPARATELY, and a child of a merged
        # parent is flagged to retarget even though it is not merge-ready
        calls = []
        prs = [{"number": 7, "title": "child", "state": "OPEN",
                "headRefName": "feat/7", "baseRefName": "feat/parent"}]
        merged = [{"number": 4, "title": "parent", "state": "MERGED",
                   "headRefName": "feat/parent", "baseRefName": "main"}]
        fake = _cli_fake_gh(calls, prs=prs, views={7: _raw_view(7, head="ccccccc", base="feat/parent")},
                            merged_prs=merged)
        state_file = os.path.join(tempfile.mkdtemp(), "orch.json")
        rc, out = _run_orchestrate(
            ["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file, "--json"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(_plan_from(out)["needs_retarget"], [7])
        self.assertEqual(_plan_from(out)["request_review"], [])   # not requested against the stale base

    def test_command_labels_use_repo_default_branch(self):
        # Codex r4 P2: action labels must name the repo's ACTUAL default branch, not hardcoded 'main'
        calls = []
        prs = [{"number": 7, "title": "child", "state": "OPEN",
                "headRefName": "feat/7", "baseRefName": "feat/parent"}]
        merged = [{"number": 4, "title": "parent", "state": "MERGED",
                   "headRefName": "feat/parent", "baseRefName": "master"}]
        fake = _cli_fake_gh(calls, prs=prs, views={7: _raw_view(7, head="ccccccc", base="feat/parent")},
                            merged_prs=merged, default_branch="master")
        state_file = os.path.join(tempfile.mkdtemp(), "orch.json")
        rc, out = _run_orchestrate(["orchestrate-reviews", "--repo", "o/r", "--state-file", state_file], fake)
        self.assertEqual(rc, 0)
        self.assertIn("RETARGET to master", out)
        self.assertNotIn("RETARGET to main", out)


if __name__ == "__main__":
    unittest.main()
