"""Window-move naming regressions on disposable tmux servers (no fzf needed)."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("TMUX_AGENT_INTEGRATION") == "1"
                     and shutil.which("tmux"), "opt-in tmux integration")
class MoveWindowIntegration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tmux-move-test-")
        self.addCleanup(directory.cleanup)
        self.socket = str(Path(directory.name) / "socket")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("TMUX", "TMUX_PANE", "MW_DEST")}
        self.env["TERM"] = "xterm-256color"
        self.addCleanup(lambda: subprocess.run(
            ["tmux", "-S", self.socket, "kill-server"],
            env=self.env, capture_output=True, timeout=5))
        self.source = self.tmux(
            "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{window_id}",
            "-s", "source", "-n", "original name", "sleep 90")
        self.tmux("set-option", "-g", "base-index", "1")
        # Route the script's plain `tmux` calls exclusively to this test server.
        self.env["TMUX"] = self.tmux("display-message", "-p", "#{socket_path},#{pid},0")

    def tmux(self, *args):
        return subprocess.check_output(
            ["tmux", "-S", self.socket, *args], env=self.env,
            text=True, timeout=10).strip()

    def move(self, destination):
        script = Path(__file__).resolve().parents[1] / "tmux-move-window.sh"
        subprocess.run(["bash", str(script), self.source, "source"],
                       env={**self.env, "MW_DEST": destination},
                       check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.source,
                                   "#{session_name}"), destination)
        self.assertEqual(self.tmux("display-message", "-p", "-t", f"={destination}:",
                                   "#{window_id}"), self.source)

    def check_new_session(self, keep_source):
        if keep_source:
            self.tmux("new-window", "-d", "-t", "source:", "-n", "remaining", "sleep 90")
        self.move("new project")
        self.assertEqual(self.tmux("list-windows", "-t", "=new project", "-F",
                                   "#{window_id}:#{window_index}:#{window_name}"),
                         f"{self.source}:1:new project")
        self.assertEqual(self.tmux("show-options", "-wv", "-t", self.source,
                                   "automatic-rename"), "off")
        sessions = self.tmux("list-sessions", "-F", "#{session_name}").splitlines()
        self.assertEqual("source" in sessions, keep_source)

    def check_existing_session(self, keep_source):
        if keep_source:
            self.tmux("new-window", "-d", "-t", "source:", "-n", "remaining", "sleep 90")
        destination_window = self.tmux(
            "new-session", "-d", "-P", "-F", "#{window_id}", "-s", "existing project",
            "-n", "destination original", "sleep 90")
        self.move("existing project")
        windows = self.tmux("list-windows", "-t", "=existing project", "-F",
                            "#{window_id}:#{window_name}").splitlines()
        self.assertCountEqual(windows, [f"{self.source}:original name",
                                        f"{destination_window}:destination original"])
        sessions = self.tmux("list-sessions", "-F", "#{session_name}").splitlines()
        self.assertEqual("source" in sessions, keep_source)

    def test_new_session_renames_last_source_window(self):
        self.check_new_session(keep_source=False)

    def test_new_session_renames_with_source_windows_remaining(self):
        self.check_new_session(keep_source=True)

    def test_existing_session_keeps_last_source_window_name(self):
        self.check_existing_session(keep_source=False)

    def test_existing_session_keeps_name_with_source_windows_remaining(self):
        self.check_existing_session(keep_source=True)


if __name__ == "__main__":
    unittest.main()
