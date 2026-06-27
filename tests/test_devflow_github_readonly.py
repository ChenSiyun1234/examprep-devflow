# -*- coding: utf-8 -*-
"""Tests for devflow's read-only GitHub layer. All `gh` calls are mocked — no network, no writes.

    python -m unittest tests.test_devflow_github_readonly
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from devflow.tools import github_cli as G
from devflow.tools.github_cli import (
    ReadOnlyGitHub, GhError, check_gh_available, _assert_read_only, is_codex_author,
)

ISSUE_COMMENTS = [
    {"user": {"login": "alice"}, "body": "looks good, thanks!",
     "created_at": "2026-01-01T00:00:00Z", "html_url": "u1"},
    {"user": {"login": "chatgpt-codex-connector[bot]"},
     "body": "## Advisory\n- scope to a dry-run scaffold\n- add tests + docs",
     "created_at": "2026-01-02T00:00:00Z", "html_url": "u2"},
]
PR_REVIEWS = [
    {"user": {"login": "codex"}, "state": "CHANGES_REQUESTED",
     "body": "Blocking issues:\n- must fix null handling\n* required change: add a test",
     "submitted_at": "2026-01-03T00:00:00Z", "html_url": "r1"},
    {"user": {"login": "bob"}, "state": "COMMENTED", "body": "nit",
     "submitted_at": "2026-01-02T00:00:00Z", "html_url": "r2"},
]


def fake_run_factory(recorder, *, auth_ok=True):
    def fake_run(cmd, **kw):
        recorder.append(cmd)
        args = cmd[1:]  # drop "gh"
        if args[:2] == ["auth", "status"]:
            return SimpleNamespace(returncode=0 if auth_ok else 1,
                                   stdout="Logged in to github.com account TESTER" if auth_ok else "",
                                   stderr="" if auth_ok else "You are not logged into any GitHub hosts.")
        if args[:2] == ["repo", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"nameWithOwner": "o/r"}), stderr="")
        if args and args[0] == "api":
            # path is the repos/... token (skip flags like --hostname github.com --paginate --slurp)
            path = next((a for a in args[1:] if a.startswith("repos/")), "")
            if "/reviews" in path:
                return SimpleNamespace(returncode=0, stdout=json.dumps(PR_REVIEWS), stderr="")
            if "/comments" in path:
                return SimpleNamespace(returncode=0, stdout=json.dumps(ISSUE_COMMENTS), stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected command")
    return fake_run


class TestReadOnlyGuard(unittest.TestCase):

    def test_refuses_write_shapes(self):
        for bad in (["pr", "merge", "1"], ["issue", "create", "-t", "x"],
                    ["pr", "comment", "1", "-b", "hi"], ["pr", "create"],
                    ["api", "repos/o/r/issues/1/comments", "-f", "body=hi"],
                    ["api", "repos/o/r/pulls/1/merge", "-X", "PUT"],
                    ["api", "x", "--method=POST"],
                    ["api", "x", "--field=body=hi"],        # equals-form write flag
                    ["api", "x", "--raw-field=b=c"],
                    ["api", "x", "-X=DELETE"],
                    ["api", "x", "-XPUT"],                  # compact attached method
                    ["api", "x", "-XPOST"],
                    ["api", "x", "-fbody=hi"],              # compact attached field
                    ["api", "x", "-Ffoo=bar"]):
            with self.assertRaises(GhError):
                _assert_read_only(bad)

    def test_allows_read_shapes(self):
        for ok in (["auth", "status"], ["repo", "view", "--json", "name"],
                   ["issue", "view", "1"], ["pr", "view", "1"],
                   ["api", "repos/o/r/pulls/1/reviews", "--paginate"]):
            _assert_read_only(ok)  # must not raise

    def test_guard_runs_before_subprocess(self):
        calls = []
        with mock.patch.object(G.shutil, "which", return_value="gh"), \
             mock.patch.object(G.subprocess, "run", side_effect=fake_run_factory(calls)):
            with self.assertRaises(GhError):
                G._run_gh(["pr", "merge", "1"])   # write shape
        self.assertEqual(calls, [])  # no subprocess was ever spawned


class TestReadOnlyOps(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self.p_which = mock.patch.object(G.shutil, "which", return_value="gh")
        self.p_run = mock.patch.object(G.subprocess, "run",
                                       side_effect=fake_run_factory(self.calls))
        self.p_which.start()
        self.p_run.start()
        self.addCleanup(self.p_which.stop)
        self.addCleanup(self.p_run.stop)

    def test_api_reads_pinned_to_github_host(self):
        ReadOnlyGitHub("o/r").get_pr_comments(2)
        api_calls = [c for c in self.calls if c[1:2] == ["api"]]
        self.assertTrue(api_calls)
        for c in api_calls:
            self.assertIn("--hostname", c)
            self.assertIn("github.com", c)

    def test_no_write_commands_are_called(self):
        gh = ReadOnlyGitHub("o/r")
        gh.get_issue_comments(1)
        gh.get_pr_comments(2)
        gh.get_pr_reviews(2)
        gh.find_latest_codex_advisory(1)
        gh.find_latest_codex_review(2)
        write_tokens = {"create", "comment", "merge", "edit", "close", "delete", "review", "push"}
        for cmd in self.calls:
            args = cmd[1:]
            self.assertNotIn(args[0] if args else "", write_tokens)
            # `gh api` must never carry a write method/field
            if args and args[0] == "api":
                self.assertNotIn("-f", args)
                self.assertNotIn("--method=POST", args)
        self.assertTrue(self.calls)  # sanity: we did call gh (reads)

    def test_detect_codex_advisory_from_comments(self):
        adv = ReadOnlyGitHub("o/r").find_latest_codex_advisory(1)
        self.assertIsNotNone(adv)
        self.assertEqual(adv["author"], "chatgpt-codex-connector[bot]")
        self.assertIn("Advisory", adv["body"])
        self.assertEqual(adv["source"], "issue_comment")

    def test_parse_codex_review_from_reviews(self):
        rev = ReadOnlyGitHub("o/r").find_latest_codex_review(2)
        self.assertIsNotNone(rev)
        self.assertEqual(rev["author"], "codex")
        self.assertTrue(rev["blocking"])               # CHANGES_REQUESTED
        self.assertGreaterEqual(len(rev["items"]), 2)  # bullet lines parsed
        self.assertEqual(rev["source"], "pr_review")

    def test_no_codex_returns_none(self):
        # comments/reviews with only non-codex authors -> None
        with mock.patch.object(ReadOnlyGitHub, "get_issue_comments",
                               return_value=[{"author": "alice", "body": "hi",
                                              "created_at": "z", "url": "u"}]):
            self.assertIsNone(ReadOnlyGitHub("o/r").find_latest_codex_advisory(1))


class TestAvailability(unittest.TestCase):

    def test_gh_missing_is_clear(self):
        with mock.patch.object(G.shutil, "which", return_value=None):
            st = check_gh_available()
        self.assertFalse(st["available"])
        self.assertFalse(st["authenticated"])
        self.assertIn("not found", st["error"].lower())

    def test_gh_unauthenticated_is_clear(self):
        calls = []
        with mock.patch.object(G.shutil, "which", return_value="gh"), \
             mock.patch.object(G.subprocess, "run",
                               side_effect=fake_run_factory(calls, auth_ok=False)):
            st = check_gh_available()
        self.assertTrue(st["available"])
        self.assertFalse(st["authenticated"])
        self.assertIn("not authenticated", st["error"].lower())

    def test_gh_available_and_authed(self):
        calls = []
        with mock.patch.object(G.shutil, "which", return_value="gh"), \
             mock.patch.object(G.subprocess, "run", side_effect=fake_run_factory(calls)):
            st = check_gh_available()
        self.assertTrue(st["authenticated"])
        self.assertEqual(st["account"], "TESTER")

    def test_auth_check_is_scoped_to_github_host(self):
        calls = []
        with mock.patch.object(G.shutil, "which", return_value="gh"), \
             mock.patch.object(G.subprocess, "run", side_effect=fake_run_factory(calls)):
            check_gh_available()
        auth_calls = [c for c in calls if c[1:3] == ["auth", "status"]]
        self.assertTrue(auth_calls)
        self.assertIn("github.com", auth_calls[0])  # scoped to the github.com host


class TestCodexAuthorMatch(unittest.TestCase):
    def test_matches_exact_trusted(self):
        for login in ("codex", "Codex", "chatgpt-codex-connector[bot]",
                      "CHATGPT-CODEX-CONNECTOR[bot]", "codex[bot]"):
            self.assertTrue(is_codex_author(login))

    def test_rejects_spoofy_and_loose(self):
        # loose "contains codex/chatgpt" logins must NOT match (anti-spoof on public repos)
        for login in ("alice", "bob", "ZeKaiNie", None, "",
                      "OpenAI-Codex", "ChatGPT", "codex-fan", "not-codex", "chatgptbot"):
            self.assertFalse(is_codex_author(login))


class TestParsingHardening(unittest.TestCase):
    def test_paginate_flatten_multipage(self):
        # gh api --paginate --slurp returns [[page1...],[page2...]] -> flatten one level
        self.assertEqual(G._flatten_pages([[1, 2], [3]]), [1, 2, 3])
        self.assertEqual(G._flatten_pages([{"a": 1}]), [{"a": 1}])  # already flat
        self.assertEqual(G._flatten_pages(None), [])

    def test_non_blocking_not_flagged(self):
        pkt = G.parse_review_packet("These are all non-blocking nits.", state="COMMENTED")
        self.assertFalse(pkt["blocking"])

    def test_blocking_word_flagged(self):
        self.assertTrue(G.parse_review_packet("This is a blocking issue.")["blocking"])
        self.assertTrue(G.parse_review_packet("you must fix this")["blocking"])

    def test_negated_blocking_phrases_not_flagged(self):
        for txt in ("No blocking issues found.", "There are not blocking concerns here.",
                    "I do not request changes.", "No required changes.", "No changes requested.",
                    "This is not a blocking issue.", "That is not a required change.",
                    "No major blocking concerns."):
            self.assertFalse(G.parse_review_packet(txt, state="COMMENTED")["blocking"], txt)

    def test_newer_blocking_comment_survives_older_approval(self):
        gh = ReadOnlyGitHub("o/r")
        reviews = [{"author": "codex", "body": "lgtm", "state": "APPROVED",
                    "created_at": "2026-01-01T00:00:00Z", "url": "r1"}]
        newer_comment = [{"author": "codex", "body": "Actually blocking: must fix X",
                          "created_at": "2026-01-09T00:00:00Z", "url": "c9"}]
        with mock.patch.object(gh, "get_pr_comments", return_value=newer_comment), \
             mock.patch.object(gh, "get_pr_review_comments", return_value=[]), \
             mock.patch.object(gh, "get_pr_reviews", return_value=reviews):
            rev = gh.find_latest_codex_review(2)
        self.assertTrue(rev["blocking"])  # newer blocking comment not hidden by older approval

    def test_changes_requested_then_approved_clears_blocking(self):
        gh = ReadOnlyGitHub("o/r")
        reviews = [
            {"author": "codex", "body": "fix", "state": "CHANGES_REQUESTED",
             "created_at": "2026-01-01T00:00:00Z", "url": "r1"},
            {"author": "codex", "body": "lgtm", "state": "APPROVED",
             "created_at": "2026-01-05T00:00:00Z", "url": "r2"},  # newer approval
        ]
        with mock.patch.object(gh, "get_pr_comments", return_value=[]), \
             mock.patch.object(gh, "get_pr_review_comments", return_value=[]), \
             mock.patch.object(gh, "get_pr_reviews", return_value=reviews):
            rev = gh.find_latest_codex_review(2)
        self.assertFalse(rev["blocking"])  # newer APPROVED clears the prior CHANGES_REQUESTED

    def test_changes_requested_stays_blocking_if_newest(self):
        gh = ReadOnlyGitHub("o/r")
        reviews = [
            {"author": "codex", "body": "lgtm", "state": "APPROVED",
             "created_at": "2026-01-01T00:00:00Z", "url": "r1"},
            {"author": "codex", "body": "fix", "state": "CHANGES_REQUESTED",
             "created_at": "2026-01-05T00:00:00Z", "url": "r2"},  # newer changes-requested
        ]
        with mock.patch.object(gh, "get_pr_comments", return_value=[]), \
             mock.patch.object(gh, "get_pr_review_comments", return_value=[]), \
             mock.patch.object(gh, "get_pr_reviews", return_value=reviews):
            rev = gh.find_latest_codex_review(2)
        self.assertTrue(rev["blocking"])

    def test_changes_requested_preserved_when_newer_comment(self):
        reviews = [
            {"user": {"login": "codex"}, "state": "CHANGES_REQUESTED", "body": "fix",
             "submitted_at": "2026-01-01T00:00:00Z", "html_url": "r1"},
        ]
        later_comment = [{"user": {"login": "codex"}, "body": "thanks, looks better",
                          "created_at": "2026-01-09T00:00:00Z", "html_url": "c9"}]
        gh = ReadOnlyGitHub("o/r")
        with mock.patch.object(gh, "get_pr_comments", return_value=G.ReadOnlyGitHub._norm_comments(later_comment)), \
             mock.patch.object(gh, "get_pr_review_comments", return_value=[]), \
             mock.patch.object(gh, "get_pr_reviews",
                               return_value=[{"author": "codex", "body": "fix",
                                              "state": "CHANGES_REQUESTED",
                                              "created_at": "2026-01-01T00:00:00Z", "url": "r1"}]):
            rev = gh.find_latest_codex_review(2)
        self.assertTrue(rev["blocking"])  # preserved despite newer non-blocking comment


if __name__ == "__main__":
    unittest.main()
