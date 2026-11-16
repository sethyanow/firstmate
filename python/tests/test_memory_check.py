import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
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

    def test_no_backends_is_a_noop_and_malformed_config_fails_closed(self):
        (self.home / "config/memory-check-required").touch()
        self.assertEqual(self.command("gate", "demo", "--brief", "absent").returncode, 0)
        result = self.command("run", "demo", "--context", str(self.context), "--query", "rules")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.home / "data").exists())


if __name__ == "__main__":
    unittest.main()
