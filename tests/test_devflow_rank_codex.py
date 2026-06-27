# -*- coding: utf-8 -*-
"""Tests for `rank-codex-reviews` (read-only PR review-priority ranking) and its pure scorer.

All `gh` calls are mocked (no network, no writes).

    python -m unittest tests.test_devflow_rank_codex
"""

import contextlib
import io
import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from devflow import cli
from devflow.tools import github_cli as G
from devflow.tools.review_priority import score, classify_type

CODEX = "chatgpt-codex-connector[bot]"
QUOTA_BODY = "You have reached your Codex usage limits for code reviews. See the Codex dashboard."
_WRITE_TOKENS = {"create", "comment", "merge", "edit", "close", "delete", "review", "push", "clone"}


# --------------------------------------------------------------------------- pure scorer
class TestReviewPriorityScore(unittest.TestCase):

    def test_unreviewed_outranks_well_reviewed(self):
        fresh = score(additions=200, changed_files=4, title="feat: x", branch="feat/x", codex_rounds=0)
        worn = score(additions=200, changed_files=4, title="feat: x", branch="feat/x", codex_rounds=6)
        self.assertGreater(fresh["priority"], worn["priority"])
        self.assertEqual(fresh["needs_review"], 10)
        self.assertLess(worn["needs_review"], fresh["needs_review"])

    def test_feature_outranks_bugfix_all_else_equal(self):
        feat = score(additions=120, changed_files=3, title="feat: add thing", branch="feat/x", codex_rounds=0)
        bug = score(additions=120, changed_files=3, title="fix: tweak thing", branch="fix/x", codex_rounds=0)
        self.assertEqual((feat["type"], bug["type"]), ("feature", "bugfix"))
        self.assertGreater(feat["priority"], bug["priority"])

    def test_bigger_change_has_higher_impact(self):
        small = score(additions=60, changed_files=1, title="feat: a", branch="feat/a")
        big = score(additions=1200, changed_files=8, title="feat: b", branch="feat/b")
        self.assertGreater(big["impact"], small["impact"])
        self.assertLessEqual(big["impact"], 10)            # capped

    def test_deletions_contribute_to_impact(self):
        # a removal-heavy PR still has blast radius -> deletions raise impact (Codex r1)
        no_del = score(additions=100, deletions=0, changed_files=1, title="feat: x", branch="feat/x")
        big_del = score(additions=100, deletions=2000, changed_files=1, title="feat: x", branch="feat/x")
        self.assertGreater(big_del["impact"], no_del["impact"])

    def test_reviewed_on_head_lowers_need(self):
        not_seen = score(additions=200, changed_files=3, title="feat: x", branch="feat/x", codex_rounds=2)
        seen = score(additions=200, changed_files=3, title="feat: x", branch="feat/x", codex_rounds=2,
                     reviewed_on_head=True)
        self.assertLess(seen["needs_review"], not_seen["needs_review"])

    def test_priority_is_clamped_0_100(self):
        s = score(additions=99999, changed_files=99, title="feat: huge", branch="feat/x", codex_rounds=0)
        self.assertLessEqual(s["priority"], 100)
        self.assertGreaterEqual(s["priority"], 0)

    def test_classify_type_large_fix_is_not_deprioritized(self):
        self.assertEqual(classify_type("fix: small typo", "fix/typo", 12), "bugfix")
        self.assertNotEqual(classify_type("fix: rewrite engine", "fix/engine", 900), "bugfix")  # big "fix" not deprioritized
        self.assertNotEqual(classify_type("fix: drop legacy engine", "fix/drop", 0, 2000), "bugfix")  # removal-heavy (Codex r2)
        self.assertEqual(classify_type("feat: new cmd", "feat/cmd", 100), "feature")
        self.assertEqual(classify_type("chore: bump", "chore/bump", 5), "mixed")


# --------------------------------------------------------------------------- command (mocked gh)
def pr(num, title, branch, adds, dels=0, files=1, state="OPEN", head=None):
    return {"number": num, "title": title, "headRefName": branch, "headRefOid": head or f"h{num}",
            "state": state, "additions": adds, "deletions": dels, "changedFiles": files,
            "url": f"https://x/pull/{num}"}


def rev(commit, body="### Codex Review\n- fix the null case", login=CODEX, state="COMMENTED"):
    return {"user": {"login": login}, "body": body, "state": state,
            "submitted_at": "2026-01-03T00:00:00Z", "commit_id": commit, "html_url": "u"}


def quota_comment(created_at="2026-02-01T00:00:00Z", login=CODEX):
    return {"user": {"login": login},
            "body": "You have reached your Codex usage limits for code reviews.",
            "created_at": created_at, "html_url": "q"}


def make_fake_gh(pr_list, reviews_by_pr=None, comments_by_pr=None, auth_ok=True, recorder=None,
                 error_prs=()):
    reviews_by_pr = reviews_by_pr or {}
    comments_by_pr = comments_by_pr or {}
    error_prs = set(error_prs)

    def fake_run(cmd, **kw):
        if recorder is not None:
            recorder.append(cmd)
        args = cmd[1:]
        if args[:2] == ["auth", "status"]:
            return SimpleNamespace(returncode=0 if auth_ok else 1,
                                   stdout="Logged in to github.com account TESTER" if auth_ok else "",
                                   stderr="" if auth_ok else "not logged in")
        if args[:2] == ["repo", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"nameWithOwner": "o/r"}), stderr="")
        if args[:2] == ["pr", "list"]:
            st = args[args.index("--state") + 1] if "--state" in args else "open"
            sel = pr_list if st == "all" else [p for p in pr_list
                                               if (p.get("state") or "OPEN").lower() == st]
            return SimpleNamespace(returncode=0, stdout=json.dumps(sel), stderr="")
        if args and args[0] == "api":
            path = next((a for a in args[1:] if a.startswith("repos/")), "")
            mr = re.search(r"/pulls/(\d+)/reviews", path)
            mc = re.search(r"/issues/(\d+)/comments", path)
            if mr:
                n = int(mr.group(1))
                if n in error_prs:
                    return SimpleNamespace(returncode=1, stdout="", stderr=f"simulated gh error for PR {n}")
                payload = reviews_by_pr.get(n, [])
            elif mc:
                payload = comments_by_pr.get(int(mc.group(1)), [])
            else:
                payload = []
            out = [payload] if "--slurp" in args else payload
            return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected: " + " ".join(args))
    return fake_run


class RankCodexBase(unittest.TestCase):
    def run_rank(self, pr_list, reviews_by_pr=None, comments_by_pr=None, top=3, include_merged=False,
                 as_json=False, limit=50, auth_ok=True, repo="o/r", error_prs=()):
        recorder = []
        fake = make_fake_gh(pr_list, reviews_by_pr, comments_by_pr, auth_ok=auth_ok, recorder=recorder,
                            error_prs=error_prs)
        args = SimpleNamespace(repo=repo, limit=limit, top=top, include_merged=include_merged,
                               json=as_json)
        buf = io.StringIO()
        with mock.patch.object(G.shutil, "which", return_value="gh"), \
             mock.patch.object(G.subprocess, "run", side_effect=fake), \
             contextlib.redirect_stdout(buf):
            rc = cli.cmd_rank_codex_reviews(args)
        return rc, buf.getvalue(), recorder

    def order_of(self, out):
        return [int(m) for m in re.findall(r"#\s*(\d+) prio=", out)]


class TestRankCodexBehavior(RankCodexBase):

    def test_unreviewed_feature_ranks_above_reviewed_feature(self):
        prs = [pr(1, "feat: big new thing", "feat/big", 600, files=5),
               pr(2, "feat: other thing", "feat/other", 600, files=5, head="hX")]
        # PR #2 already has 5 substantive Codex reviews on its head; PR #1 has none
        reviews = {2: [rev("hX") for _ in range(5)]}
        rc, out, _ = self.run_rank(prs, reviews_by_pr=reviews)
        self.assertEqual(rc, 0)
        self.assertIn("RANKED_CODEX_REVIEW_QUEUE", out)
        self.assertEqual(self.order_of(out)[0], 1)        # the never-reviewed PR is first
        self.assertIn("recommend_next=[1, 2]", out)

    def test_unreviewed_feature_ranks_above_small_bugfix(self):
        prs = [pr(1, "feat: substantial feature", "feat/f", 500, files=5),
               pr(2, "fix: small bug", "fix/b", 40, files=1)]
        rc, out, _ = self.run_rank(prs)
        self.assertEqual(self.order_of(out), [1, 2])

    def test_quota_notice_is_not_counted_as_a_review(self):
        # a PR whose only Codex "review" is a usage-limit notice must rank as UNREVIEWED (rounds=0)
        prs = [pr(1, "feat: x", "feat/x", 300, files=4)]
        rc, out, _ = self.run_rank(prs, reviews_by_pr={1: [rev("h1", body=QUOTA_BODY)]})
        self.assertIn("rounds=0", out)

    def test_top_n_recommendation_count(self):
        prs = [pr(i, f"feat: f{i}", f"feat/{i}", 300, files=3) for i in (1, 2, 3, 4)]
        _, out, _ = self.run_rank(prs, top=2)
        m = re.search(r"recommend_next=\[([^\]]*)\]", out)
        self.assertEqual(len([x for x in m.group(1).split(",") if x.strip()]), 2)

    def test_include_merged_keeps_open_and_merged_only(self):
        prs = [pr(1, "feat: open", "feat/o", 200, files=3, state="OPEN"),
               pr(2, "feat: merged", "feat/m", 200, files=3, state="MERGED"),
               pr(3, "feat: closed", "feat/c", 200, files=3, state="CLOSED")]
        _, out, _ = self.run_rank(prs, include_merged=True)
        ranked = self.order_of(out)
        self.assertIn(1, ranked)
        self.assertIn(2, ranked)
        self.assertNotIn(3, ranked)                       # closed-unmerged is excluded

    def test_empty_body_stateful_review_counts_as_a_round(self):
        # a Codex review with an empty body but a real state (content in inline comments) is a round (Codex r1)
        prs = [pr(1, "feat: x", "feat/x", 300, files=4)]
        rc, out, _ = self.run_rank(prs, reviews_by_pr={1: [rev("h1", body="", state="CHANGES_REQUESTED")]})
        self.assertIn("rounds=1", out)                    # not mis-counted as never-reviewed

    def test_errored_pr_is_excluded_not_ranked_max_priority(self):
        # a PR whose review lookup fails must NOT be ranked as rounds=0 (which would mint max priority)
        prs = [pr(1, "feat: a", "feat/a", 200, files=3), pr(2, "feat: b", "feat/b", 200, files=3)]
        rc, out, _ = self.run_rank(prs, reviews_by_pr={2: [rev("h2")]}, error_prs={1})
        self.assertEqual(self.order_of(out), [2])         # only the readable PR is ranked
        self.assertIn("! PR #1", out)                     # the errored PR is surfaced, not silently ranked

    def test_include_merged_uses_separate_state_queries(self):
        # eligibility filter is pushed into the gh query (open + merged), so --limit counts eligible PRs
        prs = [pr(1, "feat: o", "feat/o", 200, files=3, state="OPEN"),
               pr(2, "feat: m", "feat/m", 200, files=3, state="MERGED")]
        _, _, rec = self.run_rank(prs, include_merged=True)
        states = [c[c.index("--state") + 1] for c in rec if c[1:3] == ["pr", "list"] and "--state" in c]
        self.assertIn("open", states)
        self.assertIn("merged", states)
        self.assertNotIn("all", states)                   # no longer a broad 'all' fetch then post-filter

    def test_limit_caps_combined_open_and_merged(self):
        # --include-merged with --limit N must cap the COMBINED union to N, not N-per-state (Codex r2)
        prs = [pr(1, "feat: o", "feat/o", 200, files=3, state="OPEN"),
               pr(2, "feat: m", "feat/m", 200, files=3, state="MERGED")]
        rc, out, _ = self.run_rank(prs, include_merged=True, limit=1)
        self.assertEqual(len(self.order_of(out)), 1)      # only 1 ranked despite 1 open + 1 merged

    def test_quota_limited_pr_kept_out_of_recommend_next(self):
        # a PR whose latest Codex signal is a usage-limit COMMENT is rate-limited -> not recommended (Codex r2)
        prs = [pr(1, "feat: big", "feat/big", 600, files=6),     # rate-limited, would otherwise top
               pr(2, "feat: other", "feat/o", 200, files=3)]
        rc, out, _ = self.run_rank(prs, comments_by_pr={1: [quota_comment()]}, top=1)
        self.assertIn("rate-limited", out)
        m = re.search(r"recommend_next=\[([^\]]*)\]", out)
        self.assertNotIn("1", m.group(1))                 # the rate-limited PR is NOT recommended
        self.assertIn("2", m.group(1))                    # the reviewable one is

    def test_no_prs_emits_no_prs_marker(self):
        rc, out, _ = self.run_rank([])
        self.assertEqual(rc, 0)
        self.assertIn("NO_PRS_TO_RANK", out)

    def test_incomplete_marker_when_all_lookups_error(self):
        # empty ranking SOLELY because every coverage lookup errored -> distinct INCOMPLETE marker (Codex r3)
        prs = [pr(1, "feat: a", "feat/a", 200, files=3), pr(2, "feat: b", "feat/b", 200, files=3)]
        _, out, _ = self.run_rank(prs, error_prs={1, 2})
        self.assertIn("RANK_CODEX_INCOMPLETE", out)
        self.assertNotIn("NO_PRS_TO_RANK", out)
        self.assertNotIn("RANKED_CODEX_REVIEW_QUEUE", out)

    def test_include_merged_interleaves_not_starves(self):
        # with more open PRs than --limit, a merged PR must not be crowded out before scoring (Codex r3)
        prs = [pr(1, "feat: o1", "feat/o1", 200, files=3, state="OPEN"),
               pr(2, "feat: o2", "feat/o2", 200, files=3, state="OPEN"),
               pr(3, "feat: o3", "feat/o3", 200, files=3, state="OPEN"),
               pr(9, "feat: m", "feat/m", 200, files=3, state="MERGED")]
        _, out, _ = self.run_rank(prs, include_merged=True, limit=2)
        self.assertIn(9, self.order_of(out))     # merged PR interleaved in, not starved

    def test_comment_only_review_counts_as_a_round(self):
        # a PR "reviewed" only via a Codex conversation comment must score rounds>=1, not 0 (Codex r3)
        gh = G.ReadOnlyGitHub("o/r")
        with mock.patch.object(gh, "get_pr_reviews", return_value=[]), \
             mock.patch.object(gh, "get_pr_comments", return_value=[
                 {"author": CODEX, "body": "Codex review: looks fine, one nit.",
                  "created_at": "2026-01-03T00:00:00Z"}]):
            cov = gh.codex_review_rounds(1)
        self.assertEqual(cov["rounds"], 1)

    def test_json_output_parses_and_has_ranking(self):
        prs = [pr(1, "feat: a", "feat/a", 400, files=4), pr(2, "fix: b", "fix/b", 30, files=1)]
        _, out, _ = self.run_rank(prs, as_json=True)
        lines = out.splitlines()
        start = max(i for i, ln in enumerate(lines) if ln == "{")
        data = json.loads("\n".join(lines[start:]))
        self.assertEqual(data["marker"], "RANKED_CODEX_REVIEW_QUEUE")
        self.assertEqual(data["recommend_next"][0], 1)
        self.assertEqual(data["ranked"][0]["pr"], 1)

    def test_gh_unauthenticated_returns_nonzero(self):
        rc, out, _ = self.run_rank([pr(1, "feat: x", "feat/x", 100)], auth_ok=False)
        self.assertEqual(rc, 3)
        self.assertNotIn("RANKED_CODEX_REVIEW_QUEUE", out)


class TestRankCodexReadOnly(RankCodexBase):

    def test_only_read_only_gh_commands_are_spawned(self):
        prs = [pr(1, "feat: x", "feat/x", 200, files=3)]
        _, _, recorder = self.run_rank(prs, reviews_by_pr={1: [rev("h1")]})
        self.assertTrue(recorder)
        for cmd in recorder:
            args = cmd[1:]
            verb = args[0] if args else ""
            self.assertNotIn(verb, _WRITE_TOKENS, f"unexpected write verb in {args}")
            self.assertIn(verb, {"auth", "repo", "pr", "api"})
            if verb == "api":
                self.assertNotIn("-f", args)
                self.assertNotIn("--field", args)
                self.assertFalse(any(a.startswith("-X") or a == "--method" for a in args))
            G._assert_read_only(args)


if __name__ == "__main__":
    unittest.main()
