#!/usr/bin/env python3
"""Read-only discovery/preview for <prefix> t. Requires Python 3, tmux and fzf.

Process discovery and saved-session matching follow meta-workstrees' audit, but
TTY identity (never cwd) determines navigation. No dependency on that checkout.
Saved metadata is best-effort: Pi does not publish its current session file to
other processes. In particular, /new and /resume can outlive process start time.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata

from tmux_tree_filter import load_label, matching_ids, read_cache, write_cache

SCRIPT = str(Path(__file__).resolve())
PANE_FIELDS = (
    "pane_id", "pane_tty", "pane_pid", "session_id", "session_name",
    "window_id", "window_index", "window_name", "pane_index",
    "pane_current_path", "@pi_session_name", "@switcher_color", "session_last_attached",
)
COLORS = {"blue": 34, "magenta": 35, "red": 31, "yellow": 33, "green": 32, "cyan": 36}


def run(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace",
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "command unavailable")


def clean(value):
    """Metadata is literal text, never terminal controls, TSV or shell syntax."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if not unicodedata.category(c).startswith("C") else " "
                            for c in value).split())


def timestamp(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0


def age(value, now=None):
    if not value:
        return "unknown"
    seconds = max(0, int((now or time.time()) - value))
    for divisor, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= divisor:
            return f"{seconds // divisor}{unit} ago"
    return "just now"


def short_path(value):
    home = str(Path.home())
    return "~" + value[len(home):] if value == home or value.startswith(home + "/") else value


def parse_processes(output):
    processes = {}
    for line in output.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, parent, tty, state, command = parts[:5]
        args = parts[5] if len(parts) == 6 else command
        processes[int(pid)] = dict(pid=int(pid), parent=int(parent), tty=tty,
                                   state=state, command=command, args=args)
    return processes


def is_pi(process):
    command = Path(process["command"]).name
    try:
        argv = shlex.split(process["args"])
    except ValueError:
        argv = []
    first = Path(argv[0]).name if argv else ""
    if command == "pi" or first == "pi":
        return True
    # Before Pi sets its process title (or versions which leave it as node).
    return (command in ("node", "nodejs", "bun") and len(argv) > 1
            and "/pi-coding-agent/" in argv[1] and argv[1].endswith("/cli.js"))


def ancestor_is_pi(process, processes):
    seen = {process["pid"]}
    parent = process["parent"]
    while parent in processes and parent not in seen:
        seen.add(parent)
        if is_pi(processes[parent]):
            return True
        parent = processes[parent]["parent"]
    return False


def parse_panes(output):
    panes = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == len(PANE_FIELDS) and re.fullmatch(r"%\d+", parts[0]):
            panes.append(dict(zip(PANE_FIELDS, parts)))
    return panes


def find_agents(processes, panes, current_session):
    """Only top-level Pi processes with an actual tmux controlling terminal.

    Child/headless agents often inherit TMUX_PANE, so that environment variable
    is NOT evidence they have an interactive pane. Detached workers are omitted.
    Linked windows appear once, preferring their link in the current session.
    """
    by_tty = {}
    for pane in sorted(panes, key=lambda p: (
            p["session_id"] != current_session,
            -int(p["session_last_attached"] or 0), p["session_name"])):
        by_tty.setdefault(pane["pane_tty"].removeprefix("/dev/"), pane)
    agents = []
    for process in processes.values():
        pane = by_tty.get(process["tty"])
        if not pane or not is_pi(process) or ancestor_is_pi(process, processes):
            continue
        # Explicit print/RPC invocations aren't navigable interactive agents.
        try:
            argv = shlex.split(process["args"])
        except ValueError:
            argv = []
        if any(arg in ("-p", "--print", "--mode=json", "--mode=rpc") for arg in argv):
            continue
        if "--mode" in argv and any(mode in argv for mode in ("json", "rpc")):
            continue
        agents.append({**pane, **process, "cwd": pane["pane_current_path"],
                       "started": 0.0})
    # Shell job control can leave several Pi processes on one TTY. There is
    # only one screen to navigate to: prefer its foreground job, once per pane.
    by_pane = {}
    for agent in sorted(agents, key=lambda a: ("+" not in a["state"], -a["pid"])):
        by_pane.setdefault(agent["pane_id"], agent)
    return list(by_pane.values())


def process_details(agents):
    """Batch calls: /proc on Linux, lsof fallback on macOS, like the audit."""
    unresolved = []
    for agent in agents:
        try:
            agent["cwd"] = str((Path("/proc") / str(agent["pid"]) / "cwd").resolve(strict=True))
        except OSError:
            unresolved.append(agent)
    if unresolved:
        by_pid = {a["pid"]: a for a in unresolved}
        output = run(["lsof", "-a", "-p", ",".join(map(str, by_pid)), "-d", "cwd", "-Fn"]).stdout
        pid = None
        for line in output.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("n") and pid in by_pid:
                by_pid[pid]["cwd"] = line[1:]
    if agents:
        by_pid = {a["pid"]: a for a in agents}
        output = run(["env", "LC_ALL=C", "ps", "-p", ",".join(map(str, by_pid)),
                      "-o", "pid=,lstart="]).stdout
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 6 and fields[0].isdigit() and int(fields[0]) in by_pid:
                try:
                    by_pid[int(fields[0])]["started"] = dt.datetime.strptime(
                        " ".join(fields[1:]), "%a %b %d %H:%M:%S %Y").timestamp()
                except ValueError:
                    pass


def session_roots():
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return [Path(override).expanduser()]
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return [
        Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "pi/sessions",
        Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser() / "sessions",
        config_home / "pi/agent/sessions",
    ]


def reverse_entries(path, wanted=None):
    """Read backwards; skip JSON decoding for record types no longer needed.

    Pi writes literal type names. The byte check is only a prefilter: JSON is
    still decoded and its top-level type checked by callers. `wanted` may shrink
    while iterating, avoiding decoding large historical tool/image payloads.
    """
    def decode(line):
        if wanted is not None and not any(f'"{kind}"'.encode() in line for kind in wanted):
            return None
        try:
            entry = json.loads(line)
            return entry if isinstance(entry, dict) else None
        except (ValueError, UnicodeError):
            return None

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position, remainder = handle.tell(), b""
            while position:
                size = min(65536, position)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + remainder).split(b"\n")
                remainder = lines[0]
                for line in reversed(lines[1:]):
                    entry = decode(line)
                    if entry is not None:
                        yield entry
            entry = decode(remainder)
            if entry is not None:
                yield entry
    except OSError:
        return


def text_content(content):
    if isinstance(content, str):
        return clean(content)
    if isinstance(content, list):
        return clean(" ".join(block.get("text", "") for block in content
                              if isinstance(block, dict) and block.get("type") == "text"
                              and isinstance(block.get("text"), str)))
    return ""


def session_name(path):
    for entry in reverse_entries(path, {"session_info"}):
        if entry.get("type") == "session_info":
            return clean(entry.get("name"))
    return ""


def session_metadata(path):
    data = {}
    wanted = {"session_info", "thinking_level_change", "model_change", "message"}
    for entry in reverse_entries(path, wanted):
        kind = entry.get("type")
        if kind == "session_info" and "name" not in data:
            data["name"] = clean(entry.get("name"))  # Empty name clears the old one.
        elif kind == "thinking_level_change" and "thinking" not in data:
            data["thinking"] = clean(entry.get("thinkingLevel"))
        elif kind == "model_change" and "model" not in data:
            data["model"] = clean(entry.get("modelId"))
            data["provider"] = clean(entry.get("provider"))
        elif kind == "message" and isinstance(entry.get("message"), dict):
            message = entry["message"]
            role = message.get("role")
            if "last_message" not in data:
                saved_time = timestamp(entry.get("timestamp"))
                if not saved_time and isinstance(message.get("timestamp"), (int, float)):
                    saved_time = message["timestamp"] / 1000
                data["last_message"] = saved_time
                data["last_role"] = clean(role)
                data["last_tool"] = clean(message.get("toolName"))
            if role == "user" and "prompt" not in data:
                data["prompt"] = text_content(message.get("content"))[:500]
            if role == "assistant" and "model" not in data:
                data["model"] = clean(message.get("model"))
                data["provider"] = clean(message.get("provider"))
        for key, kind in (("name", "session_info"), ("thinking", "thinking_level_change"), ("model", "model_change")):
            if key in data:
                wanted.discard(kind)
        if all(key in data for key in ("last_message", "prompt", "model")):
            wanted.discard("message")
        if not wanted:
            break
    return data


def session_candidates(cwds):
    candidates, seen = [], set()
    for root in session_roots():
        for path in root.glob("**/*.jsonl"):
            try:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                with path.open(encoding="utf-8") as handle:
                    header = json.loads(handle.readline())
                if not isinstance(header, dict) or header.get("type") != "session":
                    continue
                cwd = header.get("cwd")
                if not isinstance(cwd, str) or not os.path.isabs(cwd) or os.path.abspath(cwd) not in cwds:
                    continue
                candidates.append(dict(path=str(resolved), cwd=os.path.abspath(cwd),
                                       created=timestamp(header.get("timestamp")),
                                       mtime=path.stat().st_mtime,
                                       session_id=clean(header.get("id"))))
            except (OSError, ValueError, UnicodeError):
                continue
    return candidates


def match_sessions(agents, candidates):
    """One-to-one, best-effort. Prefer live published names, then recent files.

    Unlike the audit's start-time-first rule, prefer activity since launch:
    the same Pi process may have switched sessions many times using /new.
    Metadata is explicitly labelled as a saved-session estimate in the UI.
    """
    available = sorted(candidates, key=lambda c: c["mtime"], reverse=True)
    names = {}

    def name_of(candidate):
        path = candidate["path"]
        if path not in names:
            names[path] = session_name(Path(path))
        return names[path]

    # Named panes claim their files first, before unnamed panes in the same cwd.
    for agent in sorted(agents, key=lambda a: (not bool(a["@pi_session_name"]), -a["started"])):
        if "--no-session" in agent["args"].split():
            continue
        matches = [c for c in available if c["cwd"] == os.path.abspath(agent["cwd"])]
        name = clean(agent["@pi_session_name"])
        # Newest first, stopping at the first matching name. Don't parse full
        # metadata for every historical session just to find a display name.
        candidate = next((c for c in matches if not name or name_of(c) == name), None)
        if candidate is None:
            continue
        available.remove(candidate)
        agent["saved"] = {**candidate, **session_metadata(Path(candidate["path"]))}
        agent["match"] = "pane name + cwd" if name else "cwd + recency"


def discover(current_session):
    pane_format = "\t".join("#{" + key + "}" for key in PANE_FIELDS)
    panes = parse_panes(run(["tmux", "list-panes", "-a", "-F", pane_format]).stdout)
    processes = parse_processes(run(["ps", "-axo", "pid=,ppid=,tty=,stat=,comm=,args="]).stdout)
    agents = find_agents(processes, panes, current_session)
    process_details(agents)
    if agents:
        match_sessions(agents, session_candidates({os.path.abspath(a["cwd"]) for a in agents}))
    return agents


def display_name(agent):
    return (clean(agent["@pi_session_name"]) or agent.get("saved", {}).get("name")
            or Path(agent["cwd"]).name or "unnamed Pi")


def location(agent):
    return f'{agent["session_name"]}:{agent["window_index"]}.{agent["pane_index"]}'


def matching_names(agents, query):
    """Use fzf's usual fuzzy/extended syntax, but only against literal names.

    The interactive fzf runs with --disabled so it cannot discard parent rows.
    This batch matcher has no preview/bindings and never reads process state.
    """
    names = {}
    for agent in agents:
        names[agent["session_id"]] = clean(agent["session_name"])
        names[agent["pane_id"]] = clean(display_name(agent))
    return matching_ids(names, query)


def rows(agents, current_session, current_pane, query=""):
    """Session headings with Pi children, matching the window switcher's tree.

    Two hidden fields hold the target pane and row identity. A session heading
    previews/navigates to its first Pi child; it doesn't add a snapshot entry.
    """
    matches = matching_names(agents, query) if query else None
    sessions = {}
    for agent in agents:
        sessions.setdefault(agent["session_id"], []).append(agent)
    groups = sorted(sessions.values(), key=lambda group: (
        group[0]["session_id"] != current_session,
        -int(group[0]["session_last_attached"] or 0), group[0]["session_name"]))
    output = []
    for group in groups:
        # Session-name matches retain the entire group. Otherwise retain only
        # matching Pi children, then rebuild their heading and tree connectors.
        if matches is not None and group[0]["session_id"] not in matches:
            group = [agent for agent in group if agent["pane_id"] in matches]
        if not group:
            continue
        group = sorted(group, key=lambda a: (
            a["pane_id"] != current_pane, -a.get("saved", {}).get("last_message", 0),
            int(a["window_index"]), int(a["pane_index"])))
        first = group[0]
        marker = "* " if first["session_id"] == current_session else "  "
        code = COLORS.get(first["@switcher_color"], 39)
        name = f'\033[{code}m{clean(first["session_name"])}\033[0m'
        attached = age(int(first["session_last_attached"] or 0))
        output.append(f'{first["pane_id"]}\t{first["session_id"]}\t{marker}▾ '
                      f'\033[2m({attached})\033[0m {name}')

        for index, agent in enumerate(group):
            branch = "└" if index == len(group) - 1 else "├"
            marker = "* " if agent["pane_id"] == current_pane else "  "
            folder = clean(Path(agent["cwd"]).name) or "/"
            saved_time = agent.get("saved", {}).get("last_message")
            last = f'~{age(saved_time)}' if saved_time else "time unknown"
            # Tabs delimit hidden IDs only; visible fields use single spaces.
            # Name matching is handled separately by matching_names().
            label = (f'    {branch}─ {marker}\033[2m{last}\033[0m '
                     f'{display_name(agent)} \033[2m{folder}\033[0m')
            output.append(f'{agent["pane_id"]}\t{agent["pane_id"]}\t{label}')
    return "\n".join(output) + ("\n" if output else "")


def refresh(snapshot, current_session, current_pane, query=""):
    start = time.perf_counter()
    agents = discover(current_session)
    result = rows(agents, current_session, current_pane, query)
    # Atomic replacement prevents a concurrently-running preview reading half JSON.
    write_cache(snapshot, {"agents": {a["pane_id"]: a for a in agents},
                           "load_seconds": time.perf_counter() - start})
    return result


def load_snapshot(snapshot):
    return read_cache(snapshot).get("agents", {})


def picker_output(snapshot, data):
    # fzf treats this first line as a nonselectable header, never a search row.
    return "\t\t" + load_label(read_cache(snapshot).get("load_seconds", 0)) + "\n" + data


def filter_rows(snapshot, current_session, current_pane, query):
    return rows(list(load_snapshot(snapshot).values()), current_session, current_pane, query)


def load_agent(snapshot, pane):
    return load_snapshot(snapshot).get(pane)


def still_running(agent):
    result = run(["ps", "-p", str(agent["pid"]), "-o", "pid=,ppid=,tty=,stat=,comm=,args="])
    process = parse_processes(result.stdout).get(agent["pid"])
    return bool(process and is_pi(process) and process["tty"] == agent["tty"])


def preview(snapshot, pane):
    agent = load_agent(snapshot, pane)
    if not agent:
        print("No live Pi agents in tmux. Ctrl-R to refresh; Esc to close.")
        return
    if not still_running(agent):
        print("This Pi agent has exited. Ctrl-R to refresh the list.")
        return
    saved = agent.get("saved", {})
    # Refresh saved fields when previewing, without rescanning the whole machine.
    if saved:
        saved = {**saved, **session_metadata(Path(saved["path"]))}
    branch = run(["git", "-C", agent["cwd"], "symbolic-ref", "--short", "HEAD"], timeout=1).stdout.strip()
    if not branch:
        branch = run(["git", "-C", agent["cwd"], "rev-parse", "--short", "HEAD"], timeout=1).stdout.strip()
    model = "/".join(filter(None, (saved.get("provider"), saved.get("model")))) or "unknown"
    last = saved.get("last_role", "unknown")
    if saved.get("last_tool"):
        last += ":" + saved["last_tool"]
    match = f'~ saved metadata ({agent["match"]}; best-effort)' if saved else "Saved metadata unavailable"
    header = [
        f'\033[1m{display_name(agent)}\033[0m',
        f'Tmux: {clean(location(agent))} · {clean(agent["window_name"])} · {pane} · PID {agent["pid"]}',
        f'Cwd: {clean(short_path(agent["cwd"]))}' + (f'  [{clean(branch)}]' if branch else ""),
        f'Model: {model} · thinking: {saved.get("thinking", "unknown")}',
        f'Last saved: {age(saved.get("last_message"))} ({last}) · started {age(agent["started"])}',
        f'Prompt: {saved.get("prompt") or "unavailable"}',
        match,
        '\033[2m── pane screen (Alt-R refresh) ──\033[0m',
    ]
    print("\n".join(header))
    result = run(["tmux", "capture-pane", "-e", "-p", "-t", pane])
    if result.returncode:
        print("Pane is no longer available. Ctrl-R to refresh.")
    else:
        # Keep the bottom of the screen, including Pi's editor/footer, in view.
        height = max(1, int(os.environ.get("FZF_PREVIEW_LINES", "50")) - len(header))
        print("\n".join(result.stdout.splitlines()[-height:]), end="\033[0m\n")


def pick():
    client = os.environ.get("TMUX_AGENT_CLIENT", "")
    context_args = ["tmux", "display-message", "-p"]
    if client:
        context_args += ["-c", client]
    context = run(context_args + ["#{client_name}\t#{session_id}\t#{pane_id}"]).stdout.strip().split("\t")
    if len(context) != 3:
        print("Open the agent switcher from an attached tmux client.", file=sys.stderr)
        return 1
    client, current_session, current_pane = context
    with tempfile.TemporaryDirectory(prefix="tmux-agents-") as directory:
        snapshot = str(Path(directory) / "agents.json")
        initial = picker_output(snapshot, refresh(snapshot, current_session, current_pane))
        command = shlex.join([sys.executable, SCRIPT])
        reload_command = shlex.join([sys.executable, SCRIPT, "rows", snapshot, current_session, current_pane])
        filter_command = shlex.join([sys.executable, SCRIPT, "filter", snapshot, current_session, current_pane])
        options = [
            # The helper matches cached names and restores parent headings.
            # Don't let fzf's own matching hide those nonmatching headings again.
            "fzf", "--disabled", "--delimiter=\t", "--with-nth=3..", "--layout=reverse", "--height=100%",
            "--info=inline", "--cycle", "--no-multi", "--ansi", "--track", "--no-sort", "--header-lines=1",
            "--prompt=agent> ",
            "--header=Enter: switch · Ctrl-R: rescan\nAlt-R: preview · Esc: cancel",
            # fzf shell-quotes {q}, including empty queries and embedded quotes.
            f"--bind=start:reload({filter_command} {{q}}),change:reload({filter_command} {{q}}),"
            f"ctrl-r:reload({reload_command} {{q}}),alt-r:refresh-preview",
            f"--preview={command} preview {shlex.quote(snapshot)} {{1}}",
            "--preview-window=right,60%,nowrap",
        ]
        result = subprocess.run(options, input=initial, stdout=subprocess.PIPE, text=True, check=False)
        if result.returncode in (1, 130):  # No match / Esc: leave client untouched.
            return 0
        if result.returncode:
            return result.returncode
        pane = result.stdout.split("\t", 1)[0].strip()
        agent = load_agent(snapshot, pane)
        if not agent or not still_running(agent):
            run(["tmux", "display-message", "-c", client, "Pi agent exited; reopen Ctrl-g t to refresh."])
            return 0
        target = f'{agent["session_id"]}:{agent["window_id"]}.{pane}'
        switched = run(["tmux", "switch-client", "-c", client, "-t", target])
        if switched.returncode:
            run(["tmux", "display-message", "-c", client, "Agent pane is no longer available."])
        return 0


def main():
    if len(sys.argv) in (5, 6) and sys.argv[1] == "rows":
        print(picker_output(sys.argv[2], refresh(*sys.argv[2:])), end="")
        return 0
    if len(sys.argv) == 6 and sys.argv[1] == "filter":
        print(picker_output(sys.argv[2], filter_rows(*sys.argv[2:])), end="")
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "preview":
        preview(*sys.argv[2:])
        return 0
    if len(sys.argv) == 1:
        return pick()
    print(f"usage: {sys.argv[0]} [rows SNAPSHOT SESSION PANE [QUERY] | "
          "filter SNAPSHOT SESSION PANE QUERY | preview SNAPSHOT PANE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Agent switcher: {exc}", file=sys.stderr)
        sys.exit(1)
