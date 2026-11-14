#!/usr/bin/env bash
# The Claude Code memory mod (.claude/mods/firstmate-memory) under the real installed
# Claude Code: the repository tracks no auto-load link to it, `claude plugin validate
# --strict` accepts it, and its own `claude plugin test` suites pass. No model turn is
# submitted and no credential is spent, so the guard runs by default wherever `claude`
# is installed. bin/fm-session-start.sh's side of the contract is covered in
# tests/fm-session-start.test.sh.
#
# The early-access function-hooks surface is default-off; the flag is set on this
# test's own processes only and never written into any settings file.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

fm_live_gate default-on FM_CLAUDE_MEMORY_PLUGIN_TEST claude

MOD="$ROOT/.claude/mods/firstmate-memory"
CLAUDE_VERSION=$(claude --version 2>/dev/null || true)
[ -n "$CLAUDE_VERSION" ] || fail "claude is installed but reports no version"
TMP_ROOT=$(fm_test_tmproot fm-memory-claude-mod-plugin)

# Discovery is enabled in local settings, not through an always-loaded link.
test_no_tracked_autoload_link() {
  local tracked
  tracked=$(git -C "$ROOT" ls-files -- .agents/skills/firstmate-memory .claude/skills/firstmate-memory)
  [ -z "$tracked" ] || fail "the repository tracks an auto-load link to the memory mod: $tracked"
  pass "the memory mod has no tracked auto-load link"
}

test_validate_strict() {
  local report
  if ! report=$(CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin validate --strict "$ROOT/.claude/mods" 2>&1); then
    printf '%s\n' "$report" >&2
    fail "Claude Code $CLAUDE_VERSION refused the local memory marketplace"
  fi
  if ! report=$(CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin validate --strict "$MOD" 2>&1); then
    printf '%s\n' "$report" >&2
    fail "Claude Code $CLAUDE_VERSION refused the memory mod under strict validation"
  fi
  assert_contains "$report" "hooks: prompt.compose" "Claude Code $CLAUDE_VERSION does not scan the memory mod as hooking prompt.compose"
  pass "Claude Code $CLAUDE_VERSION validates the memory mod strictly, hooking prompt.compose"
}

test_plugin_suites() {
  local report
  if ! report=$(cd "$TMP_ROOT" && CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin test "$MOD" 2>&1); then
    printf '%s\n' "$report" >&2
    fail "Claude Code $CLAUDE_VERSION failed the memory mod's plugin test suites"
  fi
  printf '%s\n' "$report" | grep -Eq '^ *[1-9][0-9]* pass$' || {
    printf '%s\n' "$report" >&2
    fail "Claude Code $CLAUDE_VERSION ran no memory mod plugin test"
  }
  printf '%s\n' "$report" | grep -Eq '^ *0 fail$' || {
    printf '%s\n' "$report" >&2
    fail "Claude Code $CLAUDE_VERSION reported memory mod plugin test failures"
  }
  pass "Claude Code $CLAUDE_VERSION runs the memory mod's plugin test suites clean"
}

test_no_tracked_autoload_link
test_validate_strict
test_plugin_suites
