import json
import os
import subprocess
import sys
import tempfile
import textwrap
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
READER = FIXTURES.parents[1] / "src" / "firstmate" / "pr_reviews.py"
HEAD = "a" * 40
OLD_HEAD = "b" * 40
DIFF_REFS = {"base_sha": "c" * 40, "start_sha": "d" * 40, "head_sha": HEAD}


def github_fixture():
    page = json.loads((FIXTURES / "github-review-state.json").read_text(encoding="utf-8"))
    page["data"]["repository"]["pullRequest"]["headRefOid"] = HEAD
    return page


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

    def test_verdicts_at_a_different_head_refuse_without_a_supplied_head(self):
        reviews = github_fixture()
        reviews["data"]["repository"]["pullRequest"]["headRefOid"] = OLD_HEAD
        with self.assertRaisesRegex(ReviewReadError, "head changed"):
            github_review_state([github_fixture()], [reviews])


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


class ReviewReaderTest(unittest.TestCase):
    def run_reader(self, cli, response, forge, *options, **env):
        with tempfile.TemporaryDirectory(prefix="fm-review-reader-") as temp:
            command = Path(temp) / cli
            command.write_text(f"#!{sys.executable}\n{textwrap.dedent(response)}", encoding="utf-8")
            command.chmod(0o700)
            return subprocess.run(
                [
                    sys.executable, "-I", str(READER), forge, "review.example", "org/repo", "7",
                    *options,
                ],
                env=os.environ | env | {"PATH": temp},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    def test_gitlab_preserves_the_operator_credential_host(self):
        result = self.run_reader(
            "glab",
            """
            import os
            import sys

            if os.environ.get("GITLAB_HOST") != "operator.example":
                sys.exit(1)
            print("[]" if "?" in sys.argv[-1] else '{"sha":"current"}')
            """,
            "gitlab",
            GITLAB_HOST="operator.example",
            GITLAB_TOKEN="test-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0 0\n")

    def test_gitlab_refuses_if_discussions_span_a_push(self):
        result = self.run_reader(
            "glab",
            """
            import json
            import sys
            from pathlib import Path

            moved = Path(sys.argv[0]).with_name("head-moved")
            if "?" in sys.argv[-1]:
                moved.touch()
                print("[]")
            else:
                print(json.dumps({"sha": "new" if moved.exists() else "old"}))
            """,
            "gitlab",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("head changed", result.stderr)

    def test_forgejo_checks_the_advertised_total_and_verified_head(self):
        response = """
            import json
            import os
            import sys

            endpoint = sys.argv[-1]
            if "page=" not in endpoint:
                print(json.dumps({"head": {"sha": "current"}}))
            else:
                print("X-Total-Count: 51", file=sys.stderr)
                if endpoint.endswith("page=1"):
                    print(json.dumps([
                        {"id": i, "state": "COMMENT", "comments_count": 0}
                        for i in range(1, 51)
                    ]))
                elif os.environ["INCOMPLETE"] == "yes":
                    print("[]")
                else:
                    print(json.dumps([{
                        "id": 51, "state": "REQUEST_CHANGES", "comments_count": 0,
                        "dismissed": False, "user": {"login": "reviewer"},
                        "submitted_at": "2026-01-01T00:00:00Z",
                    }]))
        """
        for head, incomplete, code, expected in (
            ("current", "no", 0, "0 1\n"),
            ("current", "yes", 1, "incomplete Forgejo review list"),
            ("old", "no", 1, "head changed"),
        ):
            with self.subTest(head=head, incomplete=incomplete):
                result = self.run_reader(
                    "tea", response, "forgejo", "--head", head, INCOMPLETE=incomplete
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(expected, result.stdout if code == 0 else result.stderr)
