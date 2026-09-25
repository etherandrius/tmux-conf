"""Real window-picker regression test on a disposable tmux server."""
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
class WindowIntegration(unittest.TestCase):
    def test_parent_filter_navigation_refresh_recolor_and_delete(self):
        socket = "window-switcher-test-" + str(os.getpid())
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "TMUX_PANE", "FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE")}
        env["TERM"] = "xterm-256color"

        def tmux(*args):
            return subprocess.check_output(["tmux", "-L", socket, *args], env=env, text=True).strip()

        with tempfile.TemporaryDirectory(prefix="window-picker-test-") as directory:
            master, slave = pty.openpty()
            client = None
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 180, 0, 0))
                origin = tmux("-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}",
                              "-s", "origin", "-x", "180", "-y", "45", "-c", directory, "sleep 90")
                tmux("new-session", "-d", "-s", "target", "-n", "kept", "-c", directory, "sleep 90")
                command = "printf " + shlex.quote("SCRATCH SCREEN SENTINEL\\n" * 60) + "; sleep 90"
                target = tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "target:",
                              "-n", "scratch", "-c", directory, command)
                switcher = str(Path(__file__).resolve().parents[1] / "tmux-switcher.sh")
                tmux("bind-key", "w", "display-popup", "-E", "-w", "100%", "-h", "100%",
                     shlex.quote(switcher) + " windows")
                client = subprocess.Popen(["tmux", "-L", socket, "attach-session", "-t", "origin"],
                                          env=env, stdin=slave, stdout=slave, stderr=slave)

                def read_for(seconds):
                    end, output = time.monotonic() + seconds, b""
                    while time.monotonic() < end:
                        if select.select([master], [], [], 0.1)[0]:
                            output += os.read(master, 65536)
                    return output.decode(errors="replace")

                def wait_for(needle):
                    end, output = time.monotonic() + 10, ""
                    while time.monotonic() < end:
                        output += read_for(0.2)
                        readable = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m.group(1) or 1), output)
                        readable = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", readable)
                        if needle in readable:
                            return
                    self.fail(f"Missing {needle!r}: {readable[-4000:]!r}")

                read_for(0.5)
                client_name = tmux("list-clients", "-F", "#{client_name}")

                def open_filtered():
                    os.write(master, b"\x02w")
                    wait_for("Loaded in ")
                    os.write(master, b"'scratch")
                    wait_for("SCRATCH SCREEN SENTINEL")

                open_filtered()
                os.write(master, b"\r")  # Nonmatching session heading points to matching window.
                read_for(0.6)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{pane_id}"), target)
                tmux("switch-client", "-c", client_name, "-t", "origin")
                read_for(0.3)

                open_filtered()
                tmux("rename-window", "-t", target, "scratch-reloaded")
                os.write(master, b"\x12")  # Ctrl-R, without losing the search.
                wait_for("-reloaded")
                os.write(master, b"\x1bc")  # Alt-C, also without losing the search.
                read_for(0.7)
                self.assertEqual(tmux("show-option", "-v", "-t", "target", "@switcher_color"), "blue")
                os.write(master, b"\r")
                read_for(0.6)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{pane_id}"), target)
                tmux("switch-client", "-c", client_name, "-t", "origin")
                read_for(0.3)

                open_filtered()
                os.write(master, b"\x1b[B\x18")  # Down to child, Ctrl-X kills only that window.
                read_for(1)
                self.assertEqual(tmux("list-windows", "-t", "target", "-F", "#{window_name}"), "kept")
                os.write(master, b"\x1b")
                read_for(0.4)
                self.assertEqual(tmux("display-message", "-p", "-c", client_name, "#{pane_id}"), origin)
            finally:
                subprocess.run(["tmux", "-L", socket, "kill-server"], env=env, capture_output=True)
                if client:
                    client.wait(timeout=5)
                os.close(master)
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
