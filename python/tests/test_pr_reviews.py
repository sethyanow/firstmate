import json
import os
import shutil
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
        self.assertEqual(github_review_state([page], [page]), ReviewState(0, 0))

    def test_open_whole_file_thread_does_not_hold_merge(self):
        page = github_fixture()
        threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        threads[-1]["subjectType"] = "FILE"
        self.assertEqual(github_review_state([page], [page]), ReviewState(0, 0))

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
    def test_counts_diff_discussions_and_active_change_requesters(self):
        def discussion(*notes, resolved=False):
            return {
                "individual_note": False,
                "resolvable": True,
                "resolved": resolved,
                "notes": list(notes),
            }

        def diff_note(resolved, kind="text"):
            return {
                "type": "DiffNote",
                "resolvable": True,
                "resolved": resolved,
                "position": {
                    **DIFF_REFS,
                    "position_type": kind,
                    "new_line": 14,
                },
            }

        conversation = {"type": "DiscussionNote", "resolvable": True, "resolved": False}
        discussions = [
            discussion(diff_note(True), conversation),
            discussion(diff_note(True), resolved=True),
            discussion(diff_note(False, "file")),
            discussion(conversation),
            {"individual_note": True, "notes": [{"type": None, "resolvable": False}]},
        ]
        requesters = {"nodes": [{"id": "gid://gitlab/User/1"}], "pageInfo": {"hasNextPage": False}}
        page = {"data": {"project": {"mergeRequest": {
            "diffHeadSha": HEAD, "changeRequesters": requesters,
        }}}}
        self.assertEqual(
            gitlab_review_state(discussions, [page], HEAD, DIFF_REFS), ReviewState(1, 1)
        )

    def test_excludes_an_outdated_discussion_and_refuses_an_incomplete_position(self):
        position: dict[str, object] = {
            **DIFF_REFS,
            "head_sha": OLD_HEAD,
            "position_type": "text",
            "new_line": 7,
        }
        discussion = {
            "resolvable": True,
            "resolved": False,
            "notes": [{"type": "DiffNote", "position": position}],
        }
        self.assertEqual(gitlab_review_state([discussion], None, HEAD, DIFF_REFS), ReviewState(0, 0))
        position["head_sha"] = None
        with self.assertRaisesRegex(ReviewReadError, "invalid head_sha"):
            gitlab_review_state([discussion], None, HEAD, DIFF_REFS)


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
                "comments_count": 4,
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
                    "resolver": {"id": -1, "login": "ghost"},
                },
                {
                    "id": 2,
                    "path": "example.txt",
                    "position": 0,
                    "original_position": 15,
                    "resolver": None,
                },
                {
                    "id": 4,
                    "path": "example.txt",
                    "position": 0,
                    "original_position": 0,
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
            reply = Path(temp) / "reply.py"
            reply.write_text(textwrap.dedent(response), encoding="utf-8")
            command.write_text(
                '#!/bin/bash\nexec "$FM_STUB_PYTHON" "$FM_STUB_REPLY" "$@"\n',
                encoding="utf-8",
                newline="\n",
            )
            command.chmod(0o700)
            bash = shutil.which("bash")
            search_path = [temp, str(Path(bash).parent) if bash else os.defpath, os.defpath]
            if "SystemRoot" in os.environ:
                env["SystemRoot"] = os.environ["SystemRoot"]
            return subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(READER),
                    forge,
                    "review.example",
                    "org/repo",
                    "7",
                    *options,
                ],
                env=env | {
                    "PATH": os.pathsep.join(search_path),
                    "FM_STUB_PYTHON": sys.executable,
                    "FM_STUB_REPLY": str(reply),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    def test_gitlab_routes_credentials_only_to_the_operator_host(self):
        response = """
            import json
            import os
            import sys

            trusted = os.environ["EXPECT_TOKEN"] == "yes"
            for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"):
                if (os.environ.get(name) == "stub-token") != trusted:
                    sys.exit(1)
            ci_trusted = os.environ["EXPECT_CI_TOKEN"] == "yes"
            if (os.environ.get("CI_JOB_TOKEN") == "stub-token") != ci_trusted:
                sys.exit(1)
            if os.environ.get("GITLAB_HOST") != "review.example" or "GITLAB_REPO" in os.environ:
                sys.exit(1)
            if sys.argv[-1] == "metadata":
                print('{"enterprise":true}')
            elif "graphql" in sys.argv:
                print(json.dumps({"data": {"project": {"mergeRequest": {
                    "diffHeadSha": "current", "changeRequesters": None,
                }}}}))
            else:
                print("[]" if "?" in sys.argv[-1] else '{"sha":"current"}')
        """
        for host, repo, expected in (
            ("operator.example", "", "no"),
            ("review.example", "", "yes"),
            ("", "https://review.example/team/project", "yes"),
            ("", "https://review.example.other/team/project", "no"),
            ("operator.example", "https://review.example/team/project", "no"),
        ):
            with self.subTest(host=host, repo=repo):
                result = self.run_reader(
                    "glab",
                    response,
                    "gitlab",
                    GITLAB_HOST=host,
                    GITLAB_REPO=repo,
                    GITLAB_TOKEN="stub-token",
                    GITLAB_ACCESS_TOKEN="stub-token",
                    OAUTH_TOKEN="stub-token",
                    CI_SERVER_FQDN=host,
                    CI_JOB_TOKEN="stub-token",
                    EXPECT_TOKEN=expected,
                    EXPECT_CI_TOKEN="yes" if host == "review.example" else "no",
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
            if sys.argv[-1] == "metadata":
                print('{"enterprise":true}')
            elif "graphql" in sys.argv:
                print(json.dumps({"data": {"project": {"mergeRequest": {
                    "diffHeadSha": "new", "changeRequesters": None,
                }}}}))
            elif "?" in sys.argv[-1]:
                moved.touch()
                print("[]")
            else:
                print(json.dumps({"sha": "new" if moved.exists() else "old"}))
            """,
            "gitlab",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("head changed", result.stderr)

    def test_gitlab_skips_change_requesters_only_for_confirmed_community_edition(self):
        response = """
            import os
            import sys

            if "graphql" in sys.argv:
                sys.exit(1)
            if sys.argv[-1] == "metadata":
                print(os.environ["STUB_METADATA"])
            else:
                print("[]" if "?" in sys.argv[-1] else '{"sha":"current"}')
        """
        for metadata, code in (
            ('{"enterprise":false}', 0),
            ('{"enterprise":true}', 1),
            ('{"enterprise":"false"}', 1),
        ):
            with self.subTest(metadata=metadata):
                result = self.run_reader("glab", response, "gitlab", STUB_METADATA=metadata)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, "0 0\n" if code == 0 else "")

    @unittest.skipIf(os.name == "nt", "The tea subprocess stub uses a Unix executable script")
    def test_forgejo_reads_past_a_hidden_draft_page(self):
        response = """
            import json
            import sys

            endpoint = sys.argv[-1]
            if endpoint == "/settings/api":
                print('{"max_response_items": 25}')
            elif endpoint.endswith("/comments"):
                print(json.dumps([{
                    "id": 1, "path": "demo.txt", "position": 4,
                    "original_position": 0, "resolver": None,
                }]))
            elif "page=" not in endpoint:
                print(json.dumps({"head": {"sha": "current"}}))
            else:
                print("X-Total-Count: 51", file=sys.stderr)
                if endpoint.endswith("page=1"):
                    print(json.dumps([
                        {"id": i, "state": "COMMENT", "comments_count": 0}
                        for i in range(1, 26)
                    ]))
                elif endpoint.endswith("page=2"):
                    print("[]")
                else:
                    print(json.dumps([{
                        "id": 51, "state": "REQUEST_CHANGES", "comments_count": 1,
                        "dismissed": False, "user": {"login": "reviewer"},
                        "submitted_at": "2026-01-01T00:00:00Z",
                    }]))
        """
        for head, code, expected in (
            ("current", 0, "1 1\n"),
            ("old", 1, "head changed"),
        ):
            with self.subTest(head=head):
                result = self.run_reader("tea", response, "forgejo", "--head", head)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(expected, result.stdout if code == 0 else result.stderr)
