#!/usr/bin/env bash
# Real Codex TUI lifecycle using fm-spawn's launch command and rollout binding.
# Spends two short turns in one scratch session. Opt in after Codex upgrades.
set -u
# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"
# shellcheck source=/dev/null
. "$ROOT/bin/fm-busy-lib.sh"
fm_live_gate opt-in FM_CODEX_BUSY_LIVE codex tmux python3

VERSION=$(codex --version)
TMP_ROOT=$(fm_test_tmproot fm-codex-busy-live)
SOCKET="fm-codex-busy-$$"
CODEX_DATA="$TMP_ROOT/codex"
mkdir -p "$CODEX_DATA"
[ ! -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ] ||
  ln -s "${CODEX_HOME:-$HOME/.codex}/auth.json" "$CODEX_DATA/auth.json"
printf 'model="%s"\nmodel_reasoning_effort="low"\n' \
  "${FM_CODEX_BUSY_LIVE_MODEL:-gpt-6.1-sol}" > "$CODEX_DATA/config.toml"

cleanup() {
  tmux -L "$SOCKET" kill-server 2>/dev/null || true
}
trap cleanup EXIT
HOME_DIR="$TMP_ROOT/home"
WT="$TMP_ROOT/wt"
PROJ="$TMP_ROOT/project"
ID=codex-busy
fm_test_spawn_home "$HOME_DIR" codex
fm_test_spawn_brief "$HOME_DIR" "$ID" \
  'Run sleep 10 with your shell tool, then reply exactly BUSY_PROBE_DONE. Do nothing else.'
fm_git_worktree "$PROJ" "$WT" live
FAKEBIN=$(fm_test_make_spawn_fakebin "$TMP_ROOT/fake")
CODEX_HOME="$CODEX_DATA" FM_FAKE_LAUNCH_LOG="$TMP_ROOT/launch" \
  fm_test_run_spawn "$HOME_DIR" "$WT" "$FAKEBIN" "$ID" "$PROJ" \
    --mode direct-PR --yolo off > "$TMP_ROOT/spawn.log" 2>&1 ||
  fail "$VERSION: could not build the worker launch: $(cat "$TMP_ROOT/spawn.log")"
tmux -L "$SOCKET" new-session -d -s probe -x 140 -y 40 -c "$WT" \
  "bash '$TMP_ROOT/launch'" || fail "$VERSION: launch failed"

await_state() { # <expected> <phase>
  local expected=$1 phase=$2 verdict screen attempt
  for ((attempt=0; attempt<180; attempt++)); do
    verdict=$(fm_busy_classify tmux probe codex "$ID" "$HOME_DIR/state")
    if [ "$verdict" = "$expected codex-rollout" ]; then
      printf 'ok - %s: %s -> %s\n' "$VERSION" "$phase" "$verdict"
      return 0
    fi
    screen=$(tmux -L "$SOCKET" capture-pane -p -t probe 2>/dev/null) ||
      fail "$VERSION: worker exited during $phase"
    case "$screen" in
      *'Trust this folder?'*|*'Do you trust the contents of this directory?'*)
        tmux -L "$SOCKET" send-keys -t probe Enter ;;
    esac
    sleep 0.5
  done
  fail "$VERSION: $phase expected $expected, got $verdict; $screen"
}

await_state busy 'running turn'
await_state idle 'completed turn'
tmux -L "$SOCKET" send-keys -t probe -l \
  'Run sleep 60 with your shell tool, then reply DONE. Do nothing else.'
# Codex must render pasted input before Enter can submit it.
sleep 1
tmux -L "$SOCKET" send-keys -t probe Enter
await_state busy 'second turn'
tmux -L "$SOCKET" send-keys -t probe Escape
await_state idle 'interrupted turn'
printf '# Codex lifecycle verified with %s\n' "$VERSION"
