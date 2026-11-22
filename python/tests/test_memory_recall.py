"""The ad hoc memory commands against configured providers."""

import json
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
                if self.path == "/chat":
                    payload = {"answer": f"recalled {body['query']}"}
                elif self.path == "/archive":
                    payload = {"results": [{"text": "from the archive", "id": "archive-record"}]}
                elif body["query"] == "bad-envelope":
                    payload = {"results": {"id": "bad-envelope-id"}}
                elif body["query"] == "bad-fact":
                    payload = {"results": [{"text": {"id": "bad-fact-id"}}]}
                else:
                    fact = {"text": f"fact on {body['query']}", "type": "world", "id": "f-91"}
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
            answer_path="",
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
        self.configure([honcho, {**hindsight, "answer_path": "/results"}, archive])
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
