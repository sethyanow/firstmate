#!/usr/bin/env bash
# tests/fm-spawn-agent-telemetry.test.sh - config/agent-telemetry opts ship and
# scout workers on claude, codex, and omp into OpenTelemetry export, and nothing
# else changes.
#
# The assertions drive the real spawn against a fake pane and a real isolated
# git worktree, then EXECUTE the launch command the pane received with the
# harness binary replaced by a probe that prints its environment and arguments.
# The pane environment carries content-capture switches turned on, so a launch
# that merely inherited the ambient environment would be caught.
set -u

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"
# shellcheck source=tests/secondmate-helpers.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/secondmate-helpers.sh"

TMP_ROOT=$(fm_test_tmproot fm-spawn-agent-telemetry)

PROBED="CLAUDE_CODE_ENABLE_TELEMETRY OTEL_METRICS_INCLUDE_ACCOUNT_UUID OTEL_METRICS_INCLUDE_SESSION_ID OTEL_METRICS_EXPORTER OTEL_LOGS_EXPORTER \
OTEL_TRACES_EXPORTER OTEL_EXPORTER_OTLP_PROTOCOL OTEL_EXPORTER_OTLP_ENDPOINT \
OTEL_RESOURCE_ATTRIBUTES CLAUDE_CODE_ENHANCED_TELEMETRY_BETA OTEL_LOG_USER_PROMPTS \
OTEL_LOG_ASSISTANT_RESPONSES OTEL_LOG_TOOL_DETAILS OTEL_LOG_TOOL_CONTENT \
OTEL_LOG_RAW_API_BODIES OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

# make_case <name> <harness> <id> -> sets CASE_DIR HOME_DIR PROJ_DIR WT_DIR FAKEBIN_DIR LAUNCH_LOG PANE_LOG
make_case() {
  local name=$1 harness=$2 id=$3
  CASE_DIR="$TMP_ROOT/$name"
  HOME_DIR="$CASE_DIR/home"
  PROJ_DIR="$CASE_DIR/project"
  WT_DIR="$CASE_DIR/wt"
  LAUNCH_LOG="$CASE_DIR/launch.log"
  PANE_LOG="$CASE_DIR/pane.log"
  FAKEBIN_DIR=$(fm_test_make_spawn_fakebin "$CASE_DIR/fake" claude)
  # omp needs a model catalog to resolve before launch.
  cat > "$FAKEBIN_DIR/omp" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" != models ] || printf '%s\n' '{"models":[{"provider":"openai-codex","id":"gpt-6-astra","selector":"openai-codex/gpt-6-astra"}]}'
exit 0
SH
  chmod +x "$FAKEBIN_DIR/omp"
  fm_test_spawn_home "$HOME_DIR" "$harness"
  fm_git_worktree "$PROJ_DIR" "$WT_DIR" "wt-$name"
  fm_test_spawn_brief "$HOME_DIR" "$id"
  : > "$LAUNCH_LOG"
  : > "$PANE_LOG"
}

run_spawn() {
  FM_FAKE_LAUNCH_LOG="$LAUNCH_LOG" FM_FAKE_PANE_LOG="$PANE_LOG" \
    fm_test_run_spawn "$HOME_DIR" "$WT_DIR" "$FAKEBIN_DIR" "$@"
}

# Replace the harness binary with a probe and run the emitted launch in a
# synthetic pane whose environment turns every content switch on.
probe_launch() {  # <harness>
  local name
  # shellcheck disable=SC2016 # The generated probe expands its own environment and arguments.
  {
    printf '#!/bin/sh\n'
    for name in $PROBED; do
      printf 'printf "%%s\\n" "%s=${%s-unset}"\n' "$name" "$name"
    done
    printf 'for a in "$@"; do printf "arg=%%s\\n" "$a"; done\n'
  } > "$FAKEBIN_DIR/$1"
  chmod +x "$FAKEBIN_DIR/$1"
  env -i HOME="$TMP_ROOT/pane-home" PATH="$FAKEBIN_DIR:$PATH" TERM=xterm TMUX=synthetic-pane \
    OTEL_LOG_USER_PROMPTS=1 OTEL_LOG_TOOL_DETAILS=1 OTEL_LOG_TOOL_CONTENT=1 \
    OTEL_LOG_ASSISTANT_RESPONSES=1 OTEL_LOG_RAW_API_BODIES=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true \
    /bin/sh -c "$(grep '^export ' "$PANE_LOG")
$(cat "$LAUNCH_LOG")"
}

assert_exports_metadata_only() {  # <probe-output> <id> <label>
  local seen=$1 id=$2 label=$3 name want
  for want in CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false OTEL_METRICS_INCLUDE_SESSION_ID=true OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp \
    OTEL_TRACES_EXPORTER=none OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 "OTEL_RESOURCE_ATTRIBUTES=fm.task=$id"; do
    assert_contains "$seen" "$want" "$label: the worker should start with $want"
  done
  for name in CLAUDE_CODE_ENHANCED_TELEMETRY_BETA OTEL_LOG_USER_PROMPTS OTEL_LOG_ASSISTANT_RESPONSES \
    OTEL_LOG_TOOL_DETAILS OTEL_LOG_TOOL_CONTENT OTEL_LOG_RAW_API_BODIES \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT; do
    printf '%s\n' "$seen" | grep -qx "$name=" \
      || fail "$label: $name must be empty so the pane's content switch cannot apply"$'\n'"$seen"
  done
}

test_absent_flag_changes_nothing() {
  local id=tel-off-a1 out seen
  make_case off codex "$id"
  out=$(run_spawn "$id" "$PROJ_DIR" --mode no-mistakes --yolo off)
  expect_code 0 $? "spawn without the flag should succeed: $out"
  seen=$(probe_launch codex) || fail "flag absent: the emitted launch failed to run"
  assert_contains "$seen" "OTEL_RESOURCE_ATTRIBUTES=unset" "flag absent: no task label may be exported"
  assert_contains "$seen" "OTEL_LOG_USER_PROMPTS=1" "flag absent: the ambient environment must pass through unchanged"
  assert_not_contains "$seen" "arg=otel." "flag absent: codex must receive no otel overrides"
  pass "without config/agent-telemetry a worker launch exports no telemetry"
}

test_claude_and_omp_export_through_the_allowlist() {
  local harness id out seen
  for harness in claude omp; do
    id="tel-$harness-a1"
    make_case "$harness" "$harness" "$id"
    : > "$HOME_DIR/config/agent-telemetry"
    : > "$HOME_DIR/config/launch-env-allowlist"
    if [ "$harness" = omp ]; then
      out=$(run_spawn "$id" "$PROJ_DIR" --scout --harness omp --model openai-codex/gpt-6-astra)
    else
      out=$(run_spawn "$id" "$PROJ_DIR" --mode no-mistakes --yolo off)
    fi
    expect_code 0 $? "$harness spawn with the flag should succeed: $out"
    assert_contains "$(cat "$LAUNCH_LOG")" '/usr/bin/env -i' "$harness: the allowlist should clear the environment"
    seen=$(probe_launch "$harness") || fail "$harness: the emitted launch failed to run"
    assert_exports_metadata_only "$seen" "$id" "$harness"
  done
  pass "claude and omp workers export metadata-only telemetry with the task label, even under a cleared environment"
}

test_codex_receives_config_overrides() {
  local id=tel-codex-a1 out seen
  make_case codex codex "$id"
  : > "$HOME_DIR/config/agent-telemetry"
  out=$(run_spawn "$id" "$PROJ_DIR" --mode no-mistakes --yolo off)
  expect_code 0 $? "codex spawn with the flag should succeed: $out"
  seen=$(probe_launch codex) || fail "codex: the emitted launch failed to run"
  assert_exports_metadata_only "$seen" "$id" codex
  for want in \
    'arg=otel.exporter={otlp-http={endpoint="http://127.0.0.1:4318/v1/logs",protocol="binary"}}' \
    'arg=otel.metrics_exporter={otlp-http={endpoint="http://127.0.0.1:4318/v1/metrics",protocol="binary"}}' \
    'arg=otel.trace_exporter="none"' arg=otel.log_user_prompt=false arg=otel.log_agent_responses=false; do
    printf '%s\n' "$seen" | grep -qxF "$want" \
      || fail "codex: expected the single argument ${want#arg=}"$'\n'"$seen"
  done
  pass "a codex worker receives its exporters as -c overrides, each one argument"
}

test_other_kinds_and_harnesses_are_unchanged() {
  local id=tel-grok-a1 out seen sm
  make_case grok grok "$id"
  : > "$HOME_DIR/config/agent-telemetry"
  out=$(run_spawn "$id" "$PROJ_DIR" --mode no-mistakes --yolo off)
  expect_code 0 $? "grok spawn with the flag should succeed: $out"
  seen=$(probe_launch grok) || fail "grok: the emitted launch failed to run"
  assert_contains "$seen" "OTEL_RESOURCE_ATTRIBUTES=unset" "grok: an unsupported harness must not be labelled"

  make_case secondmate claude sm-tel
  : > "$HOME_DIR/config/agent-telemetry"
  sm="$CASE_DIR/secondmate-home"
  seed_secondmate_home_marker "$sm" sm-tel
  printf 'charter for sm-tel\n' > "$sm/data/charter.md"
  printf '%s\n' 'projects/' 'state/' 'data/' 'config/' '.no-mistakes/' > "$sm/.gitignore"
  git -C "$sm" init -q -b main
  out=$(run_spawn sm-tel "$sm" --secondmate)
  expect_code 0 $? "secondmate spawn with the flag should succeed: $out"
  seen=$(probe_launch claude) || fail "secondmate: the emitted launch failed to run"
  assert_contains "$seen" "OTEL_RESOURCE_ATTRIBUTES=unset" "secondmate: a secondmate launch must stay unchanged"
  assert_not_contains "$seen" "CLAUDE_CODE_ENABLE_TELEMETRY=1" "secondmate: a secondmate launch must stay unchanged"
  pass "other harnesses and secondmate launches stay unchanged with the flag on"
}

test_absent_flag_changes_nothing
test_claude_and_omp_export_through_the_allowlist
test_codex_receives_config_overrides
test_other_kinds_and_harnesses_are_unchanged
