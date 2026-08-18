#!/usr/bin/env bash
# Fuzzy replacements for tmux's default choose-tree bindings:
#   <prefix> s  -> sessions
#   <prefix> w  -> windows across all sessions
set -euo pipefail

mode=${1:-}

fzf_common=(
  --delimiter=$'\t'
  --with-nth=2
  --layout=reverse
  --height=100%
  --info=inline
  --cycle
  --no-multi
)

pick_session() {
  local current rows selection target
  current=$(tmux display-message -p '#{session_id}')
  rows=$(tmux list-sessions -F $'#{session_id}\t#{pane_id}\t#{session_name}')

  # Keep the visible side to one field so names are not separated by uneven tabs.
  # The hidden first field is the session's active pane, used for switching and preview.
  rows=$(awk -F '\t' -v current="$current" 'BEGIN { OFS="\t" }
    {
      number = $1
      sub(/^\$/, "", number)
      print $2, ($1 == current ? "* " : "  ") "[" number "] " $3
    }' <<< "$rows")

  if ! selection=$(printf '%s\n' "$rows" | fzf "${fzf_common[@]}" \
      --prompt='session> ' \
      --header='Enter: switch session  •  Esc: cancel' \
      --preview='tmux capture-pane -ep -t {1} -S -1000 2>/dev/null | tail -n "${FZF_PREVIEW_LINES:-50}"' \
      --preview-window='right,60%,wrap'); then
    exit 0
  fi

  target=${selection%%$'\t'*}
  [[ -n "$target" ]] && tmux switch-client -t "$target"
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
    printf '%s\t%s▾ %s\n' "$active_pane" "$session_marker" "$session_name"

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
      printf '%s\t    %s─ %s%s: %s\n' \
        "$pane_id" "$branch" "$window_marker" "$window_index" "$window_name"
    done < <(tmux list-windows -t "$session_id" \
      -F $'#{pane_id}\t#{window_id}\t#{window_index}\t#{window_name}')
  done < <(tmux list-sessions \
    -F $'#{session_id}\t#{session_name}\t#{session_windows}\t#{pane_id}')
}

pick_window() {
  local current_session current_window rows selection target
  current_session=$(tmux display-message -p '#{session_id}')
  current_window=$(tmux display-message -p '#{window_id}')
  rows=$(window_tree "$current_session" "$current_window")

  if ! selection=$(printf '%s\n' "$rows" | fzf "${fzf_common[@]}" \
      --prompt='window> ' \
      --header='Enter: switch window  •  Esc: cancel' \
      --preview='tmux capture-pane -ep -t {1} -S -1000 2>/dev/null | tail -n "${FZF_PREVIEW_LINES:-50}"' \
      --preview-window='right,60%,wrap'); then
    exit 0
  fi

  # The first hidden field is the selected window's active pane ID. Targeting
  # the pane lets switch-client move across both sessions and windows at once.
  target=${selection%%$'\t'*}
  [[ -n "$target" ]] && tmux switch-client -t "$target"
}

case "$mode" in
  sessions) pick_session ;;
  windows)  pick_window ;;
  *)
    echo "usage: $0 {sessions|windows}" >&2
    exit 2
    ;;
esac
