#!/usr/bin/env bash
# tests/fm-record-export.test.sh - fixed allowlist live export and teardown archives.

set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

EXPORTER="$ROOT/bin/fm-record-export.sh"
TMP_ROOT=$(fm_test_tmproot fm-record-export)

mode_of() {
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"
}

make_home() {  # <name>
  local home="$TMP_ROOT/$1"
  mkdir -p "$home/config" "$home/data" "$home/state"
  printf '%s\n' "$home"
}

run_export() {  # <home> <args...>
  local home=$1
  shift
  FM_HOME="$home" "$EXPORTER" "$@"
}

test_sync_is_off_when_config_is_absent() {
  local home rc=0
  home=$(make_home sync-off)
  printf 'private\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "record export without config"
  [ ! -s "$home/stdout" ] || fail "config-absent sync wrote stdout"
  [ ! -s "$home/stderr" ] || fail "config-absent sync wrote stderr"
  [ "$(find "$home" -mindepth 1 -maxdepth 1 -exec basename {} \; | LC_ALL=C sort | tr '\n' ' ')" = "config data state stderr stdout " ] \
    || fail "config-absent sync created an export artifact"
  pass "record export sync is inert when config is absent"
}

test_sync_copies_only_the_v1_allowlist() {
  local home export_root rc=0 rel
  home=$(make_home sync-allowlist)
  export_root="$TMP_ROOT/export-allowlist"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  mkdir -p "$home/data/task-a" "$home/data/unrelated" \
    "$home/state/task-a.inbox/handled" "$home/state/other.inbox/handled"
  printf 'projects\n' > "$home/data/projects.md"
  printf 'backlog\n' > "$home/data/backlog.md"
  printf 'done archive\n' > "$home/data/done-archive.md"
  printf 'brief\n' > "$home/data/task-a/brief.md"
  printf 'report\n' > "$home/data/task-a/report.md"
  printf 'status zero\n' > "$home/data/task-a/status.log"
  printf 'status one\n' > "$home/data/task-a/status.1.log"
  printf 'do not copy\n' > "$home/data/task-a/notes.txt"
  printf 'do not scan\n' > "$home/data/unrelated/private.md"
  printf 'ledger\n' > "$home/state/fleet-ledger.jsonl"
  printf 'meta\n' > "$home/state/task-a.meta"
  printf 'status\n' > "$home/state/task-a.status"
  printf 'pending\n' > "$home/state/task-a.inbox/001.msg"
  printf 'handled\n' > "$home/state/task-a.inbox/handled/002.msg"
  printf 'internal\n' > "$home/state/task-a.inbox/.ring-state"
  printf 'unknown\n' > "$home/state/task-a.inbox/not-a-record"
  printf 'other internal\n' > "$home/state/other.inbox/.ring-state"
  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "allowlisted live sync: $(cat "$home/stderr")"
  for rel in \
    data/projects.md data/backlog.md data/done-archive.md \
    data/task-a/brief.md data/task-a/report.md data/task-a/status.log data/task-a/status.1.log \
    state/fleet-ledger.jsonl state/task-a.meta state/task-a.status \
    state/task-a.inbox/001.msg state/task-a.inbox/handled/002.msg; do
    cmp -s "$home/$rel" "$export_root/live/$rel" || fail "sync did not copy $rel byte-exact"
    [ "$(mode_of "$export_root/live/$rel")" = 640 ] || fail "sync did not set mode 0640 on $rel"
  done
  for rel in data/task-a/notes.txt data/unrelated/private.md \
    state/task-a.inbox/.ring-state state/task-a.inbox/not-a-record state/other.inbox/.ring-state; do
    assert_absent "$export_root/live/$rel" "sync copied non-allowlisted $rel"
  done
  while IFS= read -r rel; do
    [ "$(mode_of "$rel")" = 750 ] || fail "sync did not set mode 0750 on directory $rel"
  done < <(find "$export_root" -type d)
  pass "record export sync copies only the fixed v1 allowlist with restrictive modes"
}

test_sync_treats_empty_path_overrides_as_unset() {
  local home export_root rc=0
  home=$(make_home sync-empty-overrides)
  export_root="$TMP_ROOT/export-empty-overrides"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'projects\n' > "$home/data/projects.md"
  printf 'working: underway\n' > "$home/state/task-a.status"
  FM_HOME="$home" FM_ROOT_OVERRIDE='' FM_CONFIG_OVERRIDE='' FM_DATA_OVERRIDE='' FM_STATE_OVERRIDE='' \
    "$EXPORTER" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "sync with empty path overrides: $(cat "$home/stderr")"
  assert_grep 'projects' "$export_root/live/data/projects.md" "sync with empty overrides skipped the home data tree"
  assert_grep 'underway' "$export_root/live/state/task-a.status" "sync with empty overrides skipped the home state tree"
  pass "record export sync treats empty FM_*_OVERRIDE variables as unset"
}

test_sync_skips_hidden_state_records() {
  local home export_root rc=0
  home=$(make_home sync-hidden-state)
  export_root="$TMP_ROOT/export-hidden-state"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'status\n' > "$home/state/task-a.status"
  printf 'network checks\n' > "$home/state/.startup-network.status"
  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "sync with a hidden state status file: $(cat "$home/stderr")"
  assert_grep 'status' "$export_root/live/state/task-a.status" "sync dropped the task status beside a hidden record"
  assert_absent "$export_root/live/state/.startup-network.status" "sync exported a hidden non-task status file"
  pass "record export sync skips hidden state records instead of failing"
}

test_sync_replaces_files_atomically() {
  local home export_root rc=0
  home=$(make_home sync-atomic)
  export_root="$TMP_ROOT/export-atomic"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'old bytes\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/initial.out" 2> "$home/initial.err" \
    || fail "atomic snapshot setup failed: $(cat "$home/initial.err")"
  printf 'new bytes\n' > "$home/data/projects.md"
  chmod 000 "$home/data/projects.md"
  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  chmod 0644 "$home/data/projects.md"
  [ "$rc" -ne 0 ] || fail "sync accepted an unreadable source"
  assert_grep 'old bytes' "$export_root/live/data/projects.md" "failed sync replaced the prior complete file"
  pass "record export sync leaves the prior live snapshot intact when capture fails"
}

test_sync_accepts_an_absolute_export_root_with_spaces() {
  local home export_root rc=0
  home=$(make_home sync-root-spaces)
  export_root="$TMP_ROOT/export root with spaces"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'projects\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "absolute export root with spaces: $(cat "$home/stderr")"
  assert_grep 'projects' "$export_root/live/data/projects.md" "sync did not use an absolute export root with spaces"
  pass "record export accepts an absolute export root with spaces"
}

test_sync_refuses_malformed_config_and_unsafe_paths() {
  local home export_root outside rc case_name
  for case_name in relative multiline config-link root-link source-link source-fifo destination-link unsafe-task; do
    home=$(make_home "sync-unsafe-$case_name")
    export_root="$TMP_ROOT/export-unsafe-$case_name"
    outside="$TMP_ROOT/outside-$case_name"
    mkdir -p "$outside"
    printf 'outside sentinel\n' > "$outside/sentinel"
    printf '%s\n' "$export_root" > "$home/config/record-export-root"
    printf 'projects\n' > "$home/data/projects.md"
    case "$case_name" in
      relative) printf 'relative/path\n' > "$home/config/record-export-root" ;;
      multiline) printf '%s\n%s\n' "$export_root" "$outside" > "$home/config/record-export-root" ;;
      config-link)
        printf '%s\n' "$export_root" > "$home/config/export-target"
        rm "$home/config/record-export-root"
        ln -s "$home/config/export-target" "$home/config/record-export-root"
        ;;
      root-link)
        rm -rf "$export_root"
        ln -s "$outside" "$export_root"
        ;;
      source-link)
        rm "$home/data/projects.md"
        ln -s "$outside/sentinel" "$home/data/projects.md"
        ;;
      source-fifo)
        rm "$home/data/projects.md"
        mkfifo "$home/data/projects.md"
        ;;
      destination-link)
        mkdir -p "$export_root/live/data"
        ln -s "$outside/sentinel" "$export_root/live/data/projects.md"
        ;;
      unsafe-task)
        mkdir -p "$home/data/task space"
        printf 'brief\n' > "$home/data/task space/brief.md"
        ;;
    esac
    rc=0
    run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
    [ "$rc" -ne 0 ] || fail "sync accepted unsafe $case_name input"
    assert_grep 'outside sentinel' "$outside/sentinel" "unsafe $case_name input changed its target"
  done
  pass "record export sync refuses malformed config, traversal-shaped names, symlinks, and non-regular files"
}

test_sync_requires_a_dedicated_disjoint_single_home_root() {
  local scope home other export_root rc case_name
  for case_name in home ancestor descendant nonempty; do
    scope="$TMP_ROOT/root-boundary-$case_name"
    mkdir -p "$scope"
    home="$scope/home"
    mkdir -p "$home/config" "$home/data" "$home/state"
    printf 'projects\n' > "$home/data/projects.md"
    case "$case_name" in
      home) export_root=$home ;;
      ancestor) export_root=$scope ;;
      descendant) export_root="$home/export" ;;
      nonempty)
        export_root="$scope/export"
        mkdir -p "$export_root"
        printf 'unowned sentinel\n' > "$export_root/unowned"
        ;;
    esac
    printf '%s\n' "$export_root" > "$home/config/record-export-root"
    rc=0
    run_export "$home" sync > "$scope/stdout" 2> "$scope/stderr" || rc=$?
    [ "$rc" -ne 0 ] || fail "sync accepted a non-dedicated $case_name export root"
    if [ "$case_name" = nonempty ]; then
      assert_grep 'unowned sentinel' "$export_root/unowned" \
        "sync changed an unknown artifact in an unowned root"
    fi
  done

  scope="$TMP_ROOT/root-boundary-other-home"
  home="$scope/home-one"
  other="$scope/home-two"
  export_root="$scope/export"
  mkdir -p "$home/config" "$home/data" "$home/state" \
    "$other/config" "$other/data" "$other/state"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf '%s\n' "$export_root" > "$other/config/record-export-root"
  printf 'home one\n' > "$home/data/projects.md"
  printf 'home two\n' > "$other/data/projects.md"
  run_export "$home" sync > "$scope/first.out" 2> "$scope/first.err" \
    || fail "first home could not initialize its dedicated export root: $(cat "$scope/first.err")"
  rc=0
  run_export "$other" sync > "$scope/second.out" 2> "$scope/second.err" || rc=$?
  [ "$rc" -ne 0 ] || fail "sync accepted an export root owned by another home"
  assert_grep 'home one' "$export_root/live/data/projects.md" \
    "second home changed the first home's live snapshot"
  pass "record export requires a dedicated root disjoint from and owned by exactly one home"
}

test_sync_does_not_mutate_an_unowned_root_before_rejecting_it() {
  local home export_root rc=0
  home=$(make_home root-unowned-mode)
  export_root="$TMP_ROOT/export-root-unowned-mode"
  mkdir -p "$export_root"
  chmod 0777 "$export_root"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'projects\n' > "$home/data/projects.md"

  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  [ "$rc" -ne 0 ] || fail "sync accepted an unowned export root"
  [ "$(mode_of "$export_root")" = 777 ] \
    || fail "sync changed the mode of an unowned export root before rejecting it"
  pass "record export rejects an unowned root without changing its mode"
}

test_live_publication_staging_cannot_wedge_archives_or_sync() {
  local home export_root live_target rc=0 legacy_stage internal_stage
  home=$(make_home live-staging-recovery)
  export_root="$TMP_ROOT/export-live-staging-recovery"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'projects\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/initial.out" 2> "$home/initial.err" \
    || fail "live staging setup sync failed: $(cat "$home/initial.err")"
  live_target=$(readlink "$export_root/live")

  mkdir -p "$home/state/task-a.inbox"
  printf 'spawn_gen=live-staging-generation\n' > "$home/state/task-a.meta"
  printf 'pending\n' > "$home/state/task-a.inbox/001.msg"
  legacy_stage="$export_root/.live-next-1234-0123456789abcdef0123456789abcdef"
  ln -s "$live_target" "$legacy_stage"
  run_export "$home" archive-task task-a > "$home/archive.out" 2> "$home/archive.err" || rc=$?
  expect_code 0 "$rc" "archive after interrupted live publication: $(cat "$home/archive.err")"
  assert_absent "$legacy_stage" "archive left a stale root publication link behind"

  internal_stage="$export_root/.live-generations/.live-next-1234-0123456789abcdef0123456789abcdef"
  ln -s "$live_target" "$internal_stage"
  run_export "$home" sync > "$home/sync.out" 2> "$home/sync.err" || rc=$?
  expect_code 0 "$rc" "sync after interrupted live publication: $(cat "$home/sync.err")"
  assert_absent "$internal_stage" "sync left a stale internal publication link behind"
  pass "interrupted live publication staging cannot wedge archive or sync"
}

test_sync_retracts_stale_records_as_one_live_snapshot() {
  local home export_root first_generation rc=0
  home=$(make_home sync-converges)
  export_root="$TMP_ROOT/export-sync-converges"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  mkdir -p "$home/state/task-a.inbox/handled"
  printf 'meta\n' > "$home/state/task-a.meta"
  printf 'status\n' > "$home/state/task-a.status"
  printf 'pending\n' > "$home/state/task-a.inbox/001.msg"
  run_export "$home" sync > "$home/first.out" 2> "$home/first.err" \
    || fail "first convergent sync failed: $(cat "$home/first.err")"
  first_generation=$(readlink "$export_root/live")

  mv "$home/state/task-a.inbox/001.msg" "$home/state/task-a.inbox/handled/001.msg"
  rm "$home/state/task-a.meta" "$home/state/task-a.status"
  run_export "$home" sync > "$home/second.out" 2> "$home/second.err" || rc=$?
  expect_code 0 "$rc" "second convergent sync: $(cat "$home/second.err")"
  assert_absent "$export_root/live/state/task-a.meta" "sync retained retired metadata"
  assert_absent "$export_root/live/state/task-a.status" "sync retained retired status"
  assert_absent "$export_root/live/state/task-a.inbox/001.msg" "sync retained a moved pending message"
  assert_grep 'pending' "$export_root/live/state/task-a.inbox/handled/001.msg" \
    "sync omitted the handled destination of a moved message"
  assert_present "$export_root/$first_generation" \
    "sync removed the prior immutable generation before a consumer could finish it"
  pass "record export sync publishes the exact current allowlist as one live snapshot"
}

test_sync_refuses_unknown_managed_artifacts_before_publication() {
  local home export_root rc=0
  home=$(make_home sync-unknown-managed)
  export_root="$TMP_ROOT/export-sync-unknown-managed"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'old snapshot\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/initial.out" 2> "$home/initial.err" \
    || fail "unknown-artifact setup sync failed: $(cat "$home/initial.err")"
  printf 'intruder\n' > "$export_root/.live-generations/unknown"
  printf 'new snapshot\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/stdout" 2> "$home/stderr" || rc=$?
  [ "$rc" -ne 0 ] || fail "sync accepted an unknown artifact in its managed generation tree"
  assert_grep 'old snapshot' "$export_root/live/data/projects.md" \
    "sync published a new snapshot before refusing an unknown managed artifact"
  assert_grep 'intruder' "$export_root/.live-generations/unknown" \
    "sync changed an unknown managed artifact"
  pass "record export refuses unknown managed artifacts before publishing a snapshot"
}

test_sync_detects_destination_parent_replacement() {
  local home export_root outside pid rc=0 wait_count=0 stage='' live_target prior_snapshot
  home=$(make_home sync-destination-race)
  export_root="$TMP_ROOT/export-sync-destination-race"
  outside="$TMP_ROOT/outside-sync-destination-race"
  mkdir -p "$outside"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'initial\n' > "$home/data/projects.md"
  run_export "$home" sync > "$home/initial.out" 2> "$home/initial.err" \
    || fail "destination-race setup sync failed: $(cat "$home/initial.err")"
  live_target=$(readlink "$export_root/live" 2>/dev/null || true)
  dd if=/dev/zero of="$home/data/projects.md" bs=1048576 count=64 status=none

  run_export "$home" sync > "$home/race.out" 2> "$home/race.err" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    stage=$(find "$export_root/live/data" -maxdepth 1 -name '.record-export.*' -print -quit 2>/dev/null || true)
    [ -n "$stage" ] || stage=$(find "$export_root/.live-generations" -maxdepth 1 -name '.stage-*' -print -quit 2>/dev/null || true)
    [ -z "$stage" ] || break
    wait_count=$((wait_count + 1))
    [ "$wait_count" -le 500 ] || break
    sleep 0.01
  done
  [ -n "$stage" ] || { wait "$pid" || true; fail "destination-race fixture never observed a staged snapshot"; }
  kill -STOP "$pid"
  case "$stage" in
    "$export_root/live/data"/*)
      mv "$export_root/live/data" "$export_root/live/data.saved"
      ln -s "$outside" "$export_root/live/data"
      prior_snapshot="$export_root/live/data.saved/projects.md"
      ;;
    "$export_root/.live-generations"/*)
      mv "$export_root/.live-generations" "$export_root/.live-generations.saved"
      ln -s "$outside" "$export_root/.live-generations"
      prior_snapshot="$export_root/.live-generations.saved/${live_target#.live-generations/}/data/projects.md"
      ;;
    *) kill -CONT "$pid"; wait "$pid" || true; fail "destination-race fixture observed an unknown staging path" ;;
  esac
  kill -CONT "$pid"
  wait "$pid" || rc=$?
  [ "$rc" -ne 0 ] || fail "sync committed after its destination parent was replaced"
  assert_absent "$outside/projects.md" "sync wrote through a replacement destination symlink"
  assert_grep 'initial' "$prior_snapshot" \
    "failed destination-race sync changed the prior published snapshot"
  pass "record export sync binds destination validation to descriptor-safe publication"
}

write_archive_fixture() {  # <home> <spawn-gen>
  local home=$1 spawn_gen=$2
  mkdir -p "$home/state/task-a.inbox/handled"
  printf 'task=task-a\nspawn_gen=%s\nkind=ship\n' "$spawn_gen" > "$home/state/task-a.meta"
  printf 'working: underway\ndone: landed\n' > "$home/state/task-a.status"
  printf 'pending captain text\n' > "$home/state/task-a.inbox/001.msg"
  printf 'handled captain text\n' > "$home/state/task-a.inbox/handled/002.msg"
}

test_archive_commits_manifest_last_with_integrity_metadata() {
  local home export_root archive rc=0 rel source_rel bytes digest
  home=$(make_home archive-manifest)
  export_root="$TMP_ROOT/export-archive-manifest"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" s1700000000.1.abc
  FM_STATE_OVERRIDE="$home/state" run_export "$home" archive-task task-a \
    > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "archive task: $(cat "$home/stderr")"
  archive="$export_root/archive/task-a/s1700000000.1.abc"
  assert_present "$archive/manifest.v1.json" "archive did not commit its manifest"
  for rel in meta status inbox/001.msg inbox/handled/002.msg; do
    assert_present "$archive/$rel" "archive omitted $rel"
    [ "$(mode_of "$archive/$rel")" = 640 ] || fail "archive did not set mode 0640 on $rel"
  done
  while IFS= read -r rel; do
    [ "$(mode_of "$rel")" = 750 ] || fail "archive did not set mode 0750 on directory $rel"
  done < <(find "$export_root" -type d)
  jq -e '
    .schema == "firstmate-record-export-archive.v1"
    and .schema_version == 1
    and .task == "task-a"
    and .spawn_gen == "s1700000000.1.abc"
    and (.captured_at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T"))
    and .complete == true
    and ([.files[].source] == [
      "state/task-a.meta",
      "state/task-a.status",
      "state/task-a.inbox/001.msg",
      "state/task-a.inbox/handled/002.msg"
    ])
  ' "$archive/manifest.v1.json" >/dev/null || fail "archive manifest schema or ordered source list is wrong"
  for rel in meta status inbox/001.msg inbox/handled/002.msg; do
    case "$rel" in
      meta) source_rel=state/task-a.meta ;;
      status) source_rel=state/task-a.status ;;
      *) source_rel="state/task-a.$rel" ;;
    esac
    bytes=$(wc -c < "$home/$source_rel" | tr -d ' ')
    digest=$(shasum -a 256 "$home/$source_rel" | awk '{print $1}')
    jq -e --arg source "$source_rel" --argjson bytes "$bytes" --arg digest "$digest" '
      any(.files[]; .source == $source and .bytes == $bytes and .sha256 == $digest)
    ' "$archive/manifest.v1.json" >/dev/null || fail "archive manifest integrity is wrong for $source_rel"
  done
  pass "teardown archive commits a complete v1 manifest after every allowlisted source"
}

test_archive_is_off_before_any_state_read_when_config_is_absent() {
  local home rc=0
  home=$(make_home archive-off)
  rm -rf "$home/state"
  FM_STATE_OVERRIDE="$home/missing-state" run_export "$home" archive-task task-a \
    > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "config-absent archive"
  [ ! -s "$home/stdout" ] || fail "config-absent archive wrote stdout"
  [ ! -s "$home/stderr" ] || fail "config-absent archive wrote stderr"
  assert_absent "$home/missing-state" "config-absent archive touched the state path"
  pass "teardown archive is inert before any state read when config is absent"
}

test_archive_retry_is_idempotent_and_generations_never_overwrite() {
  local home export_root first second before rc=0
  home=$(make_home archive-retry)
  export_root="$TMP_ROOT/export-archive-retry"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" generation-one
  run_export "$home" archive-task task-a > "$home/first.out" 2> "$home/first.err" || rc=$?
  expect_code 0 "$rc" "first generation archive"
  first="$export_root/archive/task-a/generation-one"
  before=$(shasum -a 256 "$first/manifest.v1.json" | awk '{print $1}')
  rc=0
  run_export "$home" archive-task task-a > "$home/retry.out" 2> "$home/retry.err" || rc=$?
  expect_code 0 "$rc" "idempotent archive retry"
  [ "$before" = "$(shasum -a 256 "$first/manifest.v1.json" | awk '{print $1}')" ] \
    || fail "idempotent retry rewrote the committed manifest"
  printf 'done: changed after commit\n' >> "$home/state/task-a.status"
  rc=0
  run_export "$home" archive-task task-a > "$home/changed.out" 2> "$home/changed.err" || rc=$?
  [ "$rc" -ne 0 ] || fail "archive retry accepted changed sources for a committed generation"
  assert_grep 'committed archive differs from current sources' "$home/changed.err" \
    "changed-source retry did not report the immutable generation conflict"
  printf 'task=task-a\nspawn_gen=generation-two\nkind=ship\n' > "$home/state/task-a.meta"
  rc=0
  run_export "$home" archive-task task-a > "$home/second.out" 2> "$home/second.err" || rc=$?
  expect_code 0 "$rc" "second generation archive"
  second="$export_root/archive/task-a/generation-two"
  assert_present "$second/manifest.v1.json" "reused task id did not create a new generation"
  [ "$before" = "$(shasum -a 256 "$first/manifest.v1.json" | awk '{print $1}')" ] \
    || fail "new generation overwrote the prior generation"
  pass "teardown archive retries idempotently and reused task IDs stay separated by spawn_gen"
}

test_archive_retry_accepts_sources_already_retired_after_commit() {
  local home export_root manifest before rc=0
  home=$(make_home archive-retired-retry)
  export_root="$TMP_ROOT/export-archive-retired-retry"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" retired-retry-generation
  run_export "$home" archive-task task-a > "$home/first.out" 2> "$home/first.err" || rc=$?
  expect_code 0 "$rc" "initial archive before partial teardown"
  manifest="$export_root/archive/task-a/retired-retry-generation/manifest.v1.json"
  before=$(shasum -a 256 "$manifest" | awk '{print $1}')
  rm -f "$home/state/task-a.status"
  rm -rf "$home/state/task-a.inbox"
  rc=0
  run_export "$home" archive-task task-a > "$home/retry.out" 2> "$home/retry.err" || rc=$?
  expect_code 0 "$rc" "archive retry after status and inbox retirement: $(cat "$home/retry.err")"
  [ "$before" = "$(shasum -a 256 "$manifest" | awk '{print $1}')" ] \
    || fail "partial-teardown retry rewrote the committed manifest"
  pass "teardown archive retry accepts sources already retired after a committed capture"
}

test_archive_refuses_a_corrupt_committed_manifest() {
  local home export_root manifest rc case_name
  for case_name in wrong-task trailing-garbage duplicate-key wrong-type extra-root extra-entry; do
    home=$(make_home "archive-corrupt-manifest-$case_name")
    export_root="$TMP_ROOT/export-archive-corrupt-manifest-$case_name"
    printf '%s\n' "$export_root" > "$home/config/record-export-root"
    write_archive_fixture "$home" committed-generation
    rc=0
    run_export "$home" archive-task task-a > "$home/first.out" 2> "$home/first.err" || rc=$?
    expect_code 0 "$rc" "initial archive for $case_name manifest case"
    manifest="$export_root/archive/task-a/committed-generation/manifest.v1.json"
    case "$case_name" in
      wrong-task) sed 's/"task": "task-a"/"task": "another-task"/' "$manifest" > "$home/manifest.tmp" ;;
      trailing-garbage) cp "$manifest" "$home/manifest.tmp"; printf 'not-json\n' >> "$home/manifest.tmp" ;;
      duplicate-key) sed '/"captured_at":/a\  "captured_at": "2000-01-01T00:00:00Z",' "$manifest" > "$home/manifest.tmp" ;;
      wrong-type) sed 's/"captured_at": "[^"]*"/"captured_at": 1/' "$manifest" > "$home/manifest.tmp" ;;
      extra-root) sed '/"complete": true/a\  "unexpected": true,' "$manifest" > "$home/manifest.tmp" ;;
      extra-entry) sed '0,/"sha256": "[^"]*"/s//& , "unexpected": true/' "$manifest" > "$home/manifest.tmp" ;;
    esac
    mv "$home/manifest.tmp" "$manifest"
    rc=0
    run_export "$home" archive-task task-a > "$home/retry.out" 2> "$home/retry.err" || rc=$?
    [ "$rc" -ne 0 ] || fail "archive retry accepted a $case_name committed manifest"
    assert_present "$home/state/task-a.meta" "$case_name manifest retry removed live metadata"
  done
  pass "teardown archive rejects a corrupt committed manifest"
}

test_archive_detects_a_source_change_during_capture() {
  local home export_root archive outside archive_pid worker_pid rc=0 wait_count=0 stage=
  home=$(make_home archive-source-race)
  export_root="$TMP_ROOT/export-archive-source-race"
  outside="$TMP_ROOT/outside-archive-source-race"
  archive="$export_root/archive/task-a/racing-generation"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" racing-generation
  mkdir -p "$outside"
  printf 'outside sentinel\n' > "$outside/sentinel"
  dd if=/dev/zero of="$home/state/task-a.status" bs=1048576 count=64 status=none
  run_export "$home" archive-task task-a > "$home/stdout" 2> "$home/stderr" &
  archive_pid=$!
  while kill -0 "$archive_pid" 2>/dev/null; do
    stage=$(find "$export_root/archive/task-a" -maxdepth 1 -name '.archive-stage-*' -print -quit 2>/dev/null || true)
    [ -z "$stage" ] || break
    wait_count=$((wait_count + 1))
    [ "$wait_count" -le 500 ] || break
    sleep 0.01
  done
  [ -n "$stage" ] || { wait "$archive_pid" || true; fail "archive source-race fixture never observed staging"; }
  worker_pid=$(ps -eo pid=,args= | awk -v pattern="$ROOT/bin/fm-record-export.py archive-task task-a" \
    'index($0, pattern) { print $1; exit }')
  [ -n "$worker_pid" ] || { wait "$archive_pid" || true; fail "archive source-race fixture could not identify the filesystem worker"; }
  kill -STOP "$worker_pid"
  rm "$home/state/task-a.status"
  ln -s "$outside/sentinel" "$home/state/task-a.status"
  kill -CONT "$worker_pid"
  wait "$archive_pid" || rc=$?
  [ "$rc" -ne 0 ] || fail "archive committed while a source changed during capture"
  assert_absent "$archive/manifest.v1.json" "source race committed a manifest"
  assert_present "$home/state/task-a.meta" "source race removed live metadata"
  assert_grep 'outside sentinel' "$outside/sentinel" "source race changed the replacement target"
  pass "teardown archive refuses to commit when a source changes during capture"
}

test_archive_repairs_only_an_uncommitted_same_generation() {
  local home export_root archive rc=0
  home=$(make_home archive-partial)
  export_root="$TMP_ROOT/export-archive-partial"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" partial-generation
  run_export "$home" sync > "$home/sync.out" 2> "$home/sync.err" \
    || fail "partial generation setup could not initialize its root: $(cat "$home/sync.err")"
  archive="$export_root/archive/task-a/partial-generation"
  mkdir -p "$archive"
  printf 'stale partial bytes\n' > "$archive/meta"
  run_export "$home" archive-task task-a > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "partial generation retry: $(cat "$home/stderr")"
  cmp -s "$home/state/task-a.meta" "$archive/meta" || fail "retry did not repair the uncommitted same generation"
  assert_present "$archive/manifest.v1.json" "retry did not commit the repaired generation"
  pass "teardown archive safely repairs an uncommitted same-generation attempt"
}

test_archive_retry_removes_stale_files_from_a_partial_generation() {
  local home export_root archive rc=0
  home=$(make_home archive-partial-stale)
  export_root="$TMP_ROOT/export-archive-partial-stale"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  run_export "$home" sync > "$home/sync.out" 2> "$home/sync.err" \
    || fail "partial retry setup could not initialize its root: $(cat "$home/sync.err")"
  write_archive_fixture "$home" partial-stale-generation
  mv "$home/state/task-a.inbox/001.msg" "$home/state/task-a.inbox/handled/001.msg"
  archive="$export_root/archive/task-a/partial-stale-generation"
  mkdir -p "$archive/inbox"
  printf 'stale pending bytes\n' > "$archive/inbox/001.msg"
  run_export "$home" archive-task task-a > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "partial stale generation retry: $(cat "$home/stderr")"
  assert_absent "$archive/inbox/001.msg" "partial retry committed an unmanifested stale pending message"
  assert_grep 'pending captain text' "$archive/inbox/handled/001.msg" \
    "partial retry omitted the message's current handled path"
  jq -e '[.files[].archive] | index("inbox/001.msg") == null and index("inbox/handled/001.msg") != null' \
    "$archive/manifest.v1.json" >/dev/null || fail "partial retry manifest did not match the repaired generation"
  pass "teardown archive retry publishes a fresh generation without stale partial files"
}

test_archive_revalidates_sources_immediately_before_publication() {
  local home export_root archive archive_pid rc=0 wait_count=0 index
  home=$(make_home archive-final-revalidate)
  export_root="$TMP_ROOT/export-archive-final-revalidate"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" final-revalidate-generation
  run_export "$home" sync > "$home/sync.out" 2> "$home/sync.err" \
    || fail "final revalidation setup could not initialize its root: $(cat "$home/sync.err")"
  archive="$export_root/archive/task-a/final-revalidate-generation"
  mkdir -p "$archive/inbox"
  index=0
  while [ "$index" -lt 10000 ]; do
    printf 'stale %s\n' "$index" > "$archive/inbox/$(printf '%05d' "$index").msg"
    index=$((index + 1))
  done

  run_export "$home" archive-task task-a > "$home/archive.out" 2> "$home/archive.err" &
  archive_pid=$!
  while [ -e "$archive/inbox/00000.msg" ]; do
    kill -0 "$archive_pid" 2>/dev/null || break
    wait_count=$((wait_count + 1))
    [ "$wait_count" -le 1000 ] || break
    sleep 0.01
  done
  [ ! -e "$archive/inbox/00000.msg" ] \
    || { wait "$archive_pid" || true; fail "archive did not begin replacing its stale partial generation"; }
  printf 'changed after final comparison\n' >> "$home/state/task-a.status"
  wait "$archive_pid" || rc=$?
  [ "$rc" -ne 0 ] || fail "archive committed after a source changed during stale-generation replacement"
  assert_absent "$archive/manifest.v1.json" "archive committed a manifest after its final source comparison"
  assert_present "$home/state/task-a.meta" "final comparison race removed live metadata"
  pass "teardown archive revalidates sources immediately before publication"
}

test_archive_waits_for_the_task_lock_before_enumerating_sources() {
  local home export_root archive lock holder_pid archive_pid rc=0 wait_count=0
  home=$(make_home archive-source-lock)
  export_root="$TMP_ROOT/export-archive-source-lock"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  write_archive_fixture "$home" locked-generation
  lock="$home/state/.meta-task-a.lock"
  FM_HOME="$home" bash -c '
    . "$1"
    fm_lock_acquire_wait "$2"
    touch "$3"
    while [ ! -e "$4" ]; do sleep 0.02; done
    fm_lock_release "$2"
  ' _ "$ROOT/bin/fm-wake-lib.sh" "$lock" "$home/lock-held" "$home/lock-release" &
  holder_pid=$!
  while [ ! -e "$home/lock-held" ]; do
    kill -0 "$holder_pid" 2>/dev/null || fail "archive lock fixture exited before acquiring the task lock"
    wait_count=$((wait_count + 1))
    [ "$wait_count" -le 250 ] || fail "archive lock fixture never acquired the task lock"
    sleep 0.02
  done

  run_export "$home" archive-task task-a > "$home/archive.out" 2> "$home/archive.err" &
  archive_pid=$!
  sleep 0.2
  kill -0 "$archive_pid" 2>/dev/null || fail "archive bypassed the task metadata lock"
  printf 'late locked message\n' > "$home/state/task-a.inbox/003.msg"
  touch "$home/lock-release"
  wait "$holder_pid" || fail "archive lock fixture could not release the task lock"
  wait "$archive_pid" || rc=$?
  expect_code 0 "$rc" "archive after task-lock release: $(cat "$home/archive.err")"
  archive="$export_root/archive/task-a/locked-generation"
  assert_grep 'late locked message' "$archive/inbox/003.msg" \
    "archive enumerated its source set before acquiring the task lock"
  jq -e 'any(.files[]; .archive == "inbox/003.msg")' "$archive/manifest.v1.json" >/dev/null \
    || fail "archive manifest omitted a source published before the task lock was released"
  pass "teardown archive holds the task producer lock through source enumeration and commit"
}

test_archive_allows_missing_optional_records() {
  local home export_root archive rc=0
  home=$(make_home archive-optional)
  export_root="$TMP_ROOT/export-archive-optional"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf 'spawn_gen=no-status\n' > "$home/state/task-a.meta"
  run_export "$home" archive-task task-a > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 0 "$rc" "archive without optional records"
  archive="$export_root/archive/task-a/no-status"
  assert_present "$archive/meta" "archive without status lost mandatory metadata"
  assert_absent "$archive/status" "archive fabricated a missing status"
  jq -e '.complete == true and [.files[].source] == ["state/task-a.meta"]' \
    "$archive/manifest.v1.json" >/dev/null || fail "missing optional records made the archive incomplete"
  pass "teardown archive treats absent status and inbox records as a complete capture"
}

test_archive_refuses_unsafe_sources_and_destinations() {
  local home export_root outside rc case_name
  for case_name in missing-gen duplicate-gen unsafe-gen meta-link status-fifo inbox-link destination-link unknown-partial; do
    home=$(make_home "archive-unsafe-$case_name")
    export_root="$TMP_ROOT/export-archive-unsafe-$case_name"
    outside="$TMP_ROOT/archive-outside-$case_name"
    mkdir -p "$outside"
    printf 'outside sentinel\n' > "$outside/sentinel"
    printf '%s\n' "$export_root" > "$home/config/record-export-root"
    write_archive_fixture "$home" safe-generation
    case "$case_name" in
      destination-link|unknown-partial)
        run_export "$home" sync > "$home/setup.out" 2> "$home/setup.err" \
          || fail "unsafe $case_name setup could not initialize its root: $(cat "$home/setup.err")"
        ;;
    esac
    case "$case_name" in
      missing-gen) grep -v '^spawn_gen=' "$home/state/task-a.meta" > "$home/state/meta.tmp"; mv "$home/state/meta.tmp" "$home/state/task-a.meta" ;;
      duplicate-gen) printf 'spawn_gen=other\n' >> "$home/state/task-a.meta" ;;
      unsafe-gen) sed 's/safe-generation/../' "$home/state/task-a.meta" > "$home/state/meta.tmp"; mv "$home/state/meta.tmp" "$home/state/task-a.meta" ;;
      meta-link) rm "$home/state/task-a.meta"; ln -s "$outside/sentinel" "$home/state/task-a.meta" ;;
      status-fifo) rm "$home/state/task-a.status"; mkfifo "$home/state/task-a.status" ;;
      inbox-link) rm "$home/state/task-a.inbox/001.msg"; ln -s "$outside/sentinel" "$home/state/task-a.inbox/001.msg" ;;
      destination-link)
        mkdir -p "$export_root/archive/task-a"
        ln -s "$outside" "$export_root/archive/task-a/safe-generation"
        ;;
      unknown-partial)
        mkdir -p "$export_root/archive/task-a/safe-generation"
        printf 'unknown\n' > "$export_root/archive/task-a/safe-generation/unknown"
        ;;
    esac
    rc=0
    run_export "$home" archive-task task-a > "$home/stdout" 2> "$home/stderr" || rc=$?
    [ "$rc" -ne 0 ] || fail "archive accepted unsafe $case_name input"
    assert_present "$home/state/task-a.meta" "unsafe $case_name archive removed metadata"
    assert_grep 'outside sentinel' "$outside/sentinel" "unsafe $case_name archive changed its target"
  done
  pass "teardown archive refuses malformed generations, unknown artifacts, symlinks, and non-regular files"
}

test_archive_retention_prunes_only_old_committed_generations() {
  local home export_root archive_root rc=0
  home=$(make_home archive-retention)
  export_root="$TMP_ROOT/export-archive-retention"
  archive_root="$export_root/archive"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  run_export "$home" sync > "$home/sync.out" 2> "$home/sync.err" \
    || fail "retention setup could not initialize its root: $(cat "$home/sync.err")"
  printf 'spawn_gen=old-generation\n' > "$home/state/old-task.meta"
  run_export "$home" archive-task old-task > "$home/old.out" 2> "$home/old.err" \
    || fail "retention setup could not create a valid old generation: $(cat "$home/old.err")"
  touch -t 200001010000 "$archive_root/old-task/old-generation/manifest.v1.json"
  write_archive_fixture "$home" current-generation
  mkdir -p "$archive_root/current-task/current-generation" \
    "$archive_root/incomplete-task/incomplete-generation" \
    "$archive_root/invalid-task/invalid-generation" \
    "$archive_root/unknown-task/unknown-generation" \
    "$archive_root/unreadable-task"
  printf '{}\n' > "$archive_root/current-task/current-generation/manifest.v1.json"
  printf 'partial\n' > "$archive_root/incomplete-task/incomplete-generation/meta"
  printf '{}\n' > "$archive_root/invalid-task/invalid-generation/manifest.v1.json"
  printf '{}\n' > "$archive_root/unknown-task/unknown-generation/manifest.v1.json"
  printf 'unknown\n' > "$archive_root/unknown-task/unknown-generation/extra"
  touch -t 200001010000 "$archive_root/incomplete-task/incomplete-generation/meta" \
    "$archive_root/invalid-task/invalid-generation/manifest.v1.json" \
    "$archive_root/unknown-task/unknown-generation/manifest.v1.json"
  chmod 000 "$archive_root/unreadable-task"
  run_export "$home" archive-task task-a > "$home/stdout" 2> "$home/stderr" || rc=$?
  chmod 0750 "$archive_root/unreadable-task"
  expect_code 0 "$rc" "archive retention"
  assert_absent "$archive_root/old-task/old-generation" "retention kept an expired committed generation"
  assert_present "$archive_root/current-task/current-generation" "retention removed a current generation"
  assert_present "$archive_root/incomplete-task/incomplete-generation" "retention removed an incomplete generation"
  assert_present "$archive_root/invalid-task/invalid-generation" "retention removed an invalid old manifest"
  assert_present "$archive_root/unknown-task/unknown-generation" "retention removed a generation with unknown artifacts"
  assert_grep 'warning: refusing to prune unsafe record export archive' "$home/stderr" \
    "unsafe retention candidate did not warn"
  assert_grep 'warning: could not scan record export archive' "$home/stderr" \
    "unreadable retention subtree did not warn"
  pass "record export retention mirrors 30 completed days and prunes only owned committed generations"
}

test_export_root_stays_cold_to_firstmate_readers() {
  local home export_root output rc=0
  home=$(make_home cold-readers)
  export_root="$home/export-must-remain-absent"
  printf '%s\n' "$export_root" > "$home/config/record-export-root"
  printf '%s\n' '# Backlog' '' '## In flight' '' '## Queued' '' '## Done' > "$home/data/backlog.md"
  : > "$home/data/projects.md"
  FM_HOME="$home" FM_STATE_OVERRIDE="$home/state" FM_DATA_OVERRIDE="$home/data" \
    FM_CONFIG_OVERRIDE="$home/config" bash "$ROOT/bin/fm-wake-drain.sh" \
    > "$home/drain.out" 2> "$home/drain.err" || rc=$?
  expect_code 0 "$rc" "wake presentation with record export configured"
  FM_HOME="$home" FM_STATE_OVERRIDE="$home/state" FM_DATA_OVERRIDE="$home/data" \
    FM_CONFIG_OVERRIDE="$home/config" bash -c '
      . "$1/bin/fm-watch.sh"
      scan_signals
      status_presentation_snapshot "$STATE"
    ' _ "$ROOT" > "$home/watch.out" 2> "$home/watch.err" \
    || fail "watch classification or snapshot failed with record export configured"
  FM_HOME="$home" FM_STATE_OVERRIDE="$home/state" FM_DATA_OVERRIDE="$home/data" \
    FM_CONFIG_OVERRIDE="$home/config" bash "$ROOT/bin/fm-fleet-snapshot.sh" --json \
    > "$home/snapshot.out" 2> "$home/snapshot.err" \
    || fail "fleet snapshot failed with record export configured"
  assert_absent "$export_root" "a presentation, wake, classification, or snapshot reader materialized the export root"
  output=$(cat "$home/drain.out" "$home/drain.err" "$home/watch.out" "$home/watch.err" \
    "$home/snapshot.out" "$home/snapshot.err")
  assert_not_contains "$output" "$export_root" "a Firstmate reader exposed the configured export root"
  pass "presentation, wake, classification, and snapshot readers leave the export root cold"
}

test_report_publication_is_retryable_after_worktree_cleanup() {
  local home collection worktree report package rc=0
  home=$(make_home report-publish-retry)
  collection="$TMP_ROOT/report-publish-collection"
  worktree="$TMP_ROOT/report-publish-worktree"
  mkdir -p "$home/data/scout" "$worktree"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=first\nworktree=%s\n' "$worktree" > "$home/state/scout.meta"
  report="$home/data/scout/report.md"
  printf '# Scout report\n\nAn artifact was captured.\n\n[Clip](%s/clip.mp4#t=10)\n' "$worktree" > "$report"
  printf 'outside\n' > "$TMP_ROOT/outside.mp4"
  ln -s "$TMP_ROOT/outside.mp4" "$worktree/clip.mp4"
  run_export "$home" publish-report scout > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 1 "$rc" "publication of a symlinked attachment"
  assert_absent "$collection/index.md" "failed publication wrote a collection index"
  mv "$worktree/clip.mp4" "$home/rejected-link"
  printf 'clip evidence\n' > "$worktree/clip.mp4"

  run_export "$home" publish-report scout || fail "fixed report publication failed"
  package=$(dirname "$(find "$collection/reports" -name report.md)")
  assert_grep 'files/worktree/clip.mp4#t=10' "$package/report.md" "publication lost the media fragment"
  [ "$(jq -r '.title, .summary, .legacy, .origin_home' "$package/catalogue.v1.json")" = \
    "$(printf 'Scout report\nAn artifact was captured.\nfalse\n%s' "$home")" ] \
    || fail "published catalogue lost report metadata"
  mv "$worktree" "$home/retired-worktree"
  mv "$collection/index.md" "$home/expected-index"
  run_export "$home" publish-report scout || fail "committed report could not retry after cleanup"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "publication retry changed the index"
  assert_grep 'clip evidence' "$package/files/worktree/clip.mp4" "publication retry lost the clip"
  pass "report publication rejects unsafe sources and retries the committed package after cleanup"
}

test_import_legacy_reports_retains_original_and_rebuilds_index() {
  local home collection package fixture
  home=$(make_home report-import)
  collection="$TMP_ROOT/report-import-collection"
  fixture="$home/data/old/retired"
  mkdir -p "$fixture/board" "$home/data/current" "$home/data/active"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\n' > "$home/state/active.meta"
  printf 'An active report.\n' > "$home/data/active/report.md"
  printf 'An older report.\n' > "$home/data/current/report.md"
  printf 'image evidence\n' > "$fixture/board/screenshot one.png"
  {
    printf '# Retired investigation\n\nA screenshot documents the result.\n\n'
    printf '![Screenshot][shot]\n\n[shot]: <board/screenshot one.png>\n\n'
    printf '[Missing](lost.html)\n\n\x60\x60\x60base64\n'
    head -c 1048576 /dev/zero | tr '\000' A
    printf '\n[example inside a code fence](do-not-import.txt)\n\x60\x60\x60\n'
  } > "$fixture/report.md"
  run_export "$home" import-reports || fail "legacy report import failed"
  package=$(dirname "$(rg -l '^# Retired investigation' "$collection/reports" -g report.md)")
  cmp -s "$fixture/report.md" "$package/report.original" || fail "legacy import changed original report bytes"
  assert_grep 'files/data/board/screenshot%20one.png' "$package/report.md" "legacy link did not become portable"
  cmp -s "$fixture/board/screenshot one.png" "$package/files/data/board/screenshot one.png" \
    || fail "legacy import lost the screenshot"
  [ "$(jq -r '.legacy, .date, .task_id, .unresolved_links[]' "$package/catalogue.v1.json")" = \
    "$(printf 'true\nunknown\nretired\nlost.html')" ] || fail "legacy catalogue misrepresents the source"
  assert_grep 'An older report' "$collection/index.md" "legacy import skipped a retained task directory"
  assert_no_grep 'An active report' "$collection/index.md" "legacy import captured an active task"
  mv "$collection/index.md" "$home/expected-index"
  run_export "$home" import-reports || fail "repeat legacy report import failed"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "repeat import duplicated reports"
  pass "legacy import retains original bytes, records missing links and rebuilds the catalogue index"
}

test_report_publication_captures_nested_images_and_skips_code() {
  local home collection package name
  home=$(make_home report-markdown)
  collection="$TMP_ROOT/report-markdown-collection"
  mkdir -p "$home/data/scout"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=markdown\n' > "$home/state/scout.meta"
  for name in thumb.png full.png list.png quoted.png reference.png; do
    printf '%s\n' "$name" > "$home/data/scout/$name"
  done
  cat > "$home/data/scout/report.md" <<'MD'
# Markdown evidence

[![Thumbnail](thumb.png)](full.png)

- Evidence

    ![Screenshot](list.png)

    ```md
    ![Example](missing-list-code.png)
    ```

    1. References

       ![Reference][shot]

       [shot]: reference.png

> ![Quoted](quoted.png)
>
> ```md
> ![Example](missing-quoted-code.png)
> ```

    ![Example](missing-indented-code.png)

`![Example](missing-inline-code.png)`
MD
  run_export "$home" publish-report scout || fail "nested Markdown publication failed"
  package=$(dirname "$(rg --files "$collection/reports" -g report.md)")
  for name in thumb.png full.png list.png quoted.png reference.png; do
    cmp -s "$home/data/scout/$name" "$package/files/data/$name" || fail "publication omitted $name"
    assert_grep "files/data/$name" "$package/report.md" "publication did not rewrite $name"
  done
  pass "publication captures nested images and excludes code examples"
}

test_report_revisions_capture_changed_and_restored_materials() {
  local home collection worktree first second count
  home=$(make_home report-revisions)
  collection="$TMP_ROOT/report-revisions-collection"
  worktree="$TMP_ROOT/report-revisions-worktree"
  mkdir -p "$home/data/scout" "$worktree"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=revision\nworktree=%s\n' "$worktree" > "$home/state/scout.meta"
  printf '# Revisions\n\n![Shot](shot.png)\n[Clip](%s/clip.mp4)\n' "$worktree" > "$home/data/scout/report.md"
  printf 'first shot\n' > "$home/data/scout/shot.png"
  printf 'first clip\n' > "$worktree/clip.mp4"
  run_export "$home" publish-report scout || fail "initial publication failed"
  first=$(dirname "$(rg --files "$collection/reports" -g report.md)")
  printf 'corrected shot\n' > "$home/data/scout/shot.png"
  printf 'final clip\n' > "$worktree/clip.mp4"
  run_export "$home" publish-report scout || fail "changed evidence publication failed"
  count=$(rg --files "$collection/reports" -g report.md | wc -l | tr -d ' ')
  [ "$count" = 2 ] || fail "changed evidence did not create a revision"
  second=$(dirname "$(rg -l 'corrected shot' "$collection/reports" -g shot.png)")
  second=${second%/files/data}
  assert_grep 'first shot' "$first/files/data/shot.png" "revision overwrote previous evidence"
  assert_grep 'final clip' "$second/files/worktree/clip.mp4" "revision lost changed worktree evidence"
  mv "$worktree" "$home/retired-worktree"
  mv "$collection/index.md" "$home/expected-index"
  run_export "$home" publish-report scout || fail "latest revision could not retry after cleanup"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "cleanup retry created a revision"
  mv "$home/data/scout/shot.png" "$home/saved-shot.png"
  ln -s "$home/saved-shot.png" "$home/data/scout/shot.png"
  if run_export "$home" publish-report scout > "$home/stdout" 2> "$home/stderr"; then
    fail "reuse bypassed unsafe available material"
  fi

  home=$(make_home legacy-revisions)
  collection="$TMP_ROOT/legacy-revisions-collection"
  mkdir -p "$home/data/old/scout" "$home/data/scout"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf '# Legacy\n\n![Shot](shot.png)\n[Board](board.html)\n' > "$home/data/scout/report.md"
  cp "$home/data/scout/report.md" "$home/data/old/scout/report.md"
  printf 'old shot\n' > "$home/data/old/scout/shot.png"
  printf 'current shot\n' > "$home/data/scout/shot.png"
  run_export "$home" import-reports || fail "legacy import failed"
  count=$(rg --files "$collection/reports" -g report.md | wc -l | tr -d ' ')
  [ "$count" = 2 ] || fail "legacy duplicate locations hid different evidence"
  printf 'restored board\n' > "$home/data/scout/board.html"
  run_export "$home" import-reports || fail "restored legacy import failed"
  rg -q 'restored board' "$collection/reports" -g board.html || fail "legacy deduplication omitted restored material"
  mv "$collection/index.md" "$home/expected-index"
  run_export "$home" import-reports || fail "repeat legacy import failed"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "repeat legacy import created revisions"
  pass "report revisions retain changed and restored supporting materials"
}

test_report_destinations_capture_balanced_parentheses() {
  local home collection package name
  home=$(make_home report-parentheses)
  collection="$TMP_ROOT/report-parentheses-collection"
  mkdir -p "$home/data/scout"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=parentheses\n' > "$home/state/scout.meta"
  for name in diagram reference escaped angle; do
    printf '%s evidence\n' "$name" > "$home/data/scout/$name(v1(final)).png"
  done
  cat > "$home/data/scout/report.md" <<'MD'
# Balanced destinations

[![Diagram](diagram(v1(final)).png)](angle(v1(final)).png "A linked image")

- References

    ![Reference][shot]

    [shot]: reference(v1(final)).png "Nested parentheses"

![Escaped](escaped\(v1\(final\)\).png)
![Angle](<angle(v1(final)).png?zoom=1#detail>)
MD
  run_export "$home" publish-report scout || fail "balanced destination publication failed"
  package=$(dirname "$(rg --files "$collection/reports" -g report.md)")
  for name in diagram reference escaped angle; do
    cmp -s "$home/data/scout/$name(v1(final)).png" "$package/files/data/$name(v1(final)).png" \
      || fail "publication omitted the balanced $name destination"
  done
  assert_contains "$(cat "$package/report.md")" 'files/data/diagram%28v1%28final%29%29.png' "inline destination was not rewritten"
  assert_contains "$(cat "$package/report.md")" 'files/data/reference%28v1%28final%29%29.png' "reference destination was truncated"
  pass "inline and reference destinations preserve balanced and escaped parentheses"
}

test_report_retry_uses_the_last_published_compatible_revision() {
  local home collection worktree count rc=0
  home=$(make_home report-last-revision)
  collection="$TMP_ROOT/report-last-revision-collection"
  worktree="$TMP_ROOT/report-last-revision-worktree"
  mkdir -p "$home/data/scout" "$worktree"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=revision\nworktree=%s\n' "$worktree" > "$home/state/scout.meta"
  printf '# Revisions\n\n![Shot](shot.png)\n[Clip](%s/clip.mp4)\n' "$worktree" > "$home/data/scout/report.md"
  printf 'A shot\n' > "$home/data/scout/shot.png"
  printf 'A clip\n' > "$worktree/clip.mp4"
  run_export "$home" publish-report scout || fail "revision A publication failed"
  printf 'B shot\n' > "$home/data/scout/shot.png"
  printf 'B clip\n' > "$worktree/clip.mp4"
  run_export "$home" publish-report scout || fail "revision B publication failed"
  printf 'A shot\n' > "$home/data/scout/shot.png"
  printf 'A clip\n' > "$worktree/clip.mp4"
  run_export "$home" publish-report scout || fail "revision A republication failed"
  mv "$worktree" "$home/retired-worktree"
  mv "$collection/index.md" "$home/expected-index"
  run_export "$home" publish-report scout || fail "revision A cleanup retry failed"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "cleanup retry combined different revisions"
  printf 'C shot\n' > "$home/data/scout/shot.png"
  run_export "$home" publish-report scout > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 1 "$rc" "changed material with a missing companion"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "incompatible retry changed the collection"
  count=$(rg --files "$collection/reports" -g report.md | wc -l | tr -d ' ')
  [ "$count" = 2 ] || fail "retry created a package from incompatible evidence"
  pass "retries preserve the last published compatible revision without mixing evidence"
}

test_legacy_report_retry_keeps_sources_separate() {
  local home collection package catalogue
  home=$(make_home legacy-source-isolation)
  collection="$TMP_ROOT/legacy-source-isolation-collection"
  mkdir -p "$home/data/scout" "$home/data/old/scout"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf '# Legacy\n\n[Board](board.html)\n' > "$home/data/scout/report.md"
  cp "$home/data/scout/report.md" "$home/data/old/scout/report.md"
  printf 'current board\n' > "$home/data/scout/board.html"
  run_export "$home" import-reports || fail "source isolation import failed"
  jq -se '[.[] | select(.source == "old/scout/report.md")] | length == 1 and .[0].unresolved_links == ["board.html"]' \
    "$collection"/reports/*/catalogue.v1.json >/dev/null || fail "old report borrowed the current board"
  printf 'old board\n' > "$home/data/old/scout/board.html"
  run_export "$home" import-reports || fail "restored old source import failed"
  jq -se 'any(.[]; .source == "old/scout/report.md" and .unresolved_links == [])' \
    "$collection"/reports/*/catalogue.v1.json >/dev/null || fail "restored old source has no complete package"
  for catalogue in "$collection"/reports/*/catalogue.v1.json; do
    if jq -e '.source == "old/scout/report.md" and .unresolved_links == []' "$catalogue" >/dev/null; then
      package=${catalogue%/catalogue.v1.json}
      assert_grep 'old board' "$package/files/data/board.html" "restored old source lost its own board"
    fi
  done
  mv "$home/data/old/scout/board.html" "$home/retired-old-board.html"
  printf 'changed current board\n' > "$home/data/scout/board.html"
  run_export "$home" import-reports || fail "isolated source retry failed"
  for catalogue in "$collection"/reports/*/catalogue.v1.json; do
    if jq -e '.source == "old/scout/report.md" and .unresolved_links == []' "$catalogue" >/dev/null; then
      package=${catalogue%/catalogue.v1.json}
      assert_grep 'old board' "$package/files/data/board.html" "retry borrowed evidence from a different source"
    fi
  done
  pass "legacy retries retain only evidence belonging to the same report source"
}

test_report_block_recognition_preserves_nested_materials() {
  local home collection package variant name
  for variant in nested inline; do
    home=$(make_home "report-blocks-$variant")
    collection="$TMP_ROOT/report-blocks-$variant-collection"
    mkdir -p "$home/data/scout"
    printf '%s\n' "$collection" > "$home/config/report-collection-root"
    printf 'kind=scout\nspawn_gen=blocks\n' > "$home/state/scout.meta"
    if [ "$variant" = nested ]; then
      cat > "$home/data/scout/report.md" <<'MD'
# Nested containers

- - Evidence

      ![Shot](nested.png)

        ![Code](missing-indented.png)

1. - Evidence

       ![Shot](ordered.png)

- - ```md
    ![Code](missing-fenced.png)
    ```
MD
      for name in nested.png ordered.png; do
        printf '%s evidence\n' "$name" > "$home/data/scout/$name"
      done
    else
      cat > "$home/data/scout/report.md" <<'MD'
# Code and fences

```inline```

![Shot](inline.png)

- ```inline```

  ![Shot](list.png)

> ```inline```
>
> ![Shot](quote.png)

```md
![Code](missing-backtick.png)
```

~~~language`with`backticks
![Code](missing-tilde.png)
~~~
MD
      for name in inline.png list.png quote.png; do
        printf '%s evidence\n' "$name" > "$home/data/scout/$name"
      done
    fi
    run_export "$home" publish-report scout || fail "$variant block publication failed"
    package=$(dirname "$(rg --files "$collection/reports" -g report.md)")
    for name in "$home/data/scout/"*.png; do
      cmp -s "$name" "$package/files/data/${name##*/}" || fail "$variant block parsing omitted ${name##*/}"
      assert_contains "$(cat "$package/report.md")" "files/data/${name##*/}" "block parsing did not rewrite ${name##*/}"
    done
  done
  pass "nested containers and inline code preserve named evidence while code blocks stay literal"
}

test_report_retry_never_captures_a_reassigned_worktree() {
  local home collection project worktree package field other_path rc
  home=$(make_home report-worktree-ownership)
  collection="$TMP_ROOT/report-worktree-ownership-collection"
  project="$TMP_ROOT/report-worktree-ownership-project"
  worktree="$TMP_ROOT/report-worktree-ownership-pool/1/repo"
  mkdir -p "$home/data/scout" "$project" "${worktree%/repo}"
  git -C "$project" init -q
  git -C "$project" -c user.name=test -c user.email=test@example.invalid commit -q --allow-empty -m baseline
  git -C "$project" worktree add -q --detach "$worktree"
  printf '{"worktrees":[{"path":"%s"}]}\n' "$worktree" > "${worktree%/1/repo}/treehouse-state.json"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=ownership\nworktree=%s\nproject=%s\n' "$worktree" "$project" > "$home/state/scout.meta"
  printf '# Scout A\n\n![Shot](%s/shot.png)\n' "$worktree" > "$home/data/scout/report.md"
  FM_HOME="$home" bash -c '. "$1/bin/fm-wake-lib.sh"; fm_treehouse_slot_owner_claim "$2" scout "$FM_HOME"' _ "$ROOT" "$worktree" \
    || fail "could not claim the scout slot"
  printf 'scout A evidence\n' > "$worktree/shot.png"
  run_export "$home" publish-report scout || fail "owned worktree publication failed"
  package=$(dirname "$(rg --files "$collection/reports" -g report.md)")
  mv "$collection/index.md" "$home/expected-index"

  FM_HOME="$home" bash -c '. "$1/bin/fm-wake-lib.sh"; fm_treehouse_slot_owner_claim "$2" other "$FM_HOME"' _ "$ROOT" "$worktree" \
    || fail "could not reassign the scout slot"
  printf 'other task evidence\n' > "$worktree/shot.png"
  run_export "$home" publish-report scout || fail "reassigned slot retry failed"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "retry captured the reassigned slot"
  assert_grep 'scout A evidence' "$package/files/worktree/shot.png" "retry lost the original scout evidence"
  assert_grep 'other task evidence' "$worktree/shot.png" "retry changed the other task's evidence"
  assert_grep 'task=other' "${worktree%/repo}/.fm-slot-owner" "retry changed the other task's claim"

  mv "${worktree%/repo}/.fm-slot-owner" "$home/reassigned-claim"
  ln -s "$worktree" "$home/slot-alias"
  for field in worktree home; do
    other_path=$worktree
    [ "$field" != home ] || other_path="$home/slot-alias"
    printf 'kind=ship\n%s=%s\n' "$field" "$other_path" > "$home/state/other.meta"
    run_export "$home" publish-report scout || fail "active $field collision retry failed"
    cmp -s "$collection/index.md" "$home/expected-index" || fail "retry captured another task's recorded $field"
  done
  printf '# Unpublished scout revision\n\n![Shot](%s/shot.png)\n' "$worktree" > "$home/data/scout/report.md"
  rc=0
  run_export "$home" publish-report scout > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 1 "$rc" "publication from another task's worktree without a prior package"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "refused ownership check changed the collection"
  assert_grep 'other task evidence' "$worktree/shot.png" "refused publication changed the other task's evidence"
  pass "publication reuses verified evidence after slot reassignment and refuses foreign sources"
}

test_report_parser_preserves_source_at_container_boundaries() {
  local home collection fixture package command name
  for command in publish-report import-reports; do
    home=$(make_home "report-parser-$command")
    collection="$TMP_ROOT/report-parser-$command-collection"
    fixture="$home/data/scout"
    mkdir -p "$fixture"
    printf '%s\n' "$collection" > "$home/config/report-collection-root"
    if [ "$command" = publish-report ]; then
      printf 'kind=scout\nspawn_gen=parser\n' > "$home/state/scout.meta"
    fi
    python3 - "$fixture" <<'PY'
import sys
from pathlib import Path

fixture = Path(sys.argv[1])
before = '''# Parser evidence

Date: 2026-11-18

- ~~~text
  ![Example](missing.png)
- ![Shot](shot.png)

1. ```text
   ![Example](missing.png)
2. ![Ordered][ordered]

[ordered]: <ordered.png>  "Keep this title"

- - ~~~text
    ![Example](missing.png)
  - ![Nested](nested.png)

Évidence: \x00ÿ `![Example](shot.png)` and ![Unicode](café.png "Title")

![Multiline](
  <shot.png>
)

[Remote](https://example.invalid/no-network?q=1&amp;x=2)
'''
after = '''# Parser evidence

Date: 2026-11-18

- ~~~text
  ![Example](missing.png)
- ![Shot](files/data/shot.png)

1. ```text
   ![Example](missing.png)
2. ![Ordered][ordered]

[ordered]: <files/data/ordered.png>  "Keep this title"

- - ~~~text
    ![Example](missing.png)
  - ![Nested](files/data/nested.png)

Évidence: \x00ÿ `![Example](shot.png)` and ![Unicode](files/data/caf%C3%A9.png "Title")

![Multiline](
  <files/data/shot.png>
)

[Remote](https://example.invalid/no-network?q=1&amp;x=2)
'''
for filename, text in (('report.md', before), ('expected', after)):
    (fixture / filename).write_bytes(text.replace('\n', '\r\n').encode('utf-8').replace(b'\xc3\xbf', b'\xff'))
for filename in ('shot.png', 'ordered.png', 'nested.png', 'café.png'):
    (fixture / filename).write_bytes(b'captured evidence\n')
PY
    if [ "$command" = publish-report ]; then
      run_export "$home" "$command" scout || fail "parser publication failed"
    else
      run_export "$home" "$command" || fail "parser import failed"
    fi
    package=$(dirname "$(rg --files "$collection/reports" -g report.md)")
    for name in shot.png ordered.png nested.png café.png; do
      cmp -s "$fixture/$name" "$package/files/data/$name" || fail "$command omitted $name at a container boundary"
    done
    cmp -s "$fixture/expected" "$package/report.md" || fail "$command changed source bytes beyond link destinations"
    if [ "$command" = import-reports ]; then
      cmp -s "$fixture/report.md" "$package/report.original" || fail "parser import changed original bytes"
    fi
    mv "$fixture/shot.png" "$home/saved-shot.png"
    mv "$collection/index.md" "$home/expected-index"
    if [ "$command" = publish-report ]; then
      run_export "$home" "$command" scout || fail "parser publication retry failed"
    else
      run_export "$home" "$command" || fail "parser import retry failed"
    fi
    cmp -s "$collection/index.md" "$home/expected-index" || fail "$command retry changed the collection"
    cmp -s "$fixture/expected" "$package/report.md" || fail "$command retry changed source formatting"
  done
  pass "publication and import retain nested evidence and exact source bytes across retries"
}

test_report_retry_keeps_evidence_after_a_pool_return() {
  local home collection project worktree package claimed rc
  for claimed in yes legacy; do
    home=$(make_home "report-return-$claimed")
    collection="$TMP_ROOT/report-return-$claimed-collection"
    project="$TMP_ROOT/report-return-$claimed-project"
    worktree="$TMP_ROOT/report-return-$claimed-pool/1/repo"
    mkdir -p "$home/data/scout" "$project" "${worktree%/repo}"
    git -C "$project" init -q
    printf 'baseline diagram\n' > "$project/diagram.png"
    git -C "$project" add diagram.png
    git -C "$project" -c user.name=test -c user.email=test@example.invalid commit -q -m baseline
    git -C "$project" worktree add -q --detach "$worktree"
    printf '{"worktrees":[{"path":"%s"}]}\n' "$worktree" > "${worktree%/1/repo}/treehouse-state.json"
    printf '%s\n' "$collection" > "$home/config/report-collection-root"
    printf 'kind=scout\nspawn_gen=returned\nworktree=%s\nproject=%s\n' "$worktree" "$project" > "$home/state/scout.meta"
    printf '# Scout diagram\n\n![Diagram](%s/diagram.png)\n' "$worktree" > "$home/data/scout/report.md"
    if [ "$claimed" = yes ]; then
      FM_HOME="$home" bash -c '. "$1/bin/fm-wake-lib.sh"; fm_treehouse_slot_owner_claim "$2" scout "$FM_HOME"' _ "$ROOT" "$worktree" \
        || fail "could not claim the scout slot"
    fi
    printf 'scout diagram\n' > "$worktree/diagram.png"
    run_export "$home" publish-report scout || fail "$claimed initial publication failed"
    package=$(dirname "$(rg --files "$collection/reports" -g report.md)")
    cp "$collection/index.md" "$home/expected-index"
    git -C "$worktree" reset --hard -q HEAD
    if [ "$claimed" = yes ]; then
      FM_HOME="$home" bash -c '. "$1/bin/fm-wake-lib.sh"; fm_treehouse_slot_owner_release "$2" scout' _ "$ROOT" "$worktree" \
        || fail "could not release the scout slot"
    fi
    run_export "$home" publish-report scout || fail "$claimed returned slot retry failed"
    cmp -s "$collection/index.md" "$home/expected-index" || fail "$claimed retry captured the reset diagram"
    assert_grep 'scout diagram' "$package/files/worktree/diagram.png" "returned slot retry lost captured evidence"
    assert_grep 'baseline diagram' "$worktree/diagram.png" "returned slot retry changed the pool copy"

    FM_HOME="$home" bash -c '. "$1/bin/fm-wake-lib.sh"; fm_treehouse_slot_owner_claim "$2" scout "$FM_HOME"' _ "$ROOT" "$worktree" \
      || fail "could not reclaim the scout slot"
    printf 'corrected scout diagram\n' > "$worktree/diagram.png"
    run_export "$home" publish-report scout || fail "claimed slot could not publish corrected evidence"
    cp "$collection/index.md" "$home/expected-index"
    FM_HOME="$home" bash -c '. "$1/bin/fm-wake-lib.sh"; fm_treehouse_slot_owner_release "$2" scout' _ "$ROOT" "$worktree" \
      || fail "could not release the reclaimed slot"
    git -C "$worktree" reset --hard -q HEAD
    printf '# Amended scout diagram\n\n![Diagram](%s/diagram.png)\n' "$worktree" > "$home/data/scout/report.md"
    rc=0
    run_export "$home" publish-report scout > "$home/stdout" 2> "$home/stderr" || rc=$?
    expect_code 1 "$rc" "amended report with a returned source slot"
    cmp -s "$collection/index.md" "$home/expected-index" || fail "amended report captured a returned slot"
  done
  pass "returned pool slots retain captured evidence while legacy initial publication remains available"
}

test_report_commands_resolve_uv_through_mise_or_path() {
  local home collection fakebin real_uv tool resolved rc
  home=$(make_home report-uv-provider)
  collection="$TMP_ROOT/report-uv-provider-collection"
  fakebin="$TMP_ROOT/report-uv-provider-bin"
  if command -v mise >/dev/null 2>&1; then
    real_uv=$(mise --quiet --cd "$ROOT" which uv) || fail "mise could not resolve the report command fixture's uv"
  else
    real_uv=$(command -v uv) || fail "uv is required for the report command fixture"
  fi
  mkdir -p "$fakebin" "$home/data/scout" "$home/data/old/legacy"
  for tool in bash dirname basename sed uname mkdir cat stat readlink realpath date hostname id ps mktemp mv rm sleep awk cut tail head tr sort wc find ln chmod env cp git python3; do
    resolved=$(command -v "$tool") || continue
    ln -s "$resolved" "$fakebin/$tool"
  done
  cat > "$fakebin/mise" <<'SH'
#!/usr/bin/env bash
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
[ "$#" -gt 0 ] || exit 2
shift
[ "${1:-}" = uv ] || exit 2
shift
exec "${FM_TEST_REPORT_UV:?}" "$@"
SH
  chmod +x "$fakebin/mise"
  printf '%s\n' "$collection" > "$home/config/report-collection-root"
  printf 'kind=scout\nspawn_gen=provider\n' > "$home/state/scout.meta"
  printf '# Current report\n\n![Shot](shot.png)\n' > "$home/data/scout/report.md"
  printf 'current evidence\n' > "$home/data/scout/shot.png"
  printf '# Legacy report\n\n![Shot](shot.png)\n' > "$home/data/old/legacy/report.md"
  printf 'legacy evidence\n' > "$home/data/old/legacy/shot.png"
  PATH="$fakebin" FM_TEST_REPORT_UV="$real_uv" run_export "$home" publish-report scout \
    || fail "publication refused uv supplied only by mise"
  PATH="$fakebin" FM_TEST_REPORT_UV="$real_uv" run_export "$home" import-reports \
    || fail "import refused uv supplied only by mise"
  assert_grep 'Current report' "$collection/index.md" "mise publication did not generate its index"
  assert_grep 'Legacy report' "$collection/index.md" "mise import did not generate its index"
  cp "$collection/index.md" "$home/expected-index"
  mv "$fakebin/mise" "$home/provider-mise"
  ln -s "$real_uv" "$fakebin/uv"
  PATH="$fakebin" run_export "$home" publish-report scout || fail "publication refused uv from PATH"
  PATH="$fakebin" run_export "$home" import-reports || fail "import refused uv from PATH"
  cmp -s "$collection/index.md" "$home/expected-index" || fail "provider selection changed report packages"
  mv "$fakebin/uv" "$home/provider-uv"
  rc=0
  PATH="$fakebin" run_export "$home" import-reports > "$home/stdout" 2> "$home/stderr" || rc=$?
  expect_code 1 "$rc" "report command without mise or uv"
  assert_grep 'requires uv' "$home/stderr" "missing uv provider was not reported"
  pass "publication and import accept mise-managed uv and PATH uv and reject an absent provider"
}

test_report_retry_keeps_evidence_after_a_pool_return
test_report_commands_resolve_uv_through_mise_or_path
test_report_parser_preserves_source_at_container_boundaries
test_report_block_recognition_preserves_nested_materials
test_report_retry_never_captures_a_reassigned_worktree
test_report_destinations_capture_balanced_parentheses
test_report_retry_uses_the_last_published_compatible_revision
test_legacy_report_retry_keeps_sources_separate
test_report_publication_captures_nested_images_and_skips_code
test_report_revisions_capture_changed_and_restored_materials
test_report_publication_is_retryable_after_worktree_cleanup
test_import_legacy_reports_retains_original_and_rebuilds_index
test_sync_is_off_when_config_is_absent
test_sync_copies_only_the_v1_allowlist
test_sync_treats_empty_path_overrides_as_unset
test_sync_skips_hidden_state_records
test_sync_replaces_files_atomically
test_sync_accepts_an_absolute_export_root_with_spaces
test_sync_refuses_malformed_config_and_unsafe_paths
test_sync_requires_a_dedicated_disjoint_single_home_root
test_sync_does_not_mutate_an_unowned_root_before_rejecting_it
test_live_publication_staging_cannot_wedge_archives_or_sync
test_sync_retracts_stale_records_as_one_live_snapshot
test_sync_refuses_unknown_managed_artifacts_before_publication
test_sync_detects_destination_parent_replacement
test_archive_commits_manifest_last_with_integrity_metadata
test_archive_is_off_before_any_state_read_when_config_is_absent
test_archive_retry_is_idempotent_and_generations_never_overwrite
test_archive_retry_accepts_sources_already_retired_after_commit
test_archive_refuses_a_corrupt_committed_manifest
test_archive_detects_a_source_change_during_capture
test_archive_repairs_only_an_uncommitted_same_generation
test_archive_retry_removes_stale_files_from_a_partial_generation
test_archive_revalidates_sources_immediately_before_publication
test_archive_waits_for_the_task_lock_before_enumerating_sources
test_archive_allows_missing_optional_records
test_archive_refuses_unsafe_sources_and_destinations
test_archive_retention_prunes_only_old_committed_generations
test_export_root_stays_cold_to_firstmate_readers

echo "ALL TESTS PASSED"
