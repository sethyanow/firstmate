"""Memory configuration and saved-evidence decisions at their owning functions."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from firstmate.memory_check import (
    answer_at,
    ask_report,
    expand,
    read_check,
    source_text,
    task_directory,
)
from firstmate.memory_config import load_config, resolve_levels, validate_backend, validate_levels


def backend(**changes):
    return dict(
        name="archive",
        url="https://example.test/read",
        method="POST",
        answer_path="/answer",
        **changes,
    )


class BackendTests(unittest.TestCase):
    def test_accepts_root_pointer_and_optional_item_selection(self):
        configured = backend()
        configured.update(answer_path="", item_path="/a~1b/~0key", timeout_seconds=0.5)
        validate_backend(configured)

    def test_requires_named_http_backend_without_embedded_credentials(self):
        for field, value in [
            ("name", "../escape"),
            ("url", "file:///tmp/data"),
            ("url", "https://key@example.test"),
            ("method", "post"),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                configured = backend()
                configured[field] = value
                validate_backend(configured)

    def test_requires_valid_json_pointers(self):
        for field in ("answer_path", "item_path"):
            for value in ("text", "/bad~2", 3):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    configured = backend()
                    configured[field] = value
                    validate_backend(configured)

    def test_timeout_must_be_positive_and_finite(self):
        for value in (0, -1, float("inf"), float("nan"), "60"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_backend(backend(timeout_seconds=value))

    def test_levels_and_level_placeholder_come_together(self):
        validate_levels(backend(body={"budget": "{level}"}, levels={"deep": "high"}))
        in_url = backend(levels={"deep": "high"})
        in_url["url"] = "https://example.test/read?budget={level}"
        validate_levels(in_url)
        fragment = backend()
        fragment["url"] += "#budget={level}"
        validate_levels(fragment)
        for changes in (
            {"body": {"budget": "{level}"}},
            {"levels": {"deep": "high"}},
            {"body": {"budget": "{level}"}, "levels": {}},
            {"body": {"budget": "{level}"}, "levels": ["high"]},
            {"body": {"budget": "{level}"}, "levels": {"deep": ""}},
            {"body": {"budget": "{level}"}, "levels": {"deep": 3}},
            {"body": {"budget": "{level}"}, "levels": {"../deep": "high"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_levels(backend(**changes))

    def test_authentication_has_one_safe_key_source(self):
        for auth in (
            {},
            {"env": "A", "file": "key"},
            {"env": ""},
            {"env": "A", "header": "X\nHeader"},
            {"env": "A", "prefix": "Bearer\n"},
        ):
            with self.subTest(auth=auth), self.assertRaises(ValueError):
                validate_backend(backend(auth=auth))


class LevelTests(unittest.TestCase):
    def test_a_level_maps_to_each_configured_spelling_and_omits_fixed_backends(self):
        search = {**backend(levels={"deep": "top tier"}), "name": "search"}
        self.assertEqual(
            resolve_levels(
                [backend(levels={"deep": "high"}), search, {**backend(), "name": "fixed"}],
                "deep",
            ),
            {"archive": "high", "search": "top tier"},
        )

    def test_missing_or_unknown_level_lists_only_shared_choices(self):
        archive = backend(levels={"deep": "high", "quick": "low"})
        search = {**backend(levels={"thorough": "high", "quick": "low"}), "name": "search"}
        for level in (None, "max"):
            with self.subTest(level=level), self.assertRaises(ValueError) as raised:
                resolve_levels([archive, search], level)
            self.assertIn("quick", str(raised.exception))
            self.assertNotIn("deep", str(raised.exception))
            self.assertNotIn("thorough", str(raised.exception))


def result(name, seconds, answer=None, error=None):
    return {"backend": name, "seconds": seconds, "answer": answer, "error": error}


class AskReportTests(unittest.TestCase):
    def setUp(self):
        self.report = ask_report(
            ["first", "second"],
            [
                [result("chat", 0.4, "chat on first"), result("search", 0.1, [])],
                [result("chat", 12.0, {"fact": "kept"}), result("search", 2.0, error="HTTP 503")],
            ],
            "deep",
            ["chat"],
            12.5,
        )
        self.header, *self.queries = self.report.split("\n\n## q")

    def test_header_counts_answers_found_and_names_level_and_time(self):
        self.assertNotIn("\n", self.header)
        for part in (
            r"\b2 of 4\b",
            r"\b2 questions\b",
            r"\b2 backends\b",
            r"\bdeep\b",
            r"applies to chat\)",
            r"\b12\.5 s",
        ):
            self.assertRegex(self.header, part)

    def test_each_query_lists_every_backend_with_its_time_and_answer(self):
        first, second = self.queries
        self.assertTrue(first.startswith("1: first\n"))
        self.assertIn("chat, 0.4 s\n> chat on first", first)
        self.assertIn("search, 0.1 s\n> (no memory found)", first)
        self.assertTrue(second.startswith("2: second\n"))
        self.assertRegex(second, r"chat, 12\.0 s\n> \{\n> +\"fact\": \"kept\"\n> \}")
        self.assertIn("search, 2.0 s\n> Error: HTTP 503", second)

    def test_headings_inside_queries_and_answers_stay_inside_their_blocks(self):
        report = ask_report(
            ["What did we decide?\n\n### Notes from the brief"],
            [[result("chat", 1, "    first code line\n    second code line\n\n## Their heading")]],
            None,
            [],
            1,
        )
        self.assertIn(">\n> ### Notes from the brief", report)
        self.assertIn(">     first code line\n>     second code line\n>\n> ## Their heading", report)
        headings = [line for line in report.splitlines() if line.startswith("#")]
        self.assertEqual(headings, ["## q1: What did we decide?", "### chat, 1.0 s"])

    def test_blank_answers_are_not_counted_as_memory(self):
        report = ask_report(
            ["q"],
            [[result("a", 1, "  "), result("b", 1, {}), result("c", 1, "kept")]],
            None,
            [],
            1,
        )
        self.assertRegex(report.splitlines()[0], r"\b1 of 3\b")
        self.assertEqual(report.count("(no memory found)"), 2)


class AnswerTests(unittest.TestCase):
    def test_pointer_decodes_escaped_object_keys_and_array_indexes(self):
        self.assertEqual(answer_at({"a/b": [{"~key": "keep"}]}, "/a~1b/0/~0key"), "keep")

    def test_empty_pointer_keeps_the_whole_answer(self):
        self.assertEqual(answer_at(["one", "two"], ""), ["one", "two"])

    def test_unresolved_pointer_cannot_be_an_answer(self):
        for value, pointer in [({}, "/absent"), ([], "/0"), (["one"], "/01"), (None, "/text")]:
            with (
                self.subTest(pointer=pointer),
                self.assertRaises((ValueError, KeyError, IndexError)),
            ):
                answer_at(value, pointer)

    def test_expansion_preserves_values_and_never_expands_supplied_placeholders(self):
        self.assertEqual(
            expand(
                {"query": ["{query}", 3, None], "context": "{context}", "budget": "{level}"},
                {"query": 'prior "{context}"', "context": "go {level}", "level": "{query}"},
            ),
            {
                "query": ['prior "{context}"', 3, None],
                "context": "go {level}",
                "budget": "{query}",
            },
        )


class TaskIdTests(unittest.TestCase):
    def test_accepts_launch_rule_task_ids(self):
        for task in ("_demo", "-x", "x" * 64):
            with self.subTest(task=task):
                self.assertEqual(task_directory(Path("/unused-home"), task).name, task)

    def test_rejects_task_ids_outside_the_launch_rule(self):
        for task in ("x" * 65, ".demo", "", "demo/escape", "two words"):
            with self.subTest(task=task), self.assertRaises(ValueError):
                task_directory(Path("/unused-home"), task)


class SavedCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fm-memory-unit-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        for name in ("FM_DATA_OVERRIDE", "FM_CONFIG_OVERRIDE"):
            previous = os.environ.pop(name, None)
            if previous is not None:
                self.addCleanup(os.environ.__setitem__, name, previous)
        self.folder = self.home / "data/demo"
        (self.folder / "memory-checks").mkdir(parents=True)
        self.record = {
            "version": 1,
            "id": "a" * 32,
            "task": "demo",
            "context": "Build the helper.",
            "results": [
                {
                    "backend": "archive",
                    "query": "rules",
                    "answer": None,
                    "error": "TimeoutError",
                    "seconds": 1,
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ],
        }

    def save(self):
        text = json.dumps(self.record)
        (self.folder / "memory-check.json").write_text(text)
        (self.folder / "memory-checks" / ("a" * 32 + ".json")).write_text(text)

    def test_missing_latest_check_is_optional(self):
        self.assertIsNone(read_check(self.home, "demo"))

    def test_error_evidence_is_valid_without_claiming_successful_recall(self):
        self.save()
        self.assertEqual(read_check(self.home, "demo"), self.record)

    def test_explicit_id_selects_immutable_evidence(self):
        self.save()
        (self.folder / "memory-check.json").write_text("invalid")
        self.assertEqual(read_check(self.home, "demo", "Memory check: " + "a" * 32), self.record)

    def test_missing_explicit_check_is_an_error(self):
        with self.assertRaises(ValueError):
            read_check(self.home, "demo", "Memory check: " + "b" * 32)

    def test_duplicate_or_invalid_marker_is_rejected(self):
        for marker in (
            "Memory check: ../../elsewhere",
            "Memory check: " + "a" * 32 + "\nMemory check: " + "b" * 32,
        ):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                read_check(self.home, "demo", marker)

    def test_record_must_belong_to_the_task_and_match_its_id(self):
        valid = self.record.copy()
        for field, value in [
            ("task", "other"),
            ("id", "b" * 32),
            ("context", ""),
            ("results", [None]),
            ("results", []),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.record = {**valid, field: value}
                self.save()
                read_check(self.home, "demo", "Memory check: " + "a" * 32)

    def test_config_rejects_invalid_json_and_duplicate_backends(self):
        (self.home / "config").mkdir()
        config = self.home / "config/memory-check.json"
        for text in (
            "{",
            json.dumps({"backends": [backend()] * 2}),
            json.dumps({"backends": [], "quote_sources": {"card": {"command": "show {id}"}}}),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                config.write_text(text)
                load_config(self.home)

    def test_quote_source_reads_independently_of_recall_levels(self):
        (self.home / "config").mkdir()
        config = self.home / "config/memory-check.json"
        archive = backend(body={"budget": "{level}"}, levels={"deep": "high"})
        command = [sys.executable, "-c", "print('yes, build it')"]
        for levels in ({"quick": "low"}, []):
            with self.subTest(levels=levels):
                search = {**archive, "name": "search", "levels": levels}
                config.write_text(
                    json.dumps(
                        {
                            "backends": [archive, search],
                            "quote_sources": {"card": {"command": command}},
                        }
                    )
                )
                self.assertEqual(source_text(self.home, "card", "c1", 5), "yes, build it\n")


if __name__ == "__main__":
    unittest.main()
