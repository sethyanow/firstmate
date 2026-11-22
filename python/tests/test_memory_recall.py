"""The recall commands that each ask one kind of backend."""

import json
import unittest
from http.server import BaseHTTPRequestHandler

from memory_support import MemoryCommandCase


class RecallCommandTests(MemoryCommandCase):
    def answer_and_item_backends(self, seen):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                if self.path == "/chat":
                    payload = {"answer": f"recalled {body['query']}"}
                else:
                    fact = {"text": f"fact on {body['query']}", "kind": "world", "id": "f-91"}
                    payload = {"results": [fact, {**fact, "id": "f-92"}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        url = self.server(Handler)
        chat, search = self.leveled_backends(url)
        search.update(
            url=url + "/search",
            levels={"deep": "top tier", "thorough": "all"},
            answer_path="/results",
            item_path="/text",
            item_fields={"type": ["/kind"]},
        )
        return chat, search

    def test_remember_and_facts_each_ask_only_their_kind_of_backend(self):
        seen = []
        self.configure(list(self.answer_and_item_backends(seen)))
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
        self.assertNotIn("search", remembered.stdout)

        seen.clear()
        facts = self.command("facts", "--level", "thorough", "--query", "a")
        self.assertEqual(facts.returncode, 0, facts.stderr)
        self.assertEqual(seen, [("/search", {"query": "a", "budget": "all"})])
        self.assertIn("### search, ", facts.stdout)
        self.assertIn("> - world | fact on a", facts.stdout)
        self.assertNotIn("dialectic", facts.stdout)

    def test_ask_prints_answers_and_fact_lines_together(self):
        seen = []
        self.configure(list(self.answer_and_item_backends(seen)))
        result = self.command("ask", "--level", "deep", "--query", "a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(seen), 2)
        self.assertIn("> recalled a", result.stdout)
        self.assertIn("> - world | fact on a", result.stdout)

    def test_a_command_without_its_kind_of_backend_stops_before_requests(self):
        seen = []
        chat, search = self.answer_and_item_backends(seen)
        for command, backends, missing in (
            ("facts", [chat], "with an item_path"),
            ("remember", [search], "without an item_path"),
        ):
            with self.subTest(command=command):
                self.configure(backends)
                result = self.command(command, "--level", "deep", "--query", "a")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
