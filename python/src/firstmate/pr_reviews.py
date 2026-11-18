"""Read and count the forge feedback that holds a pull request for review."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

Record = dict[str, object]
Read = Callable[..., subprocess.CompletedProcess[str]]


class ReviewReadError(ValueError):
    """The forge did not provide enough information to decide."""


@dataclass(frozen=True)
class ReviewState:
    open_threads: int
    changes_requested: int

    @property
    def blocked(self) -> bool:
        return self.open_threads > 0 or self.changes_requested > 0


def _object(value: object) -> Record:
    if not isinstance(value, dict):
        raise ReviewReadError("expected an object")
    return value


def _records(value: object) -> list[Record]:
    if not isinstance(value, list):
        raise ReviewReadError("expected a list")
    return [_object(item) for item in value]


def _text(row: Record, key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise ReviewReadError(f"invalid {key}")
    return value


def _flag(row: Record, key: str) -> bool:
    value = row[key]
    if not isinstance(value, bool):
        raise ReviewReadError(f"invalid {key}")
    return value


def _number(row: Record, key: str) -> int:
    value = row[key]
    if type(value) is not int or value < 0:
        raise ReviewReadError(f"invalid {key}")
    return value


def _latest_changes(reviews: Iterable[tuple[str, str, bool]]) -> int:
    latest: dict[str, tuple[datetime, bool]] = {}
    for author, timestamp, requested in reviews:
        submitted = datetime.fromisoformat(timestamp)
        if submitted.tzinfo is None:
            raise ReviewReadError("review timestamp has no time zone")
        if author not in latest or submitted >= latest[author][0]:
            latest[author] = (submitted, requested)
    return sum(requested for _, requested in latest.values())


def _github_connection(
    pages: object, name: str, expected_head: str | None
) -> tuple[str, list[Record]]:
    connection_pages = _records(pages)
    nodes: list[Record] = []
    for index, page in enumerate(connection_pages):
        if page.get("errors"):
            raise ReviewReadError(f"GitHub returned errors for {name}")
        pr = _object(_object(_object(page["data"])["repository"])["pullRequest"])
        head = _text(pr, "headRefOid")
        if expected_head is not None and head != expected_head:
            raise ReviewReadError("GitHub head changed while checking review feedback")
        expected_head = head
        connection = _object(pr[name])
        nodes.extend(_records(connection["nodes"]))
        more = _flag(_object(connection["pageInfo"]), "hasNextPage")
        if more != (index + 1 < len(connection_pages)):
            raise ReviewReadError(f"incomplete GitHub {name} pagination")
        if not more and len(nodes) == _number(connection, "totalCount"):
            return head, nodes
    raise ReviewReadError(f"incomplete GitHub {name} list")


def _github_verdicts(reviews: Iterable[Record]) -> Iterable[tuple[str, str, bool]]:
    for review in reviews:
        state = _text(review, "state")
        if state in {"COMMENTED", "PENDING"}:
            continue
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            raise ReviewReadError("unknown GitHub review state")
        author = _object(review["author"])
        yield _text(author, "login"), _text(review, "submittedAt"), state == "CHANGES_REQUESTED"


def github_review_state(
    thread_pages: object, review_pages: object, expected_head: str | None = None
) -> ReviewState:
    """Count complete GitHub GraphQL connections."""
    head, threads = _github_connection(thread_pages, "reviewThreads", expected_head)
    _, reviews = _github_connection(review_pages, "reviews", head)
    unresolved = [(_flag(thread, "isResolved"), _flag(thread, "isOutdated")) for thread in threads]
    return ReviewState(
        unresolved.count((False, False)), _latest_changes(_github_verdicts(reviews))
    )


def _diff_refs(value: object) -> tuple[str, str, str]:
    row = _object(value)
    return _text(row, "base_sha"), _text(row, "start_sha"), _text(row, "head_sha")


def gitlab_review_state(discussions: object, reviewers: object, diff_refs: object) -> ReviewState:
    """Count current inline discussions and GitLab's current reviewer states."""
    opened = 0
    for discussion in _records(discussions):
        notes = _records(discussion["notes"])
        if not notes:
            raise ReviewReadError("GitLab discussion has no first note")
        first = notes[0]
        if (
            first["type"] != "DiffNote"
            or not _flag(first, "resolvable")
            or _flag(first, "resolved")
        ):
            continue
        if _diff_refs(first["position"]) == _diff_refs(diff_refs):
            opened += 1
    changes = 0
    for reviewer in _records(reviewers):
        state = _text(reviewer, "state")
        if state not in {
            "unreviewed",
            "review_started",
            "reviewed",
            "requested_changes",
            "approved",
            "unapproved",
        }:
            raise ReviewReadError("unknown GitLab review state")
        changes += state == "requested_changes"
    return ReviewState(opened, changes)


def _forgejo_verdicts(reviews: Iterable[Record]) -> Iterable[tuple[str, str, bool]]:
    for review in reviews:
        state = _text(review, "state")
        if state in {"COMMENT", "PENDING", "REQUEST_REVIEW"}:
            continue
        if state not in {"APPROVED", "REQUEST_CHANGES"}:
            raise ReviewReadError("unknown Forgejo review state")
        user = _object(review["user"])
        requested = not _flag(review, "dismissed") and state == "REQUEST_CHANGES"
        yield _text(user, "login"), _text(review, "submitted_at"), requested


def _forgejo_open_threads(comments: object, expected_count: int) -> int:
    rows = _records(comments)
    if len(rows) != expected_count:
        raise ReviewReadError("incomplete Forgejo review comments list")
    roots: dict[tuple[str, int, int], bool] = {}
    for row in sorted(rows, key=lambda item: _number(item, "id")):
        position = (
            _text(row, "path"),
            _number(row, "position"),
            _number(row, "original_position"),
        )
        resolver = row["resolver"]
        if resolver is not None:
            _number(_object(resolver), "id")
        roots.setdefault(position, resolver is None)
    return sum(roots.values())


def forgejo_review_state(reviews: object, comments: Mapping[int, object]) -> ReviewState:
    """Count Forgejo's conversations, grouped by their position within each review."""
    rows = _records(reviews)
    opened = 0
    for review in rows:
        if review["state"] == "PENDING":
            continue
        count = _number(review, "comments_count")
        if count:
            # The API omits per-comment outdated state. A stale review may still
            # contain current comments, so only explicit resolution unblocks it.
            opened += _forgejo_open_threads(comments[_number(review, "id")], count)
    return ReviewState(opened, _latest_changes(_forgejo_verdicts(rows)))


def _github_read(
    read: Read, host: str, repo: str, number: int, expected_head: str | None
) -> ReviewState:
    owner, name = repo.split("/")

    def connection(field: str, fields: str) -> object:
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$endCursor:String){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid "
            f"{field}(first:100,after:$endCursor)"
            "{totalCount nodes{" + fields + "} pageInfo{hasNextPage endCursor}}}}}"
        )
        result = read(
            "gh",
            "api",
            "graphql",
            "--hostname",
            host,
            "--paginate",
            "--slurp",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        )
        return json.loads(result.stdout)

    return github_review_state(
        connection("reviewThreads", "isResolved isOutdated"),
        connection("reviews", "state author{login} submittedAt"),
        expected_head,
    )


def _json_pages(output: str) -> list[Record]:
    if not output.strip():
        raise ReviewReadError("the forge returned no JSON pages")
    pages: list[Record] = []
    decoder = json.JSONDecoder()
    while remaining := output.lstrip():
        value, offset = decoder.raw_decode(remaining)
        pages.extend(_records(value))
        output = remaining[offset:]
    return pages


def _gitlab_read(
    read: Read, host: str, repo: str, number: int, expected_head: str | None
) -> ReviewState:
    prefix = ("glab", "api", "--hostname", host)
    mr = f"projects/{quote(repo, safe='')}/merge_requests/{number}"

    def revision() -> tuple[str, object]:
        request = _object(json.loads(read(*prefix, mr).stdout))
        return _text(request, "sha"), request.get("diff_refs")

    head, refs = revision()
    if expected_head is not None and head != expected_head:
        raise ReviewReadError("GitLab head changed while checking review feedback")
    if refs is not None and _diff_refs(refs)[2] != head:
        raise ReviewReadError("GitLab is still updating its diff for the current head")
    discussions = _json_pages(read(*prefix, "--paginate", f"{mr}/discussions?per_page=100").stdout)
    reviewers = _json_pages(read(*prefix, "--paginate", f"{mr}/reviewers?per_page=100").stdout)
    if revision() != (head, refs):
        raise ReviewReadError("GitLab head changed or diff changed while checking review feedback")
    return gitlab_review_state(discussions, reviewers, refs)


def _forgejo_review_pages(read: Read, prefix: tuple[str, ...], endpoint: str) -> dict[int, Record]:
    reviews: dict[int, Record] = {}
    total: int | None = None
    page = 1
    while True:
        result = read(*prefix, "--include", f"{endpoint}?limit=50&page={page}")
        count_header = next(
            (
                line
                for line in result.stderr.splitlines()
                if line.lower().startswith("x-total-count:")
            ),
            "",
        )
        if not count_header:
            raise ReviewReadError("Forgejo review list is missing X-Total-Count")
        count = int(count_header.split(":", 1)[1])
        if total is not None and count != total:
            raise ReviewReadError("Forgejo review total changed during pagination")
        total = count
        rows = _records(json.loads(result.stdout))
        for review in rows:
            review_id = _number(review, "id")
            if review_id in reviews:
                raise ReviewReadError("Forgejo repeated a review during pagination")
            reviews[review_id] = review
        if len(reviews) == total:
            return reviews
        if not rows or len(reviews) > total:
            raise ReviewReadError("incomplete Forgejo review list")
        page += 1


def _forgejo_read(
    read: Read, repo: str, number: int, login: str, expected_head: str | None
) -> ReviewState:
    prefix = ("tea", "api", "--login", login, "--repo", repo)
    pull = f"/repos/{repo}/pulls/{number}"
    endpoint = f"{pull}/reviews"

    def revision() -> str:
        request = _object(json.loads(read(*prefix, pull).stdout))
        return _text(_object(request["head"]), "sha")

    head = revision()
    if expected_head is not None and head != expected_head:
        raise ReviewReadError("Forgejo head changed while checking review feedback")
    reviews = _forgejo_review_pages(read, prefix, endpoint)
    comments = {
        review_id: json.loads(read(*prefix, f"{endpoint}/{review_id}/comments").stdout)
        for review_id, review in reviews.items()
        if review["state"] != "PENDING" and _number(review, "comments_count")
    }
    if revision() != head:
        raise ReviewReadError("Forgejo head changed while checking review feedback")
    return forgejo_review_state(list(reviews.values()), comments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("forge", choices=("github", "gitlab", "forgejo"))
    parser.add_argument("host")
    parser.add_argument("repo")
    parser.add_argument("number", type=int)
    parser.add_argument("--head")
    parser.add_argument("--login", default="")
    args = parser.parse_args()

    deadline = time.monotonic() + 20
    expected_head = args.head or None

    def read(*command: str) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewReadError("forge requests timed out")
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=remaining
        )
        if result.returncode:
            raise ReviewReadError(f"{command[0]} could not complete the review lookup")
        return result

    try:
        if args.forge == "github":
            state = _github_read(read, args.host, args.repo, args.number, expected_head)
        elif args.forge == "gitlab":
            state = _gitlab_read(read, args.host, args.repo, args.number, expected_head)
        else:
            state = _forgejo_read(read, args.repo, args.number, args.login, expected_head)
    except (KeyError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        sys.stderr.write(f"could not read {args.forge} review state: {error}\n")
        return 1
    sys.stdout.write(f"{state.open_threads} {state.changes_requested}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
