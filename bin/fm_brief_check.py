#!/usr/bin/env python3
"""Gut check on a brief or message as it is sent. It warns and blocks nothing.

Steps: one Honcho call on the task, a trace of the sender's Claude session log,
one Jev request through OpenRouter. FM_HOME selects the home: config/brief-check
names the file that holds OPENROUTER_API_KEY when the environment does not, and
runs are logged under state/brief-check/. Questions and settings live in
fm-brief-check-questions.toml beside this file.

  fm_brief_check.py --text FILE|- [--ask FILE] [--session LOG.jsonl] [--to MATE]
           [--honcho-level medium|high] [--honcho-answer FILE]
           [--upto-line N] [--label NAME] [--questions FILE]

Exits 0 and prints the result on stdout. With no key, no session log, or
no answer, the result is one "brief-check: skipped: <reason>" line.
A flag from a question that can name a block of the text shows only when it names one.
The log keeps every flag.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = str(Path.home())

CITED_RE = re.compile(r"(?:~|/|[\w.-]+/)[\w./-]*\.(?:md|txt|toml|json|ya?ml|msg)\b")
READ_VERBS = {"rg", "grep", "cat", "bat", "sed", "head", "tail", "less", "jq", "yq", "awk"}
RECALL_RE = re.compile(r"""honcho peer chat[^'"\n]*['"]([^'"]+)|--tool recall[^\n]*?"query"\s*:\s*"([^"]+)""")
MACHINE_RE = re.compile(
    r"^\W*(: Firstmate|FIRSTMATE_OP|FIRSTMATE SUPERVISION WAKE|MAIN DIALOG MIRROR"
    r"|Firstmate (note|instruction) waiting|Firstmate: a new instruction|One more firstmate instruction"
    r"|fm-from-firstmate|Stop hook feedback|<)"
)
KEY_LINE_RE = re.compile(r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*)")
HEADER_SAFE_RE = re.compile(r"[\x21-\x7e]+")


def cut(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def short_path(p):
    return re.sub(r"(~/)?\.treehouse/firstmate-\w+/\d+/firstmate/", "<home>/", p.replace(HOME, "~"))


def task_lines(text):
    """Numbered lines to judge. A scaffolded brief keeps only its task sections."""
    lines = text.splitlines()
    start, end = 0, len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## Captain's intent"):
            start = i
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("# "):
                    end = j
                    break
            break
    return [(i + 1, lines[i]) for i in range(start, end) if lines[i].strip()]


def blocks_from(numbered, max_blocks):
    """Adjacent lines merged by size so a locator question has few options."""
    total = sum(len(t) for _, t in numbered)
    target = total / max_blocks
    split_at_headings = True
    while True:
        out, cur, size = [], [], 0
        for n, t in numbered:
            if cur and (size >= target or (split_at_headings and t.startswith("#"))):
                out.append(cur)
                cur, size = [], 0
            cur.append((n, t))
            size += len(t)
        if cur:
            out.append(cur)
        if len(out) <= max_blocks:
            break
        target *= 1.2
        # More headings than blocks can never fit while each heading forces a split.
        split_at_headings = False
    blocks = {}
    for group in out:
        a, b = group[0][0], group[-1][0]
        blocks[f"L{a}" if a == b else f"L{a}-{b}"] = "\n".join(t for _, t in group)
    return blocks


def api_key(home):
    """A value a header cannot carry is no key, because http.client echoes it in its error."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        try:
            config = Path(os.environ.get("FM_CONFIG_OVERRIDE") or home / "config")
            named = (config / "brief-check").read_text().partition("\n")[0].strip()
            with open(os.path.expanduser(named), errors="replace") as fh:
                for line in fh:
                    m = KEY_LINE_RE.fullmatch(line.rstrip("\n"))
                    if m:
                        key = m.group(1).strip().strip("'\"")
                        break
        except OSError:
            return ""
    return key if HEADER_SAFE_RE.fullmatch(key) else ""


def find_session():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    found = glob.glob(os.path.join(HOME, ".claude", "projects", "*", glob.escape(sid) + ".jsonl")) if sid else []
    return Path(max(found, key=os.path.getmtime)) if found else None


def intent_section(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## Captain's intent"):
            body = []
            for later in lines[i + 1:]:
                if later.startswith(("# ", "## ")):
                    break
                body.append(later)
            return "\n".join(body).strip()
    return ""


def typed_text(o):
    """His words from one log record, in any of the three shapes that carry them."""
    a = o.get("attachment")
    if isinstance(a, dict) and a.get("type") == "queued_command":
        return a.get("prompt") if (a.get("origin") or {}).get("kind") == "human" else None
    if o.get("type") == "queue-operation":
        return o.get("content")
    if o.get("type") == "user" and (o.get("origin") or {}).get("kind") == "human":
        c = o["message"].get("content")
        return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
    return None


def last_typed(path, upto, count=3):
    """One message can appear under more than one shape, so repeats collapse."""
    said = []
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if upto and n >= upto:
                break
            try:
                text = typed_text(json.loads(line))
            except ValueError:
                continue
            if isinstance(text, str) and text.strip() and not MACHINE_RE.match(text):
                said.append(text.strip())
    return list(dict.fromkeys(reversed(said)))[:count][::-1]


def ask_honcho(cfg, ask, level, limit):
    h = cfg["honcho"]
    query = h["prompt"].replace("{ask}", ask[: h["ask_chars"]]).replace("{max_points}", str(cfg["settings"]["max_points"]))
    cmd = ["honcho", "peer", "chat", "-w", h["workspace"], "-p", h["peer"], "-r", level, "--json", query]
    env = dict(os.environ, HONCHO_BASE_URL=h["base_url"])
    env.pop("OPENROUTER_API_KEY", None)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=limit)
    if r.returncode != 0:
        raise RuntimeError(f"honcho exit {r.returncode}: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout)["response"], query


def points_from(answer, max_points):
    found = []
    for line in answer.splitlines():
        m = re.match(r"\s*(?:\d+[.)]|[-*])\s+(.*\S)", line)
        if m:
            found.append(m.group(1).replace("**", ""))
    if not found:
        found = [p.strip().replace("**", "") for p in answer.split("\n\n") if p.strip()]
    return {f"p{i + 1}": p for i, p in enumerate(found[:max_points])}, max(0, len(found) - max_points)


def read_paths(command, cwd):
    """Paths a shell command read, with its own variables expanded. Writes do not count."""
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\1\b", " ", command, flags=re.S)
    for name, value in re.findall(r"(?:^|[\s;])(\w+)=([^\s;]+)", cmd):
        value = value.strip("'\"")
        cmd = re.sub(r"\$\{?" + name + r"\b\}?", lambda _: value, cmd)
    cmd = re.sub(r"\d?>>?\s*\S+", " ", cmd)
    out = []
    for seg in re.split(r"[;|\n]|&&", cmd):
        words = [w for w in seg.split() if not re.match(r"\w+=", w)]
        if words and words[0] in READ_VERBS:
            for p in CITED_RE.findall(seg):
                p = os.path.expanduser(p)
                out.append(p if p.startswith("/") else os.path.join(cwd or "", p))
    return out


def tool_results_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))


def trace_session(path, upto, cited_in, cfg):
    """Facts from the log, no judgment. Reads and Honcho calls count only since the
    last compaction, because earlier ones are no longer in the sender's context."""
    s = cfg["settings"]
    know = re.compile(s["knowledge_pattern"])
    go = re.compile(s["go_pattern"], re.I)
    reads, honcho, asked, words, inbox_ids = [], [], [], [], set()
    last_text, compactions, last_compact, n = "", 0, 0, 0
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if upto and n >= upto:
                n -= 1
                break
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t == "system" and o.get("subtype") == "compact_boundary":
                reads, honcho, asked, inbox_ids = [], [], [], set()
                compactions += 1
                last_compact = n
                for w in words:
                    w["before_compaction"] = True
            elif t == "assistant":
                for b in o["message"].get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b["text"].strip():
                        last_text = b["text"]
                        asked += [cut(q, 160) for q in b["text"].splitlines() if q.rstrip().endswith("?")][-2:]
                    elif b.get("type") == "tool_use":
                        name, inp = b["name"], b.get("input") or {}
                        cmd = inp.get("command", "")
                        if "honcho" in name and name.rsplit("__", 1)[-1] in ("honcho_remember", "chat", "search"):
                            q = inp.get("queries") or inp.get("query")
                            honcho.append(cut(" / ".join(q) if isinstance(q, list) else q, 200))
                        elif name == "Read":
                            reads.append(inp.get("file_path", ""))
                        elif name == "Skill":
                            reads.append("skill:" + str(inp.get("skill")))
                        elif name == "Bash":
                            q = RECALL_RE.search(cmd)
                            if q:
                                honcho.append(cut(q.group(1) or q.group(2), 200))
                            reads += read_paths(cmd, o.get("cwd"))
                        if ".inbox" in cmd or "FM_TASK_INBOX" in cmd or ".inbox" in str(inp.get("file_path", "")):
                            inbox_ids.add(b["id"])
            elif t == "user":
                c = o["message"].get("content")
                if (o.get("origin") or {}).get("kind") == "human":
                    said = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                    if not MACHINE_RE.match(said):
                        words.append({"line": n, "words": said, "answered": last_text, "via": "chat"})
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in inbox_ids:
                            for said in tool_results_text(b).split("[fm-from-firstmate]")[1:]:
                                said = re.sub(r"^\W*corr=\w+\s*", "", said.split("\n=== ")[0])
                                words.append({"line": n, "words": said, "answered": "", "via": "relayed by main"})
    seen = {}
    for p in reads:
        seen[p] = True
    all_reads = list(seen)
    kept = [p for p in all_reads if p.startswith("skill:") or (know.search(p) and not p.startswith("/tmp/"))]
    cited = sorted({c for c in CITED_RE.findall(cited_in)})
    def was_read(c):
        tail = re.sub(rf"^(~/|{re.escape(HOME)}/)", "", c)
        return any(r.endswith(tail) for r in all_reads)
    goes = [w for w in words if w["via"] != "chat" or go.search(w["words"])][-s["trace_goes"]:]
    return {
        "log_lines": n,
        "last_compaction_line": last_compact,
        "compactions": compactions,
        "honcho_asked": honcho[-s["trace_honcho"]:],
        "honcho_total": len(honcho),
        "read": [short_path(p) for p in kept[-s["trace_reads"]:]],
        "read_total": len(kept),
        "cited_unread": [c for c in cited if not was_read(c)],
        "asked_him": asked[-s["trace_asked"]:],
        "his_go": [
            {
                "line": w["line"],
                "via": w["via"],
                "before_compaction": w.get("before_compaction", False),
                "words": cut(w["words"], s["trace_chars"]),
                "answered": cut(w["answered"][-s["trace_chars"]:], s["trace_chars"]),
            }
            for w in goes
        ],
    }


def noul(instructions, true, false):
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


def locator(instructions, keys, none_text):
    # The expected answer stays last because these models lean to the first option.
    return {"type": "choice", "instructions": instructions, "criteria": {**{k: None for k in keys}, "none": none_text}}


def build_questions(cfg, blocks, points):
    qs, meta = {}, {}
    for q in cfg["question"]:
        qs[q["id"]] = noul(q["instructions"], q["true"], q["false"])
        meta[q["id"]] = {"dim": q["dim"], "flag_on": q["flag_on"], "label": q["flag"], "point": None,
                         "locates": bool(q.get("where"))}
        if q.get("where"):
            qs[q["id"] + "__where"] = locator(q["where"], blocks, "No block of `text`")
        if q.get("behind") and points:
            qs[q["id"] + "__behind"] = locator(q["behind"], points, "No point in `points`")
    e = cfg["expectation"]
    for key, point in points.items():
        qid = "x_" + key
        qs[qid] = noul(e["instructions"].replace("{p}", key), e["true"], e["false"])
        qs[qid + "__where"] = locator(e["where"].replace("{p}", key), blocks, "No block of `text`")
        meta[qid] = {"dim": "expectation", "flag_on": e["flag_on"], "label": point, "point": key, "locates": True}
    return qs, meta


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would otherwise carry the Authorization header to the redirect's host."""

    def redirect_request(self, *args, **kwargs):
        return None


def post_jev(cfg, body, key, limit):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "firstmate-brief-check"}
    req = urllib.request.Request(cfg["settings"]["endpoint"], data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=limit) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"jev HTTP {e.code}: {e.read().decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"jev request failed: {e}")


def read_flags(cfg, answers, qs, meta, blocks, points):
    s = cfg["settings"]
    missing = [q for q in qs if q not in answers]
    if missing:
        raise RuntimeError(f"jev reply is missing {len(missing)} answers: {missing[:5]}")
    flags, near = [], []
    for qid, m in meta.items():
        p = answers[qid]["noul"]
        strength = p if m["flag_on"] else 1 - p
        if strength < s["near_floor"]:
            continue
        if strength < s["flag_floor"]:
            near.append((qid, strength))
            continue
        flag = {"id": qid, "dim": m["dim"], "label": m["label"], "strength": round(strength, 2), "line": None, "behind": None,
                "locates": m["locates"]}
        where = answers.get(qid + "__where")
        if where and where["choice"] != "none" and where["probabilities"][where["choice"]] >= s["locate_floor"]:
            flag["line"] = (where["choice"], blocks[where["choice"]])
        if m["point"]:
            flag["behind"] = (m["point"], points[m["point"]])
        else:
            bh = answers.get(qid + "__behind")
            if bh and bh["choice"] != "none" and bh["probabilities"][bh["choice"]] >= s["locate_floor"]:
                flag["behind"] = (bh["choice"], points[bh["choice"]])
        flags.append(flag)
    flags.sort(key=lambda f: -f["strength"])
    return flags, near


def render(cfg, run):
    s = cfg["settings"]
    # A question that can name a block and names none is a false flag in nearly every reviewed run.
    flags = [f for f in run["flags"] if f["line"] or not f["locates"]]
    tr = run["trace"]
    w = s["width"]
    out = [
        f"brief-check {run['label']}: {len(flags)} flags of {len(run['meta'])} questions"
        f" | jev {run['jev_seconds']}s, {run['input_tokens']} tokens, {len(run['questions'])} asks"
        f" | honcho {run['honcho_seconds']}s ({run['honcho_level']})"
    ]
    for i, f in enumerate(flags[: s["max_flags_shown"]], 1):
        out.append(cut(f"{i}. {f['strength']:.2f} {f['dim']}: {f['label']}", w))
        out.append("   " + cut(f"{f['line'][0]}: {f['line'][1]}", w - 3) if f["line"] else "   line: not located")
        if f["behind"] and f["dim"] != "expectation":
            out.append("   " + cut(f"behind {f['behind'][0]}: {f['behind'][1]}", w - 3))
    hidden = len(run["flags"]) - min(len(flags), s["max_flags_shown"])
    if hidden:
        out.append(f"+{hidden} more in the log, {len(run['flags']) - len(flags)} of them with no line")
    if run["near"]:
        out.append(cut("Below the floor: " + ", ".join(f"{q} {v:.2f}" for q, v in run["near"]), w))
    if run["points_dropped"]:
        out.append(f"Honcho points dropped over the cap: {run['points_dropped']}")
    out.append("Sender since last compaction:")
    last_q = f", last: {tr['honcho_asked'][-1]}" if tr["honcho_asked"] else ""
    out.append("  " + cut(f"Honcho asked {tr['honcho_total']}{last_q}", w - 2))
    out.append("  " + cut(f"Read {tr['read_total']} knowledge files. Cited, no read seen: {', '.join(tr['cited_unread']) or 'none'}", w - 2))
    if tr["his_go"]:
        g = tr["his_go"][-1]
        when = " before compaction" if g["before_compaction"] else ""
        out.append("  " + cut(f"Latest go-like word, line {g['line']}{when}: {g['words']}", w - 2))
        if g["answered"]:
            out.append("  " + cut(f"It answered: {g['answered'][-(w - 16):]}", w - 2))
    else:
        out.append("  No go-like word from him in the log.")
    out.append(f"Log: {short_path(run['log_path'])}")
    return "\n".join(out)


def run_check(a, home):
    """One send's check. Returns the screen text."""
    import tomllib  # Python 3.11 or later. Below that, the send still gets its one skip line.

    cfg = tomllib.loads(a.questions.read_text())
    text = sys.stdin.read() if str(a.text) == "-" else a.text.read_text()
    key = api_key(home)
    if not key:
        return "brief-check: skipped: no OPENROUTER_API_KEY"

    def clean(s):
        return s.replace(key, "[key]")

    text = clean(text)
    session = a.session or find_session()
    if not session or not session.is_file():
        return "brief-check: skipped: no session log"
    if a.ask:
        ask = a.ask.read_text().strip()
    else:
        ask = "\n\n".join(filter(None, [intent_section(text), *last_typed(session, a.upto_line)]))
    ask = clean(ask)
    if not ask:
        return "brief-check: skipped: no ask found"

    numbered = task_lines(text)
    blocks = blocks_from(numbered, cfg["settings"]["max_blocks"])
    level = a.honcho_level or cfg["honcho"]["level"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = home / "state" / "brief-check" / f"{stamp}-{a.label}.json"
    run = {"label": a.label, "stamp": stamp, "log_path": str(log_path), "text_file": str(a.text), "to": a.to,
           "ask": ask, "session": str(session), "upto_line": a.upto_line, "honcho_level": level}

    def finish():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(clean(json.dumps(run, indent=1, ensure_ascii=False)))

    begun = time.monotonic()

    def time_left():
        return max(1, cfg["settings"]["time_limit_seconds"] - (time.monotonic() - begun))

    try:
        t0 = time.monotonic()
        if a.honcho_answer:
            answer, query = a.honcho_answer.read_text(), None
        else:
            answer, query = ask_honcho(cfg, ask, level, time_left())
        run.update(honcho_seconds=round(time.monotonic() - t0, 1), honcho_query=query, honcho_answer=answer)
        points, dropped = points_from(answer, cfg["settings"]["max_points"])
        run.update(points=points, points_dropped=dropped)

        t0 = time.monotonic()
        trace = trace_session(session, a.upto_line, "\n".join(blocks.values()), cfg)
        run.update(trace=trace, trace_seconds=round(time.monotonic() - t0, 1))

        state = {"ask": ask, "text": blocks, "points": points,
                 "trace": {k: trace[k] for k in ("honcho_asked", "read", "cited_unread", "asked_him", "his_go")}}
        qs, meta = build_questions(cfg, blocks, points)
        request = {"model": cfg["settings"]["model"], "state": state, "questions": qs}
        body = clean(json.dumps(request, ensure_ascii=False)).encode()
        run.update(request=request, request_sha256=hashlib.sha256(body).hexdigest(), questions=list(qs), meta=meta)

        t0 = time.monotonic()
        reply = post_jev(cfg, body, key, time_left())
        run.update(jev_seconds=round(time.monotonic() - t0, 1), reply=reply,
                   input_tokens=reply.get("usage", {}).get("input_tokens"))
        flags, near = read_flags(cfg, reply["answers"], qs, meta, blocks, points)
        run.update(flags=flags, near=near)
    except Exception as e:
        run["error"] = f"{type(e).__name__}: {e}"
        finish()
        return "brief-check: skipped: " + cut(clean(run["error"]), 200)
    finish()
    return clean(render(cfg, run))


def main():
    ap = argparse.ArgumentParser(description="Warn as a send goes out. Blocks nothing.")
    ap.add_argument("--text", required=True, type=Path, help="the text being sent, or - for stdin")
    ap.add_argument("--ask", type=Path, help="his ask, in his words; default: the text's intent section and his last typed messages")
    ap.add_argument("--session", type=Path, help="the sender's Claude session log; default: found from CLAUDE_CODE_SESSION_ID")
    ap.add_argument("--to", help="who receives it, for the log: a mate name or a worker id")
    ap.add_argument("--honcho-level", choices=["medium", "high"], help="high for a cross-cutting question")
    ap.add_argument("--honcho-answer", type=Path, help="reuse this saved answer instead of calling Honcho")
    ap.add_argument("--upto-line", type=int, help="read the log only before this line, for a recorded send")
    ap.add_argument("--label", default="send")
    ap.add_argument("--questions", type=Path, default=HERE / "fm-brief-check-questions.toml")
    a = ap.parse_args()
    try:
        print(run_check(a, Path(os.environ["FM_HOME"])))
    except Exception as e:
        print(f"brief-check: skipped: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
