"""Memory answers selected as advisory brief-check points."""

import itertools
import json
import re
from typing import Any


def point_lines(answer: str) -> list[str]:
    """An answer's list lines, or its paragraphs when it has no list."""
    found = []
    for line in answer.splitlines():
        m = re.match(r"\s*(?:\d+[.)]|[-*])\s+(.*\S)", line)
        if m:
            found.append(m.group(1).replace("**", ""))
    return found or [p.strip().replace("**", "") for p in answer.split("\n\n") if p.strip()]


def no_recall(point: str) -> bool:
    """Recognize explicit absence-only answers, preserving mixed substantive answers."""
    sentences = [
        s.strip() for s in re.split(r"[.!?](?:\s+|$)", point.replace("\u2019", "'")) if s.strip()
    ]
    knowledge = r"(?:records?|memory|information|details|findings|review|context|notes)"
    topic = r"(?:of|about|on|for|in)\b.*"
    qualifiers = r"(?:(?:any|a|the|your|that|this|specific|saved|available|prior|relevant)\s+)*"
    absence = re.compile(
        rf"(?:i (?:don't|do not) have {qualifiers}{knowledge}\b.*"
        rf"|(?:i have |there is |there's )?no {qualifiers}{knowledge}"
        rf"(?:\s+{topic}| available| (?:was |were )?found)?"
        rf"|i (?:couldn't|could not|cannot|can't) (?:find|recall) .+\b{knowledge}\b.*)",
        re.IGNORECASE,
    )
    return bool(sentences) and all(
        absence.fullmatch(sentence)
        and not re.search(
            r"[,;:]|\b(?:but|however|although|though|yet|and)\b", sentence, re.IGNORECASE
        )
        for sentence in sentences
    )


def unique_points(found: list[str], max_points: int) -> tuple[dict[str, str], int]:
    found = list(dict.fromkeys(p for p in found if not no_recall(p)))
    return {f"p{i + 1}": p for i, p in enumerate(found[:max_points])}, max(
        0, len(found) - max_points
    )


def points_from(answer: str, max_points: int) -> tuple[dict[str, str], int]:
    return unique_points(point_lines(answer), max_points)


def result_points(result: dict[str, Any]) -> list[str]:
    """A list answer gives one point per item, using the items its backend's item_path selected."""
    answer = result.get("items", result["answer"])
    if isinstance(answer, str):
        return point_lines(answer)
    items = answer if isinstance(answer, list) else [answer]
    return [
        i.strip() if isinstance(i, str) else json.dumps(i, ensure_ascii=False)
        for i in items
        if i is not None and str(i).strip()
    ]


def memory_points(results: list[dict[str, Any]], max_points: int) -> tuple[dict[str, str], int]:
    """Points taken in turn from each answer, so one backend cannot fill the cap alone."""
    rows = [
        [p for p in result_points(r) if not no_recall(p)] for r in results if r["error"] is None
    ]
    return unique_points(
        [p for turn in itertools.zip_longest(*rows) for p in turn if p is not None], max_points
    )
