#!/usr/bin/env bash
# Runs the python/ project's own unittest suite in its locked uv environment.
# The Python tests own their behavior. This script only connects them to
# bin/fm-test-run.sh, so it skips while python/tests holds no test modules.
# It does not source tests/lib.sh: that library exports bash-test settings
# such as FM_TEST_SEAM, which would leak into the Python tests' environment.
set -u

python_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/python"

if [ -z "$(find "$python_dir/tests" -name 'test*.py' -type f -print 2>/dev/null)" ]; then
  printf 'skip: no Python tests under python/tests\n'
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  printf 'not ok - uv not found; install the uv version that python/pyproject.toml requires\n' >&2
  exit 1
fi
if ! uv run --locked --directory "$python_dir" python -m unittest discover -s tests; then
  printf 'not ok - the python/ unittest suite failed\n' >&2
  exit 1
fi
printf 'ok - the python/ unittest suite passed\n'
