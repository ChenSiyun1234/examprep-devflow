# -*- coding: utf-8 -*-
"""Tests for the read-only `watch-codex-reviews` command. All `gh` calls are mocked (no network,
no writes); dedupe state goes to a throwaway temp seen-file.

    python -m unittest tests.test_devflow_watch_codex
"""

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from devflow import cli
from devflow.tools import github_cli as G

CODEX = "chatgpt-codex-connector[bot]"

# write-ish tokens that must NEVER appear as a leading gh verb in a read-only watcher
_WRITE_TOKENS = {"create", "comment", "merge", "edit", "close", "delete", "review", "push", "clone"}


def codex_review(created_at, url, body="Findings:\n- fix the null case\n- add a test",
                 state="COMMENTED", login=CODEX):
    return {"user": {"login": login}, "state": state, "body": body,
            "submitted_at": created_at, "html_url": url}


def codex_comment(created_at, url, body="Note:\n- consider X", login=CODEX):
    # conversation comment / inline review comment shape (created_at + html_url, no review state)
    return {"user": {"login": login}, "body": body, "created_at": created_at, "html_url": url}


QUOTA_BODY = ("You have reached your Codex usage limits for code reviews. "
              "You can see your limits in the Codex usage dashboard. "
              "To continue using code reviews, you can upgrade your account or add credits.")


def codex_quota(created_at, url, login=CODEX):
    # the trusted-Codex "rate-limited" notice — NOT a review (must not be flagged as actionable)
    return {"user": {"login": login}, "body": QUOTA_BODY, "created_at": created_at, "html_url": url}


def make_fake_gh(open_prs, reviews_by_pr=None, comments_by_pr=None, review_comments_by_pr=None,
                 error_prs=(), auth_ok=True, recorder=None, repo_name="o/r"):
    reviews_by_pr = reviews_by_pr or {}
    comments_by_pr = comments_by_pr or {}
    review_comments_by_pr = review_comments_by_pr or {}
    error_prs = set(error_prs)

    def fake_run(cmd, **kw):
        if recorder is not None:
            recorder.append(cmd)
        args = cmd[1:]  # drop "gh"
        if args[:2] == ["auth", "status"]:
            return SimpleNamespace(returncode=0 if auth_ok else 1,
                                   stdout="Logged in to github.com account TESTER" if auth_ok else "",
                                   stderr="" if auth_ok else "You are not logged into any GitHub hosts.")
        if args[:2] == ["repo", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"nameWithOwner": repo_name}), stderr="")
        if args[:2] == ["pr", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(open_prs), stderr="")
        if args and args[0] == "api":
            path = next((a for a in args[1:] if a.startswith("repos/")), "")
            m = re.search(r"/(?:issues|pulls)/(\d+)/", path)
            n = int(m.group(1)) if m else -1
            if n in error_prs:
                return SimpleNamespace(returncode=1, stdout="", stderr=f"simulated gh error for PR {n}")
            if "/reviews" in path:
                payload = reviews_by_pr.get(n, [])
            elif "/pulls/" in path and path.endswith("/comments"):
                payload = review_comments_by_pr.get(n, [])
            elif "/comments" in path:
                payload = comments_by_pr.get(n, [])
            else:
                payload = []
            # real `gh api --paginate --slurp` returns an ARRAY OF PAGES; emit that exact shape so
            # the test drives the real _flatten_pages page-unwrapping (not just a flat array).
            out = [payload] if "--slurp" in args else payload
            return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected: " + " ".join(args))
    return fake_run


class WatchCodexBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="watchcodex-")
        self.seen = os.path.join(self.dir, "seen.json")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_watch(self, open_prs, reviews_by_pr=None, comments_by_pr=None, review_comments_by_pr=None,
                  error_prs=(), reset=False, init=False, as_json=False, exit_actionable=False,
                  limit=50, auth_ok=True, repo="o/r", repo_name="o/r"):
        recorder = []
        fake = make_fake_gh(open_prs, reviews_by_pr, comments_by_pr, review_comments_by_pr,
                            error_prs=error_prs, auth_ok=auth_ok, recorder=recorder, repo_name=repo_name)
        args = SimpleNamespace(repo=repo, seen_file=self.seen, limit=limit, reset=reset,
                               init=init, json=as_json, exit_actionable=exit_actionable,
                               body_chars=600)
        buf = io.StringIO()
        with mock.patch.object(G.shutil, "which", return_value="gh"), \
             mock.patch.object(G.subprocess, "run", side_effect=fake), \
             contextlib.redirect_stdout(buf):
            rc = cli.cmd_watch_codex_reviews(args)
        return rc, buf.getvalue(), recorder

    def assertActionable(self, out, *pr_nums):
        self.assertIn("ACTIONABLE_CODEX_REVIEWS", out)
        self.assertNotIn("NO_NEW_CODEX_REVIEWS", out)
        for n in pr_nums:
            self.assertIn(f"#{n}", out)

    def assertNoNew(self, out):
        self.assertIn("NO_NEW_CODEX_REVIEWS", out)
        self.assertNotIn("ACTIONABLE_CODEX_REVIEWS", out)

    def assertQuota(self, out):
        self.assertIn("CODEX_QUOTA_LIMITED", out)
        self.assertNotIn("ACTIONABLE_CODEX_REVIEWS", out)
        self.assertNotIn("NO_NEW_CODEX_REVIEWS", out)


class TestWatchCodexBehavior(WatchCodexBase):

    def test_actionable_when_new_codex_review(self):
        prs = [{"number": 1, "title": "Add feature", "updatedAt": "2026-01-03T00:00:00Z", "url": "p1"}]
        rc, out, _ = self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]})
        self.assertEqual(rc, 0)
        self.assertActionable(out, 1)
        # marker is the very first line (strict-consumer compatible)
        self.assertEqual(out.splitlines()[0], "ACTIONABLE_CODEX_REVIEWS")
        # seen file persisted with the dedupe key for PR #1
        with open(self.seen, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("1", saved.get("o/r", {}))

    def test_dedupe_second_run_is_no_new(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        reviews = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]}
        _, out1, _ = self.run_watch(prs, reviews_by_pr=reviews)
        self.assertActionable(out1, 1)
        _, out2, _ = self.run_watch(prs, reviews_by_pr=reviews)   # identical -> already seen
        self.assertNoNew(out2)

    def test_new_feedback_after_seen_is_actionable_again(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-01T00:00:00Z", "p1#r1")]})
        # a newer Codex review appears -> latest signal changed -> actionable
        _, out, _ = self.run_watch(prs, reviews_by_pr={
            1: [codex_review("2026-01-01T00:00:00Z", "p1#r1"),
                codex_review("2026-02-01T00:00:00Z", "p1#r2", body="Now blocking: must fix",
                             state="CHANGES_REQUESTED")]})
        self.assertActionable(out, 1)
        self.assertIn("blocking=True", out)

    def test_no_codex_feedback_is_no_new(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        human = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1", login="alice")]}
        _, out, _ = self.run_watch(prs, reviews_by_pr=human)
        self.assertNoNew(out)

    def test_spoofy_codex_author_ignored(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        spoof = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1", login="codex-fan")]}
        _, out, _ = self.run_watch(prs, reviews_by_pr=spoof)
        self.assertNoNew(out)

    def test_no_open_prs_is_no_new(self):
        rc, out, _ = self.run_watch([])
        self.assertEqual(rc, 0)
        self.assertNoNew(out)
        self.assertIn("checked=0", out)

    def test_multiple_prs_only_codex_ones_actionable(self):
        prs = [{"number": 1, "title": "A", "updatedAt": "z", "url": "p1"},
               {"number": 2, "title": "B", "updatedAt": "z", "url": "p2"},
               {"number": 3, "title": "C", "updatedAt": "z", "url": "p3"}]
        reviews = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")],
                   2: [codex_review("2026-01-03T00:00:00Z", "p2#r1", login="bob")],  # human
                   3: [codex_review("2026-01-03T00:00:00Z", "p3#r1")]}
        _, out, _ = self.run_watch(prs, reviews_by_pr=reviews)
        self.assertActionable(out, 1, 3)
        self.assertIn("new=2", out)
        self.assertNotIn("#2", out)   # the human-reviewed PR is not flagged

    def test_reset_treats_all_as_new(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        reviews = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]}
        self.run_watch(prs, reviews_by_pr=reviews)                 # prime seen
        _, out_seen, _ = self.run_watch(prs, reviews_by_pr=reviews)
        self.assertNoNew(out_seen)
        _, out_reset, _ = self.run_watch(prs, reviews_by_pr=reviews, reset=True)
        self.assertActionable(out_reset, 1)

    def test_per_pr_gh_error_does_not_abort_sweep(self):
        prs = [{"number": 1, "title": "A", "updatedAt": "z", "url": "p1"},
               {"number": 2, "title": "B", "updatedAt": "z", "url": "p2"}]
        reviews = {2: [codex_review("2026-01-03T00:00:00Z", "p2#r1")]}
        _, out, _ = self.run_watch(prs, reviews_by_pr=reviews, error_prs={1})
        self.assertIn("! PR #1", out)        # PR 1 errored but was reported, not fatal
        self.assertActionable(out, 2)        # PR 2 still processed

    def test_incomplete_marker_on_partial_failure(self):
        # a read failure with no actionable feedback must NOT surface as a clean NO_NEW (Codex r1 #3)
        prs = [{"number": 1, "title": "A", "updatedAt": "z", "url": "p1"}]
        rc, out, _ = self.run_watch(prs, error_prs={1})
        self.assertIn("CODEX_WATCH_INCOMPLETE", out)
        self.assertNotIn("NO_NEW_CODEX_REVIEWS", out)
        self.assertEqual(out.splitlines()[0], "CODEX_WATCH_INCOMPLETE")
        self.assertIn("! PR #1", out)

    def test_incomplete_marker_outranks_quota(self):
        # Codex r5 P2: when one PR read fails AND another only has a quota notice, the incomplete sweep
        # must win the first-line marker — a consumer must not back off for quota having missed a PR
        prs = [{"number": 1, "title": "A", "updatedAt": "z", "url": "p1"},
               {"number": 2, "title": "B", "updatedAt": "z", "url": "p2"}]
        rc, out, _ = self.run_watch(prs, error_prs={1},
                                    comments_by_pr={2: [codex_quota("2026-01-04T00:00:00Z", "p2#q1")]})
        self.assertEqual(out.splitlines()[0], "CODEX_WATCH_INCOMPLETE")   # errors outrank quota
        self.assertIn("! PR #1", out)

    def test_quota_notice_rejects_prefixed_phrase(self):
        # Codex r5 P2: a review that opens with a PREFIX before the phrase is NOT a quota notice
        self.assertFalse(G.is_codex_quota_notice("Bug: reached your Codex usage limits parsing the body."))
        self.assertTrue(G.is_codex_quota_notice(
            "You have reached your Codex usage limits for code reviews. You can see your limits in the dashboard."))

    def test_quota_notice_requires_code_review_context(self):
        # Codex r6 P2: even the opener phrase, WITHOUT the "for code reviews" context, is not the notice
        # (a real review can open by quoting the opener while discussing this matcher)
        self.assertFalse(G.is_codex_quota_notice(
            "You have reached your Codex usage limits parsing — the matcher needs the full opener."))
        self.assertTrue(G.is_codex_quota_notice(
            "You have reached your Codex usage limits for code reviews. You can see your limits in the dashboard."))

    def test_quota_notice_requires_full_notice_not_just_first_sentence(self):
        # Codex r7 P2: a review quoting ONLY the canonical first sentence then giving feedback (no
        # "you can see your limits" continuation, still <600 chars) is NOT a quota notice
        self.assertFalse(G.is_codex_quota_notice(
            "You have reached your Codex usage limits for code reviews. But here is my real feedback: "
            "fix the null deref in line 12 and add a test."))
        self.assertTrue(G.is_codex_quota_notice(
            "You have reached your Codex usage limits for code reviews. You can see your limits in the dashboard."))

    def test_init_surfaces_errored_prs_not_baselined(self):
        # --init must report PRs it could NOT baseline (else the next poll re-alerts on them) (Codex r1 #3)
        prs = [{"number": 1, "title": "A", "updatedAt": "z", "url": "p1"},
               {"number": 2, "title": "B", "updatedAt": "z", "url": "p2"}]
        rc, out, _ = self.run_watch(prs, reviews_by_pr={2: [codex_review("2026-01-03T00:00:00Z", "p2#r1")]},
                                    error_prs={1}, init=True)
        self.assertEqual(rc, 0)
        self.assertIn("! PR #1", out)
        self.assertIn("NOT baselined", out)

    def test_same_second_lower_ranked_comment_re_alerts(self):
        # a newly-visible inline comment sharing the review's 1s timestamp changes the dedupe key (Codex r1 #3)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        T = "2026-01-03T00:00:00Z"
        self.run_watch(prs, reviews_by_pr={1: [codex_review(T, "p1#r1")]})              # baseline: review only
        _, out, _ = self.run_watch(                                                      # +inline comment, same second
            prs, reviews_by_pr={1: [codex_review(T, "p1#r1")]},
            review_comments_by_pr={1: [codex_comment(T, "p1#rc1")]})
        self.assertActionable(out, 1)        # the same-second lower-ranked comment is not swallowed

    def test_seen_key_is_case_normalized(self):
        # different --repo casings map to ONE seen slice (GitHub repos are case-insensitive) (Codex r1 #3)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        reviews = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]}
        self.run_watch(prs, reviews_by_pr=reviews, repo="Owner/Repo")                   # prime under mixed case
        with open(self.seen, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("owner/repo", saved)   # stored lowercased
        _, out2, _ = self.run_watch(prs, reviews_by_pr=reviews, repo="owner/repo")       # other casing
        self.assertNoNew(out2)               # already-seen under the normalized key, not re-alerted

    def test_save_seen_is_atomic_no_tmp_leftover(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]})
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])      # temp file was os.replace'd into place, none left behind

    def test_is_codex_quota_notice_is_precise(self):
        # the exact bot notice -> True; a substantive (long) review that merely MENTIONS usage limits
        # -> False (must not be dropped as a rate-limit notice) (Codex r2)
        self.assertTrue(G.is_codex_quota_notice(
            "You have reached your Codex usage limits for code reviews. You can see your limits in the dashboard."))
        long_review = "### Codex Review\n- " + "the watcher usage limit / codex handling looks off; " * 30
        self.assertFalse(G.is_codex_quota_notice(long_review))

    def test_quota_notice_after_seen_review_not_re_alerted(self):
        # a quota notice arriving AFTER a seen review must not advance the dedupe key / re-alert (Codex r2)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]})  # baseline
        _, out, _ = self.run_watch(
            prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
            comments_by_pr={1: [codex_quota("2026-01-04T00:00:00Z", "p1#q1")]})   # newer quota notice
        self.assertNotIn("ACTIONABLE_CODEX_REVIEWS", out)   # stale review NOT re-alerted (key over non-quota)
        self.assertIn("CODEX_QUOTA_LIMITED", out)           # but rate-limit is still signalled

    def test_case_colliding_seen_slices_merged_not_clobbered(self):
        # a file holding BOTH 'Owner/Repo' and 'owner/repo' must merge their PR entries (Codex r2)
        from devflow.cli import _load_codex_seen
        with open(self.seen, "w", encoding="utf-8") as f:
            json.dump({"Owner/Repo": {"1": {"key": "a"}}, "owner/repo": {"2": {"key": "b"}}}, f)
        merged = _load_codex_seen(self.seen)
        self.assertEqual(set(merged.get("owner/repo", {})), {"1", "2"})   # neither slice clobbered

    def test_legacy_mixed_case_seen_slice_is_migrated_on_load(self):
        # a seen file written by pre-normalization code (mixed-case key) must still match (Codex r2)
        with open(self.seen, "w", encoding="utf-8") as f:
            json.dump({"Owner/Repo": {"1": {"key": "2026-01-03T00:00:00Z|p1#r1"}}}, f)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
                                   repo="owner/repo")
        self.assertNoNew(out)   # legacy slice migrated on load -> already-seen, not re-alerted

    def test_quota_notice_must_be_at_opener_not_mid_body(self):
        # a SHORT (<600) real review that QUOTES the canonical notice mid-sentence is NOT a quota
        # notice — the match must anchor at the opener, not anywhere in the body (Codex r4 P2)
        self.assertFalse(G.is_codex_quota_notice(
            "Note: when Codex says 'You have reached your Codex usage limits', this watcher backs off."))
        self.assertTrue(G.is_codex_quota_notice(
            "You have reached your Codex usage limits for code reviews. You can see your limits in the dashboard."))

    def test_case_colliding_seen_keeps_newest_entry(self):
        # same PR in both case-variant slices -> keep the NEWEST entry, not JSON insertion order (Codex r4 P2)
        from devflow.cli import _load_codex_seen
        with open(self.seen, "w", encoding="utf-8") as f:
            json.dump({"Owner/Repo": {"1": {"key": "new", "created_at": "2026-01-05T00:00:00Z"}},
                       "owner/repo": {"1": {"key": "old", "created_at": "2026-01-01T00:00:00Z"}}}, f)
        merged = _load_codex_seen(self.seen)
        self.assertEqual(merged["owner/repo"]["1"]["key"], "new")   # newest wins over the later stale slice

    def test_case_colliding_seen_prefers_fuller_key_on_timestamp_tie(self):
        # Codex r7 P2: on an EQUAL created_at across case variants, the FULLER key (more same-second URLs)
        # must win, not whichever slice appears later in the JSON
        from devflow.cli import _load_codex_seen
        with open(self.seen, "w", encoding="utf-8") as f:
            json.dump({"Owner/Repo": {"1": {"key": "T|review,inline", "created_at": "2026-01-05T00:00:00Z"}},
                       "owner/repo": {"1": {"key": "T|review", "created_at": "2026-01-05T00:00:00Z"}}}, f)
        merged = _load_codex_seen(self.seen)
        self.assertEqual(merged["owner/repo"]["1"]["key"], "T|review,inline")   # fuller key kept on tie

    def test_legacy_quota_inclusive_seen_key_not_re_alerted(self):
        # upgrade path: a seen file storing the OLD quota-INCLUSIVE key must not re-alert the same older
        # review now that the key excludes quota notices (Codex r4 P2)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        with open(self.seen, "w", encoding="utf-8") as f:   # old watcher stored the newer quota's key
            json.dump({"owner/repo": {"1": {"key": "2026-01-04T00:00:00Z|p1#q1"}}}, f)
        _, out, _ = self.run_watch(
            prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
            comments_by_pr={1: [codex_quota("2026-01-04T00:00:00Z", "p1#q1")]}, repo="owner/repo")
        self.assertNotIn("ACTIONABLE_CODEX_REVIEWS", out)   # legacy quota-inclusive key recognized as seen

    def test_legacy_key_migrated_forward_when_accepted(self):
        # Codex r8 P2: when a PR is seen via the legacy quota-inclusive key, MIGRATE the stored key to the
        # current dedupe_key so a later real change isn't masked by the stale legacy key
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        with open(self.seen, "w", encoding="utf-8") as f:   # old watcher stored the quota's key
            json.dump({"owner/repo": {"1": {"key": "2026-01-04T00:00:00Z|p1#q1"}}}, f)
        self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
                       comments_by_pr={1: [codex_quota("2026-01-04T00:00:00Z", "p1#q1")]}, repo="owner/repo")
        with open(self.seen, encoding="utf-8") as f:
            stored = json.load(f)["owner/repo"]["1"]["key"]
        self.assertEqual(stored, "2026-01-03T00:00:00Z|p1#r1")   # migrated to the current non-quota key

    def test_gh_unauthenticated_returns_nonzero(self):
        rc, out, _ = self.run_watch([{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}],
                                    auth_ok=False)
        self.assertEqual(rc, 3)
        self.assertNotIn("ACTIONABLE_CODEX_REVIEWS", out)

    def test_init_baseline_records_without_alerting(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        reviews = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]}
        rc, out, _ = self.run_watch(prs, reviews_by_pr=reviews, init=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("ACTIONABLE_CODEX_REVIEWS", out)   # neither marker on baseline
        self.assertNotIn("NO_NEW_CODEX_REVIEWS", out)
        self.assertIn("baseline recorded", out)
        # a subsequent normal run sees the pre-existing review as already-seen
        _, out2, _ = self.run_watch(prs, reviews_by_pr=reviews)
        self.assertNoNew(out2)

    def test_json_output_includes_marker_and_parses(self):
        prs = [{"number": 1, "title": "Add x", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
                                   as_json=True)
        self.assertIn("ACTIONABLE_CODEX_REVIEWS", out)
        lines = out.splitlines()
        start = max(i for i, ln in enumerate(lines) if ln == "{")   # the indented JSON blob
        data = json.loads("\n".join(lines[start:]))
        self.assertEqual(data["marker"], "ACTIONABLE_CODEX_REVIEWS")
        self.assertEqual(data["actionable"][0]["pr"], 1)
        self.assertEqual(data["checked"], [1])

    def test_exit_actionable_opt_in_returns_10(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        rc, out, _ = self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
                                    exit_actionable=True)
        self.assertEqual(rc, 10)
        self.assertIn("ACTIONABLE_CODEX_REVIEWS", out)
        rc2, _, _ = self.run_watch([], exit_actionable=True)   # nothing new -> default 0
        self.assertEqual(rc2, 0)

    def test_actionable_from_conversation_comment(self):
        # Codex feedback can arrive as a PR conversation comment (not just a formal review)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(prs, comments_by_pr={1: [codex_comment("2026-01-03T00:00:00Z", "p1#c1")]})
        self.assertActionable(out, 1)
        self.assertIn("pr_comment", out)

    def test_actionable_from_inline_review_comment(self):
        # ...or as an inline (file-level) review comment
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(prs,
                                   review_comments_by_pr={1: [codex_comment("2026-01-03T00:00:00Z", "p1#rc1")]})
        self.assertActionable(out, 1)
        self.assertIn("pr_review_comment", out)

    def test_new_review_in_same_second_re_alerts(self):
        # regression: a brand-new review posted in the SAME 1s-resolution timestamp as a seen comment
        # must still re-alert (the latest-signal tie-break must advance the dedupe key).
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        T = "2026-01-03T00:00:00Z"
        _, out1, _ = self.run_watch(prs, comments_by_pr={1: [codex_comment(T, "p1#c1")]})
        self.assertActionable(out1, 1)
        _, out2, _ = self.run_watch(
            prs, comments_by_pr={1: [codex_comment(T, "p1#c1")]},
            reviews_by_pr={1: [codex_review(T, "p1#r1", body="Blocking: must fix",
                                            state="CHANGES_REQUESTED")]})
        self.assertActionable(out2, 1)        # new same-second review is not swallowed
        self.assertIn("blocking=True", out2)

    def test_quota_notice_emits_quota_marker_not_actionable(self):
        # a bare Codex usage-limits notice must NOT be flagged as actionable -> CODEX_QUOTA_LIMITED
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        rc, out, _ = self.run_watch(prs, comments_by_pr={1: [codex_quota("2026-01-03T00:00:00Z", "p1#q1")]})
        self.assertEqual(rc, 0)
        self.assertQuota(out)
        self.assertEqual(out.splitlines()[0], "CODEX_QUOTA_LIMITED")   # strict-consumer marker first
        self.assertIn("rate-limited", out)
        self.assertIn("quota_limited=1", out)

    def test_stale_quota_notice_flagged_only_once(self):
        # Codex r9 P2: a PERSISTENT quota notice must emit CODEX_QUOTA_LIMITED only once — else a
        # scheduler backing off on the marker never returns to normal polling after the limit resets
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        quota = {1: [codex_quota("2026-01-04T00:00:00Z", "p1#q1")]}
        _, out1, _ = self.run_watch(prs, comments_by_pr=quota)
        self.assertIn("CODEX_QUOTA_LIMITED", out1)        # first time: flagged
        _, out2, _ = self.run_watch(prs, comments_by_pr=quota)
        self.assertNotIn("CODEX_QUOTA_LIMITED", out2)     # same stale notice -> not re-flagged
        self.assertNoNew(out2)

    def test_real_review_with_newer_quota_is_still_actionable(self):
        # a genuine review followed by a NEWER quota notice must not be hidden -> still actionable,
        # and quota is still signalled so a scheduler knows Codex is now rate-limited.
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(
            prs,
            reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
            comments_by_pr={1: [codex_quota("2026-01-04T00:00:00Z", "p1#q1")]})
        self.assertActionable(out, 1)
        self.assertIn("rate-limited", out)

    def test_quota_then_real_review_becomes_actionable(self):
        # the quota notice is never recorded as 'seen', so a later real review is detected as new
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out1, _ = self.run_watch(prs, comments_by_pr={1: [codex_quota("2026-01-03T00:00:00Z", "p1#q1")]})
        self.assertQuota(out1)
        _, out2, _ = self.run_watch(
            prs,
            comments_by_pr={1: [codex_quota("2026-01-03T00:00:00Z", "p1#q1")]},
            reviews_by_pr={1: [codex_review("2026-01-05T00:00:00Z", "p1#r1")]})
        self.assertActionable(out2, 1)

    def test_spoofy_quota_author_ignored(self):
        # a non-trusted login posting the quota text is ignored entirely (not even a quota signal)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(
            prs, comments_by_pr={1: [codex_quota("2026-01-03T00:00:00Z", "p1#q1", login="codex-fan")]})
        self.assertNoNew(out)

    def test_json_includes_quota_limited(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, out, _ = self.run_watch(prs, comments_by_pr={1: [codex_quota("2026-01-03T00:00:00Z", "p1#q1")]},
                                   as_json=True)
        lines = out.splitlines()
        start = max(i for i, ln in enumerate(lines) if ln == "{")
        data = json.loads("\n".join(lines[start:]))
        self.assertEqual(data["marker"], "CODEX_QUOTA_LIMITED")
        self.assertEqual(data["quota_limited"], [1])
        self.assertEqual(data["actionable"], [])

    def test_corrupt_nested_seen_file_degrades_without_crashing(self):
        # spec: a corrupt seen file degrades to {} (never crashes) — including PARTIAL/legacy slices
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        reviews = {1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]}
        for corrupt in ({"o/r": None}, {"o/r": "garbage"}, {"o/r": ["a", "b"]},
                        {"o/r": {"1": "legacystring"}}, ["not", "a", "dict"]):
            with open(self.seen, "w", encoding="utf-8") as f:
                json.dump(corrupt, f)
            rc, out, _ = self.run_watch(prs, reviews_by_pr=reviews)
            self.assertEqual(rc, 0, corrupt)
            self.assertActionable(out, 1)     # corrupt slice treated as unseen -> new

    def test_limit_is_forwarded_and_clamped(self):
        def limit_of(rec):
            for cmd in rec:
                a = cmd[1:]
                if a[:2] == ["pr", "list"] and "--limit" in a:
                    return a[a.index("--limit") + 1]
            return None
        _, _, rec5 = self.run_watch([], limit=5)
        _, _, rec0 = self.run_watch([], limit=0)   # SimpleNamespace bypasses argparse -> tests the clamp
        self.assertEqual(limit_of(rec5), "5")
        self.assertEqual(limit_of(rec0), "1")      # gh pr list --limit 0 is invalid -> clamped to 1

    def test_reset_preserves_other_repos_in_shared_seen_file(self):
        # prime an unrelated repo's slice in the SAME seen file, then --reset for o/r
        with open(self.seen, "w", encoding="utf-8") as f:
            json.dump({"other/repo": {"9": {"key": "k", "created_at": "t", "url": "u"}}}, f)
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]},
                       reset=True)
        with open(self.seen, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("other/repo", saved)   # untouched by reset of o/r
        self.assertIn("o/r", saved)


class TestWatchCodexReadOnly(WatchCodexBase):

    def test_only_read_only_gh_commands_are_spawned(self):
        prs = [{"number": 1, "title": "T", "updatedAt": "z", "url": "p1"}]
        _, _, recorder = self.run_watch(prs, reviews_by_pr={1: [codex_review("2026-01-03T00:00:00Z", "p1#r1")]})
        self.assertTrue(recorder)  # we did call gh
        for cmd in recorder:
            args = cmd[1:]
            verb = args[0] if args else ""
            self.assertNotIn(verb, _WRITE_TOKENS, f"unexpected write verb in {args}")
            # only allow-listed read shapes
            self.assertIn(verb, {"auth", "repo", "pr", "api"})
            if verb == "api":  # never a write method/field
                self.assertNotIn("-f", args)
                self.assertNotIn("--field", args)
                self.assertFalse(any(a.startswith("-X") or a == "--method" for a in args))
            # the read-only guard must accept every command we actually spawned
            G._assert_read_only(args)

    def test_writes_are_structurally_impossible(self):
        # the watcher must construct no writer; assert it never references the write layer
        import devflow.cli as climod
        self.assertFalse(hasattr(climod, "GitHubWriter"))
        self.assertNotIn("GitHubWriter", climod.cmd_watch_codex_reviews.__code__.co_names)
        self.assertNotIn("comment_on_pr", climod.cmd_watch_codex_reviews.__code__.co_names)

    def test_parser_rejects_limit_below_one(self):
        from devflow.cli import build_parser
        for bad in ("0", "-1"):
            with self.assertRaises(SystemExit):   # argparse error -> exit (matches the ">= 1" help)
                build_parser().parse_args(["watch-codex-reviews", "--repo", "o/r", "--limit", bad])


if __name__ == "__main__":
    unittest.main()
