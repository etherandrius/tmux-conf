#!/usr/bin/env bash
# tmux-move-window.sh — move a window to an existing or new session, chosen via fzf.
#
# Invoked from a tmux binding. display-popup does NOT expand #{...} formats in its
# command, so the source window/session are stashed by the binding via `set -gF`
# (which does expand) and read back here:
#   bind . set -gF @mw_src_win "#{window_id}" \; \
#          set -gF @mw_src_sess "#{session_name}" \; \
#          display-popup -E "~/.config/tmux/tmux-move-window.sh"
#
# fzf --print-query lets you pick an existing session OR type a new name to create it.
# Testing hook: set MW_DEST=<name> to skip fzf; args $1/$2 override the stashed source.
set -euo pipefail

src_win=${1:-$(tmux show -gv @mw_src_win 2>/dev/null || true)}
src_sess=${2:-$(tmux show -gv @mw_src_sess 2>/dev/null || true)}
[ -n "$src_win" ] && [ -n "$src_sess" ] || { echo "no source window/session" >&2; exit 1; }

choice=${MW_DEST:-}
if [ -z "$choice" ]; then
  # Candidate destinations: every session except the one we're moving out of.
  sessions=$(tmux list-sessions -F '#{session_name}' | grep -vFx "$src_sess" || true)
  # fzf exit codes: 0 = match chosen, 1 = no match (=> new name), 130 = aborted.
  set +e
  out=$(printf '%s\n' "$sessions" | fzf --print-query --reverse --height 100% \
          --prompt 'move window to session> ' \
          --header 'Enter an existing session, or type a new name to create it')
  code=$?
  set -e
  [ "$code" -eq 130 ] && exit 0            # Esc / Ctrl-C: do nothing
  choice=$(printf '%s' "$out" | tail -n 1)
fi
[ -z "$choice" ] && exit 0

if tmux has-session -t "=$choice" 2>/dev/null; then
  created=0
else
  tmux new-session -d -s "$choice"          # placeholder window removed after the move
  created=1
fi

# Follow to the destination BEFORE moving. If this is the source session's last
# window, the move destroys that session; switching the client away first means
# the client is no longer attached to it, so tmux doesn't detach/close.
tmux switch-client -t "=$choice" 2>/dev/null || true

tmux move-window -s "$src_win" -t "$choice:"

if [ "$created" = 1 ]; then
  tmux list-windows -t "=$choice" -F '#{window_id}' \
    | grep -vFx "$src_win" \
    | while read -r w; do tmux kill-window -t "$w"; done
  tmux move-window -r -t "=$choice:"        # renumber so our window sits at base-index
fi

tmux select-window -t "$src_win" 2>/dev/null || true
