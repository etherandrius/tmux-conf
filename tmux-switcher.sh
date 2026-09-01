#!/usr/bin/env bash
# Fuzzy replacements for tmux's default choose-tree bindings:
#   <prefix> s  -> sessions
#   <prefix> w  -> windows across all sessions
set -euo pipefail

mode=${1:-}
script_path=$0
[[ $script_path == /* ]] || script_path="$PWD/${script_path#./}"
now_epoch=''
session_format=$'#{session_id}\t#{pane_id}\t#{session_name}\t#{session_windows}\t#{?session_last_attached,#{session_last_attached},0}\t#{?@switcher_color,#{@switcher_color},default}'
window_format=$'#{session_id}\t#{session_name}\t#{session_windows}\t#{?session_last_attached,#{session_last_attached},0}\t#{?@switcher_color,#{@switcher_color},default}\t#{pane_id}\t#{window_id}\t#{window_index}\t#{window_name}\t#{window_stack_index}'
next_color_format='#{?#{==:#{@switcher_color},red},yellow,#{?#{==:#{@switcher_color},yellow},green,#{?#{==:#{@switcher_color},green},cyan,#{?#{==:#{@switcher_color},cyan},blue,#{?#{==:#{@switcher_color},blue},magenta,#{?#{==:#{@switcher_color},magenta},,red}}}}}}'

fzf_common=(
  --delimiter=$'\t'
  --with-nth=3
  --expect=ctrl-x
  --layout=reverse
  --height=100%
  --info=inline
  --cycle
  --no-multi
  --ansi
  --track
)

relative_time() {
  local timestamp=$1 delta value suffix
  if [[ $timestamp == 0 ]]; then
    RELATIVE_TIME='never'
    return
  fi

  delta=$((now_epoch - timestamp))
  ((delta < 0)) && delta=0
  if ((delta < 60)); then
    RELATIVE_TIME='just now'
  elif ((delta < 3600)); then
    value=$((delta / 60))
    ((value == 1)) && suffix='' || suffix='s'
    RELATIVE_TIME="${value}min${suffix} ago"
  elif ((delta < 86400)); then
    value=$((delta / 3600))
    ((value == 1)) && suffix='' || suffix='s'
    RELATIVE_TIME="${value}hr${suffix} ago"
  elif ((delta < 604800)); then
    value=$((delta / 86400))
    ((value == 1)) && suffix='' || suffix='s'
    RELATIVE_TIME="${value}day${suffix} ago"
  elif ((delta < 2629800)); then
    value=$((delta / 604800))
    ((value == 1)) && suffix='' || suffix='s'
    RELATIVE_TIME="${value}wk${suffix} ago"
  elif ((delta < 31557600)); then
    value=$((delta / 2629800))
    ((value == 1)) && suffix='' || suffix='s'
    RELATIVE_TIME="${value}mo${suffix} ago"
  else
    value=$((delta / 31557600))
    ((value == 1)) && suffix='' || suffix='s'
    RELATIVE_TIME="${value}yr${suffix} ago"
  fi
}

color_session_name() {
  local color=$1 name=$2 code
  case "$color" in
    red) code=31 ;;
    yellow) code=33 ;;
    green) code=32 ;;
    cyan) code=36 ;;
    blue) code=34 ;;
    magenta) code=35 ;;
    *) COLORED_SESSION_NAME=$name; return ;;
  esac
  COLORED_SESSION_NAME=$'\033['"${code}m${name}"$'\033[0m'
}

# Current session first, then all others by most recent attachment. The final
# two fields are internal sort keys consumed by callers but never displayed.
sorted_sessions() {
  local current=$1 data=${2-}
  local session_id pane_id session_name window_count last_attached color rank
  [[ -n $data ]] || data=$(tmux list-sessions -F "$session_format")
  while IFS=$'\t' read -r session_id pane_id session_name window_count last_attached color; do
    [[ $session_id == "$current" ]] && rank=0 || rank=1
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%d\t%020d\n' \
      "$session_id" "$pane_id" "$session_name" "$window_count" \
      "$last_attached" "$color" "$rank" "$last_attached"
  done <<< "$data" \
    | LC_ALL=C sort -t $'\t' -k7,7n -k8,8nr -k3,3
}

color_binding() {
  local picker_mode=$1 current_session=$2 current_pane=$3
  local quoted_script quoted_session quoted_pane
  printf -v quoted_script '%q' "$script_path"
  printf -v quoted_session '%q' "$current_session"
  printf -v quoted_pane '%q' "$current_pane"
  printf 'alt-c:reload(%s recolor-rows %s %s %s %s {1})' \
    "$quoted_script" "$picker_mode" "$quoted_session" "$quoted_pane" "$now_epoch"
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
  local current=$1 data=${2-}
  local session_id pane_id session_name window_count last_attached color
  local rank sort_time number marker

  # Two hidden fields hold the active pane and session ID. Only the final,
  # already-formatted field is displayed, avoiding uneven tabular columns.
  while IFS=$'\t' read -r session_id pane_id session_name window_count last_attached color rank sort_time; do
    number=${session_id#\$}
    [[ $session_id == "$current" ]] && marker='* ' || marker='  '
    relative_time "$last_attached"
    color_session_name "$color" "$session_name"
    printf '%s\t%s\t%s[%s] %s  (%s)\n' \
      "$pane_id" "$session_id" "$marker" "$number" "$COLORED_SESSION_NAME" "$RELATIVE_TIME"
  done < <(sorted_sessions "$current" "$data")
}

recolor_rows() {
  local picker_mode=$1 current_session=$2 current_pane=$3 selected_pane=$5 data
  now_epoch=$4
  case "$picker_mode" in
    sessions)
      data=$(tmux set-option -F -t "$selected_pane" @switcher_color "$next_color_format" \
        \; list-sessions -F "$session_format")
      session_rows "$current_session" "$data"
      ;;
    windows)
      data=$(tmux set-option -F -t "$selected_pane" @switcher_color "$next_color_format" \
        \; list-windows -a -F "$window_format")
      window_tree "$current_session" "$current_pane" "$data"
      ;;
  esac
}

pick_session() {
  local client context current_session current_window current_pane result bind_color
  local switch_target delete_id ignored

  now_epoch=$(date +%s)
  context=$(tmux display-message -p $'#{client_name}\t#{session_id}\t#{window_id}\t#{pane_id}')
  IFS=$'\t' read -r client current_session current_window current_pane <<< "$context"
  while :; do
    bind_color=$(color_binding sessions "$current_session" "$current_pane")

    if ! result=$(session_rows "$current_session" | fzf "${fzf_common[@]}" \
        --prompt='session> ' \
        --header='Enter: switch  •  Alt-C: colour  •  Ctrl-X: kill session  •  Esc: cancel' \
        --bind="$bind_color" \
        --preview='tmux capture-pane -ep -t {1} -S "-${FZF_PREVIEW_LINES:-50}" 2>/dev/null' \
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

    if [[ -n $switch_target ]]; then
      tmux switch-client -c "$client" -t "$switch_target"
    fi
    return
  done
}

window_tree() {
  local current_session=$1 current_pane=$2 data=${3-}
  local rank sort_time session_id session_name window_count last_attached color
  local pane_id window_id window_index window_name stack_index previous_session=''
  local session_marker window_marker window_number=0 branch

  # list-windows also exposes its owning session's metadata, so one tmux query
  # is enough to build and order the complete tree.
  [[ -n $data ]] || data=$(tmux list-windows -a -F "$window_format")
  while IFS=$'\t' read -r rank sort_time session_name stack_index session_id window_count last_attached color pane_id window_id window_index window_name; do
    if [[ $session_id != "$previous_session" ]]; then
      [[ $session_id == "$current_session" ]] && session_marker='* ' || session_marker='  '
      relative_time "$last_attached"
      color_session_name "$color" "$session_name"
      printf '%s\t%s\t%s▾ %s  (%s)\n' \
        "$pane_id" "$session_id" "$session_marker" "$COLORED_SESSION_NAME" "$RELATIVE_TIME"
      previous_session=$session_id
      window_number=0
    fi

    window_number=$((window_number + 1))
    ((window_number == window_count)) && branch='└' || branch='├'
    [[ $pane_id == "$current_pane" ]] && window_marker='* ' || window_marker='  '
    printf '%s\t%s\t    %s─ %s%s: %s\n' \
      "$pane_id" "$window_id" "$branch" "$window_marker" "$window_index" "$window_name"
  done < <(
    while IFS=$'\t' read -r session_id session_name window_count last_attached color pane_id window_id window_index window_name stack_index; do
      [[ $session_id == "$current_session" ]] && rank=0 || rank=1
      printf '%d\t%020d\t%s\t%020d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$rank" "$last_attached" "$session_name" "$stack_index" "$session_id" \
        "$window_count" "$last_attached" "$color" "$pane_id" "$window_id" \
        "$window_index" "$window_name"
    done <<< "$data" \
      | LC_ALL=C sort -t $'\t' -k1,1n -k2,2nr -k3,3 -k4,4n
  )
}

pick_window() {
  local client context current_session current_window current_pane result bind_color
  local switch_target delete_id ignored

  now_epoch=$(date +%s)
  context=$(tmux display-message -p $'#{client_name}\t#{session_id}\t#{window_id}\t#{pane_id}')
  IFS=$'\t' read -r client current_session current_window current_pane <<< "$context"
  while :; do
    bind_color=$(color_binding windows "$current_session" "$current_pane")

    if ! result=$(window_tree "$current_session" "$current_pane" | fzf "${fzf_common[@]}" \
        --prompt='window> ' \
        --header='Enter: switch  •  Alt-C: colour session  •  Ctrl-X: kill window/session  •  Esc: cancel' \
        --bind="$bind_color" \
        --preview='tmux capture-pane -ep -t {1} -S "-${FZF_PREVIEW_LINES:-50}" 2>/dev/null' \
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
    if [[ -n $switch_target ]]; then
      tmux switch-client -c "$client" -t "$switch_target"
    fi
    return
  done
}

case "$mode" in
  sessions) pick_session ;;
  windows) pick_window ;;
  recolor-rows) recolor_rows "${2:?picker mode}" "${3:?session ID}" "${4:?current pane ID}" "${5:?timestamp}" "${6:?selected pane ID}" ;;
  reopen-after-kill) reopen_after_kill "${2:?picker mode}" "${3:?target}" "${4:?client}" ;;
  *)
    echo "usage: $0 {sessions|windows}" >&2
    exit 2
    ;;
esac
