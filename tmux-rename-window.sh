#!/usr/bin/env bash
# tmux-rename-window.sh — rename the current window via an fzf popup pre-filled
# with useful suggestions; type a custom name to override.
#
# Invoked from a binding that stashes context (display-popup doesn't expand #{...}):
#   bind a set -gF @rn_win  "#{window_id}"          \; \
#          set -gF @rn_path "#{pane_current_path}"  \; \
#          set -gF @rn_cmd  "#{pane_current_command}" \; \
#          set -gF @rn_sess "#{session_name}"       \; \
#          set -gF @rn_pane "#{pane_id}"            \; \
#          display-popup -E "~/.config/tmux/tmux-rename-window.sh"
#
# Testing hook: set RN_NAME=<name> to skip fzf.
set -euo pipefail

win=$(tmux show -gv @rn_win 2>/dev/null || true)
path=$(tmux show -gv @rn_path 2>/dev/null || true)
cmd=$(tmux show -gv @rn_cmd 2>/dev/null || true)
sess=$(tmux show -gv @rn_sess 2>/dev/null || true)
pane=$(tmux show -gv @rn_pane 2>/dev/null || true)
[ -n "$win" ] || { echo "no source window" >&2; exit 1; }

base=$(basename "$path" 2>/dev/null || true)
branch=$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || true)

# Suggestions (mirrors the old rename menu); drop empties and dupes, keep order.
cands=$(printf '%s\n' \
  "agent-$base" \
  "$base" \
  "$cmd" \
  "$cmd-$base" \
  "$sess" \
  ${branch:+"$branch" "agent-$branch" "$cmd-$branch"} \
  "$path" \
  "$pane" \
  | awk 'NF && !seen[$0]++')

name=${RN_NAME:-}
if [ -z "$name" ]; then
  set +e
  out=$(printf '%s\n' "$cands" | fzf --print-query --reverse --height 100% \
          --prompt 'rename window> ' \
          --header 'Pick a suggestion or type a custom name')
  code=$?
  set -e
  [ "$code" -eq 130 ] && exit 0            # Esc / Ctrl-C: do nothing
  name=$(printf '%s' "$out" | tail -n 1)
fi
[ -z "$name" ] && exit 0

# Turn off automatic-rename so the chosen name sticks, then rename.
tmux set-option -w -t "$win" automatic-rename off
tmux rename-window -t "$win" "$name"
