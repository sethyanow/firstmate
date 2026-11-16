#!/usr/bin/env python3
"""Collect saved brief memory: run, gate, or feedback (see --help)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))
from firstmate.memory_check import main

if __name__ == "__main__":
    sys.exit(main())
