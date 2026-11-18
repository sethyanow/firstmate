import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ROOT / "bin/fm_memory_check.py"


class MemoryCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fm-memory-check-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "config").mkdir()
        self.context = self.home / "exchange.txt"
        self.context.write_text("Option 4A: build a local prototype.\nReply: 4A go\n")
        self.env = dict(os.environ, FM_HOME=str(self.home), MEMORY_TEST_KEY="secret-for-test")
        for name in ("FM_CONFIG_OVERRIDE", "FM_DATA_OVERRIDE", "CLAUDE_CODE_SESSION_ID"):
            self.env.pop(name, None)

    def command(self, *args):
        return subprocess.run(
            [sys.executable, str(COMMAND), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def configure(self, backends):
        (self.home / "config/memory-check.json").write_text(json.dumps({"backends": backends}))

    def test_ask_prints_every_answer_and_error_concurrently_without_task_files(self):
        barrier = threading.Barrier(4, timeout=5)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    self.send_error(500, "requests did not overlap")
                    return
                if self.path == "/denied":
                    self.send_response(401)
                    payload = b"access denied"
                else:
                    self.send_response(200)
                    payload = json.dumps({"answer": {"remembered": body["query"]}}).encode()
                self.end_headers()
                self.wfile.write(payload)

        url = self.server(Handler)
        self.configure(
            [
                {
                    "name": name,
                    "url": url + endpoint,
                    "method": "POST",
                    "body": {"query": "{query}"},
                    "answer_path": "/answer",
                }
                for name, endpoint in [("archive", "/ok"), ("unavailable", "/denied")]
            ]
        )
        (self.home / "config/memory-check-required").touch()
        result = self.command("ask", "--query", "prior decisions", "--query", "brief rules")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("archive", result.stdout)
        self.assertIn("unavailable", result.stdout)
        self.assertIn('"remembered": "prior decisions"', result.stdout)
        self.assertIn('"remembered": "brief rules"', result.stdout)
        self.assertEqual(result.stdout.count("HTTP 401: access denied"), 2)
        self.assertFalse((self.home / "data").exists())
        self.assertNotIn("Memory check:", result.stdout)

    def recording_server(self, seen):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"answer": f"recalled {body['query']}"}).encode())

        return self.server(Handler)

    def leveled_backends(self, url):
        return [
            {
                "name": "dialectic",
                "url": url + "/chat",
                "method": "POST",
                "body": {"query": "{query}", "reasoning_level": "{level}"},
                "levels": {"quick": "low", "deep": "high"},
                "answer_path": "/answer",
            },
            {
                "name": "search",
                "url": url + "/recall?budget={level}",
                "method": "POST",
                "body": {"query": "{query}", "budget": "{level}"},
                "levels": {"quick": "low", "deep": "top tier"},
                "answer_path": "/answer",
            },
        ]

    def test_chosen_level_reaches_each_backend_in_its_own_spelling(self):
        seen = []
        self.configure(self.leveled_backends(self.recording_server(seen)))
        result = self.command("ask", "--level", "deep", "--query", "one", "--query", "two")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(
            seen,
            [
                ("/chat", {"query": "one", "reasoning_level": "high"}),
                ("/chat", {"query": "two", "reasoning_level": "high"}),
                ("/recall?budget=top%20tier", {"query": "one", "budget": "top tier"}),
                ("/recall?budget=top%20tier", {"query": "two", "budget": "top tier"}),
            ],
        )

    def test_ask_reports_a_header_then_each_question_with_timed_answers_and_misses(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.startswith("/recall") and body["query"] == "second":
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"recall is down")
                    return
                answer = [] if self.path.startswith("/recall") else f"chat on {body['query']}"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"answer": answer}).encode())

        self.configure(self.leveled_backends(self.server(Handler)))
        result = self.command("ask", "--level", "deep", "--query", "first", "--query", "second")
        self.assertEqual(result.returncode, 0, result.stderr)
        header, first, second = result.stdout.split("## q")[0], *result.stdout.split("## q")[1:]
        self.assertRegex(header, r"^recalled 2 of 4 answers\b.*\b2 questions\b.*\bdeep\b.* s\n")
        self.assertRegex(first, r"^1: first\n")
        self.assertRegex(first, r"### dialectic, [0-9.]+ s\nchat on first\n")
        self.assertRegex(first, r"### search, [0-9.]+ s\n\(no memory found\)")
        self.assertRegex(second, r"^2: second\n")
        self.assertRegex(second, r"### dialectic, [0-9.]+ s\nchat on second\n")
        self.assertRegex(second, r"### search, [0-9.]+ s\nError: HTTP 503: recall is down")

    def test_run_saves_the_level_its_evidence_was_recalled_at(self):
        seen = []
        self.configure(self.leveled_backends(self.recording_server(seen)))
        result = self.command(
            "run", "demo", "--level", "quick", "--context", str(self.context), "--query", "one"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({body["budget"] for _, body in seen if "budget" in body}, {"low"})
        record = json.loads((self.home / "data/demo/memory-check.json").read_text())
        self.assertEqual(record["level"], "quick")
        self.assertIn("Level: quick", (self.home / "data/demo/memory-check.md").read_text())

    def test_missing_or_unconfigured_level_stops_before_any_request(self):
        seen = []
        self.configure(self.leveled_backends(self.recording_server(seen)))
        for args, wanted in [
            ((), "--level"),
            (("--level", "max"), "max"),
        ]:
            with self.subTest(args=args):
                result = self.command("ask", *args, "--query", "one")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(wanted, result.stderr)
                self.assertIn("quick, deep", result.stderr)
                self.assertEqual(result.stdout, "")
        result = self.command("run", "demo", "--context", str(self.context), "--query", "one")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "data").exists())
        self.assertEqual(seen, [])

    def test_queries_reach_every_backend_together_with_context_and_credentials(self):
        barrier = threading.Barrier(4, timeout=5)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    self.send_error(500, "requests did not overlap")
                    return
                payload = json.dumps(
                    {
                        "answer": {
                            "query": body["query"],
                            "context": body["context"],
                            "authenticated": self.headers.get("Authorization")
                            == "Bearer secret-for-test",
                        }
                    }
                ).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

        url = self.server(Handler)
        key_file = self.home / "key"
        key_file.write_text("secret-for-test\n")
        self.configure(
            [
                {
                    "name": "alpha",
                    "url": url,
                    "method": "POST",
                    "body": {"query": "{query}", "context": "{context}"},
                    "answer_path": "/answer",
                    "auth": {"env": "MEMORY_TEST_KEY"},
                },
                {
                    "name": "beta",
                    "url": url,
                    "method": "POST",
                    "body": {"query": "{query}", "context": "{context}"},
                    "answer_path": "/answer",
                    "auth": {"file": str(key_file)},
                },
            ]
        )
        result = self.command(
            "run",
            "prototype",
            "--context",
            str(self.context),
            "--query",
            'prior "decisions"',
            "--query",
            "brief rules",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.home / "data/prototype/memory-check.json").read_text())
        self.assertEqual(record["id"], result.stdout.strip())
        self.assertEqual(record["context"], self.context.read_text())
        self.assertEqual(len(record["results"]), 4)
        self.assertEqual({r["backend"] for r in record["results"]}, {"alpha", "beta"})
        self.assertEqual(
            {r["query"] for r in record["results"]}, {'prior "decisions"', "brief rules"}
        )
        for response in record["results"]:
            self.assertIsNone(response["error"])
            self.assertTrue(response["answer"]["authenticated"])
            self.assertEqual(response["answer"]["context"], self.context.read_text())
            self.assertGreaterEqual(response["seconds"], 0)
        markdown = (self.home / "data/prototype/memory-check.md").read_text()
        self.assertIn("4A go", markdown)
        self.assertIn("brief rules", markdown)
        self.assertNotIn("secret-for-test", markdown)
        old_id = result.stdout.strip()
        newer = self.command(
            "run",
            "prototype",
            "--context",
            str(self.context),
            "--query",
            "new scope",
            "--query",
            "new constraints",
        )
        self.assertEqual(newer.returncode, 0, newer.stderr)
        brief = self.home / "brief.md"
        brief.write_text("Memory check: " + old_id + "\n")
        (self.home / "config/memory-check-required").touch()
        self.assertEqual(self.command("gate", "prototype", "--brief", str(brief)).returncode, 0)

    def test_failures_are_saved_without_losing_other_answers_or_leaking_keys(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                elif self.path == "/error":
                    self.send_response(401)
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"answer": "saved secret-for-test", "empty": []}')

        url = self.server(Handler)
        self.configure(
            [
                {
                    "name": name,
                    "url": url + endpoint,
                    "method": "GET",
                    "answer_path": pointer,
                    "auth": {"env": "MEMORY_TEST_KEY"},
                }
                for name, endpoint, pointer in [
                    ("good", "/ok", "/answer"),
                    ("denied", "/error", "/answer"),
                    ("redirect", "/redirect", "/answer"),
                ]
            ]
        )
        result = self.command("run", "failure", "--context", str(self.context), "--query", "rules")
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = self.home / "data/failure/memory-check.json"
        self.assertNotIn("secret-for-test", artifact.read_text())
        record = json.loads(artifact.read_text())
        answers = {r["backend"]: r for r in record["results"]}
        self.assertEqual(answers["good"]["answer"], "saved [key]")
        self.assertTrue(answers["denied"]["error"].startswith("HTTP 401:"))
        self.assertTrue(answers["redirect"]["error"].startswith("HTTP 302:"))
        self.assertIn("saved [key]", answers["denied"]["error"])

    def test_error_diagnostics_do_not_wait_for_the_whole_body(self):
        prefix_sent = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                return

            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Length", "65536")
                self.end_headers()
                self.wfile.write(b"secret-for-test " + b"x" * 8192)
                self.wfile.flush()
                prefix_sent.set()
                release.wait(20)

        url = self.server(Handler)
        self.configure(
            [
                {
                    "name": "archive",
                    "url": url,
                    "method": "GET",
                    "answer_path": "",
                    "auth": {"env": "MEMORY_TEST_KEY"},
                }
            ]
        )
        with ThreadPoolExecutor(max_workers=1) as calls:
            collected = calls.submit(
                self.command, "run", "bounded", "--context", str(self.context), "--query", "rules"
            )
            try:
                self.assertTrue(prefix_sent.wait(10), "the server did not send its error prefix")
                result = collected.result(timeout=10)
            finally:
                release.set()
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = self.home / "data/bounded/memory-check.json"
        record = json.loads(artifact.read_text())
        error = record["results"][0]["error"]
        self.assertTrue(error.startswith("HTTP 500: [key] "))
        self.assertEqual(len(error), 2010)
        self.assertNotIn("secret-for-test", artifact.read_text())

    def test_gate_accepts_underscore_task_id_and_reports_missing_evidence(self):
        self.configure(
            [
                {
                    "name": "archive",
                    "url": "https://example.test/read",
                    "method": "GET",
                    "answer_path": "",
                }
            ]
        )
        (self.home / "config/memory-check-required").touch()
        brief = self.home / "brief.md"
        brief.write_text("Memory check: " + "a" * 32 + "\n")
        result = self.command("gate", "_demo", "--brief", str(brief))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no saved check", result.stderr)

    def test_no_backends_is_a_noop_and_malformed_config_fails_closed(self):
        (self.home / "config/memory-check-required").touch()
        self.assertEqual(self.command("gate", "demo", "--brief", "absent").returncode, 0)
        result = self.command("run", "demo", "--context", str(self.context), "--query", "rules")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.home / "data").exists())
        result = self.command("ask", "--query", "rules")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("no backends configured", result.stderr)
        self.assertFalse((self.home / "data").exists())

    def test_ask_requires_nonempty_queries(self):
        for args in [("ask",), ("ask", "--query", " ")]:
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "query|queries")
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
