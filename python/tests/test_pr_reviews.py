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


def github_fixture():
    page = json.loads((FIXTURES / "github-review-state.json").read_text(encoding="utf-8"))
    page["data"]["repository"]["pullRequest"]["headRefOid"] = HEAD
    return page


class GitHubReviewsTest(unittest.TestCase):
    def test_recorded_reply_has_one_current_open_thread(self):
        page = github_fixture()
        self.assertEqual(github_review_state(page), ReviewState(1, 0))

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
        self.assertEqual(github_review_state(page), ReviewState(1, 1))

    def test_open_outdated_thread_does_not_hold_merge(self):
        page = github_fixture()
        threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        threads[-1]["isOutdated"] = True
        self.assertEqual(github_review_state(page), ReviewState(0, 0))

    def test_open_whole_file_thread_does_not_hold_merge(self):
        page = github_fixture()
        threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        threads[-1]["subjectType"] = "FILE"
        self.assertEqual(github_review_state(page), ReviewState(0, 0))

    def test_incomplete_thread_pages_refuse(self):
        page = github_fixture()
        page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]["hasNextPage"] = (
            True
        )
        with self.assertRaises(ReviewReadError):
            github_review_state(page)

    def test_verdict_continuation_at_a_different_head_refuses(self):
        first = github_fixture()
        first["data"]["repository"]["pullRequest"]["reviews"]["pageInfo"]["hasNextPage"] = True
        reviews = github_fixture()
        reviews["data"]["repository"]["pullRequest"]["headRefOid"] = OLD_HEAD
        with self.assertRaisesRegex(ReviewReadError, "head changed"):
            github_review_state(first, review_pages=[reviews])


def gitlab_fixture():
    # Built from the two example responses in GitLab 18.9's discussions API
    # documentation (doc/api/discussions.md), not recorded from a live instance.
    pages = json.loads((FIXTURES / "gitlab-discussions.json").read_text(encoding="utf-8"))
    return [discussion for page in pages for discussion in page]


def gitlab_diff_notes(discussions):
    return next(d["notes"] for d in discussions if d["notes"][0]["type"] == "DiffNote")


class GitLabReviewsTest(unittest.TestCase):
    def test_documented_reply_counts_only_the_open_diff_thread(self):
        self.assertEqual(gitlab_review_state(gitlab_fixture()), ReviewState(1, 0))

    def test_resolved_diff_thread_does_not_hold_merge(self):
        discussions = gitlab_fixture()
        gitlab_diff_notes(discussions)[0]["resolved"] = True
        self.assertEqual(gitlab_review_state(discussions), ReviewState(0, 0))

    def test_reopened_reply_holds_a_resolved_thread(self):
        discussions = gitlab_fixture()
        notes = gitlab_diff_notes(discussions)
        reply = {**notes[0], "id": 1130, "body": "reopened reply"}
        notes[0]["resolved"] = True
        notes.append(reply)
        self.assertEqual(gitlab_review_state(discussions), ReviewState(1, 0))

    def test_whole_file_thread_does_not_hold_merge(self):
        discussions = gitlab_fixture()
        gitlab_diff_notes(discussions)[0]["position"]["position_type"] = "file"
        self.assertEqual(gitlab_review_state(discussions), ReviewState(0, 0))

    def test_missing_note_resolution_is_unreadable(self):
        discussions = gitlab_fixture()
        del gitlab_diff_notes(discussions)[0]["resolved"]
        with self.assertRaises(ReviewReadError):
            gitlab_review_state(discussions)


def forgejo_fixture():
    # Recorded from one Forgejo pull request and reduced to the fields the
    # reader uses, with user and path names replaced by neutral values.
    recorded = json.loads((FIXTURES / "forgejo-review-state.json").read_text(encoding="utf-8"))
    comments = {int(review_id): rows for review_id, rows in recorded["comments"].items()}
    return recorded["reviews"], comments


class ForgejoReviewsTest(unittest.TestCase):
    def test_recorded_reply_counts_every_unresolved_conversation(self):
        reviews, comments = forgejo_fixture()
        self.assertEqual(forgejo_review_state(reviews, comments), ReviewState(24, 1))

    def test_imported_conversation_uses_the_first_api_comment_as_root(self):
        reviews, comments = forgejo_fixture()
        latest = max(comments)
        first, second = comments[latest]
        first["resolver"] = first["user"]
        first["id"] = second["id"] + 2
        comments[latest].insert(
            1,
            {
                **first,
                "id": second["id"] + 1,
                "created_at": "2026-10-01T00:00:00Z",
                "resolver": None,
            },
        )
        reviews[-1]["comments_count"] += 1
        self.assertEqual(forgejo_review_state(reviews, comments), ReviewState(23, 1))

    def test_deleted_reviewers_cannot_clear_each_others_verdicts(self):
        reviews = [
            {
                "state": state,
                "user": {"id": -1, "login": "Ghost"},
                "dismissed": False,
                "submitted_at": f"2026-01-0{day}T00:00:00Z",
                "comments_count": 0,
            }
            for day, state in enumerate(("REQUEST_CHANGES", "APPROVED"), start=1)
        ]
        with self.assertRaises(ReviewReadError):
            forgejo_review_state(reviews, {})

    def test_dismissed_verdict_leaves_the_previous_changes_request_active(self):
        request = {
            "state": "REQUEST_CHANGES",
            "user": {"id": 42},
            "dismissed": False,
            "submitted_at": "2026-01-01T00:00:00Z",
            "comments_count": 0,
        }
        for state in ("APPROVED", "REQUEST_CHANGES"):
            with self.subTest(state=state):
                dismissed = {
                    **request,
                    "state": state,
                    "dismissed": True,
                    "submitted_at": "2026-01-02T00:00:00Z",
                }
                self.assertEqual(forgejo_review_state([request, dismissed], {}), ReviewState(0, 1))

    def test_deleted_reviewer_with_no_active_request_does_not_hold_merge(self):
        for state, dismissed in (("APPROVED", False), ("REQUEST_CHANGES", True)):
            with self.subTest(state=state):
                review = {
                    "state": state,
                    "user": {"id": -1, "login": "Ghost"},
                    "dismissed": dismissed,
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "comments_count": 0,
                }
                self.assertEqual(forgejo_review_state([review], {}), ReviewState(0, 0))

    def test_deleted_resolver_still_settles_the_conversation(self):
        reviews, comments = forgejo_fixture()
        latest = max(comments)
        comments[latest][0]["resolver"] = {"id": -1, "login": "Ghost"}
        self.assertEqual(forgejo_review_state(reviews, comments), ReviewState(23, 1))

    def test_missing_comment_resolution_is_unreadable(self):
        reviews, comments = forgejo_fixture()
        del comments[max(comments)][0]["resolver"]
        with self.assertRaises(ReviewReadError):
            forgejo_review_state(reviews, comments)


@unittest.skipIf(os.name == "nt", "Forge CLI stubs use executable Unix shell scripts")
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
            return subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8=0",
                    str(READER),
                    forge,
                    "review.example",
                    "org/repo",
                    "7",
                    *options,
                ],
                env=env
                | {
                    "PATH": temp,
                    "FM_STUB_PYTHON": sys.executable,
                    "FM_STUB_REPLY": str(reply),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    def test_github_holds_merge_when_an_approval_adds_an_open_thread(self):
        result = self.run_reader(
            "gh",
            """
            import json
            import sys
            from pathlib import Path

            def connection(nodes):
                return dict(nodes=nodes, totalCount=len(nodes), pageInfo={"hasNextPage": False})

            submitted = Path(__file__).with_name("approval-submitted")
            approved = submitted.exists()
            submitted.touch()
            query = next(arg for arg in sys.argv if arg.startswith("query="))
            pr = {"headRefOid": "unchanged"}
            if "reviewThreads(" in query:
                pr["reviewThreads"] = connection([
                    {"subjectType": "LINE", "isResolved": False, "isOutdated": False}
                ] if approved else [])
            if "reviews(" in query:
                pr["reviews"] = connection([{
                    "state": "APPROVED" if approved else "CHANGES_REQUESTED",
                    "author": {"login": "reviewer"},
                    "submittedAt": "2026-01-01T00:00:00Z",
                }])
            page = {"data": {"repository": {"pullRequest": pr}}}
            print(json.dumps([page] if "--slurp" in sys.argv else page))
            """,
            "github",
            "--head",
            "unchanged",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0 1\n")

    def test_github_reads_remaining_threads_and_verdicts(self):
        result = self.run_reader(
            "gh",
            """
            import json
            import sys

            def connection(nodes, cursor=None):
                return dict(nodes=nodes, totalCount=1, pageInfo={
                    "hasNextPage": cursor is not None, "endCursor": cursor,
                })

            cursor = next((arg for arg in sys.argv if arg.startswith("endCursor=")), None)
            pr = {"headRefOid": "unchanged"}
            if cursor is None:
                pr["reviewThreads"] = connection([], "threads")
                pr["reviews"] = connection([], "reviews")
            elif cursor == "endCursor=threads":
                pr["reviewThreads"] = connection([
                    {"subjectType": "LINE", "isResolved": False, "isOutdated": False}
                ])
            elif cursor == "endCursor=reviews":
                pr["reviews"] = connection([{
                    "state": "CHANGES_REQUESTED",
                    "author": {"login": "reviewer"},
                    "submittedAt": "2026-01-01T00:00:00Z",
                }])
            page = {"data": {"repository": {"pullRequest": pr}}}
            print(json.dumps([page] if "--slurp" in sys.argv else page))
            """,
            "github",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "1 1\n")

    def test_gitlab_reads_the_merge_request_host(self):
        response = """
            import json
            import os
            import sys

            if os.environ.get("GITLAB_HOST") != "review.example":
                sys.exit(1)
            if sys.argv[-1].endswith("?per_page=100"):
                page = json.load(open(os.environ["STUB_FIXTURE"], encoding="utf-8"))[1]
                page[0]["notes"][0]["body"] = "Ł"
                sys.stdout.buffer.write(json.dumps(page, ensure_ascii=False).encode("utf-8"))
            else:
                print('{"sha":"current"}')
        """
        result = self.run_reader(
            "glab",
            response,
            "gitlab",
            GITLAB_HOST="operator.example",
            LC_ALL="C",
            STUB_FIXTURE=str(FIXTURES / "gitlab-discussions.json"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "1 0\n")

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
                        "dismissed": False, "user": {"id": 1, "login": "reviewer"},
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
