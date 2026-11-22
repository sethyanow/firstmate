"""The memory-check command run against stub backends in a temporary home."""

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


class MemoryCommandCase(unittest.TestCase):
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
        result = subprocess.run(
            [sys.executable, str(COMMAND), *args],
            env=self.env,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return subprocess.CompletedProcess(
            result.args, result.returncode, result.stdout.decode(), result.stderr.decode()
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
                "url": url + "/recall?budget={level}#section#details",
                "method": "POST",
                "body": {"query": "{query}", "budget": "{level}"},
                "levels": {"quick": "low", "deep": "top tier"},
                "answer_path": "/answer",
            },
        ]
