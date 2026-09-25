# Pi / Claude agent switcher

`Ctrl-g t` replaces tmux's default clock with a full-screen fuzzy picker, alongside
`Ctrl-g s` (sessions) and `Ctrl-g w` (windows).

- Type to search **tmux session names and agent names only**. Displayed folder/time,
  Pi/Claude badges, preview metadata, and hidden IDs never participate in matching.
- **Enter** switches the invoking client to the selected agent's session/window/pane.
  On a tmux session heading, it jumps to the first visible agent underneath it.
- **Ctrl-R** rescans processes and saved sessions, preserving the query/selection.
- **Alt-R** refreshes the selected preview, including its saved metadata.
- **Esc** cancels. There are deliberately no kill/delete bindings.

The list is a tree like `Ctrl-g w`: coloured tmux session headings (`▾`) with
indented Pi and Claude Code agents (`├─` / `└─`) underneath. Only sessions containing
supported agents are shown. The current session comes first, followed by most recently attached
sessions. Within each session, the current pane comes first, then agents by
latest saved-message time. `*` marks the current session and pane. Filtering keeps
the tree's order rather than sorting by match score:

- An agent-name match keeps its **parent session heading**, even if the heading
  doesn't match. Other nonmatching agents in that session are hidden.
- A session-name match shows **all its agents**.
- Headings preview/navigate to their first **visible** child. Tree connectors are
  rebuilt after filtering. Clearing the query restores the full tree.
- Matching uses fzf's normal fuzzy/extended syntax (including exact, OR, and
  negated terms), not a separate regular-expression engine.

Pi names come from `@pi_session_name` when available, otherwise from a matched
saved session. Claude names use the saved custom title (`/rename`) or generated
AI title. Both fall back to the working directory's basename; stale Pi pane names
are ignored for Claude. Session headings preview their first visible child.
Dim relative last-saved-message age comes before each agent name (`~` indicates
estimated metadata), with a `[Pi]` / `[Claude]` badge and folder basename after
the name. Session headings likewise show relative last-attachment time before
the name, keeping times visible even for long names. Visible fields are separated
by single spaces, not tab stops; tree indentation is preserved. Full paths,
models, and prompts stay in the preview; window/pane numbers and agent counts
are omitted.

The right-hand preview shows name, tmux location, PID, cwd/Git branch, saved
model/reasoning level, last-message age/type, process start age, and latest user
prompt. Below that is a fresh ANSI capture of the **actual pane screen**, not a
transcript reconstruction. The bottom of the screen is retained when space is
limited, so the agent's editor/footer remains visible. Screens refresh on selection or
Alt-R; this isn't continuous polling.

## Window picker (`Ctrl-g w`)

The window picker uses the same parent-preserving filtering: matching a window
keeps its tmux session heading; matching a session shows the whole group. Its
existing session/MRU window order, pane previews, **Alt-C** session colours, and
**Ctrl-X** window/session deletion are preserved. **Ctrl-R** rescans windows while
retaining the query. Window names/indices and session names are searchable;
relative attachment times and hidden IDs are not.

Both pickers filter private per-popup snapshots while typing, without rescanning
processes or tmux on each keystroke. `tmux_tree_filter.py` supplies the shared fzf
name matcher and the window tree/cache handling.

## Load time and profiling

Both pickers show a nonselectable **Loaded in … ms/s** line. It measures the most
recent data/metadata load, not preview rendering or terminal drawing. Filtering
keeps that measurement; Ctrl-R updates it (as does window recolouring).

Profiling found the Pi picker was parsing full historical sessions just to check
their names. It now tries recent candidates first, reads full metadata only for
selected sessions, and skips JSON decoding of historical message/tool payloads
after the required fields are found.

Local samples with roughly 18–19 live agents:

- Profiled discovery: **1.27 s → 0.58 s**; saved-session matching: **0.93 s → 0.25 s**.
- Normal optimized data loads: **389 ms median** over five runs (first run 658 ms).
- Cached Pi filtering, including starting Python: **58–62 ms** for nonempty queries.
- Window data loads: **36 ms median**; cached filtering including Python: **43 ms**.

Filesystem caches and the amount of history affect these numbers; the displayed
measurement reflects each actual scan. Typing does not reread saved sessions.

## Discovery and accuracy

`tmux-agent-switcher.py` uses only Python 3.9+ stdlib, `tmux`, `fzf`, `ps`, and
optionally `lsof` (macOS cwd lookup) and `git` (preview branch). It adapts the
process/session discovery approach from
`/Volumes/git/meta-workstrees/src/worktree_status/audit.py` but does not import or
depend on that checkout.

- Live Pi/Claude process names (including Node CLI entrypoints) are matched to
  **tmux pane TTYs**, not working directories or inherited `TMUX_PANE` values.
  Two agents in the same cwd still navigate to different panes. Headless/non-tmux
  processes and nested workers of either kind are omitted. Claude `--print`,
  `daemon run`, `bg-pty-host`, and `bg-spare` processes are excluded.
- Linked panes appear once, preferring the current session's link. If shell job
  control leaves multiple agents on one TTY, the foreground one wins.
- Pi saved JSONL discovery supports flat and per-directory stores. An explicit
  `PI_CODING_AGENT_SESSION_DIR` overrides defaults; otherwise the helper checks
  XDG state, `PI_CODING_AGENT_DIR`/legacy `~/.pi/agent`, and XDG config stores.
- Claude saved sessions are read from `~/.claude/projects/*/*.jsonl`, or
  `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl` when set in the popup environment. The
  first 100 records are checked for cwd; nested subagent and older `agent-*`
  transcripts are excluded. Claude metadata includes saved title, model/effort,
  latest user prompt and user/assistant message time, ignoring tool-result text
  as prompts. Missing metadata does not prevent pane discovery/navigation.
- **`~` means saved metadata is a best-effort estimate**, not live agent state.
  For Pi, a pane-published name plus cwd is preferred; unnamed Pi panes and
  Claude panes use cwd and recency. Files are claimed one-to-one within each
  agent kind, never across Pi and Claude. `/new`, `/resume`, ephemeral agents whose
  process title hides their flags, multiple unnamed agents in one cwd, or custom
  stores not visible to the popup can make this association ambiguous/unavailable.
  Reload with Ctrl-R after switching agent sessions. The pane navigation itself is
  independent of this estimate.
- "Last saved" measures a persisted message, not screen activity. It does **not**
  claim an agent is idle/busy, or that a model in an old message is still selected.

The helper reads processes, session files and panes; it does not send keystrokes
to agents, change their state, call an LLM, or write to their session files.
Per-popup metadata snapshots live in a private temporary directory and are
removed when the picker closes. Existing Pi/Claude agents need no restart/extension.

## Tests

```sh
python3 -m unittest discover -s tests -v
TMUX_AGENT_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

The opt-in agent integration tests use their own tmux socket, a PTY client, and
harmless `sleep` processes titled `pi` and `claude`, with temporary saved-session
fixtures. They test Pi-only and mixed-agent popups, fzf filtering/preview/refresh/
cancel, and cross-session navigation without touching the user's tmux server. Window
integration tests also exercise rescan/recolour with an active query and verify
that deleting a filtered child deletes only its window, not its parent session.
