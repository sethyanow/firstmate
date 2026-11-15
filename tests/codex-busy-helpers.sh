#!/usr/bin/env bash
# Codex's on-disk session format, reduced to the fields the public reader needs.
codex_session_fixture() { # <codex-home> <workspace> <id>
  python3 - "$@" <<'PY'
import json
import sqlite3
import sys
import time
from pathlib import Path

home, workspace, session = sys.argv[1:]
root = Path(home)
root.mkdir(parents=True, exist_ok=True)
log = root / 'sessions' / (session + '.jsonl')
log.parent.mkdir(exist_ok=True)
log.write_text(json.dumps({'type': 'session_meta', 'payload': {
    'id': session, 'cwd': workspace, 'source': 'cli', 'originator': 'codex-tui'}}) + '\n')
with sqlite3.connect(root / 'state_5.sqlite') as db:
    db.execute('CREATE TABLE IF NOT EXISTS threads '
               '(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, source TEXT, created_at_ms INTEGER)')
    db.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?)',
               (session, str(log), workspace, 'cli', time.time_ns() // 1_000_000))
PY
}

codex_event_fixture() { # <codex-home> <session> <event> <turn>
  printf '{"type":"event_msg","payload":{"type":"%s","turn_id":"%s"}}\n' \
    "$3" "$4" >> "$1/sessions/$2.jsonl"
}
