#!/usr/bin/env bash
# Fuzzy replacements for tmux's default choose-tree bindings:
#   <prefix> s  -> sessions
#   <prefix> w  -> windows across all sessions
set -euo pipefail

mode=${1:-}
script_dir=$(cd "$(dirname "$0")" && pwd)
script_path="$script_dir/$(basename "$0")"

fzf_common=(
  --delimiter=$'\t'
  --with-nth=3
  --expect=ctrl-x
  --layout=reverse
  --height=100%
  --info=inline
  --cycle
  --no-multi
)

# Return the session and window currently displayed by a particular client.
client_context() {
  local client=$1 line
  line=$(tmux list-clients -F $'#{client_name}\t#{session_id}\t#{window_id}' \
    | awk -F '\t' -v client="$client" '$1 == client { print $2 "\t" $3; exit }')
  if [[ -n $line ]]; then
    printf '%s\n' "$line"
  else
    tmux display-message -p $'#{session_id}\t#{window_id}'
  fi
}

# Pick the most recently active session other than the one being deleted.
fallback_session() {
  local excluded=$1 session_id activity
  local best_id='' best_activity=-1

  while IFS=$'\t' read -r session_id activity; do
    if [[ $session_id != "$excluded" ]] && ((activity > best_activity)); then
      best_id=$session_id
      best_activity=$activity
    fi
  done < <(tmux list-sessions -F $'#{session_id}\t#{session_activity}')
  printf '%s' "$best_id"
}

# Pick any active pane outside the window being deleted.
fallback_pane() {
  local excluded=$1 pane_id window_id
  while IFS=$'\t' read -r pane_id window_id; do
    if [[ $window_id != "$excluded" ]]; then
      printf '%s' "$pane_id"
      return
    fi
  done < <(tmux list-windows -a -F $'#{pane_id}\t#{window_id}')
}

schedule_kill_and_reopen() {
  local picker_mode=$1 target=$2 client=$3 command
  printf -v command '%q %q %q %q %q' \
    "$script_path" reopen-after-kill "$picker_mode" "$target" "$client"
  tmux run-shell -b "$command"
}

# Used by a tmux background job when deleting the item containing this popup.
# Waiting lets the old popup exit before the replacement is opened.
reopen_after_kill() {
  local picker_mode=$1 target=$2 client=$3 popup_command
  sleep 0.15
  if [[ $target == \$* ]]; then
    tmux kill-session -t "$target" 2>/dev/null || true
  elif [[ $target == @* ]]; then
    tmux kill-window -t "$target" 2>/dev/null || true
  fi
  sleep 0.10

  # The client may have exited while the deletion was in progress.
  if tmux list-clients -F '#{client_name}' | grep -Fxq "$client"; then
    printf -v popup_command '%q %q' "$script_path" "$picker_mode"
    tmux display-popup -c "$client" -E -w 100% -h 100% \
      -T " $picker_mode " "$popup_command"
  fi
}

# Delete an item. DELETE_CONTINUE tells the picker whether it can redraw in
# place. Deleting the item containing the popup requires switching the client
# first, then reopening the picker from a short-lived tmux background job.
delete_target() {
  local picker_mode=$1 target=$2 client=$3 current_session=$4 current_window=$5
  local fallback
  DELETE_CONTINUE=1

  if [[ $target == \$* ]]; then
    if [[ $target != "$current_session" ]]; then
      tmux kill-session -t "$target"
      return
    fi

    fallback=$(fallback_session "$target")
    if [[ -z $fallback ]]; then
      # Last session globally: allowing tmux to exit is the least surprising
      # fallback because there is nowhere else for the client to go.
      tmux kill-session -t "$target" 2>/dev/null || true
      DELETE_CONTINUE=0
      return
    fi

    tmux switch-client -c "$client" -t "$fallback"
    schedule_kill_and_reopen "$picker_mode" "$target" "$client"
    DELETE_CONTINUE=0
    return
  fi

  if [[ $target == @* ]]; then
    if [[ $target != "$current_window" ]]; then
      tmux kill-window -t "$target"
      return
    fi

    fallback=$(fallback_pane "$target")
    if [[ -z $fallback ]]; then
      tmux kill-window -t "$target" 2>/dev/null || true
      DELETE_CONTINUE=0
      return
    fi

    tmux switch-client -c "$client" -t "$fallback"
    schedule_kill_and_reopen "$picker_mode" "$target" "$client"
    DELETE_CONTINUE=0
  fi
}

parse_result() {
  local result=$1
  if [[ $result == *$'\n'* ]]; then
    PICK_KEY=${result%%$'\n'*}
    PICK_SELECTION=${result#*$'\n'}
  else
    # fzf --filter (useful for scripts/tests) omits the --expect key line.
    PICK_KEY=''
    PICK_SELECTION=$result
  fi
}

session_rows() {
  local current=$1 rows
  rows=$(tmux list-sessions -F $'#{session_id}\t#{pane_id}\t#{session_name}')

  # Two hidden fields hold the active pane and session ID. Only the final,
  # already-formatted field is displayed, avoiding uneven tabular columns.
  awk -F '\t' -v current="$current" 'BEGIN { OFS="\t" }
    {
      number = $1
      sub(/^\$/, "", number)
      print $2, $1, ($1 == current ? "* " : "  ") "[" number "] " $3
    }' <<< "$rows"
}

pick_session() {
  local client context current_session current_window rows result
  local switch_target delete_id ignored

  client=$(tmux display-message -p '#{client_name}')
  while :; do
    context=$(client_context "$client")
    IFS=$'\t' read -r current_session current_window <<< "$context"
    rows=$(session_rows "$current_session")

    if ! result=$(printf '%s\n' "$rows" | fzf "${fzf_common[@]}" \
        --prompt='session> ' \
        --header='Enter: switch  •  Ctrl-X: kill session  •  Esc: cancel' \
        --preview='tmux capture-pane -ep -t {1} -S -1000 2>/dev/null | tail -n "${FZF_PREVIEW_LINES:-50}"' \
        --preview-window='right,60%,wrap'); then
      return
    fi

    parse_result "$result"
    IFS=$'\t' read -r switch_target delete_id ignored <<< "$PICK_SELECTION"
    if [[ $PICK_KEY == ctrl-x ]]; then
      delete_target sessions "$delete_id" "$client" "$current_session" "$current_window"
      [[ $DELETE_CONTINUE == 1 ]] && continue
      return 0
    fi

    [[ -n $switch_target ]] && tmux switch-client -c "$client" -t "$switch_target"
    return
  done
}

window_tree() {
  local current_session=$1 current_window=$2
  local session_id session_name window_count active_pane
  local pane_id window_id window_index window_name window_number branch
  local session_marker window_marker

  while IFS=$'\t' read -r session_id session_name window_count active_pane; do
    if [[ $session_id == "$current_session" ]]; then
      session_marker='* '
    else
      session_marker='  '
    fi
    printf '%s\t%s\t%s▾ %s\n' \
      "$active_pane" "$session_id" "$session_marker" "$session_name"

    window_number=0
    while IFS=$'\t' read -r pane_id window_id window_index window_name; do
      window_number=$((window_number + 1))
      if ((window_number == window_count)); then
        branch='└'
      else
        branch='├'
      fi
      if [[ $window_id == "$current_window" ]]; then
        window_marker='* '
      else
        window_marker='  '
      fi
      printf '%s\t%s\t    %s─ %s%s: %s\n' \
        "$pane_id" "$window_id" "$branch" "$window_marker" "$window_index" "$window_name"
    done < <(tmux list-windows -t "$session_id" \
      -F $'#{pane_id}\t#{window_id}\t#{window_index}\t#{window_name}')
  done < <(tmux list-sessions \
    -F $'#{session_id}\t#{session_name}\t#{session_windows}\t#{pane_id}')
}

pick_window() {
  local client context current_session current_window rows result
  local switch_target delete_id ignored

  client=$(tmux display-message -p '#{client_name}')
  while :; do
    context=$(client_context "$client")
    IFS=$'\t' read -r current_session current_window <<< "$context"
    rows=$(window_tree "$current_session" "$current_window")

    if ! result=$(printf '%s\n' "$rows" | fzf "${fzf_common[@]}" \
        --prompt='window> ' \
        --header='Enter: switch  •  Ctrl-X: kill window/session  •  Esc: cancel' \
        --preview='tmux capture-pane -ep -t {1} -S -1000 2>/dev/null | tail -n "${FZF_PREVIEW_LINES:-50}"' \
        --preview-window='right,60%,wrap'); then
      return
    fi

    parse_result "$result"
    IFS=$'\t' read -r switch_target delete_id ignored <<< "$PICK_SELECTION"
    if [[ $PICK_KEY == ctrl-x ]]; then
      delete_target windows "$delete_id" "$client" "$current_session" "$current_window"
      [[ $DELETE_CONTINUE == 1 ]] && continue
      return 0
    fi

    # Targeting the active pane moves across both sessions and windows at once.
    [[ -n $switch_target ]] && tmux switch-client -c "$client" -t "$switch_target"
    return
  done
}

case "$mode" in
  sessions) pick_session ;;
  windows) pick_window ;;
  reopen-after-kill) reopen_after_kill "${2:?picker mode}" "${3:?target}" "${4:?client}" ;;
  *)
    echo "usage: $0 {sessions|windows}" >&2
    exit 2
    ;;
esac
