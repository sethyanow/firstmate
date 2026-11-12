# shellcheck shell=bash
# Session-digest visibility. Source this library, then call fm_digest_hide_init
# once per presentation. Marks belong to fm-captain-hold.sh's command contract.
# In-flight tasks and live workers override task marks, never decision marks.
# Failed backlog reads and timed-out endpoint reads keep marked tasks visible.
# No mark means no extra backlog or endpoint read.
# Task initialization is read-only. The mutation-authorized wake drain checks
# decision marks when rendering OPEN DECISIONS.

if ! declare -F status_open_decisions_incremental >/dev/null; then
  # shellcheck source=bin/fm-classify-lib.sh disable=SC1091
  . "$SCRIPT_DIR/fm-classify-lib.sh"
fi

fm_digest_hide_init() {
  [ "${FM_DIGEST_HIDE_INITIALIZED:-0}" = 0 ] || return 0
  FM_DIGEST_HIDE_INITIALIZED=1
  FM_DIGEST_HIDDEN_IDS=
  local data=${FM_DATA_OVERRIDE:-${FM_HOME:-$FM_ROOT}/data}
  local state=${FM_STATE_OVERRIDE:-${FM_HOME:-$FM_ROOT}/state}
  local mark id marked='' in_flight backend target rc
  for mark in "$data/digest-hidden/"task-*; do
    [ -f "$mark" ] || continue
    [ "$(cat "$mark")" = 1 ] || continue
    id=${mark##*/task-}
    case "$id" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
    marked="$marked$id
"
  done
  [ -n "$marked" ] || return 0
  # shellcheck source=bin/fm-tasks-axi-lib.sh disable=SC1091
  . "$SCRIPT_DIR/fm-tasks-axi-lib.sh"
  # shellcheck source=bin/fm-backlog-transition-lib.sh disable=SC1091
  . "$SCRIPT_DIR/fm-backlog-transition-lib.sh"
  data=$(fm_backlog_data_absolute "$data") || return 0
  backend=$(fm_tasks_axi_backend "$(dirname "$data")" 2>/dev/null) || return 0
  if [ "$backend" = markdown ]; then
    [ -f "$data/backlog.md" ] || return 0
    in_flight=$(awk '
      /^## / { active = ($0 ~ /^## In flight[[:space:]]*$/) }
      active && /^[-*] / { sub(/^[-*] \[[^]]*\] +/, ""); sub(/[ :].*$/, ""); print }
    ' "$data/backlog.md") || return 0
  else
    in_flight=$(FM_TASKS_AXI_TIMEOUT=2 fm_backlog_row_list "$data" --state in_flight 2>/dev/null) || return 0
    in_flight=$(printf '%s\n' "$in_flight" | awk '/^  [^ ]*,in_flight,/ { sub(/^  /, ""); sub(/,.*/, ""); print }')
  fi
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    case "
$in_flight
" in *"
$id
"*) continue ;; esac
    if [ -f "$state/$id.meta" ]; then
      # shellcheck source=bin/fm-backend.sh disable=SC1091
      . "$SCRIPT_DIR/fm-backend.sh"
      [ -z "$(fm_meta_get "$state/$id.meta" remote_host)" ] || continue
      backend=$(fm_backend_of_meta "$state/$id.meta")
      target=$(fm_backend_target_of_meta "$state/$id.meta")
      if [ -n "$target" ]; then
        rc=0
        # shellcheck disable=SC2016
        fm_run_timed 2 bash -c '. "$1"; fm_backend_target_exists "$2" "$3" "$4"' \
          _ "$SCRIPT_DIR/fm-backend.sh" "$backend" "$target" "fm-$id" >/dev/null 2>&1 || rc=$?
        [ "$rc" = 1 ] || continue
      fi
    fi
    FM_DIGEST_HIDDEN_IDS="$FM_DIGEST_HIDDEN_IDS$id
"
  done <<EOF
$marked
EOF
}

fm_digest_task_hidden() {  # <task-id>
  case "
${FM_DIGEST_HIDDEN_IDS:-}" in *"
$1
"*) return 0 ;; esac
  return 1
}

fm_digest_decision_token() {  # <state-dir> <task-or-mate-id> <key>
  local open
  open=$(status_open_decisions_incremental "$1/$2.status" '' occurrence) || return 1
  printf '%s\n' "$open" | awk -F '\t' -v key="$3" '$1 == key && $3 != "" { print $3; found=1 } END { exit !found }'
}

fm_digest_decision_mark_active() {  # <state-dir> <task-or-mate-id> <key>
  local data=${FM_DATA_OVERRIDE:-${FM_HOME:-$FM_ROOT}/data} mark token file
  file="$data/digest-hidden/decision-$2/key-$3"
  [ -f "$file" ] || return 1
  mark=$(cat "$file" 2>/dev/null) || return 1
  case "$mark" in 1$'\n'?*) ;; *) return 1 ;; esac
  token=$(fm_digest_decision_token "$1" "$2" "$3") || return 1
  [ "$mark" = "1"$'\n'"$token" ]
}

fm_digest_decision_hidden() {  # <task-or-mate-id> <key>
  fm_digest_task_hidden "$1" && return 0
  local state=${FM_STATE_OVERRIDE:-${FM_HOME:-$FM_ROOT}/state}
  fm_digest_decision_mark_active "$state" "$1" "$2"
}

fm_digest_hidden_count() {  # <count>
  [ "$1" -gt 0 ] || return 0
  printf '(%s hidden row(s); bin/fm-captain-hold.sh hidden)\n' "$1"
}

# Filter before reason decoding or queued-row limits, so hidden prose cannot
# leak through continuation lines or consume the visible queued allowance.
fm_digest_hide_rows() {  # <toon|markdown|text>
  [ -n "${FM_DIGEST_HIDDEN_IDS:-}" ] || { cat; return; }
  FM_DIGEST_HIDE_IDS="$FM_DIGEST_HIDDEN_IDS" awk -v format="$1" '
    BEGIN { n = split(ENVIRON["FM_DIGEST_HIDE_IDS"], keys, "\n"); for (i = 1; i <= n; i++) hidden[keys[i]] = 1 }
    {
      if (format == "text" && $0 ~ /^(OPEN DECISIONS|UNREAD STATUS|STATUS OUTCOME BACKSTOP|RECORD DIVERGENCE|BRANCH OUTCOMES|BRIEF CHECK FLAGS)[ (:]/ && omitted) {
        lines[++size] = "(" omitted " hidden row(s); bin/fm-captain-hold.sh hidden)"; omitted = 0
      }
      if (format == "text" && continuation && /^[[:space:]]/) next
      continuation = 0
      id = $0
      if (format == "markdown") {
        if ($0 ~ /^## /) done = ($0 ~ /^## Done[[:space:]]*$/)
        if ($0 !~ /^[-*] \[/) { lines[++size] = $0; next }
        sub(/^[-*] \[[^]]*\] +/, "", id); sub(/[ :].*$/, "", id)
      } else if (format == "toon") {
        if ($0 !~ /^  [^ ]*,/) { lines[++size] = $0; next }
        sub(/^  /, "", id); sub(/,.*/, "", id)
      } else {
        if (id ~ /^wake annotation: /) {
          sub(/^wake annotation: [^:]*: /, "", id); sub(/\.status:.*/, "", id)
        }
        if (id ~ /^(open-loop|unresolved) /) sub(/^(open-loop|unresolved) /, "", id)
        if (id ~ /^\[seq [^]]+\] /) sub(/^\[seq [^]]+\] /, "", id)
        if (id ~ /^[A-Z_]+: /) sub(/^[A-Z_]+: /, "", id)
        if (id ~ /^[0-9]+\t[0-9]+\t(signal|stale)\t/) {
          split(id, fields, "\t"); id = fields[4]; sub(/^.*\//, "", id); sub(/\.status$/, "", id)
        }
        sub(/[[:space:]:].*$/, "", id)
      }
      if (id in hidden && id != "" && !done) { omitted++; continuation = 1; next }
      lines[++size] = $0
    }
    END {
      for (i = 1; i <= size; i++) {
        line = lines[i]
        if (omitted && format == "toon") {
          if (line ~ /^count: [0-9]+$/) { split(line, a, " "); line = "count: " (a[2] - omitted) }
          if (line ~ /^(tasks|ready)\[[0-9]+\]/) {
            start = index(line, "["); rest = substr(line, start + 1); count = rest + 0
            sub(/\[[0-9]+\]/, "[" (count - omitted) "]", line)
          }
        }
        print line
      }
      if (omitted) printf "(%d hidden row(s); bin/fm-captain-hold.sh hidden)\n", omitted
    }
  '
}
