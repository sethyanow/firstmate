import json
import unittest
from pathlib import Path

from firstmate.pr_reviews import (
    ReviewReadError,
    ReviewState,
    forgejo_review_state,
    github_review_state,
    gitlab_review_state,
)

FIXTURES = Path(__file__).with_name("fixtures")
HEAD = "a" * 40
OLD_HEAD = "b" * 40
DIFF_REFS = {"base_sha": "c" * 40, "start_sha": "d" * 40, "head_sha": HEAD}


def github_fixture():
    return json.loads((FIXTURES / "github-review-state.json").read_text(encoding="utf-8"))


class GitHubReviewsTest(unittest.TestCase):
    def test_recorded_reply_has_one_current_open_thread(self):
        page = github_fixture()
        self.assertEqual(github_review_state([page], [page]), ReviewState(1, 0))

    def test_latest_review_verdict_ignores_comments(self):
        page = github_fixture()
        pr = page["data"]["repository"]["pullRequest"]
        pr["reviews"]["nodes"] = [
            {
                "state": state,
                "author": {"login": login},
                "submittedAt": f"2026-01-{day:02d}T00:00:00Z",
            }
            for login, day, state in [
                ("review-bot[bot]", 1, "CHANGES_REQUESTED"),
                ("review-bot[bot]", 3, "COMMENTED"),
                ("reviewer", 1, "CHANGES_REQUESTED"),
                ("reviewer", 2, "APPROVED"),
            ]
        ]
        pr["reviews"]["totalCount"] = 4
        self.assertEqual(github_review_state([page], [page]), ReviewState(1, 1))

    def test_open_outdated_thread_does_not_hold_merge(self):
        page = github_fixture()
        threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        threads[-1]["isOutdated"] = True
        self.assertFalse(github_review_state([page], [page]).blocked)

    def test_incomplete_thread_pages_refuse(self):
        page = github_fixture()
        page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]["hasNextPage"] = (
            True
        )
        with self.assertRaises(ReviewReadError):
            github_review_state([page], [page])


class GitLabReviewsTest(unittest.TestCase):
    def test_counts_diff_discussions_and_current_reviewer_verdicts(self):
        # Discussions API: a reply shares the first DiffNote's discussion.
        def discussion(*notes):
            return {"individual_note": False, "notes": list(notes)}

        def diff_note(head, resolved):
            return {
                "type": "DiffNote",
                "resolvable": True,
                "resolved": resolved,
                "position": {
                    **DIFF_REFS,
                    "position_type": "text",
                    "head_sha": head,
                    "new_line": 14,
                },
            }

        conversation = {"type": "DiscussionNote", "resolvable": True, "resolved": False}
        discussions = [
            discussion(diff_note(HEAD, False), conversation),
            discussion(diff_note(HEAD, True)),
            discussion(diff_note(OLD_HEAD, False)),
            discussion(conversation),
            {"individual_note": True, "notes": [{"type": None, "resolvable": False}]},
        ]
        reviewers = [
            {"user": {"id": 1, "username": "reviewer"}, "state": "requested_changes"},
            {"user": {"id": 2, "username": "review-bot"}, "state": "approved"},
        ]
        self.assertEqual(gitlab_review_state(discussions, reviewers, DIFF_REFS), ReviewState(1, 1))


class ForgejoReviewsTest(unittest.TestCase):
    def test_recorded_empty_review_list_is_clear(self):
        reviews = json.loads((FIXTURES / "forgejo-reviews.json").read_text(encoding="utf-8"))
        self.assertEqual(forgejo_review_state(reviews, {}), ReviewState(0, 0))

    def test_resolver_on_first_comment_settles_the_conversation(self):
        # The live fixture has no reviews. These rows use Forgejo's published
        # PullReview and PullReviewComment response shapes.
        reviews = [
            {
                "id": 1,
                "state": "REQUEST_CHANGES",
                "user": {"id": 10, "login": "reviewer"},
                "dismissed": False,
                "submitted_at": "2026-01-01T00:00:00Z",
                "comments_count": 3,
            }
        ]
        comments = {
            1: [
                {
                    "id": 3,
                    "path": "example.txt",
                    "position": 15,
                    "original_position": 0,
                    "resolver": None,
                },
                {
                    "id": 1,
                    "path": "example.txt",
                    "position": 15,
                    "original_position": 0,
                    "resolver": {"id": 11, "login": "maintainer"},
                },
                {
                    "id": 2,
                    "path": "example.txt",
                    "position": 0,
                    "original_position": 15,
                    "resolver": None,
                },
            ]
        }
        self.assertEqual(forgejo_review_state(reviews, comments), ReviewState(1, 1))

    def test_missing_comment_resolution_is_unreadable(self):
        review = {"id": 1, "state": "COMMENT", "comments_count": 1}
        with self.assertRaises(KeyError):
            forgejo_review_state(
                [review], {1: [{"id": 1, "path": "file", "position": 2, "original_position": 0}]}
            )
