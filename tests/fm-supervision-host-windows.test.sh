#!/usr/bin/env bash
# Native-Windows behavior tests for the supervision host's process lifecycle
# (bin/fm-supervision-host.sh, python/src/firstmate/bashlib.py): start, a wake,
# a hand-back, a stop, and recovery from a host that died without its cleanup.
#
# The host's interpreter is a Windows process there, and every process the
# bash libraries name is a Git Bash process. The session lock is a native
# Windows harness, as on a real Windows home: a copy of node named claude.exe,
# published through CLAUDE_PID. The engine is the stub the other host suites
# use (tests/supervision-host-helpers.sh).
# shellcheck disable=SC2016 # single-quoted scripts expand inside their own shells
set -u

if [ "$(node -p 'process.platform' 2>/dev/null)" != win32 ]; then
  echo "skip: native Windows Node required"
  exit 0
fi

# shellcheck source=tests/supervision-host-helpers.sh
. "$(dirname "${BASH_SOURCE[0]}")/supervision-host-helpers.sh"

# The bash locks are symlinks, and Git Bash copies a directory for `ln -s`
# unless MSYS asks for native links.
mkdir -p "$TMP_ROOT/link-probe/target"
ln -s target "$TMP_ROOT/link-probe/link" 2>/dev/null
[ -L "$TMP_ROOT/link-probe/link" ] \
  || fail "fixture: ln -s makes no symlink here; run with MSYS=winsymlinks:nativestrict"

# The first run of the host builds its interpreter's environment, which takes
# longer than any case waits for a watcher.
"$HOST" --help >/dev/null || fail "fixture: the host's interpreter could not be prepared"

# Start the home's native harness and make it the session-lock holder. Prints
# its Windows pid.
start_native_harness() {  # <home>
  local home=$1
  mkdir -p "$home/harness"
  cp "$(command -v node)" "$home/harness/claude.exe"
  # Git Bash's /proc names its own stub for a native program, so the harness
  # writes its Windows pid itself.
  "$home/harness/claude.exe" -e '
    require("fs").writeFileSync(process.argv[1], process.pid + "\n");
    setInterval(() => {}, 1 << 30);
  ' "$(cygpath -w "$home/harness/winpid")" >/dev/null 2>&1 </dev/null &
  printf '%s\n' "$!" >> "$home/claude-pids"
  wait_until_deadline 30 test -s "$home/harness/winpid" || fail "fixture: the native harness never started"
  printf 'win:%s\n' "$(cat "$home/harness/winpid")" > "$home/state/.lock"
  cat "$home/harness/winpid"
}

# Run the host as the harness's hook would, with the harness's published pid.
start_windows_host() {  # <home> <harness winpid>
  env -u CLAUDE_PID -u CLAUDE_CODE_SESSION_ID \
    FM_HOME="$1" FM_CREW_STATE_BIN="$1/fakebin/fm-crew-state.sh" PATH="$1/fakebin:$PATH" \
    CLAUDE_PID="$2" CLAUDE_CODE_SESSION_ID=windows-session \
    bash -c 'rm -f "$FM_HOME/host.rc"; "$0" park > "$FM_HOME/host.out" 2>&1; printf "%s\n" "$?" > "$FM_HOME/host.rc"' \
    "$HOST" 2>> "$1/claude.err" &
}

recorded() {  # <home> <role>
  awk -F '\t' -v role="$2" '$1 == role { print $2 }' "$1/state/.supervision-host" 2>/dev/null
}

# What a failed case shows: the host's output, errors, and ledger.
host_story() {  # <home>
  cat "$1/host.out" "$1/claude.err" "$1/state/.supervision-host.log" 2>/dev/null
}

watcher_gone() { ! watcher_live "$1"; }

none_alive() {  # <pid>...
  local pid
  for pid in "$@"; do
    ! kill -0 "$pid" 2>/dev/null || return 1
  done
}

test_host_handles_a_wake_and_a_term_stops_it_through_its_cleanup() {
  local home harness host arms watcher
  home=$(make_home windows-stop away)
  echo hold-lease > "$home/stub-mode"
  harness=$(start_native_harness "$home")
  start_windows_host "$home" "$harness"
  wait_until_deadline 90 watcher_live "$home" \
    || fail "start: the host never started a watcher cycle: $(host_story "$home")"
  wait_until_deadline 30 grep -Eq '^watcher: (started|attached) ' "$home/host.out" \
    || fail "start: the host must stream the first cycle's status line: $(host_story "$home")"
  host=$(recorded "$home" host)
  kill -0 "$host" 2>/dev/null || fail "start: the recorded host pid $host is not a live Git Bash pid"

  append_status "$home" 'step one'
  wait_until_deadline 180 handled_at_least "$home" 1 \
    || fail "wake: the engine did not handle the wake: $(host_story "$home")"
  assert_re "^holder=win:$harness\$" "$home/engine-call.1" "wake: the engine's lease holder must be the native harness"
  assert_re "^branch win:$harness [0-9]+ live\$" "$home/engine-lease.log" "wake: the engine's lease must name the native harness"
  host_exited "$home" && fail "wake: a handled away wake must leave the host parked: $(host_story "$home")"

  arms=$(recorded "$home" arm)
  watcher=$(cat "$home/state/.watch.lock/pid")
  # shellcheck disable=SC2086 # one pid per word
  none_alive $arms && fail "stop: fixture error: no recorded arm is running, so this case proves nothing"
  kill -TERM "$host"
  wait_until_deadline 120 host_exited "$home" || fail "stop: the host did not stop on TERM"
  [ "$(cat "$home/host.rc")" = 143 ] || fail "stop: a TERM must end the host with 143, got $(cat "$home/host.rc")"
  # shellcheck disable=SC2086 # one pid per word
  wait_until_deadline 60 none_alive $arms "$watcher" \
    || fail "stop: the host's cleanup left an arm or its watcher running"
  [ ! -e "$home/state/.supervision-host" ] || fail "stop: the cleanup left the host record behind"
  FM_HOME="$home" "$LEASE" check demo 2>/dev/null | grep -q '^branch ' \
    && fail "stop: the cleanup left the engine's branch lease held"
  pass "windows: a host handles a wake, and a TERM stops it through its cleanup"
}

test_host_hands_a_wake_to_main_and_stops_its_successor() {
  local home harness
  home=$(make_home windows-handback away)
  echo noreport > "$home/stub-mode"
  harness=$(start_native_harness "$home")
  start_windows_host "$home" "$harness"
  wait_until_deadline 90 watcher_live "$home" || fail "hand-back: the host never started a watcher cycle: $(host_story "$home")"
  append_status "$home" 'needs main'
  wait_until_deadline 120 host_exited "$home" || fail "hand-back: the host never handed the wake to main"
  [ "$(cat "$home/host.rc")" = 0 ] || fail "hand-back: the host must exit 0, got $(cat "$home/host.rc"): $(cat "$home/host.out")"
  assert_re '^signal: .*demo.status' "$home/host.out" "hand-back: main must get the close"
  assert_re '^supervision-host: ' "$home/host.out" "hand-back: main must be told why it has the wake"
  wait_until_deadline 60 watcher_gone "$home" \
    || fail "hand-back: the successor cycle's watcher still runs after the hand-back: $(host_story "$home"; ps -ef)"
  [ ! -e "$home/state/.supervision-host" ] || fail "hand-back: the host record was left behind"
  pass "windows: a host hands a wake to main and stops its successor cycle"
}

test_restarted_host_stops_what_a_host_that_died_left() {
  local home harness host launcher interpreter arm watcher
  home=$(make_home windows-recovery away)
  harness=$(start_native_harness "$home")
  start_windows_host "$home" "$harness"
  wait_until_deadline 90 watcher_live "$home" || fail "recovery: the host never started a watcher cycle: $(host_story "$home")"
  host=$(recorded "$home" host)
  arm=$(recorded "$home" arm)
  watcher=$(cat "$home/state/.watch.lock/pid")
  # The interpreter dies at once, with no cleanup: the shim survives it only
  # long enough to report its status. The shim's child is the venv launcher,
  # whose own Windows child is the interpreter. Ending the launcher instead
  # would end the host's children with it, through the launcher's job.
  launcher=$(ps -ef | awk -v shim="$host" '$3 == shim { print $2; exit }')
  launcher=$(cat "/proc/$launcher/winpid" 2>/dev/null || true)
  [ -n "$launcher" ] || fail "recovery: fixture error: the host's interpreter launcher was not found"
  interpreter=$(powershell.exe -NoProfile -Command \
    "(Get-CimInstance Win32_Process -Filter 'ParentProcessId=$launcher').ProcessId" | tr -d '\r')
  [ -n "$interpreter" ] || fail "recovery: fixture error: the host's interpreter was not found"
  taskkill //F //PID "$interpreter" >/dev/null
  wait_until_deadline 60 host_exited "$home" || fail "recovery: the shim outlived its interpreter"
  kill -0 "$arm" 2>/dev/null || fail "recovery: fixture error: the arm died with its host, so this case proves nothing"
  [ -n "$(recorded "$home" host)" ] || fail "recovery: fixture error: the dead host's record is gone"

  start_windows_host "$home" "$harness"
  wait_until_deadline 120 none_alive "$arm" "$watcher" \
    || fail "recovery: a restarted host left its dead predecessor's arm or watcher running"
  wait_until_deadline 90 watcher_live "$home" || fail "recovery: the restarted host never started its own cycle"
  [ "$(recorded "$home" host)" != "$host" ] || fail "recovery: the restarted host did not take the record"
  kill -TERM "$(recorded "$home" host)"
  wait_until_deadline 120 host_exited "$home" || fail "recovery: the restarted host did not stop on TERM"
  pass "windows: a restarted host stops, by recorded identity, the cycle a dead host left running"
}

fm_test_run \
  test_host_handles_a_wake_and_a_term_stops_it_through_its_cleanup \
  test_host_hands_a_wake_to_main_and_stops_its_successor \
  test_restarted_host_stops_what_a_host_that_died_left
