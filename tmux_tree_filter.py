#!/usr/bin/env python3
"""Shared name matching and cached parent-preserving filter for tmux pickers."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def matching_ids(names, query):
    """Batch fzf matching: no previews, interactive defaults, or process scans."""
    if not names:
        return set()
    env = {key: value for key, value in os.environ.items()
           if key not in ("FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE")}
    result = subprocess.run(
        ["fzf", "--delimiter=\t", "--nth=2..", "--no-sort", "--filter=" + query],
        input="".join(f"{key}\t{name}\n" for key, name in names.items()),
        capture_output=True, text=True, env=env, timeout=5, check=False)
    if result.returncode not in (0, 1):
        raise OSError("fzf name matching failed: " + result.stderr.strip())
    return {line.split("\t", 1)[0] for line in result.stdout.splitlines()}


def read_cache(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write_cache(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def load_label(seconds):
    return f"Loaded in {seconds * 1000:.0f} ms" if seconds < 1 else f"Loaded in {seconds:.2f} s"


def filter_window_rows(data, query):
    """Rows: pane ID, session/window ID, display label, searchable name.

    Keep groups in input order (already sorted by tmux MRU), not match score.
    Headers navigate to the first visible child but retain their session IDs,
    so Ctrl-X on a header still addresses the session rather than the window.
    """
    if not query:
        return data
    groups = []
    for line in data.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        if fields[1].startswith("$"):
            groups.append([fields, []])
        elif groups:
            groups[-1][1].append(fields)
    names = {row[1]: row[3] for header, children in groups for row in [header, *children]}
    matches = matching_ids(names, query)
    output = []
    for header, children in groups:
        if header[1] not in matches:
            children = [row for row in children if row[1] in matches]
        if not children:
            continue
        header[0] = children[0][0]
        output.append("\t".join(header))
        for index, child in enumerate(children):
            branch = "└" if index == len(children) - 1 else "├"
            child[2] = re.sub(r"^(    )[├└]─", r"\g<1>" + branch + "─", child[2])
            output.append("\t".join(child))
    return "\n".join(output) + ("\n" if output else "")


def window_output(snapshot, query, command=None):
    if command is not None:
        start = time.perf_counter()
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        cache = {"rows": result.stdout, "load_seconds": time.perf_counter() - start}
        write_cache(snapshot, cache)
    else:
        cache = read_cache(snapshot)
    return "\t\t" + load_label(cache.get("load_seconds", 0)) + "\n" + filter_window_rows(cache.get("rows", ""), query)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "filter":
        print(window_output(sys.argv[2], sys.argv[3]), end="")
        return 0
    if len(sys.argv) >= 6 and sys.argv[1] == "update" and sys.argv[4] == "--":
        print(window_output(sys.argv[2], sys.argv[3], sys.argv[5:]), end="")
        return 0
    print(f"usage: {sys.argv[0]} filter SNAPSHOT QUERY | update SNAPSHOT QUERY -- COMMAND...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Tree filter: {exc}", file=sys.stderr)
        sys.exit(1)
