#!/usr/bin/env python3
"""Ask memory questions or collect saved brief memory (see --help)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))
from firstmate.memory_check import main

if __name__ == "__main__":
    sys.exit(main())
