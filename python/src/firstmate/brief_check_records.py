"""Brief-check flags and their append-only dispositions."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .memory_check import CHECK_ID, task_directory, write_json


def save_flags(home: Path, task: str, run_record: dict[str, Any]) -> str:
    run_id = run_record["run_id"]
    path = task_directory(home, task) / "brief-check" / f"{run_id}.json"
    write_json(
        path,
        {
            "run_id": run_id,
            "task": task,
            "log_path": run_record["log_path"],
            "memory_check_id": run_record.get("memory_check_id"),
            "flags": run_record["flags"],
        },
    )
    return str(path)


def feedback(home: Path, args: argparse.Namespace) -> None:
    folder = task_directory(home, args.task)
    if not CHECK_ID.fullmatch(args.run):
        raise ValueError("invalid brief-check run id")
    run_record = json.loads((folder / "brief-check" / f"{args.run}.json").read_text())
    if run_record["task"] != args.task or run_record["run_id"] != args.run:
        raise ValueError("brief-check run belongs to another task")
    if args.flag not in {flag["id"] for flag in run_record["flags"]}:
        raise ValueError("unknown flag in this brief-check run")
    if not args.reason.strip():
        raise ValueError("feedback needs a reason")
    record = {
        "run_id": args.run,
        "flag_id": args.flag,
        "disposition": args.disposition,
        "reason": args.reason,
        "at": datetime.now(UTC).isoformat(),
    }
    with (folder / "brief-check-feedback.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def feedback_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Record a brief-check flag disposition.")
    parser.add_argument("task")
    parser.add_argument("--run", required=True)
    parser.add_argument("--flag", required=True)
    parser.add_argument("--disposition", choices=["acted-on", "dropped"], required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        feedback(Path(os.environ["FM_HOME"]), args)
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"brief-check feedback: {error}\n")
        return 1
    return 0
