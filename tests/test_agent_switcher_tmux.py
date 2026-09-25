"""Opt-in real tmux + fzf test; never connects to the user's tmux server.

TMUX_AGENT_INTEGRATION=1 python3 -m unittest discover -s tests -v
"""
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest


@unittest.skipUnless(os.environ.get("TMUX_AGENT_INTEGRATION") == "1"
                     and shutil.which("tmux") and shutil.which("fzf"), "opt-in tmux/fzf integration")
class TmuxIntegration(unittest.TestCase):
    def test_popup_refresh_cancel_and_cross_session_navigation(self):
        socket = "agent-switcher-test-" + str(os.getpid())
        env = {**os.environ, "TERM": "xterm-256color"}
        for key in ("TMUX", "TMUX_PANE", "TMUX_AGENT_CLIENT", "FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE"):
            env.pop(key, None)

        def tmux(*args):
            return subprocess.check_output(["tmux", "-L", socket, *args], env=env, text=True).strip()

        with tempfile.TemporaryDirectory(prefix="tmux-agent-test-") as directory:
            master, slave = pty.openpty()
            client = None
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 160, 0, 0))
                origin = tmux("-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}",
                              "-s", "origin", "-x", "160", "-y", "45", "-c", directory, "sleep 90")
                # A harmless stand-in with the same process name and TTY semantics.
                command = "printf " + shlex.quote("\\n" * 50 + "AGENT SCREEN SENTINEL\\n")
                command += "; exec -a pi " + shlex.quote(shutil.which("sleep")) + " 90"
                target = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "target",
                              "-c", directory, "bash -c " + shlex.quote(command))
                tmux("set-option", "-p", "-t", target, "@pi_session_name", "integration-agent")
                sibling = tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "target:",
                               "-c", directory, "bash -c " + shlex.quote(
                                   command.replace("AGENT SCREEN SENTINEL", "SECOND AGENT SCREEN")))
                tmux("set-option", "-p", "-t", sibling, "@pi_session_name", "other-agent")
                switcher = str(Path(__file__).resolve().parents[1] / "tmux-switcher.sh")
                # Use the same popup binding as tmux.conf, with this checkout's path.
                tmux("bind-key", "t", "display-popup", "-E", "-w", "100%", "-h", "100%",
                     "-e", "TMUX_AGENT_CLIENT=#{client_name}", shlex.quote(switcher) + " agents")
                client = subprocess.Popen(["tmux", "-L", socket, "attach-session", "-t", "origin"],
                                          env=env, stdin=slave, stdout=slave, stderr=slave)

                def read_for(seconds):
                    end, output = time.monotonic() + seconds, b""
                    while time.monotonic() < end:
                        if select.select([master], [], [], 0.1)[0]:
                            output += os.read(master, 65536)
                    return output.decode(errors="replace")

                def wait_for(needle, seconds=12):
                    end, output = time.monotonic() + seconds, ""
                    while time.monotonic() < end:
                        output += read_for(0.2)
                        # tmux may encode spaces as cursor-right when redrawing
                        # a previously visited pane instead of writing them out.
                        readable = re.sub(r"\x1b\[(\d*)C",
                                          lambda m: " " * int(m.group(1) or 1), output)
                        readable = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", readable)
                        if needle in readable:
                            return readable
                    readable = re.sub(r" {3,}", " ", readable)
                    self.fail(f"Missing {needle!r} in popup output: {readable[-8000:]!r}")

                read_for(0.5)
                client_name = tmux("list-clients", "-F", "#{client_name}")
                os.write(master, b"\x02t")  # Default C-b prefix on the isolated server.
                screen = wait_for("AGENT SCREEN SENTINEL")
                self.assertIn("integration-agent", screen)
                self.assertIn("agent>", screen)
                self.assertIn("Loaded in ", screen)
                self.assertIn("Saved metadata unavailable", screen)
                self.assertIn("▾", screen)
                self.assertNotIn("1 agent ·", screen)
                self.assertIn("└─", screen)
                tmux("set-option", "-p", "-t", target, "@pi_session_name", "refreshed-agent")
                os.write(master, b"\x12")  # Ctrl-R: rescan without leaving the popup.
                wait_for("refreshed-agent")
                os.write(master, b"\x1br")  # Alt-R: refresh the pane preview.
                read_for(0.5)
                os.write(master, b"\x1b")  # Esc: never switches the client.
                read_for(0.5)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{pane_id}"), origin)
                os.write(master, b"\x02t")
                wait_for("AGENT SCREEN SENTINEL")
                os.write(master, b"\r")  # Session heading targets its first Pi child.
                read_for(1)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{pane_id}"), target)
                tmux("switch-client", "-c", client_name, "-t", "origin")
                read_for(0.3)
                os.write(master, b"\x02t")
                wait_for("AGENT SCREEN SENTINEL")
                # An exact-match query (with a shell quote) retains the heading,
                # but retargets its preview/navigation to the matching second Pi.
                os.write(master, b"'other-agent")
                wait_for("SECOND AGENT SCREEN")
                tmux("set-option", "-p", "-t", sibling, "@pi_session_name", "other-agent-renamed")
                os.write(master, b"\x12")  # Rescan must preserve and reapply the query.
                wait_for("-renamed")  # tmux may redraw only the changed suffix.
                os.write(master, b"\x1b[B\r")  # Select the matching child directly.
                read_for(1)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{pane_id}"), sibling)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{session_name}"), "target")
            finally:
                subprocess.run(["tmux", "-L", socket, "kill-server"], env=env, capture_output=True)
                if client:
                    client.wait(timeout=5)
                os.close(master)
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
