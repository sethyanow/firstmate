"""The ad hoc memory commands against configured providers."""

import json
import re
import unittest
from http.server import BaseHTTPRequestHandler

from memory_support import MemoryCommandCase


class RecallCommandTests(MemoryCommandCase):
    def recall_backends(self, seen):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                query = body["query"]
                if query.startswith("http-error "):
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"first\x1b[31m\r\nsecond")
                    return
                if self.path == "/chat":
                    text = "one\x07\r\ntwo\ud800end" if query == "controls" else f"recalled {query}"
                    payload = {"answer": text}
                elif self.path == "/archive":
                    payload = {"results": [{"text": "from the archive", "id": "archive-record"}]}
                elif query == "bad-envelope":
                    payload = {"results": {"results": [{"text": "bad-envelope-id"}]}}
                elif query == "bad-fact":
                    payload = {"results": [{"text": {"id": "bad-fact-id"}}]}
                elif query == "redaction":
                    key = self.headers["Authorization"].removeprefix("Bearer ")
                    fact = {"text": f"same {key}", "metadata": {"session_id": key}}
                    payload = {"results": [fact, {**fact, "text": "same [key]"}, fact]}
                elif query == "controls":
                    payload = {
                        "results": [
                            {
                                "text": "Café\t\x1b[31mred\udfffblue",
                                "occurred_start": "2026-\x1b04",
                                "type": "wor\x07ld",
                                "metadata": {"session_id": "abc\x9bdef123"},
                            }
                        ]
                    }
                else:
                    fact = {"text": f"fact on {query}", "type": "world", "id": "f-91"}
                    payload = {"results": [fact, {**fact, "id": "f-92"}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        url = self.server(Handler)
        honcho, hindsight = self.leveled_backends(url)
        honcho.update(name="honcho", item_path="/text")
        hindsight.update(
            name="hindsight",
            url=url + "/search",
            levels={"deep": "top tier", "thorough": "all"},
            answer_path="/results",
        )
        archive = {
            "name": "archive",
            "url": url + "/archive",
            "method": "POST",
            "body": {"query": "{query}"},
            "answer_path": "/results",
            "item_path": "/text",
        }
        return honcho, hindsight, archive

    def test_remember_and_facts_route_by_backend_name(self):
        seen = []
        self.configure(list(self.recall_backends(seen)))
        remembered = self.command("remember", "--level", "quick", "--query", "a", "--query", "b")
        self.assertEqual(remembered.returncode, 0, remembered.stderr)
        self.assertCountEqual(
            seen,
            [
                ("/chat", {"query": "a", "reasoning_level": "low"}),
                ("/chat", {"query": "b", "reasoning_level": "low"}),
            ],
        )
        self.assertIn("> recalled a", remembered.stdout)
        self.assertIn("> recalled b", remembered.stdout)
        self.assertNotIn("### hindsight", remembered.stdout)

        seen.clear()
        facts = self.command("facts", "--level", "thorough", "--query", "a")
        self.assertEqual(facts.returncode, 0, facts.stderr)
        self.assertEqual(seen, [("/search", {"query": "a", "budget": "all"})])
        self.assertIn("### hindsight, ", facts.stdout)
        self.assertEqual(facts.stdout.splitlines().count("> - world | fact on a"), 1)
        self.assertNotIn("f-91", facts.stdout)
        self.assertNotIn("f-92", facts.stdout)

    def test_ask_formats_only_hindsight_as_fact_lines(self):
        seen = []
        honcho, hindsight, archive = self.recall_backends(seen)
        self.configure([honcho, hindsight, archive])
        result = self.command("ask", "--level", "deep", "--query", "a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(
            seen,
            [
                ("/chat", {"query": "a", "reasoning_level": "high"}),
                ("/search", {"query": "a", "budget": "top tier"}),
                ("/archive", {"query": "a"}),
            ],
        )
        self.assertIn("> recalled a", result.stdout)
        self.assertIn("> - world | fact on a", result.stdout)
        self.assertIn('"id": "archive-record"', result.stdout)
        self.assertNotIn("f-91", result.stdout)

    def test_malformed_hindsight_answers_do_not_stop_other_questions(self):
        _, hindsight, _ = self.recall_backends([])
        self.configure([hindsight])
        for command in ("facts", "ask"):
            with self.subTest(command=command):
                result = self.command(
                    command,
                    "--level",
                    "deep",
                    "--query",
                    "bad-envelope",
                    "--query",
                    "bad-fact",
                    "--query",
                    "a",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("recalled 1 of 3 answers", result.stdout)
                self.assertIn("> Error: unexpected Hindsight answer", result.stdout)
                self.assertIn("> Error: unexpected Hindsight fact", result.stdout)
                self.assertNotIn("bad-envelope-id", result.stdout)
                self.assertNotIn("bad-fact-id", result.stdout)
                self.assertIn("> - world | fact on a", result.stdout)

    def test_hindsight_does_not_unwrap_a_whole_response(self):
        _, hindsight, _ = self.recall_backends([])
        self.configure([{**hindsight, "answer_path": ""}])
        report = self.command("facts", "--level", "deep", "--query", "a")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("> Error: unexpected Hindsight answer", report.stdout)
        self.assertNotIn("fact on a", report.stdout)

    def test_hindsight_compares_original_fact_text_before_redacting_it(self):
        _, hindsight, _ = self.recall_backends([])
        self.configure([{**hindsight, "auth": {"env": "MEMORY_TEST_KEY"}}])
        report = self.command("facts", "--level", "deep", "--query", "redaction")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(report.stdout.splitlines().count("> - s:[key] | same [key]"), 2)
        self.assertNotIn("secret-f", report.stdout)

    def test_reports_escape_unsafe_text_and_preserve_line_breaks(self):
        seen = []
        honcho, hindsight, _ = self.recall_backends(seen)
        self.configure([honcho, hindsight])
        self.env["PYTHONIOENCODING"] = "utf-8:strict"
        error_query = "http-error \x1b[31mheading\r\n\x07continuation"
        answer = r"(?m)^> one.+\n> two.+end$"
        fact = r"(?m)^> - 2026-.+04 wor.+ld s:abc.+def1 \| Café.+\[31mred.+blue$"
        error = r"(?m)^> Error: HTTP 503: first.+\[31m\n> second$"
        for command, answers in (
            ("remember", [answer]),
            ("facts", [fact]),
            ("ask", [answer, fact]),
        ):
            with self.subTest(command=command):
                seen.clear()
                report = self.command(
                    command, "--level", "deep", "--query", "controls", "--query", error_query
                )
                self.assertEqual(report.returncode, 0, report.stderr)
                self.assertCountEqual(
                    [body["query"] for _, body in seen], ["controls", error_query] * len(answers)
                )
                self.assertNotRegex(report.stdout, r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
                self.assertRegex(
                    report.stdout, r"(?m)^## q2: http-error .+\[31mheading\n> .+continuation$"
                )
                for expected in answers:
                    self.assertRegex(report.stdout, expected)
                self.assertEqual(len(re.findall(error, report.stdout)), len(answers))

    def test_a_command_without_its_named_backend_stops_before_requests(self):
        seen = []
        _, _, archive = self.recall_backends(seen)
        for command, backend, missing in (
            ("facts", archive, "hindsight"),
            ("remember", {**archive, "item_path": ""}, "honcho"),
        ):
            for backends in ([], [backend]):
                with self.subTest(command=command, configured=bool(backends)):
                    self.configure(backends)
                    result = self.command(command, "--query", "a")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"backend named {missing}", result.stderr)
                    self.assertEqual(result.stdout, "")
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
